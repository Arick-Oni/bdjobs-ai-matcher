import os
import re
import json
import threading
from typing import Dict, Any, List, Optional, Callable

from backend import jobs_db
from backend.matcher import ask_gemini_json, extract_cv_text
from backend.job_details import fetch_description

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TAILORED_DIR = os.path.join(BASE_DIR, "data", "tailored")

# One tailoring at a time: keeps Gemini usage predictable on free-tier quotas
_tailor_lock = threading.Lock()

CV_SCHEMA = """{
  "name": "Full Name",
  "headline": "Short professional headline",
  "contact": {"email": "", "phone": "", "location": "", "linkedin": "", "github": "", "website": ""},
  "summary": "Professional summary paragraph",
  "sections": [
    {"title": "Experience", "type": "entries", "entries": [
      {"title": "Role / Degree", "organization": "Company / University", "location": "", "dates": "Jan 2024 – Present",
       "bullets": ["..."]}
    ]},
    {"title": "Skills", "type": "skills", "groups": [{"name": "Programming", "items": ["Python", "..."]}]},
    {"title": "Publications", "type": "list", "items": ["..."]}
  ]
}"""


# ---------- Master CV ----------

def build_master_cv(cv_path: str) -> Dict[str, Any]:
    text = extract_cv_text(cv_path)
    prompt = f"""Convert this CV into structured JSON using exactly this schema:
{CV_SCHEMA}

Rules:
- Copy wording VERBATIM from the CV. Do not summarise, improve, add or drop anything.
- Every section of the CV must appear, in the original order. Use type "entries" for dated items
  (experience, education, projects, research, teaching), "skills" for skill lists, "list" for anything else
  (publications, certifications, awards, languages, references...).
- If the CV has no summary, use "" for summary. Leave unknown contact fields as "".

CV text:
\"\"\"
{text}
\"\"\"
"""
    cv = ask_gemini_json(prompt, temperature=0.0)
    jobs_db.set_meta("master_cv", {"cv_hash": jobs_db.file_hash(cv_path), "cv": cv, "edited": False})
    return cv


def get_master_cv(cv_path: str) -> Dict[str, Any]:
    """The structured master CV, rebuilt automatically when a different CV PDF is uploaded."""
    stored = jobs_db.get_meta("master_cv")
    if stored and stored.get("cv_hash") == jobs_db.file_hash(cv_path):
        return stored["cv"]
    return build_master_cv(cv_path)


def save_master_cv(cv_path: str, cv: Dict[str, Any]) -> None:
    jobs_db.set_meta("master_cv", {"cv_hash": jobs_db.file_hash(cv_path), "cv": cv, "edited": True})


# ---------- Helpers ----------

def cv_text(cv: Dict[str, Any]) -> str:
    """Flattened text of a structured CV, for keyword scoring."""
    parts = [cv.get("headline", ""), cv.get("summary", "")]
    for s in cv.get("sections", []):
        parts.append(s.get("title", ""))
        for e in s.get("entries", []):
            parts += [e.get("title", ""), e.get("organization", "")] + e.get("bullets", [])
        for g in s.get("groups", []):
            parts += [g.get("name", "")] + g.get("items", [])
        parts += s.get("items", [])
    return "\n".join(str(p) for p in parts if p)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+#]+", " ", s.lower()).strip()


def keyword_coverage(keywords: List[str], text: str) -> Dict[str, Any]:
    hay = f" {_norm(text)} "
    found = [k for k in keywords if _norm(k) and f" {_norm(k)} " in hay]
    missing = [k for k in keywords if k not in found]
    pct = round(100 * len(found) / len(keywords)) if keywords else 0
    return {"percent": pct, "found": found, "missing": missing}


def safe_filename(*parts: str) -> str:
    name = "_".join(re.sub(r"[^A-Za-z0-9]+", "_", p or "").strip("_") for p in parts if p)
    return name[:120] or "CV"


# ---------- Agent steps ----------

def analyse_job(job: Dict[str, Any], description: str) -> Dict[str, Any]:
    prompt = f"""You are a senior technical recruiter. Analyse this job posting.

Title: {job.get('Title')}
Company: {job.get('Company')}
Location: {job.get('Location')}

Posting:
\"\"\"
{description}
\"\"\"

Return JSON:
{{
  "role_summary": "one sentence: what this role really is",
  "seniority": "intern | junior | mid | senior | lead | academic",
  "must_have": ["key requirements, short phrases"],
  "nice_to_have": ["..."],
  "keywords": ["15-25 short ATS keywords exactly as written in the posting: tools, skills, domains, degrees"]
}}"""
    return ask_gemini_json(prompt, temperature=0.1)


def rewrite_cv(master: Dict[str, Any], job: Dict[str, Any], analysis: Dict[str, Any], description: str) -> Dict[str, Any]:
    prompt = f"""You are an expert CV writer. Tailor the candidate's CV to this job.

JOB: {job.get('Title')} at {job.get('Company')}
Analysis: {json.dumps(analysis, ensure_ascii=False)}
Posting:
\"\"\"
{description}
\"\"\"

MASTER CV (the only source of truth about the candidate):
{json.dumps(master, ensure_ascii=False)}

You MAY:
- Rewrite the headline and summary to target this role, using only facts in the master CV.
- Reorder sections, entries and bullets so the most relevant come first; reorder skills within groups.
- Rephrase bullets to use the posting's terminology when the bullet genuinely supports it.
- Drop bullets or list items that are irrelevant to this job, to keep the CV focused (1-2 pages).

You MUST NOT:
- Add any skill, tool, technology, responsibility, achievement, number, title, organisation or date that is not
  in the master CV. If the job needs something the candidate lacks, list it under "gaps" instead.
- Remove any Experience or Education entry entirely (keep the timeline complete), or change any dates.
- Change contact details or the name.

Return JSON:
{{
  "cv": <the tailored CV, same schema as the master CV>,
  "changes": ["short human-readable list of what you changed and why"],
  "gaps": ["job requirements the CV does not show evidence for"]
}}"""
    return ask_gemini_json(prompt, temperature=0.3)


