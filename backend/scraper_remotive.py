import requests
import time
from typing import List, Dict, Any

from backend.job_details import html_to_text

REMOTIVE_API_URL = "https://remotive.com/api/remote-jobs"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}

def clean_str(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()

def fetch_remotive_jobs(
    categories: List[str] = None, 
    limit_per_category: int = 25
) -> List[Dict[str, Any]]:
    """
    Fetches remote jobs from Remotive API suitable for candidates in Bangladesh or Worldwide.
    """
    if categories is None:
        categories = ["software-dev", "data", "qa", "devops"]

    all_jobs = []
    seen_ids = set()

    print(f"[Remotive Scraper] Fetching remote jobs across categories: {categories}...")

    for cat in categories:
        try:
            params = {
                "category": cat,
                "limit": limit_per_category
            }
            res = requests.get(REMOTIVE_API_URL, params=params, headers=HEADERS, timeout=15)
            if res.status_code != 200:
                print(f"[Remotive Scraper] Category '{cat}' returned status {res.status_code}")
                continue

            data = res.json()
            jobs = data.get("jobs", [])

            for item in jobs:
                job_id = f"rem_{item.get('id')}"
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                candidate_loc = clean_str(item.get("candidate_required_location")) or "Worldwide / Remote"
                
                # Format salary if present
                salary_str = clean_str(item.get("salary"))
                if not salary_str:
                    salary_str = "Competitive / USD Remote"

                # Parse publication date
                pub_date = clean_str(item.get("publication_date", ""))
                if "T" in pub_date:
                    pub_date = pub_date.split("T")[0]

                parsed_job = {
                    "Job_ID": job_id,
                    "Title": clean_str(item.get("title")),
                    "Company": clean_str(item.get("company_name")),
                    "Location": f"Remote ({candidate_loc})",
                    "Experience": clean_str(item.get("job_type", "Full-time")),
                    "Deadline": "Ongoing / Remote",
                    "Deadline_ISO": "",
                    "Publish_Date": pub_date or "Recent",
                    "Vacancies": "1+",
                    "Job_Type": clean_str(item.get("job_type", "Full-time")),
                    "Workplace": "100% Remote",
                    "Salary": salary_str,
                    "Category_ID": clean_str(item.get("category")),
                    "Job_URL": clean_str(item.get("url")),
                    "Source": "Remotive",
                    "Description": html_to_text(item.get("description", ""))[:8000],
                }
                all_jobs.append(parsed_job)

            print(f"[Remotive Scraper] Category '{cat}': Extracted {len(jobs)} jobs.")
            time.sleep(0.3)

        except Exception as e:
            print(f"[Remotive Scraper Error] Category '{cat}' failed: {e}")

    print(f"[Remotive Scraper] Completed! Total Remotive jobs: {len(all_jobs)}")
    return all_jobs
