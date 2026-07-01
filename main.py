#!/usr/bin/env python3
"""
BFILMY GA4 Exporter

A production-grade single-file script that exports Google Analytics 4 page statistics
into optimized JSON files for a website. Designed for hourly execution on GitHub Actions.

Uses GA4_KEY_JSON environment variable (if present) or falls back to key.json file.
"""

import hashlib
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Any, Optional

# Google Analytics Data API
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    RunReportRequest,
)
from google.api_core.exceptions import (
    ResourceExhausted,
    InternalServerError,
    ServiceUnavailable,
    GatewayTimeout,
    DeadlineExceeded,
    TooManyRequests,
    RetryError,
)
from google.oauth2 import service_account

# =============================================================================
# Configuration
# =============================================================================

PROPERTY_ID = "538422281"
KEY_FILE = "key.json"          # Fallback for local development
OUTPUT_DIR = "stats"

# Only three ranges: all time, today, yesterday
DATE_RANGES = [
    ("all", "365daysAgo", "today"),
    ("today", "today", "today"),
    ("yesterday", "yesterday", "yesterday"),
]

# For daily breakdown in summary.json – last 30 days
DAILY_START = "30daysAgo"
DAILY_END = "today"

PAGE_SIZE = 100000
MAX_RETRIES = 5
INITIAL_BACKOFF = 2
BACKOFF_MULTIPLIER = 2

IGNORED_PATHS = [
    "/404", "/500", "/search", "/login", "/admin",
    "/favicon.ico", "/robots.txt",
]

# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class PageMetrics:
    slug: str
    views: int
    users: int
    sessions: int

@dataclass
class SectionData:
    name: str
    pages: List[PageMetrics]
    total_views: int = 0
    total_users: int = 0
    total_sessions: int = 0

    def __post_init__(self):
        self.total_views = sum(p.views for p in self.pages)
        self.total_users = sum(p.users for p in self.pages)
        self.total_sessions = sum(p.sessions for p in self.pages)

@dataclass
class DailyStats:
    date: str
    views: int
    users: int
    sessions: int
    pages: int

# =============================================================================
# Helper Functions
# =============================================================================

def sanitize_filename(name: str) -> str:
    s = name.lower().strip()
    s = s.replace(" ", "-")
    s = re.sub(r"[^a-z0-9\-_]", "", s)
    s = re.sub(r"-+", "-", s)
    s = s.strip("-_")
    if len(s) > 80:
        return hashlib.sha1(s.encode("utf-8")).hexdigest() + ".json"
    return s + ".json" if s else "index.json"

def extract_slug_and_section(path: str) -> Tuple[str, str]:
    path = path.split("?")[0]
    path = path.rstrip("/")
    if not path or path == "/":
        return "index", "index"
    parts = path.split("/")
    section = parts[1] if len(parts) > 1 else "index"
    slug = "/".join(parts[2:]) if len(parts) > 2 else "index"
    if not slug:
        slug = "index"
    section_safe = sanitize_filename(section).replace(".json", "")
    return section_safe, slug

def is_ignored_path(path: str) -> bool:
    path_lower = path.lower()
    return any(path_lower.startswith(ignored.lower()) for ignored in IGNORED_PATHS)

def generate_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def safe_write_json(data: Dict, filepath: str) -> None:
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    temp_path = filepath + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
    os.replace(temp_path, filepath)

def format_date_yyyymmdd(date_str: str) -> str:
    if len(date_str) == 8:
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    return date_str

# =============================================================================
# GA4 Data Fetcher with Retry Logic
# =============================================================================

