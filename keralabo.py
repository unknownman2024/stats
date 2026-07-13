#!/usr/bin/env python3
"""
Box‑office data updater for Kerala special cinemas.
Fetches from Cloudflare Worker, merges with existing JSON files,
and stores per‑day statistics.
"""

import os
import sys
import json
import time
import logging
import re
import difflib
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
API_URL = "https://kerala.bfilmyisback.workers.dev/"
DATA_ROOT = Path("data/keralaspecial")
ATP = 150  # average ticket price (used only if gross missing)

# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# Time helpers
# ------------------------------------------------------------
def ist_now():
    """Return current time in IST (UTC+5:30) as ISO 8601 string."""
    tz = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(tz).isoformat()

def ist_today_str():
    """Return today's date in YYYY-MM-DD format in IST."""
    tz = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(tz).strftime("%Y-%m-%d")

# ------------------------------------------------------------
# String normalisation
# ------------------------------------------------------------
def normalize_movie_name(name):
    """Normalise a movie title for fuzzy matching."""
    if not name:
        return ""
    s = name.lower()
    s = s.replace(",", " ").replace(".", " ").replace("'", " ")
    s = ''.join(c for c in s if c.isalnum() or c.isspace())
    return ' '.join(s.split())

# ------------------------------------------------------------
# Robust date parsing (handles suffixes like "-1")
# ------------------------------------------------------------
def parse_date(date_str):
    """
    Parse a date string into YYYY-MM-DD.
    Supports:
      - YYYY-MM-DD
      - DD-MM-YYYY, DD/MM/YYYY, etc.
      - YYYY-MM-DD-1  (extra dash + number suffix)
    Returns None if parsing fails.
    """
    if not date_str:
        return None
    raw = date_str.strip()

    # If there's a suffix like "-1", remove the last dash+digits
    # e.g., "2026-07-13-1" -> "2026-07-13"
    match = re.match(r'^(.+)-(\d+)$', raw)
    if match:
        candidate = match.group(1)
        # Try to parse the candidate as a date
        if parse_date(candidate):  # recursive call
            return parse_date(candidate)

    # Try various formats
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d.%m.%Y", "%Y.%m.%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    # If it starts with YYYY-MM-DD (e.g., "2026-07-13T...")
    if len(raw) >= 10 and raw[4] == '-' and raw[7] == '-':
        try:
            dt = datetime.strptime(raw[:10], "%Y-%m-%d")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    return None

