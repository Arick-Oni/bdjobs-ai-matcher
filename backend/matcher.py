import os
import requests
import json
import time
import glob
from typing import List, Dict, Any, Tuple
import pdfplumber

MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"
# Free tier limits requests per day, so pack many jobs into each request
BATCH_SIZE = int(os.environ.get("GEMINI_BATCH_SIZE", "40"))

_key_cursor = 0

def gemini_keys() -> List[str]:
    """GEMINI_API_KEYS (comma separated) plus GEMINI_API_KEY, de-duplicated."""
    raw = os.environ.get("GEMINI_API_KEYS", "") + "," + os.environ.get("GEMINI_API_KEY", "")
    keys = []
    for k in raw.split(","):
        k = k.strip()
        if k and k not in keys:
            keys.append(k)
    return keys

def post_gemini(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Round-robins across keys; on rate-limit/quota/auth errors moves on to the next key."""
    global _key_cursor
    keys = gemini_keys()
    if not keys:
        raise RuntimeError("No Gemini API key configured")
    last_err = None
    for attempt in range(len(keys)):
        idx = (_key_cursor + attempt) % len(keys)
        # Key goes in a header, not the URL, so it never shows up in error messages / logs
        headers = {"Content-Type": "application/json", "x-goog-api-key": keys[idx]}
        res = requests.post(GEMINI_URL, json=payload, headers=headers, timeout=120)
        if res.status_code in (429, 403, 401) or res.status_code >= 500:
            last_err = f"key #{idx + 1} returned {res.status_code}"
            print(f"[Gemini] {last_err}, trying next key...")
            continue
        res.raise_for_status()
        _key_cursor = idx + 1
        return res.json()
    _key_cursor += 1
    raise RuntimeError(f"All {len(keys)} Gemini key(s) failed ({last_err})")

def extract_cv_text(pdf_path: str) -> str:
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"CV PDF file not found at: {pdf_path}")

    print(f"[CV Reader] Reading text from PDF: {pdf_path}")
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text += (page.extract_text() or "") + "\n"
    return text.strip()

def call_gemini_batch_match(cv_summary: str, jobs_batch: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    prompt = f"""You are an expert AI Career Assistant and Resume Evaluator.

Candidate CV Summary:
\"\"\"
{cv_summary[:3000]}
\"\"\"

Task:
Evaluate each of the following job listings for fit against the candidate's CV (Computer Science & Engineering graduate with background in Software Engineering, Machine Learning/AI, Data Science, Web/App Development, Python, Embedded Systems/IoT, Research/Academic roles).

Jobs to evaluate:
{json.dumps(jobs_batch, indent=2)}

Respond ONLY with a valid JSON array of objects, one per job, in the exact same order as input:
[
  {{
    "Job_ID": "...",
    "Match_Score": 85,
    "Match_Level": "HIGH",
    "Match_Reason": "Directly matches candidate's ML/AI and Python background."
  }}
]

Match_Level criteria:
- HIGH: CS, Software, AI/ML, Data, Web, Tech, Research, Engineering, Teaching/Lecturer roles matching candidate.
- MEDIUM: General tech, IT support, hardware, operations with basic CS/math component.
- LOW: Marginal non-tech roles (e.g. general sales, admin, office assistant).
- IRRELEVANT: Completely unrelated roles (e.g., Nurse, Peon, Delivery Man, Juice Maker, Driver, Caregiver).
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
    }

    try:
        res_data = post_gemini(payload)
        raw_text = res_data["candidates"][0]["content"]["parts"][0]["text"].strip()
        matched_evals = json.loads(raw_text)
        for ev in matched_evals:
            if isinstance(ev, dict):
                ev["AI_Scored"] = True
        return matched_evals
    except Exception as e:
        print(f"[Gemini API Warning] Batch eval failed ({e}). Using fallback keyword evaluator...")
        evals = []
        for j in jobs_batch:
            title = str(j.get("Title", "")).lower()
            if any(k in title for k in ["soft", "dev", "ai", "ml", "data", "python", "tech", "web", "intern", "engineer", "research", "tutor", "lecturer", "academic", "system", "analyst"]):
                score, level, reason = 80, "HIGH", "Title matches software/data/tech keywords."
            elif any(k in title for k in ["admin", "account", "office", "exec", "sales", "customer"]):
                score, level, reason = 40, "LOW", "General non-engineering role."
            else:
                score, level, reason = 10, "IRRELEVANT", "Unrelated field."
            evals.append({"Job_ID": j.get("Job_ID"), "Match_Score": score, "Match_Level": level, "Match_Reason": reason, "AI_Scored": False})
        return evals

def match_jobs_to_cv(cv_pdf_path: str, jobs: List[Dict[str, Any]], batch_size: int = BATCH_SIZE) -> List[Dict[str, Any]]:
    if not jobs:
        return []

    cv_text = extract_cv_text(cv_pdf_path)
    print(f"[AI Matcher] Loaded {len(jobs)} jobs. Evaluating with {MODEL_NAME}...")

    evaluated_jobs = []
    total_batches = (len(jobs) + batch_size - 1) // batch_size

    for i in range(0, len(jobs), batch_size):
        batch = jobs[i:i+batch_size]
        batch_num = (i // batch_size) + 1
        print(f"[AI Matcher] Batch {batch_num}/{total_batches} ({len(batch)} jobs)...")

        prompt_batch = [
            {
                "Job_ID": j.get("Job_ID"), 
                "Title": j.get("Title"), 
                "Company": j.get("Company"), 
                "Location": j.get("Location"),
                "Source": j.get("Source", "BDJobs"),
                "Experience": j.get("Experience"), 
                "Job_Type": j.get("Job_Type")
            }
            for j in batch
        ]

        evals = call_gemini_batch_match(cv_text, prompt_batch)

        eval_dict = {str(e.get("Job_ID")): e for e in evals if isinstance(e, dict)}
        for j in batch:
            jid = str(j.get("Job_ID"))
            ev = eval_dict.get(jid, {"Match_Score": 0, "Match_Level": "IRRELEVANT", "Match_Reason": "No response"})
            j_copy = dict(j)
            j_copy["Match_Score"] = ev.get("Match_Score", 0)
            j_copy["Match_Level"] = ev.get("Match_Level", "IRRELEVANT")
            j_copy["Match_Reason"] = ev.get("Match_Reason", "")
            j_copy["AI_Scored"] = ev.get("AI_Scored", False)
            evaluated_jobs.append(j_copy)

        if batch_num < total_batches:
            time.sleep(3.0)  # Rate limiting courtesy pause

    evaluated_jobs.sort(key=lambda x: x.get("Match_Score", 0), reverse=True)
    return evaluated_jobs
