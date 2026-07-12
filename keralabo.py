#!/usr/bin/env python3
"""
Box‑office data updater for Kerala special cinemas.
Fetches from Cloudflare Worker, merges with existing JSON files,
and stores per‑day statistics.
"""

import os
import json
import difflib
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path

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
    """Return current time in IST (UTC+5:30) as ISO string."""
    tz = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(tz).isoformat(tzinfo=tz)

def normalize_movie_name(name):
    """Normalise a movie title for fuzzy matching."""
    if not name:
        return ""
    # lower case, remove punctuation, collapse spaces
    s = name.lower()
    # remove common separators
    s = s.replace(",", " ").replace(".", " ").replace("'", " ")
    # keep only alphanumeric and spaces
    s = ''.join(c for c in s if c.isalnum() or c.isspace())
    # collapse multiple spaces
    return ' '.join(s.split())

def get_date_key(date_str):
    """Extract YYYY-MM-DD from various date formats."""
    if not date_str:
        return None
    if len(date_str) >= 10:
        # assume first 10 chars are YYYY-MM-DD
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

    # Normalise and create unique list
    unique = {}
    for t in titles:
        norm = normalize_movie_name(t)
        if norm:
            unique[t] = norm

    # Build groups
    groups = []
    used = set()
    for title, norm in unique.items():
        if title in used:
            continue
        # find similar
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

    # Pick canonical: shortest title (or most frequent)
    mapping = {}
    for group in groups:
        # pick shortest as canonical
        canonical = min(group, key=len)
        # but if there's a common one with more words? we can also pick the one that appears most often in the whole set? We'll stick with shortest.
        mapping[canonical] = sorted(set(group))
    return mapping

# ------------------------------------------------------------
# Fetch and parse API
# ------------------------------------------------------------
def fetch_api():
    """Fetch data from Cloudflare Worker with cache‑busting."""
    params = {"t": int(datetime.now().timestamp())}
    resp = requests.get(API_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()

def extract_shows(api_data):
    """
    Extract shows from API response, group by date.
    Returns dict: date_str -> list of show dicts.
    """
    shows_by_date = defaultdict(list)
    for theatre in api_data.get("theatres", []):
        theatre_name = theatre.get("name")
        if not theatre_name:
            continue
        for show in theatre.get("data", []):
            date_str = show.get("date")
            date_key = get_date_key(date_str)
            if not date_key:
                continue  # skip shows with invalid date
            # build show record
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
# Merge logic
# ------------------------------------------------------------
def merge_show(existing_shows, new_show):
    """
    Merge a single show into the existing list (in‑place).
    We identify a show by (venue, showid) – unique per theatre.
    Only update if new tickets_sold > existing sold.
    """
    existing = None
    for idx, s in enumerate(existing_shows):
        if s.get("venue") == new_show["venue"] and s.get("showid") == new_show["showid"]:
            existing = s
            break

    if existing:
        # update only if new sold is higher (monotonic)
        if new_show["tickets_sold"] > existing["tickets_sold"]:
            existing["tickets_sold"] = new_show["tickets_sold"]
            existing["gross"] = new_show["gross"]
            # keep other fields (time, screen) unchanged unless new has more info
            if new_show.get("time") and not existing.get("time"):
                existing["time"] = new_show["time"]
            if new_show.get("screen") and not existing.get("screen"):
                existing["screen"] = new_show["screen"]
    else:
        # new show, add it
        existing_shows.append(new_show)

def merge_date_data(existing_data, new_shows, movie_mapping):
    """
    Merge new shows for a single date into existing_data (in‑place).
    existing_data is the dict loaded from JSON.
    new_shows is list of show records for that date.
    movie_mapping is dict canonical -> list of aliases (for verification).
    """
    # Ensure structure
    if "movies" not in existing_data:
        existing_data["movies"] = {}
    if "verification" not in existing_data:
        existing_data["verification"] = {"merged_movies": []}

    # Helper to get or create movie entry
    def get_movie_entry(canonical):
        if canonical not in existing_data["movies"]:
            existing_data["movies"][canonical] = {
                "show_breakdown": []
            }
        return existing_data["movies"][canonical]

    # For each show, find its canonical movie name
    for show in new_shows:
        raw_movie = show["movie"]
        # find canonical via mapping
        canon = None
        for c, aliases in movie_mapping.items():
            if raw_movie in aliases:
                canon = c
                break
        if not canon:
            canon = raw_movie  # fallback

        # store the canonical name in the show record? we'll store movie as raw for show breakdown, but group under canon
        # merge show
        movie_entry = get_movie_entry(canon)
        merge_show(movie_entry["show_breakdown"], show)

    # After merging, recompute aggregates for each movie
    for movie, entry in existing_data["movies"].items():
        breakdown = entry["show_breakdown"]
        total_shows = len(breakdown)
        total_sold = sum(s.get("tickets_sold", 0) for s in breakdown)
        total_gross = sum(s.get("gross", 0) for s in breakdown)

        # Split with/without gross (gross > 0 considered "with_gross")
        with_gross = [s for s in breakdown if s.get("gross", 0) > 0]
        without_gross = [s for s in breakdown if s.get("gross", 0) == 0]

        entry["shows"] = total_shows
        entry["tickets_sold"] = total_sold
        entry["gross"] = total_gross
        # occupancy not available because we lack seat counts, set null
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

        # Sort show_breakdown by sold descending (for display)
        entry["show_breakdown"].sort(key=lambda x: x.get("tickets_sold", 0), reverse=True)

    # Store verification: merge aliases (we'll keep a list)
    # We can store the mapping we used
    existing_data["verification"]["merged_movies"] = [
        {"canonical": c, "aliases": aliases} for c, aliases in movie_mapping.items()
    ]

    # Update last_updated
    existing_data["last_updated"] = ist_now()
    existing_data["date"] = existing_data.get("date", "")  # already set

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
def main():
    print("Fetching API data...")
    api_data = fetch_api()
    print("API fetch OK.")

    # Extract shows by date
    shows_by_date = extract_shows(api_data)
    if not shows_by_date:
        print("No shows found in API response.")
        return

    # Group all movie titles for fuzzy matching
    all_titles = set()
    for shows in shows_by_date.values():
        for s in shows:
            all_titles.add(s["movie"])
    movie_groups = group_similar_movies(all_titles)
    print(f"Found {len(movie_groups)} movie groups after fuzzy matching.")

    # Process each date
    for date_str, new_shows in shows_by_date.items():
        # Path: data/keralaspecial/YYYY/MM/DD.json
        year, month, day = date_str.split("-")
        file_path = DATA_ROOT / year / month / f"{day}.json"
        existing = load_json(file_path)

        # Ensure date field
        if "date" not in existing:
            existing["date"] = date_str
        elif existing["date"] != date_str:
            existing["date"] = date_str

        # Merge
        merge_date_data(existing, new_shows, movie_groups)

        # Save
        save_json(file_path, existing)
        print(f"Updated {file_path}")

    print("Done.")

if __name__ == "__main__":
    main()
