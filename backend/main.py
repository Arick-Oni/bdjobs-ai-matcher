import os
import json
import shutil
from datetime import datetime
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()


from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from backend.scraper import fetch_all_jobs
from backend.matcher import match_jobs_to_cv, extract_cv_text

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

LATEST_MATCHES_FILE = os.path.join(DATA_DIR, "latest_matches.json")
CURRENT_CV_FILE = os.path.join(CV_STORE_DIR, "current_cv.pdf")

# Global task status for progress feedback
execution_status = {
    "is_running": False,
    "step": "Idle",
    "message": "Ready to fetch and match jobs.",
    "last_updated": None,
    "error": None
}

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
    if os.path.exists(LATEST_MATCHES_FILE):
        try:
            with open(LATEST_MATCHES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to read cache: {e}")
    return {"jobs": [], "fetched_at": None, "total_count": 0}

def run_matching_process(cv_path: str, is_fresher: bool, deadline_today: bool, sources_list: List[str]):
    global execution_status
    execution_status["is_running"] = True
    execution_status["step"] = "Scraping Multi-Sites"
    execution_status["message"] = f"Fetching live jobs from {', '.join(sources_list)}..."
    execution_status["error"] = None

    try:
        jobs = fetch_all_jobs(sources=sources_list, is_fresher=is_fresher, deadline_today=deadline_today)

        if not jobs:
            execution_status["is_running"] = False
            execution_status["step"] = "Done"
            execution_status["message"] = "No jobs found for the selected filters."
            return

        execution_status["step"] = "AI Matching"
        execution_status["message"] = f"Evaluating fit for {len(jobs)} jobs using Gemini 2.5 Flash AI..."

        matched_jobs = match_jobs_to_cv(cv_path, jobs)

        timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result_payload = {
            "fetched_at": timestamp_str,
            "total_count": len(matched_jobs),
            "high_count": sum(1 for j in matched_jobs if j.get("Match_Level") == "HIGH"),
            "medium_count": sum(1 for j in matched_jobs if j.get("Match_Level") == "MEDIUM"),
            "low_count": sum(1 for j in matched_jobs if j.get("Match_Level") in ["LOW", "IRRELEVANT"]),
            "bdjobs_count": sum(1 for j in matched_jobs if j.get("Source") == "BDJobs"),
            "linkedin_count": sum(1 for j in matched_jobs if j.get("Source") == "LinkedIn"),
            "remotive_count": sum(1 for j in matched_jobs if j.get("Source") == "Remotive"),
            "jobs": matched_jobs
        }

        with open(LATEST_MATCHES_FILE, "w", encoding="utf-8") as f:
            json.dump(result_payload, f, ensure_ascii=False, indent=2)

        execution_status["is_running"] = False
        execution_status["step"] = "Done"
        execution_status["message"] = f"Successfully matched {len(matched_jobs)} jobs across {len(sources_list)} sources!"
        execution_status["last_updated"] = timestamp_str

    except Exception as e:
        execution_status["is_running"] = False
        execution_status["step"] = "Error"
        execution_status["error"] = str(e)
        execution_status["message"] = f"Error during matching: {e}"

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

# Mount frontend static files
if os.path.exists(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