class GA4DataFetcher:
    def __init__(self, property_id: str, credentials: service_account.Credentials):
        self.property_id = property_id
        self.client = BetaAnalyticsDataClient(credentials=credentials)

    def fetch_range(self, range_name: str, start_date: str, end_date: str) -> List[Dict]:
        all_rows = []
        offset = 0
        total_fetched = 0

        while True:
            request = RunReportRequest(
                property=f"properties/{self.property_id}",
                dimensions=[Dimension(name="pagePath")],
                metrics=[
                    Metric(name="screenPageViews"),
                    Metric(name="activeUsers"),
                    Metric(name="sessions"),
                ],
                date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
                offset=offset,
                limit=PAGE_SIZE,
                return_property_quota=False,
                keep_empty_rows=False,
            )

            response = self._call_with_retry(request)
            rows = response.rows
            if not rows:
                break

            for row in rows:
                path = row.dimension_values[0].value
                views = int(row.metric_values[0].value) if row.metric_values[0].value else 0
                users = int(row.metric_values[1].value) if row.metric_values[1].value else 0
                sessions = int(row.metric_values[2].value) if row.metric_values[2].value else 0

                if is_ignored_path(path) or views <= 0:
                    continue

                all_rows.append({
                    "path": path,
                    "views": views,
                    "users": users,
                    "sessions": sessions,
                })

            total_fetched += len(rows)
            logger.info(f"Fetched {total_fetched} rows for {range_name}...")

            if len(rows) < PAGE_SIZE:
                break

            offset += PAGE_SIZE

        return all_rows

    def fetch_daily_range(self, start_date: str, end_date: str) -> List[Dict]:
        all_rows = []
        offset = 0
        total_fetched = 0

        while True:
            request = RunReportRequest(
                property=f"properties/{self.property_id}",
                dimensions=[
                    Dimension(name="date"),
                    Dimension(name="pagePath"),
                ],
                metrics=[
                    Metric(name="screenPageViews"),
                    Metric(name="activeUsers"),
                    Metric(name="sessions"),
                ],
                date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
                offset=offset,
                limit=PAGE_SIZE,
                return_property_quota=False,
                keep_empty_rows=False,
            )

            response = self._call_with_retry(request)
            rows = response.rows
            if not rows:
                break

            for row in rows:
                date_val = row.dimension_values[0].value
                path = row.dimension_values[1].value
                views = int(row.metric_values[0].value) if row.metric_values[0].value else 0
                users = int(row.metric_values[1].value) if row.metric_values[1].value else 0
                sessions = int(row.metric_values[2].value) if row.metric_values[2].value else 0

                if is_ignored_path(path) or views <= 0:
                    continue

                all_rows.append({
                    "date": date_val,
                    "path": path,
                    "views": views,
                    "users": users,
                    "sessions": sessions,
                })

            total_fetched += len(rows)
            logger.info(f"Fetched {total_fetched} daily rows...")

            if len(rows) < PAGE_SIZE:
                break

            offset += PAGE_SIZE

        return all_rows

    def _call_with_retry(self, request: RunReportRequest) -> Any:
        retries = 0
        delay = INITIAL_BACKOFF

        while retries <= MAX_RETRIES:
            try:
                return self.client.run_report(request)
            except (
                ResourceExhausted,
                InternalServerError,
                ServiceUnavailable,
                GatewayTimeout,
                DeadlineExceeded,
                TooManyRequests,
                RetryError,
            ) as e:
                retries += 1
                if retries > MAX_RETRIES:
                    logger.error(f"Max retries exceeded: {e}")
                    raise
                logger.warning(f"Retry {retries}/{MAX_RETRIES} after error: {e}. Waiting {delay}s...")
                time.sleep(delay)
                delay *= BACKOFF_MULTIPLIER
            except Exception as e:
                logger.error(f"Non-retryable error: {e}")
                raise
        raise RuntimeError("Retry loop exhausted without success.")

# =============================================================================
# Data Processing
# =============================================================================

def process_rows(rows: List[Dict]) -> Tuple[Dict[str, SectionData], Dict]:
    sections: Dict[str, SectionData] = {}
    total_views = total_users = total_sessions = 0
    total_pages = 0

    for row in rows:
        path = row["path"]
        section_name, slug = extract_slug_and_section(path)

        if section_name not in sections:
            sections[section_name] = SectionData(name=section_name, pages=[])

        sections[section_name].pages.append(PageMetrics(
            slug=slug,
            views=row["views"],
            users=row["users"],
            sessions=row["sessions"],
        ))

        total_views += row["views"]
        total_users += row["users"]
        total_sessions += row["sessions"]
        total_pages += 1

    for sec in sections.values():
        sec.total_views = sum(p.views for p in sec.pages)
        sec.total_users = sum(p.users for p in sec.pages)
        sec.total_sessions = sum(p.sessions for p in sec.pages)

    range_totals = {
        "views": total_views,
        "users": total_users,
        "sessions": total_sessions,
        "pages": total_pages,
    }
    return sections, range_totals

def process_daily_rows(rows: List[Dict]) -> List[DailyStats]:
    daily_data: Dict[str, Dict] = {}
    for row in rows:
        date_str = row["date"]
        if date_str not in daily_data:
            daily_data[date_str] = {
                "views": 0,
                "users": 0,
                "sessions": 0,
                "pages_set": set()
            }
        daily_data[date_str]["views"] += row["views"]
        daily_data[date_str]["users"] += row["users"]
        daily_data[date_str]["sessions"] += row["sessions"]
        daily_data[date_str]["pages_set"].add(row["path"])

    daily_stats = []
    for date_str, data in sorted(daily_data.items()):
        daily_stats.append(DailyStats(
            date=format_date_yyyymmdd(date_str),
            views=data["views"],
            users=data["users"],
            sessions=data["sessions"],
            pages=len(data["pages_set"]),
        ))
    return daily_stats

# =============================================================================
# File Writers
# =============================================================================

