import re
import requests
from typing import Dict, Any
from bs4 import BeautifulSoup

BDJOBS_DETAILS_URL = "https://gateway.bdjobs.com/ActtivejobsTest/api/JobSubsystem/jobDetails"
LINKEDIN_DETAILS_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://bdjobs.com",
    "Referer": "https://bdjobs.com/",
}
MAX_CHARS = 8000


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for li in soup.find_all("li"):
        li.insert_before("\n• ")
    text = soup.get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _bdjobs(job_id: str) -> str:
    res = requests.get(BDJOBS_DETAILS_URL, params={"jobId": job_id}, headers=HEADERS, timeout=20)
    res.raise_for_status()
    d = (res.json().get("data") or [{}])[0]
    sections = [
        ("Job Description", d.get("JobDescription")),
        ("Requirements", d.get("AdditionJobRequirements")),
        ("Education", d.get("EducationRequirements")),
        ("Experience", d.get("experience")),
        ("Required Skills", d.get("SkillsRequired")),
        ("Suggested Skills", d.get("SuggestedSkills")),
        ("Job Nature", d.get("JobNature")),
        ("About the Company", d.get("CompanyBusiness")),
    ]
    parts = [f"## {name}\n{html_to_text(val)}" for name, val in sections if val and str(val).strip() not in ("", "None", "--")]
    return "\n\n".join(parts)


def _linkedin(job_id: str) -> str:
    res = requests.get(LINKEDIN_DETAILS_URL.format(id=job_id), headers=HEADERS, timeout=20)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")
    desc = soup.find("div", class_="show-more-less-html__markup") or soup.find("div", class_="description__text")
    criteria = [
        f"{h.get_text(strip=True)}: {t.get_text(strip=True)}"
        for h, t in zip(soup.find_all("h3", class_="description__job-criteria-subheader"),
                        soup.find_all("span", class_="description__job-criteria-text"))
    ]
    text = html_to_text(str(desc)) if desc else ""
    if criteria:
        text += "\n\n## Job Criteria\n" + "\n".join(criteria)
    return text


def _generic(url: str) -> str:
    res = requests.get(url, headers=HEADERS, timeout=20)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    return html_to_text(str(soup.body or soup))


def fetch_description(job: Dict[str, Any]) -> str:
    """Full job description text for a stored job. Raises if nothing usable could be fetched."""
    if job.get("Description"):
        return job["Description"][:MAX_CHARS]

    jid = str(job.get("Job_ID", ""))
    source = job.get("Source", "")
    if source == "BDJobs" and jid.startswith("bd_"):
        text = _bdjobs(jid[3:])
    elif source == "LinkedIn" and jid.startswith("li_"):
        text = _linkedin(jid[3:])
    else:
        text = _generic(job.get("Job_URL", ""))

    if len(text) < 150:
        raise RuntimeError(f"Couldn't fetch a usable job description from {source} (got {len(text)} chars).")
    return text[:MAX_CHARS]
