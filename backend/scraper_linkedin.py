import requests
import time
import re
from typing import List, Dict, Any
from bs4 import BeautifulSoup

LINKEDIN_API_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}

def clean_str(val: Any) -> str:
    if val is None:
        return ""
    return re.sub(r"\s+", " ", str(val)).strip()

def fetch_linkedin_jobs(
    keywords: List[str] = None, 
    location: str = "Bangladesh", 
    max_per_keyword: int = 15
) -> List[Dict[str, Any]]:
    """
    Fetches job listings from LinkedIn Bangladesh via public guest search.
    """
    if keywords is None:
        keywords = [
            "software engineer", 
            "python developer", 
            "machine learning", 
            "data scientist", 
            "web developer", 
            "lecturer computer science"
        ]

    all_jobs = []
    seen_ids = set()

    print(f"[LinkedIn BD Scraper] Starting fetch across {len(keywords)} keywords in '{location}'...")

    for kw in keywords:
        try:
            params = {
                "keywords": kw,
                "location": location,
                "start": 0
            }
            res = requests.get(LINKEDIN_API_URL, params=params, headers=HEADERS, timeout=15)
            if res.status_code != 200:
                print(f"[LinkedIn BD Scraper] Search for '{kw}' returned status {res.status_code}")
                continue

            soup = BeautifulSoup(res.text, "html.parser")
            cards = soup.find_all("li")

            count_for_kw = 0
            for card in cards:
                if count_for_kw >= max_per_keyword:
                    break

                title_elem = card.find("h3", class_="base-search-card__title")
                comp_elem = card.find("h4", class_="base-search-card__subtitle")
                loc_elem = card.find("span", class_="job-search-card__location")
                link_elem = card.find("a", class_="base-card__full-link")
                date_elem = card.find("time")

                if not title_elem or not link_elem:
                    continue

                title = clean_str(title_elem.text)
                company = clean_str(comp_elem.text) if comp_elem else "Confidential"
                loc = clean_str(loc_elem.text) if loc_elem else location
                job_url = link_elem.get("href", "").split("?")[0]
                posted_date = clean_str(date_elem.text) if date_elem else "Recent"

                # Extract unique LinkedIn Job ID
                urn_match = re.search(r"urn:li:jobPosting:(\d+)", str(card))
                if urn_match:
                    job_id = f"li_{urn_match.group(1)}"
                else:
                    id_match = re.search(r"/view/.*?(\d+)", job_url)
                    job_id = f"li_{id_match.group(1)}" if id_match else f"li_{abs(hash(job_url)) % 10000000}"

                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                parsed_job = {
                    "Job_ID": job_id,
                    "Title": title,
                    "Company": company,
                    "Location": loc,
                    "Experience": "Fresher / Mid",
                    "Deadline": "Open / Apply soon",
                    "Deadline_ISO": "",
                    "Publish_Date": posted_date,
                    "Vacancies": "1+",
                    "Job_Type": "Full-time / Hybrid",
                    "Workplace": "On-site / Hybrid",
                    "Salary": "Negotiable / Standard",
                    "Category_ID": "LinkedIn Tech",
                    "Job_URL": job_url,
                    "Source": "LinkedIn"
                }
                all_jobs.append(parsed_job)
                count_for_kw += 1

            print(f"[LinkedIn BD Scraper] '{kw}': Extracted {count_for_kw} jobs.")
            time.sleep(0.5)

        except Exception as e:
            print(f"[LinkedIn BD Scraper Error] Keyword '{kw}' failed: {e}")

    print(f"[LinkedIn BD Scraper] Completed! Total LinkedIn jobs: {len(all_jobs)}")
    return all_jobs