def write_section_files(
    sections: Dict[str, SectionData],
    range_name: str,
    generated: str,
    property_id: str
) -> Dict[str, Dict]:
    range_dir = os.path.join(OUTPUT_DIR, range_name)
    os.makedirs(range_dir, exist_ok=True)

    index_entries = {}

    for section_name, section_data in sections.items():
        pages_dict = {
            page.slug: {
                "views": page.views,
                "users": page.users,
                "sessions": page.sessions,
            }
            for page in section_data.pages
        }

        file_data = {
            "generated": generated,
            "property": property_id,
            "section": section_name,
            "range": range_name,
            "count": len(section_data.pages),
            "totalViews": section_data.total_views,
            "totalUsers": section_data.total_users,
            "totalSessions": section_data.total_sessions,
            "pages": pages_dict,
        }

        filename = sanitize_filename(section_name)
        filepath = os.path.join(range_dir, filename)
        safe_write_json(file_data, filepath)
        logger.info(f"✓ {filepath}")

        if range_name == "all":
            index_entries[section_name] = {
                "filename": filename,
                "folder": range_name,
                "pages": len(section_data.pages),
                "views": section_data.total_views,
            }

    return index_entries

def write_summary(daily_stats: List[DailyStats], generated: str, property_id: str) -> None:
    daily_list = [asdict(d) for d in daily_stats]
    file_data = {
        "generated": generated,
        "property": property_id,
        "daily": daily_list,
    }
    filepath = os.path.join(OUTPUT_DIR, "summary.json")
    safe_write_json(file_data, filepath)
    logger.info(f"✓ {filepath}")

def write_index(index_entries: Dict, generated: str, property_id: str) -> None:
    file_data = {
        "generated": generated,
        "property": property_id,
        "sections": index_entries,
    }
    filepath = os.path.join(OUTPUT_DIR, "index.json")
    safe_write_json(file_data, filepath)
    logger.info(f"✓ {filepath}")

# =============================================================================
# Main Orchestrator
# =============================================================================

def get_credentials() -> service_account.Credentials:
    """
    Obtain credentials from environment variable GA4_KEY_JSON (JSON string)
    or fall back to the file specified by KEY_FILE.
    """
    env_key = os.environ.get("GA4_KEY_JSON")
    if env_key:
        try:
            info = json.loads(env_key)
            return service_account.Credentials.from_service_account_info(info)
        except json.JSONDecodeError:
            logger.error("GA4_KEY_JSON environment variable is not valid JSON.")
            sys.exit(1)
    else:
        if not os.path.isfile(KEY_FILE):
            logger.error(f"Service account key file '{KEY_FILE}' not found and "
                         "GA4_KEY_JSON environment variable not set.")
            sys.exit(1)
        return service_account.Credentials.from_service_account_file(KEY_FILE)

def export_ga4_data() -> None:
    start_time = time.time()

    logger.info("=" * 41)
    logger.info("BFILMY GA4 Exporter")
    logger.info("=" * 41)
    logger.info("")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    credentials = get_credentials()
    fetcher = GA4DataFetcher(PROPERTY_ID, credentials)

    generated = generate_timestamp()
    all_index_entries = {}

    # ---- Fetch range data (all, today, yesterday) in parallel ----
    logger.info("Fetching range data...")
    with ThreadPoolExecutor(max_workers=3) as executor:
        future_to_range = {
            executor.submit(fetcher.fetch_range, name, start, end): (name, start, end)
            for name, start, end in DATE_RANGES
        }

        for future in as_completed(future_to_range):
            range_name, start_date, end_date = future_to_range[future]
            try:
                rows = future.result()
                logger.info(f"Range '{range_name}' done. Rows: {len(rows)}")
                sections, _ = process_rows(rows)
                index_entries = write_section_files(
                    sections, range_name, generated, PROPERTY_ID
                )
                if range_name == "all":
                    all_index_entries = index_entries
            except Exception as e:
                logger.error(f"Failed to process range '{range_name}': {e}")
                sys.exit(1)

    # ---- Fetch daily data for summary ----
    logger.info("Fetching daily data for summary...")
    try:
        daily_rows = fetcher.fetch_daily_range(DAILY_START, DAILY_END)
        daily_stats = process_daily_rows(daily_rows)
        logger.info(f"Daily stats computed for {len(daily_stats)} days.")
    except Exception as e:
        logger.error(f"Failed to fetch daily data: {e}")
        sys.exit(1)

    # ---- Write summary and index ----
    write_summary(daily_stats, generated, PROPERTY_ID)
    write_index(all_index_entries, generated, PROPERTY_ID)

    # ---- Final stats ----
    total_sections = len(all_index_entries)
    total_pages = sum(entry["pages"] for entry in all_index_entries.values())
    total_views = sum(entry["views"] for entry in all_index_entries.values())

    elapsed = time.time() - start_time
    logger.info("")
    logger.info(f"Completed in {elapsed:.2f} sec")
    logger.info("")
    logger.info(f"Total Sections : {total_sections}")
    logger.info(f"Total Pages    : {total_pages:,}")
    logger.info(f"Total Views    : {total_views:,}")

if __name__ == "__main__":
    export_ga4_data()
