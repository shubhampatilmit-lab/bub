#!/usr/bin/env python3
"""
Hyderabad GCC job alert system.

Runs against company career APIs/pages, stores previously seen jobs in
data/seen_jobs.json, and notifies only when a new Hyderabad role appears.
Designed for GitHub Actions, but also works locally.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import smtplib
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
SEEN_FILE = DATA_DIR / "seen_jobs.json"
REPORT_FILE = DATA_DIR / "latest_report.json"
COMPANIES_FILE = ROOT / "companies.json"

DEFAULT_LOCATION = os.environ.get("LOCATION_KEYWORD", "Hyderabad")
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "25"))
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY_SECONDS", "0.7"))
HEADERS = {
    "User-Agent": os.environ.get(
        "JOB_ALERT_USER_AGENT",
        "Mozilla/5.0 (compatible; HyderabadJobAlert/2.0; +https://github.com/)",
    ),
    "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


@dataclass(frozen=True)
class Job:
    company: str
    title: str
    location: str
    url: str
    source: str
    external_id: str = ""
    posted_at: str = ""
    description: str = ""

    @property
    def key(self) -> str:
        raw = "|".join(
            [
                self.company.lower(),
                self.source.lower(),
                self.external_id.lower(),
                self.title.lower(),
                self.location.lower(),
                self.url.lower(),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def to_state_record(self) -> dict[str, str]:
        return {
            "company": self.company,
            "title": self.title,
            "location": self.location,
            "url": self.url,
            "source": self.source,
            "external_id": self.external_id,
            "posted_at": self.posted_at,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    value = html.unescape(str(value))
    return re.sub(r"\s+", " ", value).strip()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")


def load_state() -> dict[str, Any]:
    raw = load_json(SEEN_FILE, {"version": 2, "jobs": {}})
    if isinstance(raw, dict) and raw.get("version") == 2 and isinstance(raw.get("jobs"), dict):
        return raw

    # Migration for the earlier format: {"Company": ["job-id", ...]}.
    migrated: dict[str, Any] = {"version": 2, "jobs": {}, "migrated_at": utc_now()}
    if isinstance(raw, dict):
        for company, ids in raw.items():
            if not isinstance(ids, list):
                continue
            for old_id in ids:
                key_raw = f"{company}|legacy|{old_id}"
                key = hashlib.sha256(key_raw.encode("utf-8")).hexdigest()[:24]
                migrated["jobs"][key] = {
                    "company": company,
                    "title": "",
                    "location": "",
                    "url": "",
                    "source": "legacy",
                    "external_id": str(old_id),
                    "first_seen": utc_now(),
                    "last_seen": utc_now(),
                }
    return migrated


def request_json(method: str, url: str, **kwargs: Any) -> Any:
    response = requests.request(
        method,
        url,
        headers={**HEADERS, **kwargs.pop("headers", {})},
        timeout=REQUEST_TIMEOUT,
        **kwargs,
    )
    response.raise_for_status()
    return response.json()


def request_text(url: str, **kwargs: Any) -> str:
    response = requests.get(url, headers={**HEADERS, **kwargs.pop("headers", {})}, timeout=REQUEST_TIMEOUT, **kwargs)
    response.raise_for_status()
    return response.text


def flatten_json_ld(value: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            items.extend(flatten_json_ld(item))
    elif isinstance(value, dict):
        items.append(value)
        graph = value.get("@graph")
        if graph:
            items.extend(flatten_json_ld(graph))
    return items


def location_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ", ".join(filter(None, [location_to_text(item) for item in value]))
    if isinstance(value, dict):
        address = value.get("address", value)
        if isinstance(address, dict):
            parts = [
                address.get("addressLocality"),
                address.get("addressRegion"),
                address.get("addressCountry"),
                address.get("streetAddress"),
            ]
            return ", ".join(clean_text(part) for part in parts if clean_text(part))
    return ""


def job_matches(job: Job, location_keyword: str, keywords: list[str]) -> bool:
    haystack = " ".join([job.title, job.location, job.description]).lower()
    if location_keyword.lower() not in haystack:
        return False
    return all(keyword.lower() in haystack for keyword in keywords)


def dedupe_jobs(jobs: list[Job]) -> list[Job]:
    result: list[Job] = []
    seen: set[str] = set()
    for job in jobs:
        key = job.key
        if key in seen:
            continue
        seen.add(key)
        result.append(job)
    return result


def fetch_workday(company: str, source: dict[str, Any], location_keyword: str) -> list[Job]:
    base_url = source["base_url"].rstrip("/")
    tenant = source["tenant"]
    site = source["site"]
    api_url = f"{base_url}/wday/cxs/{tenant}/{site}/jobs"
    locale = source.get("locale", "en-US")
    limit = int(source.get("limit", 50))
    max_pages = int(source.get("max_pages", 8))
    jobs: list[Job] = []

    for page in range(max_pages):
        payload = {
            "appliedFacets": source.get("applied_facets", {}),
            "limit": limit,
            "offset": page * limit,
            "searchText": source.get("search_text", location_keyword),
        }
        data = request_json(
            "POST",
            api_url,
            headers={"Content-Type": "application/json"},
            json=payload,
        )
        postings = data.get("jobPostings", [])
        if not postings:
            break
        for item in postings:
            external_path = clean_text(item.get("externalPath"))
            bullet_fields = item.get("bulletFields") or []
            external_id = clean_text(item.get("jobReqId") or (bullet_fields[0] if bullet_fields else external_path))
            location = clean_text(item.get("locationsText") or ", ".join(map(str, bullet_fields[1:])))
            if external_path:
                public_url = f"{base_url}/{locale}/{site}{external_path}"
            else:
                public_url = source.get("url", base_url)
            jobs.append(
                Job(
                    company=company,
                    title=clean_text(item.get("title")),
                    location=location,
                    url=public_url,
                    source=source_name(source),
                    external_id=external_id,
                    posted_at=clean_text(item.get("postedOn")),
                    description=clean_text(item.get("jobDescription")),
                )
            )
    return jobs


def fetch_greenhouse(company: str, source: dict[str, Any], _location_keyword: str) -> list[Job]:
    board = source["board"]
    data = request_json("GET", f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true")
    jobs = []
    for item in data.get("jobs", []):
        location = clean_text((item.get("location") or {}).get("name"))
        jobs.append(
            Job(
                company=company,
                title=clean_text(item.get("title")),
                location=location,
                url=clean_text(item.get("absolute_url")),
                source=source_name(source),
                external_id=clean_text(item.get("id")),
                posted_at=clean_text(item.get("updated_at")),
                description=clean_text(item.get("content")),
            )
        )
    return jobs


def fetch_lever(company: str, source: dict[str, Any], _location_keyword: str) -> list[Job]:
    org = source["company"]
    data = request_json("GET", f"https://api.lever.co/v0/postings/{org}?mode=json")
    jobs = []
    for item in data:
        categories = item.get("categories") or {}
        jobs.append(
            Job(
                company=company,
                title=clean_text(item.get("text")),
                location=clean_text(categories.get("location")),
                url=clean_text(item.get("hostedUrl")),
                source=source_name(source),
                external_id=clean_text(item.get("id")),
                posted_at=clean_text(item.get("createdAt")),
                description=clean_text(item.get("descriptionPlain") or item.get("description")),
            )
        )
    return jobs


def fetch_smartrecruiters(company: str, source: dict[str, Any], location_keyword: str) -> list[Job]:
    org = source["company"]
    params = {"limit": source.get("limit", 100), "offset": 0, "q": source.get("query", location_keyword)}
    data = request_json("GET", f"https://api.smartrecruiters.com/v1/companies/{org}/postings", params=params)
    jobs = []
    for item in data.get("content", []):
        location = item.get("location") or {}
        location_text = ", ".join(clean_text(location.get(part)) for part in ("city", "region", "country") if location.get(part))
        jobs.append(
            Job(
                company=company,
                title=clean_text(item.get("name")),
                location=location_text,
                url=clean_text(item.get("ref") or item.get("applyUrl")),
                source=source_name(source),
                external_id=clean_text(item.get("id")),
                posted_at=clean_text(item.get("releasedDate")),
                description=clean_text(item.get("jobAd", {}).get("sections", {}).get("jobDescription")),
            )
        )
    return jobs


def fetch_ashby(company: str, source: dict[str, Any], _location_keyword: str) -> list[Job]:
    org = source["organization"]
    data = request_json("GET", f"https://api.ashbyhq.com/posting-api/job-board/{org}")
    jobs = []
    for item in data.get("jobs", []):
        location = clean_text(item.get("locationName") or item.get("location"))
        jobs.append(
            Job(
                company=company,
                title=clean_text(item.get("title")),
                location=location,
                url=clean_text(item.get("jobUrl") or item.get("applyUrl")),
                source=source_name(source),
                external_id=clean_text(item.get("id")),
                posted_at=clean_text(item.get("publishedAt")),
                description=clean_text(item.get("descriptionHtml") or item.get("descriptionPlain")),
            )
        )
    return jobs


def fetch_microsoft(company: str, source: dict[str, Any], location_keyword: str) -> list[Job]:
    params = {
        "lc": source.get("country", "India"),
        "l": source.get("location", location_keyword),
        "pg": 1,
        "pgSz": source.get("page_size", 100),
        "o": "Recent",
        "flt": "true",
    }
    url = source.get("api_url", "https://gcsservices.careers.microsoft.com/search/api/v1/search")
    data = request_json("GET", url, params=params)
    jobs_data = (
        data.get("operationResult", {}).get("result", {}).get("jobs")
        or data.get("jobs")
        or data.get("results")
        or []
    )
    jobs = []
    for item in jobs_data:
        job_id = clean_text(item.get("jobId") or item.get("id") or item.get("postingId"))
        locations = item.get("locations") or item.get("location") or []
        location = location_to_text(locations)
        public_url = item.get("externalApplyLink") or item.get("url")
        if not public_url and job_id:
            public_url = f"https://careers.microsoft.com/us/en/job/{job_id}"
        jobs.append(
            Job(
                company=company,
                title=clean_text(item.get("title")),
                location=location,
                url=clean_text(public_url),
                source=source_name(source),
                external_id=job_id,
                posted_at=clean_text(item.get("postedDate") or item.get("postingDate")),
                description=clean_text(item.get("description") or item.get("overview")),
            )
        )
    return jobs


def fetch_amazon(company: str, source: dict[str, Any], location_keyword: str) -> list[Job]:
    params = {
        "base_query": source.get("query", ""),
        "loc_query": source.get("location_query", f"{location_keyword}, India"),
        "offset": 0,
        "result_limit": source.get("limit", 100),
        "sort": "recent",
    }
    url = source.get("api_url", "https://www.amazon.jobs/en/search.json")
    data = request_json("GET", url, params=params)
    jobs_data = data.get("jobs") or data.get("results") or []
    jobs = []
    for item in jobs_data:
        job_path = clean_text(item.get("job_path") or item.get("url_next_step") or item.get("url"))
        public_url = urljoin("https://www.amazon.jobs", job_path)
        location = clean_text(item.get("normalized_location") or item.get("location"))
        jobs.append(
            Job(
                company=company,
                title=clean_text(item.get("title")),
                location=location,
                url=public_url,
                source=source_name(source),
                external_id=clean_text(item.get("id") or item.get("job_id")),
                posted_at=clean_text(item.get("posted_date") or item.get("updated_time")),
                description=clean_text(item.get("description")),
            )
        )
    return jobs


def fetch_rss(company: str, source: dict[str, Any], _location_keyword: str) -> list[Job]:
    text = request_text(source["url"])
    root = ET.fromstring(text)
    jobs = []
    for item in root.findall(".//item") + root.findall(".//{http://www.w3.org/2005/Atom}entry"):
        title = clean_text(item.findtext("title") or item.findtext("{http://www.w3.org/2005/Atom}title"))
        link = clean_text(item.findtext("link") or item.findtext("{http://www.w3.org/2005/Atom}link"))
        description = clean_text(item.findtext("description") or item.findtext("{http://www.w3.org/2005/Atom}summary"))
        guid = clean_text(item.findtext("guid") or item.findtext("{http://www.w3.org/2005/Atom}id") or link)
        jobs.append(
            Job(
                company=company,
                title=title,
                location=source.get("assumed_location", ""),
                url=link,
                source=source_name(source),
                external_id=guid,
                description=description,
            )
        )
    return jobs


def fetch_html(company: str, source: dict[str, Any], _location_keyword: str) -> list[Job]:
    url = source["url"]
    text = request_text(url)
    soup = BeautifulSoup(text, "html.parser")
    jobs: list[Job] = []

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not script.string:
            continue
        try:
            objects = flatten_json_ld(json.loads(script.string))
        except json.JSONDecodeError:
            continue
        for item in objects:
            item_type = item.get("@type", "")
            item_types = item_type if isinstance(item_type, list) else [item_type]
            if "JobPosting" not in item_types:
                continue
            job_url = clean_text(item.get("url") or url)
            jobs.append(
                Job(
                    company=company,
                    title=clean_text(item.get("title")),
                    location=location_to_text(item.get("jobLocation")),
                    url=urljoin(url, job_url),
                    source=source_name(source),
                    external_id=clean_text(item.get("identifier", {}).get("value") if isinstance(item.get("identifier"), dict) else item.get("identifier")),
                    posted_at=clean_text(item.get("datePosted")),
                    description=clean_text(item.get("description")),
                )
            )

    # Conservative fallback for pages without JobPosting structured data.
    job_words = re.compile(r"\b(job|career|role|engineer|analyst|developer|manager|architect|consultant)\b", re.I)
    for anchor in soup.find_all("a", href=True):
        label = clean_text(anchor.get_text(" ", strip=True))
        href = clean_text(anchor.get("href"))
        parent_text = clean_text(anchor.parent.get_text(" ", strip=True) if anchor.parent else label)
        context = f"{label} {href} {parent_text}"
        if not label or not job_words.search(context):
            continue
        jobs.append(
            Job(
                company=company,
                title=label[:180],
                location=parent_text[:220],
                url=urljoin(url, href),
                source=source_name(source),
                external_id=href,
                description=parent_text,
            )
        )
    return jobs


FETCHERS = {
    "workday": fetch_workday,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "smartrecruiters": fetch_smartrecruiters,
    "ashby": fetch_ashby,
    "microsoft": fetch_microsoft,
    "amazon": fetch_amazon,
    "rss": fetch_rss,
    "html": fetch_html,
}


def source_name(source: dict[str, Any]) -> str:
    label = source.get("name") or source.get("type") or "unknown"
    return clean_text(label)


def normalize_company_entry(entry: dict[str, Any]) -> dict[str, Any]:
    # Backward compatibility with the original companies.json shape.
    if "sources" not in entry and "ats" in entry:
        ats = entry["ats"]
        converted = dict(entry)
        converted["sources"] = [{"type": ats, **{k: v for k, v in entry.items() if k not in {"name", "ats"}}}]
        return converted
    return entry


def fetch_company(entry: dict[str, Any], location_keyword: str, keywords: list[str]) -> tuple[list[Job], list[str]]:
    entry = normalize_company_entry(entry)
    company = entry["name"]
    if entry.get("enabled") is False:
        return [], [f"{company}: disabled"]

    jobs: list[Job] = []
    failures: list[str] = []
    sources = entry.get("sources") or []
    if not sources:
        return [], [f"{company}: no sources configured"]

    for source in sources:
        source_type = source.get("type")
        fetcher = FETCHERS.get(source_type)
        if not fetcher:
            failures.append(f"{company}: unknown source type '{source_type}'")
            continue
        try:
            found = fetcher(company, source, location_keyword)
            matching = [job for job in found if job_matches(job, location_keyword, keywords)]
            jobs.extend(matching)
        except Exception as exc:  # noqa: BLE001 - each provider should fail independently.
            failures.append(f"{company} / {source_name(source)}: {exc}")
        time.sleep(REQUEST_DELAY_SECONDS)

    return dedupe_jobs(jobs), failures


def render_jobs_text(jobs: list[Job], failures: list[str]) -> str:
    lines = [f"{len(jobs)} new Hyderabad job posting(s) found.", ""]
    for index, job in enumerate(jobs, start=1):
        lines.extend(
            [
                f"{index}. {job.company} - {job.title}",
                f"   Location: {job.location or 'Not listed'}",
                f"   Source: {job.source}",
                f"   Posted: {job.posted_at or 'Not listed'}",
                f"   Apply: {job.url}",
                "",
            ]
        )
    if failures and os.environ.get("INCLUDE_FAILURES_IN_EMAIL", "false").lower() == "true":
        lines.append("Source warnings:")
        lines.extend(f"- {failure}" for failure in failures[:30])
        lines.append("")
    return "\n".join(lines).strip()


def send_email(subject: str, body: str) -> bool:
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    to_addr = os.environ.get("ALERT_TO_EMAIL")

    if not all([smtp_host, smtp_user, smtp_pass, to_addr]):
        print("Email secrets are not fully set; printing alert instead.")
        print(body)
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp_user
    message["To"] = to_addr
    message.set_content(body)

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.send_message(message)
    print(f"Email sent to {to_addr}.")
    return True


def send_twilio_sms(body: str) -> bool:
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_FROM_NUMBER")
    to_number = os.environ.get("ALERT_TO_PHONE")
    if not all([account_sid, auth_token, from_number, to_number]):
        return False

    sms_body = body[:1500]
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    response = requests.post(
        url,
        data={"From": from_number, "To": to_number, "Body": sms_body},
        auth=(account_sid, auth_token),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    print(f"SMS sent to {to_number}.")
    return True


def notify(new_jobs: list[Job], failures: list[str]) -> None:
    if not new_jobs:
        if os.environ.get("SEND_NO_CHANGE_EMAIL", "false").lower() == "true":
            send_email("[Job Alert] No new Hyderabad roles", "No new Hyderabad job postings were found in this run.")
        return

    subject = f"[Job Alert] {len(new_jobs)} new Hyderabad role(s)"
    body = render_jobs_text(new_jobs, failures)
    send_email(subject, body)
    try:
        send_twilio_sms(body)
    except Exception as exc:  # noqa: BLE001
        print(f"SMS failed: {exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check configured companies for new Hyderabad jobs.")
    parser.add_argument("--companies", default=str(COMPANIES_FILE), help="Path to companies.json")
    parser.add_argument("--location", default=DEFAULT_LOCATION, help="Location keyword to match")
    parser.add_argument("--keyword", action="append", default=[], help="Extra keyword that must also match")
    parser.add_argument("--baseline-only", action="store_true", help="Record current jobs as seen without sending alerts")
    parser.add_argument("--no-notify", action="store_true", help="Do not send email/SMS; print results only")
    parser.add_argument("--fail-on-source-error", action="store_true", help="Return exit code 2 if any provider fails")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    companies = load_json(Path(args.companies), [])
    if not isinstance(companies, list):
        raise SystemExit("companies.json must be a JSON array.")

    state = load_state()
    seen_jobs: dict[str, Any] = state.setdefault("jobs", {})
    started_at = utc_now()
    all_jobs: list[Job] = []
    all_new: list[Job] = []
    failures: list[str] = []

    print(f"Checking {len(companies)} companies for '{args.location}' roles...")
    for entry in companies:
        company_name = entry.get("name", "<unnamed>")
        print(f"- {company_name}")
        jobs, company_failures = fetch_company(entry, args.location, args.keyword)
        failures.extend(company_failures)
        all_jobs.extend(jobs)

        for job in jobs:
            record = seen_jobs.get(job.key)
            if not record:
                all_new.append(job)
                record = {**job.to_state_record(), "first_seen": started_at}
            record.update({**job.to_state_record(), "last_seen": started_at})
            seen_jobs[job.key] = record

        print(f"  matched {len(jobs)} Hyderabad role(s)")

    state["version"] = 2
    state["last_checked"] = started_at
    state["location_keyword"] = args.location
    save_json(SEEN_FILE, state)

    report = {
        "checked_at": started_at,
        "companies_checked": len(companies),
        "matching_jobs": len(all_jobs),
        "new_jobs": [job.to_state_record() for job in all_new],
        "failures": failures,
    }
    save_json(REPORT_FILE, report)

    if args.baseline_only or os.environ.get("BASELINE_ONLY", "false").lower() == "true":
        print(f"Baseline recorded. {len(all_jobs)} current Hyderabad role(s) marked as seen; no alert sent.")
    elif args.no_notify:
        print(render_jobs_text(all_new, failures) if all_new else "No new Hyderabad postings found.")
    else:
        notify(all_new, failures)

    if failures:
        print("\nSource warnings:")
        for failure in failures:
            print(f"- {failure}")

    if args.fail_on_source_error and failures:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