def parse_theatre_date(updated_at, timestamp):
    """
    Parse the theatre's updated_at string (like "13-07-2026 04:51:pm") or
    use the timestamp (Unix) to get a date string YYYY-MM-DD.
    """
    if updated_at:
        # Try to parse "DD-MM-YYYY HH:MM:am/pm"
        # Remove extra spaces, handle single digit day/month
        s = updated_at.strip()
        # Try with different separators
        for fmt in ("%d-%m-%Y %I:%M:%S%p", "%d-%m-%Y %I:%M%p", "%d/%m/%Y %I:%M:%S%p", "%d/%m/%Y %I:%M%p"):
            try:
                dt = datetime.strptime(s, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        # If that fails, try generic parse
        for fmt in ("%d-%m-%Y", "%d/%m/%Y"):
            try:
                dt = datetime.strptime(s.split()[0], fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
    if timestamp:
        try:
            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(timezone(timedelta(hours=5, minutes=30)))
            return dt.strftime("%Y-%m-%d")
        except:
            pass
    return None

# ------------------------------------------------------------
# File I/O
# ------------------------------------------------------------
def load_json(path):
    """Load JSON from file, return empty dict if not exists or invalid."""
    if not path.exists():
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Failed to load {path}: {e}")
        return {}

def save_json(path, data):
    """Save data as JSON with indentation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, sort_keys=True)

# ------------------------------------------------------------
# Deduplication and merging
# ------------------------------------------------------------
def show_key(show):
    """
    Return a tuple uniquely identifying a show.
    Uses (venue, showid) if showid exists, else (venue, time, movie, screen).
    """
    venue = show.get("venue", "")
    showid = show.get("showid", "").strip()
    if showid:
        return (venue, showid)
    else:
        return (venue,
                show.get("time", ""),
                show.get("movie", ""),
                show.get("screen", ""))

def deduplicate_shows(shows):
    """
    Remove duplicate shows based on show_key, keeping the one with max tickets_sold.
    If tickets_sold tie, keep the first encountered.
    """
    groups = defaultdict(list)
    for s in shows:
        groups[show_key(s)].append(s)

    cleaned = []
    for key, group in groups.items():
        if len(group) == 1:
            cleaned.append(group[0])
        else:
            # Keep the show with highest tickets_sold; if tie, keep first
            best = max(group, key=lambda x: x.get("tickets_sold", 0))
            cleaned.append(best)
    return cleaned

def merge_show(existing_shows, new_show):
    """
    Merge a single show into a list of existing shows.
    If show_key matches, update only if tickets_sold increased.
    Otherwise, append.
    """
    key = show_key(new_show)
    for idx, s in enumerate(existing_shows):
        if show_key(s) == key:
            if new_show["tickets_sold"] > s["tickets_sold"]:
                s["tickets_sold"] = new_show["tickets_sold"]
                s["gross"] = new_show["gross"]
                # Update optional fields if missing
                if new_show.get("time") and not s.get("time"):
                    s["time"] = new_show["time"]
                if new_show.get("screen") and not s.get("screen"):
                    s["screen"] = new_show["screen"]
            return
    # Not found → append
    existing_shows.append(new_show)

# ------------------------------------------------------------
# Fuzzy grouping of movie titles
# ------------------------------------------------------------
def group_similar_movies(titles, threshold=0.85):
    """
    Group movie titles by similarity.
    Returns dict: canonical_name -> list of aliases.
    """
    if not titles:
        return {}

    unique = {}
    for t in titles:
        norm = normalize_movie_name(t)
        if norm:
            unique[t] = norm

    groups = []
    used = set()
    for title, norm in unique.items():
        if title in used:
            continue
        group = [title]
        used.add(title)
        for other, other_norm in unique.items():
            if other in used:
                continue
            ratio = difflib.SequenceMatcher(None, norm, other_norm).ratio()
            if ratio >= threshold:
                group.append(other)
                used.add(other)
        groups.append(group)

    mapping = {}
    for group in groups:
        canonical = min(group, key=len)  # pick shortest name as canonical
        mapping[canonical] = sorted(set(group))
    return mapping

# ------------------------------------------------------------
# API fetching with retries
# ------------------------------------------------------------
def fetch_api():
    """
    Fetch data from Cloudflare Worker with retries and backoff.
    """
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    params = {"t": int(datetime.now().timestamp())}
    headers = {
        "User-Agent": "KeralaBO-Updater/1.0",
        "Accept": "application/json",
    }

    timeouts = [30, 60, 90]
    last_exception = None

    for timeout in timeouts:
        try:
            resp = session.get(API_URL, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exception = e
            logger.warning(f"Timeout ({timeout}s) – retrying...")
            time.sleep(5)

    raise last_exception or RuntimeError("Failed to fetch API after multiple attempts")

# ------------------------------------------------------------
# Extract shows from API response
# ------------------------------------------------------------
def extract_shows(api_data):
    """
    Extract shows from API response, group by parsed date.
    If a show has null date, try to use the theatre's updated_at or timestamp.
    Returns dict: date_key (YYYY-MM-DD) -> list of show dicts.
    """
    shows_by_date = defaultdict(list)
    for theatre in api_data.get("theatres", []):
        theatre_name = theatre.get("name")
        if not theatre_name:
            continue

        # Get fallback date from theatre metadata
        fallback_date = parse_theatre_date(
            theatre.get("updated_at"),
            theatre.get("timestamp")
        )

        for show in theatre.get("data", []):
            raw_date = show.get("date")
            date_key = parse_date(raw_date)
            # If date is missing or unparseable, use fallback
            if not date_key and fallback_date:
                date_key = fallback_date
                logger.debug(f"Assigned fallback date {date_key} for show at {theatre_name}")

            if not date_key:
                logger.warning(f"Skipping show with unparseable date: {raw_date} (theatre: {theatre_name})")
                continue

            rec = {
                "venue": theatre_name,
                "movie": show.get("movie", "Unknown"),
                "showid": show.get("showid", ""),
                "time": show.get("time"),
                "screen": show.get("screen"),
                "tickets_sold": int(show.get("tickets_sold", 0)),
                "gross": int(show.get("gross", 0)),
            }
            shows_by_date[date_key].append(rec)

    return dict(shows_by_date)

# ------------------------------------------------------------
# Merge new shows into existing date data
# ------------------------------------------------------------
def merge_date_data(existing_data, new_shows, movie_mapping):
    """
    Merge new_shows into existing_data (for one date).
    Deduplicates after merging.
    """
    # Ensure structure
    if "movies" not in existing_data:
        existing_data["movies"] = {}
    if "verification" not in existing_data:
        existing_data["verification"] = {"merged_movies": []}

    # Build alias -> canonical mapping
    alias_to_canon = {}
    for canon, aliases in movie_mapping.items():
        for alias in aliases:
            alias_to_canon[alias] = canon

    # Merge shows into each movie's breakdown
    for show in new_shows:
        raw_movie = show["movie"]
        canon = alias_to_canon.get(raw_movie, raw_movie)
        if canon not in existing_data["movies"]:
            existing_data["movies"][canon] = {"show_breakdown": []}
        merge_show(existing_data["movies"][canon]["show_breakdown"], show)

    # Deduplicate each movie's breakdown
    for movie, entry in existing_data["movies"].items():
        entry["show_breakdown"] = deduplicate_shows(entry["show_breakdown"])

    # Recompute aggregates
    for movie, entry in existing_data["movies"].items():
        breakdown = entry["show_breakdown"]
        total_shows = len(breakdown)
        total_sold = sum(s.get("tickets_sold", 0) for s in breakdown)
        total_gross = sum(s.get("gross", 0) for s in breakdown)

        with_gross = [s for s in breakdown if s.get("gross", 0) > 0]
        without_gross = [s for s in breakdown if s.get("gross", 0) == 0]

        entry["shows"] = total_shows
        entry["tickets_sold"] = total_sold
        entry["gross"] = total_gross
        entry["occupancy"] = None
        entry["seats_type"] = {
            "with_gross": {
                "shows": len(with_gross),
                "tickets_sold": sum(s["tickets_sold"] for s in with_gross),
                "gross": sum(s["gross"] for s in with_gross),
                "breakdown": with_gross,
            },
            "without_gross": {
                "shows": len(without_gross),
                "tickets_sold": sum(s["tickets_sold"] for s in without_gross),
                "breakdown": without_gross,
            }
        }
        # Sort master breakdown by sold descending
        entry["show_breakdown"].sort(key=lambda x: x.get("tickets_sold", 0), reverse=True)

    # Store verification info (optional)
    existing_data["verification"]["merged_movies"] = [
        {"canonical": c, "aliases": aliases} for c, aliases in movie_mapping.items()
    ]

    existing_data["last_updated"] = ist_now()
    # Preserve the date field (should match the file's date)

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
def main():
    logger.info("Starting Kerala BO updater")
    try:
        api_data = fetch_api()
        logger.info("API fetch successful")
    except Exception as e:
        logger.error(f"Failed to fetch API: {e}")
        sys.exit(1)

    shows_by_date = extract_shows(api_data)
    if not shows_by_date:
        logger.warning("No shows found in API response")
        return

    # Collect all movie titles for fuzzy grouping
    all_titles = set()
    for shows in shows_by_date.values():
        for s in shows:
            all_titles.add(s["movie"])
    movie_mapping = group_similar_movies(all_titles)
    logger.info(f"Fuzzy grouped into {len(movie_mapping)} movie groups")

    # Process each date
    for date_str, new_shows in shows_by_date.items():
        year, month, day = date_str.split("-")
        file_path = DATA_ROOT / year / month / f"{day}.json"

        existing = load_json(file_path)
        # Ensure the date field is correct
        if "date" not in existing or existing["date"] != date_str:
            existing["date"] = date_str

        merge_date_data(existing, new_shows, movie_mapping)
        save_json(file_path, existing)
        logger.info(f"Updated {file_path} ({len(new_shows)} shows)")

    logger.info("Done")

if __name__ == "__main__":
    main()