def fact_check(master: Dict[str, Any], tailored: Dict[str, Any]) -> List[Dict[str, str]]:
    prompt = f"""You are a strict fact-checker. Compare a TAILORED CV against the ORIGINAL CV.
Flag every statement in the tailored CV that is NOT supported by the original: invented or inflated skills,
tools, numbers, responsibilities, achievements, titles, organisations or dates. Rewording and reordering are fine;
new facts are not. Be strict but don't flag pure style changes.

ORIGINAL:
{json.dumps(master, ensure_ascii=False)}

TAILORED:
{json.dumps(tailored, ensure_ascii=False)}

Return JSON: {{"unsupported": [{{"text": "exact text from tailored CV", "reason": "why it's not supported"}}]}}
Return an empty list if everything is supported."""
    return ask_gemini_json(prompt, temperature=0.0).get("unsupported", [])


def repair_cv(master: Dict[str, Any], tailored: Dict[str, Any], issues: List[Dict[str, str]]) -> Dict[str, Any]:
    prompt = f"""Fix this tailored CV. Each listed statement is not supported by the original CV.
For each one, rewrite it so it only states what the original supports, or remove it. Change nothing else.

ORIGINAL:
{json.dumps(master, ensure_ascii=False)}

TAILORED:
{json.dumps(tailored, ensure_ascii=False)}

UNSUPPORTED STATEMENTS:
{json.dumps(issues, ensure_ascii=False)}

Return JSON: {{"cv": <corrected tailored CV, same schema>}}"""
    return ask_gemini_json(prompt, temperature=0.0)["cv"]


def write_cover_letter(cv: Dict[str, Any], job: Dict[str, Any], description: str) -> str:
    prompt = f"""Write a concise cover letter (200-280 words) for this job, from the candidate.
Use only facts from the CV; never invent experience. Specific, warm, professional; no clichés like
"I am writing to express my interest". Plain text with paragraphs; start with "Dear Hiring Manager," and end
with the candidate's name.

JOB: {job.get('Title')} at {job.get('Company')}
Posting:
\"\"\"
{description}
\"\"\"

CV:
{json.dumps(cv, ensure_ascii=False)}

Return JSON: {{"letter": "..."}}"""
    return ask_gemini_json(prompt, temperature=0.5)["letter"]


# ---------- Orchestration ----------

def tailor_job(job: Dict[str, Any], cv_path: str, progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Runs the full pipeline for one job and stores the result. Returns the tailored record."""
    from backend.cv_render import render_pdf

    say = progress or (lambda msg: None)
    jid = str(job["Job_ID"])

    with _tailor_lock:
        try:
            jobs_db.save_tailored(job, status="working", error=None)

            say("Reading the job description…")
            description = fetch_description(job)
            if not job.get("Description"):
                jobs_db.save_job_fields(jid, Description=description)

            say("Loading your master CV…")
            master = get_master_cv(cv_path)

            say("Analysing what the job needs…")
            analysis = analyse_job(job, description)

            say("Tailoring your CV…")
            draft = rewrite_cv(master, job, analysis, description)
            tailored = draft["cv"]

            say("Fact-checking against your original CV…")
            issues = fact_check(master, tailored)
            if issues:
                say(f"Fixing {len(issues)} unsupported statement(s)…")
                tailored = repair_cv(master, tailored, issues)

            keywords = analysis.get("keywords", [])
            before = keyword_coverage(keywords, cv_text(master))
            after = keyword_coverage(keywords, cv_text(tailored))

            os.makedirs(TAILORED_DIR, exist_ok=True)
            filename = safe_filename(master.get("name", "CV"), "CV", job.get("Company", ""), job.get("Title", "")) + ".pdf"
            pdf_path = os.path.join(TAILORED_DIR, filename)
            render_pdf(tailored, pdf_path)

            result = {
                "analysis": analysis,
                "changes": draft.get("changes", []),
                "gaps": draft.get("gaps", []),
                "removed_claims": issues,
                "keywords_before": before,
                "keywords_after": after,
                "cv": tailored,
                "description": description,
            }
            jobs_db.save_tailored(job, status="done", pdf_path=pdf_path, result=result, error=None)
            return jobs_db.get_tailored(jid)
        except Exception as e:
            jobs_db.save_tailored(job, status="failed", error=str(e))
            raise


def cover_letter_for(job_id: str) -> str:
    t = jobs_db.get_tailored(job_id)
    if not t or t["status"] != "done":
        raise RuntimeError("Tailor the CV for this job first.")
    if t.get("cover_letter"):
        return t["cover_letter"]
    with _tailor_lock:
        letter = write_cover_letter(t["result"]["cv"], t["job"], t["result"].get("description", ""))
    jobs_db.save_tailored(t["job"], cover_letter=letter)
    return letter
