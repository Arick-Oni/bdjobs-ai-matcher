import os
import json
import html
import shutil
import threading
import time
from datetime import datetime
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()


from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, Response

from backend.scraper import fetch_all_jobs
from backend.matcher import match_jobs_to_cv, extract_cv_text
from backend import notifier, jobs_db, telegram_bot, tailor, cv_render

app = FastAPI(title="AI Career Matcher API (Multi-Platform BD & Remote)", version="2.0.0")

# Enable CORS for local/mobile access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CV_STORE_DIR = os.path.join(BASE_DIR, "cv_store")
DATA_DIR = os.path.join(BASE_DIR, "data")
FRONTEND_DIR = os.path.join(os.path.dirname(BASE_DIR), "frontend")

os.makedirs(CV_STORE_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

CURRENT_CV_FILE = os.path.join(CV_STORE_DIR, "current_cv.pdf")

# Global task status for progress feedback
execution_status = {
    "is_running": False,
    "step": "Idle",
    "message": "Ready to fetch and match jobs.",
    "last_updated": None,
    "error": None
}
run_lock = threading.Lock()

def env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")

# Scheduled runs: comma separated HH:MM in the container's local time (TZ env var)
SCHEDULE_TIMES = [t.strip() for t in os.environ.get("SCHEDULE_TIMES", "").split(",") if t.strip()]
SCHEDULE_SOURCES = [s.strip() for s in os.environ.get("SCHEDULE_SOURCES", "bdjobs,linkedin,remotive").split(",") if s.strip()]
SCHEDULE_FRESHER = env_bool("SCHEDULE_FRESHER", True)
SCHEDULE_DEADLINE_TODAY = env_bool("SCHEDULE_DEADLINE_TODAY", True)

def get_active_cv_path() -> Optional[str]:
    if os.path.exists(CURRENT_CV_FILE):
        return CURRENT_CV_FILE
    pdfs = [os.path.join(CV_STORE_DIR, f) for f in os.listdir(CV_STORE_DIR) if f.endswith(".pdf")]
    if pdfs:
        return pdfs[0]
    return None

@app.get("/api/cv-info")
def get_cv_info():
    cv_path = get_active_cv_path()
    if not cv_path or not os.path.exists(cv_path):
        return {"has_cv": False, "filename": None, "message": "No CV PDF uploaded yet."}
    
    try:
        text = extract_cv_text(cv_path)
        preview = text[:300] + "..." if len(text) > 300 else text
        return {
            "has_cv": True,
            "filename": os.path.basename(cv_path),
            "size_kb": round(os.path.getsize(cv_path) / 1024, 1),
            "preview": preview
        }
    except Exception as e:
        return {"has_cv": True, "filename": os.path.basename(cv_path), "error": str(e)}

@app.post("/api/upload-cv")
async def upload_cv(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    dest_path = CURRENT_CV_FILE
    with open(dest_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    return {
        "status": "success",
        "message": f"Successfully uploaded CV '{file.filename}'.",
        "filename": file.filename
    }

@app.get("/api/status")
def get_status():
    return execution_status

@app.get("/api/latest-jobs")
def get_latest_jobs():
    # Straight from the DB so tailoring/applied status is always current
    jobs = [{k: v for k, v in j.items() if k != "Description"} for j in jobs_db.active_jobs()]
    return build_payload(jobs, jobs_db.last_run())

def build_payload(jobs: List[Dict[str, Any]], fetched_at: Optional[str]) -> Dict[str, Any]:
    return {
        "fetched_at": fetched_at,
        "total_count": len(jobs),
        "high_count": sum(1 for j in jobs if j.get("Match_Level") == "HIGH"),
        "medium_count": sum(1 for j in jobs if j.get("Match_Level") == "MEDIUM"),
        "low_count": sum(1 for j in jobs if j.get("Match_Level") in ["LOW", "IRRELEVANT"]),
        "bdjobs_count": sum(1 for j in jobs if j.get("Source") == "BDJobs"),
        "linkedin_count": sum(1 for j in jobs if j.get("Source") == "LinkedIn"),
        "remotive_count": sum(1 for j in jobs if j.get("Source") == "Remotive"),
        "jobs": jobs,
    }

def run_matching_process(cv_path: str, is_fresher: bool, deadline_today: bool, sources_list: List[str]):
    if not run_lock.acquire(blocking=False):
        print("[Runner] A run is already in progress, skipping.")
        return
    try:
        _run_matching_process(cv_path, is_fresher, deadline_today, sources_list)
    finally:
        run_lock.release()

def _run_matching_process(cv_path: str, is_fresher: bool, deadline_today: bool, sources_list: List[str]):
    global execution_status
    execution_status["is_running"] = True
    execution_status["step"] = "Scraping Multi-Sites"
    execution_status["message"] = f"Fetching live jobs from {', '.join(sources_list)}..."
    execution_status["error"] = None

    try:
        jobs = fetch_all_jobs(sources=sources_list, is_fresher=is_fresher, deadline_today=deadline_today)
        # Sources can list the same job twice (e.g. across BDJobs pages)
        jobs = list({str(j.get("Job_ID")): j for j in jobs}.values())

        # Only jobs not already in the DB go to Gemini; known jobs keep their score
        _, new_jobs = jobs_db.split_known(jobs, cv_path)

        execution_status["step"] = "AI Matching"
        execution_status["message"] = f"Evaluating fit for {len(new_jobs)} new jobs (of {len(jobs)} scraped) using Gemini..."

        scored_new = match_jobs_to_cv(cv_path, new_jobs)
        summary = jobs_db.update(jobs, scored_new, cv_path)

        timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        execution_status["is_running"] = False
        execution_status["step"] = "Done"
        execution_status["message"] = (
            f"{summary['added']} new, {summary['expired']} expired, {summary['active']} open jobs "
            f"across {len(sources_list)} sources."
        )
        execution_status["last_updated"] = timestamp_str

        try:
            failed = sum(1 for j in scored_new if not j.get("AI_Scored"))
            if failed and notifier.is_configured():
                notifier.send_message(
                    f"⚠️ Gemini failed for {failed} of {len(scored_new)} new jobs "
                    "(they'll be retried next run). Check the API key / quota."
                )
            notifier.send_daily_digest(summary)
        except Exception as ne:
            print(f"[Telegram Error] {ne}")

    except Exception as e:
        execution_status["is_running"] = False
        execution_status["step"] = "Error"
        execution_status["error"] = str(e)
        execution_status["message"] = f"Error during matching: {e}"
        if notifier.is_configured():
            try:
                notifier.send_message(f"❌ Job matcher run failed: {e}")
            except Exception as ne:
                print(f"[Telegram Error] {ne}")

@app.post("/api/match-jobs")
def trigger_match_jobs(
    background_tasks: BackgroundTasks, 
    is_fresher: bool = True, 
    deadline_today: bool = True,
    sources: str = Query("bdjobs,linkedin,remotive", description="Comma separated sources: bdjobs,linkedin,remotive")
):
    global execution_status

    cv_path = get_active_cv_path()
    if not cv_path:
        raise HTTPException(status_code=400, detail="Please upload a CV PDF first.")

    if execution_status["is_running"]:
        return {"status": "in_progress", "message": "Scraping and matching is already running."}

    parsed_sources = [s.strip() for s in sources.split(",") if s.strip()]
    if not parsed_sources:
        parsed_sources = ["bdjobs", "linkedin", "remotive"]

    background_tasks.add_task(run_matching_process, cv_path, is_fresher, deadline_today, parsed_sources)
    return {"status": "started", "message": f"Job matching started for sources: {parsed_sources}"}

@app.get("/api/schedule")
def get_schedule():
    return {
        "times": SCHEDULE_TIMES,
        "timezone": os.environ.get("TZ", "system default"),
        "sources": SCHEDULE_SOURCES,
        "is_fresher": SCHEDULE_FRESHER,
        "deadline_today": SCHEDULE_DEADLINE_TODAY,
        "telegram_configured": notifier.is_configured(),
    }

def scheduler_loop():
    last_fired = None
    print(f"[Scheduler] Running daily at {SCHEDULE_TIMES} (TZ={os.environ.get('TZ', 'system')})")
    while True:
        now = datetime.now()
        key = now.strftime("%Y-%m-%d %H:%M")
        if now.strftime("%H:%M") in SCHEDULE_TIMES and key != last_fired:
            last_fired = key
            cv_path = get_active_cv_path()
            if cv_path:
                print(f"[Scheduler] Starting scheduled run at {key}")
                run_matching_process(cv_path, SCHEDULE_FRESHER, SCHEDULE_DEADLINE_TODAY, SCHEDULE_SOURCES)
            else:
                print("[Scheduler] No CV uploaded, skipping scheduled run.")
        time.sleep(20)

def start_run_from_bot() -> str:
    cv_path = get_active_cv_path()
    if not cv_path:
        return "No CV uploaded yet. Upload one in the web app first."
    if run_lock.locked():
        return "⏳ A run is already in progress."
    threading.Thread(
        target=run_matching_process,
        args=(cv_path, SCHEDULE_FRESHER, SCHEDULE_DEADLINE_TODAY, SCHEDULE_SOURCES),
        daemon=True,
    ).start()
    return "🔄 Fetching and matching jobs now. I'll send the updated list when it's done."

# ---------- CV tailoring ----------

def start_tailoring(job: Dict[str, Any]) -> str:
    """Starts tailoring in the background; the result goes to Telegram (if set up) and the web app."""
    cv_path = get_active_cv_path()
    if not cv_path:
        return "No CV uploaded yet. Upload one in the web app first."
    existing = jobs_db.get_tailored(job_id=job["Job_ID"])
    if existing and existing["status"] == "working":
        return f"⏳ #{job.get('Num')} is already being tailored."
    jobs_db.save_tailored(job, status="working", error=None)

    def run():
        try:
            t = tailor.tailor_job(job, cv_path, progress=lambda m: print(f"[Tailor #{job.get('Num')}] {m}"))
            if notifier.is_configured():
                notifier.send_tailored_result(t)
        except Exception as e:
            print(f"[Tailor #{job.get('Num')}] Failed: {e}")
            if notifier.is_configured():
                try:
                    notifier.send_message(f"❌ Tailoring #{job.get('Num')} failed: {str(e)[:300]}")
                except Exception:
                    pass

    threading.Thread(target=run, daemon=True).start()
    return (f"✍️ Tailoring your CV for <b>#{job.get('Num')} {html.escape(job.get('Title', ''))}</b> at {html.escape(job.get('Company', ''))}. "
            "This takes about a minute — I'll send the PDF here.")

def _tailored_or_404(job_id: str) -> Dict[str, Any]:
    t = jobs_db.get_tailored(job_id=job_id)
    if not t:
        raise HTTPException(status_code=404, detail="Not tailored yet.")
    return t

@app.post("/api/tailor/{job_id}")
def api_tailor(job_id: str):
    job = jobs_db.get_job(job_id=job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found (it may have expired).")
    return {"status": "started", "message": start_tailoring(job)}

@app.get("/api/tailored")
def api_tailored_list():
    return [{k: v for k, v in t.items() if k != "result"} | {"keywords_after": t["result"].get("keywords_after", {}).get("percent")}
            for t in jobs_db.list_tailored()]

@app.get("/api/tailored/{job_id}")
def api_tailored(job_id: str):
    t = _tailored_or_404(job_id)
    r = t["result"]
    return {
        "status": t["status"], "error": t["error"], "num": t["num"], "applied": t["applied"],
        "created": t["created"], "cover_letter": t["cover_letter"], "job": t["job"],
        "changes": r.get("changes", []), "gaps": r.get("gaps", []),
        "removed_claims": r.get("removed_claims", []),
        "keywords_before": r.get("keywords_before", {}), "keywords_after": r.get("keywords_after", {}),
    }

@app.get("/api/tailored/{job_id}/pdf")
def api_tailored_pdf(job_id: str):
    t = _tailored_or_404(job_id)
    if t["status"] != "done" or not t["pdf_path"] or not os.path.exists(t["pdf_path"]):
        raise HTTPException(status_code=404, detail="PDF not ready.")
    return FileResponse(t["pdf_path"], media_type="application/pdf", filename=os.path.basename(t["pdf_path"]))

@app.post("/api/tailored/{job_id}/cover")
def api_cover_letter(job_id: str):
    try:
        return {"letter": tailor.cover_letter_for(job_id)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/tailored/{job_id}/applied")
def api_mark_applied(job_id: str, applied: bool = True):
    job = jobs_db.get_job(job_id=job_id) or (jobs_db.get_tailored(job_id=job_id) or {}).get("job")
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    extra = {} if jobs_db.get_tailored(job_id=job_id) else {"status": "none"}
    jobs_db.save_tailored(job, applied=applied, **extra)
    return {"applied": applied}

@app.get("/api/master-cv")
def api_master_cv():
    stored = jobs_db.get_meta("master_cv")
    cv_path = get_active_cv_path()
    current = bool(stored and cv_path and stored.get("cv_hash") == jobs_db.file_hash(cv_path))
    return {"cv": stored["cv"] if stored else None, "edited": bool(stored and stored.get("edited")), "up_to_date": current}

@app.post("/api/master-cv/rebuild")
def api_master_cv_rebuild():
    cv_path = get_active_cv_path()
    if not cv_path:
        raise HTTPException(status_code=400, detail="Upload a CV PDF first.")
    try:
        return {"cv": tailor.build_master_cv(cv_path)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gemini failed: {e}")

@app.put("/api/master-cv")
def api_master_cv_save(cv: Dict[str, Any]):
    cv_path = get_active_cv_path()
    if not cv_path:
        raise HTTPException(status_code=400, detail="Upload a CV PDF first.")
    if not isinstance(cv.get("sections"), list) or not cv.get("name"):
        raise HTTPException(status_code=400, detail="CV JSON needs at least 'name' and a 'sections' list.")
    tailor.save_master_cv(cv_path, cv)
    return {"status": "saved"}

@app.get("/api/master-cv/preview", response_class=HTMLResponse)
def api_master_cv_preview():
    stored = jobs_db.get_meta("master_cv")
    if not stored:
        raise HTTPException(status_code=404, detail="Build the master CV first.")
    return cv_render.render_html(stored["cv"])

@app.on_event("startup")
def start_background_workers():
    jobs_db.reset_stuck_tailoring()
    if SCHEDULE_TIMES:
        threading.Thread(target=scheduler_loop, daemon=True, name="scheduler").start()
    telegram_bot.start(start_run_from_bot, lambda: execution_status, start_tailoring)

# Mount frontend static files
if os.path.exists(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
