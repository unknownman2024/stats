#!/usr/bin/env python3
"""
Box‑office data updater for Kerala special cinemas.
Fetches from Cloudflare Worker, merges with existing JSON files,
and stores per‑day statistics.
"""

import os
import json
import time
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
# Helpers
# ------------------------------------------------------------
def ist_now():
    """Return current time in IST (UTC+5:30) as ISO 8601 string with offset."""
    tz = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(tz).isoformat()  # FIXED: removed tzinfo kwarg

def normalize_movie_name(name):
    """Normalise a movie title for fuzzy matching."""
    if not name:
        return ""
    s = name.lower()
    s = s.replace(",", " ").replace(".", " ").replace("'", " ")
    s = ''.join(c for c in s if c.isalnum() or c.isspace())
    return ' '.join(s.split())

def get_date_key(date_str):
    """Extract YYYY-MM-DD from various date formats."""
    if not date_str:
        return None
    if len(date_str) >= 10:
        try:
            datetime.strptime(date_str[:10], "%Y-%m-%d")
            return date_str[:10]
        except ValueError:
            pass
    return None

def load_json(path):
    """Load JSON from file, return empty dict if not exists or invalid."""
    if not path.exists():
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

def save_json(path, data):
    """Save data as JSON with indentation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, sort_keys=True)

# ------------------------------------------------------------
# Fuzzy group movies
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
        canonical = min(group, key=len)
        mapping[canonical] = sorted(set(group))
    return mapping

# ------------------------------------------------------------
# Fetch API with exponential backoff
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
        "User-Agent": "KeralaBO-Updater/1.0 (+https://github.com/your-repo)",
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
            print(f"Timeout ({timeout}s) – retrying...")
            time.sleep(5)

    raise last_exception or RuntimeError("Failed to fetch API after multiple attempts")

# ------------------------------------------------------------
# Extract and merge
# ------------------------------------------------------------
def extract_shows(api_data):
    """Extract shows from API response, group by date."""
    shows_by_date = defaultdict(list)
    for theatre in api_data.get("theatres", []):
        theatre_name = theatre.get("name")
        if not theatre_name:
            continue
        for show in theatre.get("data", []):
            date_str = show.get("date")
            date_key = get_date_key(date_str)
            if not date_key:
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

def merge_show(existing_shows, new_show):
    """Merge a single show, only update if sold increases."""
    existing = None
    for idx, s in enumerate(existing_shows):
        if s.get("venue") == new_show["venue"] and s.get("showid") == new_show["showid"]:
            existing = s
            break
    if existing:
        if new_show["tickets_sold"] > existing["tickets_sold"]:
            existing["tickets_sold"] = new_show["tickets_sold"]
            existing["gross"] = new_show["gross"]
            if new_show.get("time") and not existing.get("time"):
                existing["time"] = new_show["time"]
            if new_show.get("screen") and not existing.get("screen"):
                existing["screen"] = new_show["screen"]
    else:
        existing_shows.append(new_show)

def merge_date_data(existing_data, new_shows, movie_mapping):
    """Merge new shows for a single date into existing_data."""
    if "movies" not in existing_data:
        existing_data["movies"] = {}
    if "verification" not in existing_data:
        existing_data["verification"] = {"merged_movies": []}

    # Build inverse alias -> canonical
    alias_to_canon = {}
    for canon, aliases in movie_mapping.items():
        for alias in aliases:
            alias_to_canon[alias] = canon

    for show in new_shows:
        raw_movie = show["movie"]
        canon = alias_to_canon.get(raw_movie, raw_movie)
        if canon not in existing_data["movies"]:
            existing_data["movies"][canon] = {"show_breakdown": []}
        merge_show(existing_data["movies"][canon]["show_breakdown"], show)

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
            },
            "without_gross": {
                "shows": len(without_gross),
                "tickets_sold": sum(s["tickets_sold"] for s in without_gross),
            }
        }
        # Sort by sold descending
        entry["show_breakdown"].sort(key=lambda x: x.get("tickets_sold", 0), reverse=True)

    # Store verification
    existing_data["verification"]["merged_movies"] = [
        {"canonical": c, "aliases": aliases} for c, aliases in movie_mapping.items()
    ]

    existing_data["last_updated"] = ist_now()
    existing_data["date"] = existing_data.get("date", "")

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
def main():
    print("Fetching API data...")
    api_data = fetch_api()
    print("API fetch OK.")

    shows_by_date = extract_shows(api_data)
    if not shows_by_date:
        print("No shows found in API response.")
        return

    all_titles = set()
    for shows in shows_by_date.values():
        for s in shows:
            all_titles.add(s["movie"])
    movie_groups = group_similar_movies(all_titles)
    print(f"Found {len(movie_groups)} movie groups after fuzzy matching.")

    for date_str, new_shows in shows_by_date.items():
        year, month, day = date_str.split("-")
        file_path = DATA_ROOT / year / month / f"{day}.json"
        existing = load_json(file_path)
        if "date" not in existing:
            existing["date"] = date_str
        elif existing["date"] != date_str:
            existing["date"] = date_str
        merge_date_data(existing, new_shows, movie_groups)
        save_json(file_path, existing)
        print(f"Updated {file_path}")

    print("Done.")

if __name__ == "__main__":
    main()
