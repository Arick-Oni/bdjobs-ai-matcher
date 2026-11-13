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
    # "High demand" 503s are temporary, so retry every key a few times with a growing pause
    for round_num, pause in enumerate((0, 15, 45)):
        if pause:
            print(f"[Gemini] All keys overloaded, retrying in {pause}s...")
            time.sleep(pause)
        overloaded = False
        for attempt in range(len(keys)):
            idx = (_key_cursor + attempt) % len(keys)
            # Key goes in a header, not the URL, so it never shows up in error messages / logs
            headers = {"Content-Type": "application/json", "x-goog-api-key": keys[idx]}
            try:
                res = requests.post(GEMINI_URL, json=payload, headers=headers, timeout=120)
            except requests.exceptions.Timeout:
                last_err, overloaded = f"key #{idx + 1} timed out", True
                print(f"[Gemini] {last_err}, trying next key...")
                continue
            if res.status_code in (429, 403, 401) or res.status_code >= 500:
                last_err = f"key #{idx + 1} returned {res.status_code}"
                overloaded = overloaded or res.status_code >= 500
                print(f"[Gemini] {last_err}, trying next key...")
                continue
            res.raise_for_status()
            _key_cursor = idx + 1
            return res.json()
        if not overloaded:
            break  # Only quota/auth errors: waiting won't help
    _key_cursor += 1
    raise RuntimeError(f"All {len(keys)} Gemini key(s) failed ({last_err})")

def ask_gemini_json(prompt: str, temperature: float = 0.2) -> Any:
    """One Gemini call that must return JSON. Raises on failure (no keyword fallback)."""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
    }
    res_data = post_gemini(payload)
    raw_text = res_data["candidates"][0]["content"]["parts"][0]["text"].strip()
    return json.loads(raw_text)

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
    # The CV is the only description of the candidate: several users with different careers share this prompt
    prompt = f"""You are an expert career assistant and resume evaluator.

Candidate CV:
\"\"\"
{cv_summary[:6000]}
\"\"\"

Task:
Evaluate each job listing below for fit against THIS candidate. Work out the candidate's field, degree, skills,
experience and seniority from the CV text only. Do not assume any field, skill or tool that the CV does not show.

Jobs to evaluate:
{json.dumps(jobs_batch, indent=2)}

Respond ONLY with a valid JSON array of objects, one per job, in the exact same order as input:
[
  {{
    "Job_ID": "...",
    "Match_Score": 85,
    "Match_Level": "HIGH",
    "Match_Reason": "One sentence citing specific evidence from the CV."
  }}
]

Match_Level criteria, judged against the candidate's own field:
- HIGH (score 70-100): the role is in the candidate's field and fits their skills and experience level.
- MEDIUM (score 45-69): a related field or partial fit (transferable skills, adjacent discipline, or a seniority mismatch).
- LOW (score 20-44): marginal fit; only general skills would apply.
- IRRELEVANT (score 0-19): unrelated to the candidate's field.
Match_Reason must only mention qualifications that actually appear in the CV.
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

REMOTIVE_CATEGORIES = ["software-dev", "customer-support", "design", "marketing", "sales-business", "product",
                       "project-management", "data", "devops", "finance", "human-resources", "qa", "writing"]


def suggest_search_terms(cv_text: str) -> Dict[str, List[str]]:
    """Job-search phrases for LinkedIn and Remotive categories that fit this CV. One Gemini request."""
    prompt = f"""Read this CV and suggest how to search job boards for roles this person is qualified for.

CV:
\"\"\"
{cv_text[:6000]}
\"\"\"

Respond ONLY with JSON:
{{"keywords": ["4 to 6 short job-title search phrases, 1-3 words each, most relevant first"],
  "remotive": ["0 to 3 of: {', '.join(REMOTIVE_CATEGORIES)}"]}}
Use only fields the CV actually shows. Prefer titles at the person's level (e.g. "junior architect" for a recent graduate)."""
    out = ask_gemini_json(prompt) or {}
    keywords = [str(k).strip() for k in out.get("keywords", []) if str(k).strip()][:6]
    remotive = [c for c in out.get("remotive", []) if c in REMOTIVE_CATEGORIES][:3]
    return {"keywords": keywords, "remotive": remotive}


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
