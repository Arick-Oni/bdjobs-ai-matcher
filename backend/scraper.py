import requests
import json
import time
from typing import List, Dict, Any, Optional

from backend.scraper_linkedin import fetch_linkedin_jobs
from backend.scraper_remotive import fetch_remotive_jobs

BDJOBS_API_URL = "https://gateway.bdjobs.com/recruitment-account-test/api/JobSearch/GetJobSearch"
BDJOBS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Origin": "https://bdjobs.com",
    "Referer": "https://bdjobs.com/"
}

def clean_str(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()

def parse_salary(salary_data: Any) -> str:
    if not salary_data or not isinstance(salary_data, dict):
        return "Negotiable / Not specified"
    min_sal = salary_data.get("MinSalary")
    max_sal = salary_data.get("MaxSalary")
    is_neg = salary_data.get("IsNegotiable")
    hide_sal = salary_data.get("HideSalary")

    if hide_sal:
        return "Confidential"
    
    if max_sal is not None and max_sal <= 0:
        max_sal = None
    if min_sal is not None and min_sal <= 0:
        min_sal = None

    if min_sal and max_sal:
        return f"BDT {min_sal:,} - {max_sal:,}"
    elif min_sal:
        return f"BDT {min_sal:,}+"
    elif max_sal:
        return f"Up to BDT {max_sal:,}"
    elif is_neg:
        return "Negotiable"
    return "Negotiable / Not specified"

def fetch_bdjobs(is_fresher: bool = True, deadline_today: bool = True, max_pages: int = 5, rpp: int = 50) -> List[Dict[str, Any]]:
    all_jobs = []
    page = 1

    params = {
        "rpp": rpp,
        "pg": page,
        "isPro": 1,
    }
    if is_fresher:
        params["isFresher"] = "true"
    if deadline_today:
        params["deadline"] = 1

    print(f"[BDJobs Scraper] Starting fetch (Fresher={is_fresher}, DeadlineToday={deadline_today})...")

    while page <= max_pages:
        params["pg"] = page
        try:
            response = requests.get(BDJOBS_API_URL, params=params, headers=BDJOBS_HEADERS, timeout=15)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            print(f"[BDJobs Scraper Error] Failed to fetch page {page}: {e}")
            break

        jobs = data.get("data", [])
        common = data.get("common", {})
        total_pages = common.get("totalpages", 1)

        if not jobs:
            break

        for item in jobs:
            job_id = clean_str(item.get("Jobid"))
            job_url = f"https://jobs.bdjobs.com/jobdetails.asp?id={job_id}" if job_id else ""

            parsed_job = {
                "Job_ID": f"bd_{job_id}" if job_id else f"bd_{len(all_jobs)}",
                "Title": clean_str(item.get("jobTitle")),
                "Company": clean_str(item.get("companyName")),
                "Location": clean_str(item.get("location")),
                "Experience": clean_str(item.get("experience")),
                "Deadline": clean_str(item.get("deadline")),
                "Deadline_ISO": clean_str(item.get("deadlineDB")),
                "Publish_Date": clean_str(item.get("publishDate")),
                "Vacancies": item.get("Vacancies") if item.get("Vacancies") is not None else "N/A",
                "Job_Type": clean_str(item.get("JobType")),
                "Workplace": clean_str(item.get("WorkPlace")),
                "Salary": parse_salary(item.get("Salary")),
                "Category_ID": clean_str(item.get("Cat_id")),
                "Job_URL": job_url,
                "Source": "BDJobs"
            }
            all_jobs.append(parsed_job)

        print(f"[BDJobs Scraper] Page {page}/{total_pages} extracted ({len(jobs)} items).")

        if page >= total_pages:
            break

        page += 1
        time.sleep(0.2)

    print(f"[BDJobs Scraper] Completed! Total extracted: {len(all_jobs)}")
    return all_jobs

def fetch_all_jobs(
    sources: Optional[List[str]] = None,
    is_fresher: bool = True,
    deadline_today: bool = True,
    linkedin_keywords: Optional[List[str]] = None,
    remotive_categories: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Unified multi-site aggregator for BDJobs, LinkedIn Bangladesh, and Remotive.
    LinkedIn keywords and Remotive categories come from users' CVs (jobs_db.search_terms()).
    """
    if not sources:
        sources = ["bdjobs", "linkedin", "remotive"]

    sources_lower = [s.lower().strip() for s in sources]
    combined_jobs = []

    # 1. BDJobs. Users range from freshers to experienced, so the fresher listing is fetched alongside the full one:
    # the full listing alone would fill its pages with experienced roles and push fresher ones out
    if "bdjobs" in sources_lower or "all" in sources_lower:
        for fresher_pass in ([True, False] if is_fresher else [False]):
            try:
                combined_jobs.extend(fetch_bdjobs(is_fresher=fresher_pass, deadline_today=deadline_today))
            except Exception as e:
                print(f"[MultiScraper Error] BDJobs scraper failed (fresher={fresher_pass}): {e}")

    # 2. LinkedIn Bangladesh
    if "linkedin" in sources_lower or "all" in sources_lower:
        try:
            li_jobs = fetch_linkedin_jobs(keywords=linkedin_keywords)
            combined_jobs.extend(li_jobs)
        except Exception as e:
            print(f"[MultiScraper Error] LinkedIn scraper failed: {e}")

    # 3. Remotive (remote jobs)
    if "remotive" in sources_lower or "all" in sources_lower:
        try:
            rem_jobs = fetch_remotive_jobs(categories=remotive_categories)
            combined_jobs.extend(rem_jobs)
        except Exception as e:
            print(f"[MultiScraper Error] Remotive scraper failed: {e}")

    print(f"[MultiScraper] Aggregated {len(combined_jobs)} total jobs across sources: {sources}")
    return combined_jobs
