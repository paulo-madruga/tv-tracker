#!/usr/bin/env python3
"""
Weekly sync script for SIGNAL TV Tracker.
1. Checks TMDB for new seasons on shows in waiting_for_next_season
2. Moves newly available shows to available_to_watch_next
3. Generates 3-5 Claude recommendations based on finished_watching ratings
4. Dedups recommendations against series_to_explore
"""

import json
import os
import time
import requests
import re
from datetime import datetime, timezone

SHOWS_FILE = os.environ.get('SHOWS_FILE', 'shows.json')
TMDB_TOKEN = os.environ.get('TMDB_TOKEN', '')
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# ── LOAD / SAVE ────────────────────────────────────────────────────────────

def load_db():
    with open(SHOWS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_db(db):
    db['last_updated'] = datetime.now(timezone.utc).isoformat()
    with open(SHOWS_FILE, 'w', encoding='utf-8') as f:
        json.dump(db, f, indent=2, ensure_ascii=False)
    print(f"Saved {SHOWS_FILE}")

# ── TMDB ───────────────────────────────────────────────────────────────────

def tmdb_get(path):
    if not TMDB_TOKEN:
        print(f"  SKIP (no TMDB token): {path}")
        return None
    url = f"https://api.themoviedb.org/3{path}"
    res = requests.get(url, headers={
        'Authorization': f'Bearer {TMDB_TOKEN}',
        'Accept': 'application/json'
    }, timeout=10)
    if res.status_code == 200:
        return res.json()
    print(f"  TMDB error {res.status_code} for {path}")
    return None

def check_season_updates(db):
    """
    For each show in waiting_for_next_season:
    - Fetch TMDB details
    - If total_seasons > seasons_watched, move to available_to_watch_next
    - Update total_seasons and show_status regardless
    """
    waiting = db.get('waiting_for_next_season', [])
    still_waiting = []
    moved_to_available = []

    for show in waiting:
        tmdb_id = show.get('tmdb_id')
        if not tmdb_id:
            still_waiting.append(show)
            continue

        print(f"  Checking: {show['title']} (TMDB {tmdb_id})")
        data = tmdb_get(f"/tv/{tmdb_id}")
        time.sleep(0.25)  # rate limit courtesy

        if not data:
            still_waiting.append(show)
            continue

        tmdb_total = data.get('number_of_seasons', show.get('total_seasons', 1))
        tmdb_status = data.get('status', show.get('show_status', 'Continuing'))
        seasons_watched = show.get('seasons_watched', 1)

        # Update metadata
        show['total_seasons'] = tmdb_total
        show['show_status'] = tmdb_status

        if tmdb_total > seasons_watched:
            print(f"    → New season available! S{seasons_watched + 1} of {tmdb_total}")
            moved_to_available.append({
                'id': show['id'],
                'title': show['title'],
                'tmdb_id': tmdb_id,
                'next_season': seasons_watched + 1,
                'total_seasons': tmdb_total,
                'show_status': tmdb_status,
                'notes': show.get('notes', '')
            })
        else:
            print(f"    → Still waiting. {seasons_watched}/{tmdb_total} seasons")
            still_waiting.append(show)

    db['waiting_for_next_season'] = still_waiting

    existing_ids = {s['id'] for s in db.get('available_to_watch_next', [])}
    for show in moved_to_available:
        if show['id'] not in existing_ids:
            db['available_to_watch_next'].append(show)
            print(f"  Moved to Available Next: {show['title']}")

    return len(moved_to_available)

# ── CLAUDE RECOMMENDATIONS ─────────────────────────────────────────────────

TASTE_PROFILE = """
Paulo's TV Taste Profile (use this to generate recommendations):

STRONGLY PREFERS:
- Tight narrative arcs, season-long momentum, minimal filler
- Controlled cast size, thematic coherence
- Dark tone, morally complex characters, serious intelligent writing
- Every episode advances main arc — subplots feed the central narrative
- Genres: sci-fi dystopia, tech paranoia, espionage, political power struggles, survival thrillers

RATED EXCELLENT (ideal benchmark):
Dark, Mr. Robot, Midnight Mass, Breaking Bad, Better Call Saul, Succession, Shogun,
Band of Brothers, Chernobyl, The Man in the High Castle, Battlestar Galactica, The Queen's Gambit

RATED GOOD (liked but not top tier):
Westworld (S1-2), Altered Carbon, Lovecraft Country, Dracula, Barbarians

ABANDONED (do NOT recommend similar shows):
Stranger Things (cast bloat, sprawl), True Detective (too atmospheric, not arc-driven),
The Expanse (slow, sprawling), The Witcher (quality drift, worldbuilding bloat)

HARD AVOIDS:
- Anthology format
- Slow-burn without clear payoff
- Expanding ensemble cast every season
- Mystery-box without resolution
- Prestige vibes over narrative momentum
- Shows that peaked early and declined
- Lore-heavy franchises / expanding universes
"""

def build_exclusion_ids(db):
    """All show IDs already in the tracker — exclude from recommendations."""
    ids = set()
    for list_name in ['watching_now', 'available_to_watch_next', 'waiting_for_next_season',
                      'series_to_explore', 'claude_recommendations', 'finished_watching']:
        for show in db.get(list_name, []):
            ids.add(show.get('id', ''))
            ids.add(show.get('title', '').lower())
    return ids

def generate_recommendations(db):
    if not ANTHROPIC_KEY:
        print("  SKIP (no Anthropic key)")
        return

    exclusions = build_exclusion_ids(db)
    series_to_explore_titles = [s['title'] for s in db.get('series_to_explore', [])]

    finished = db.get('finished_watching', [])
    excellent = [s['title'] for s in finished if s.get('rating') == 'Excellent']
    good = [s['title'] for s in finished if s.get('rating') == 'Good']
    abandoned = [s['title'] for s in finished if s.get('rating') == 'Abandoned Halfway']

    prompt = f"""{TASTE_PROFILE}

CURRENT DATA:
Excellent: {', '.join(excellent) or 'none'}
Good: {', '.join(good) or 'none'}
Abandoned: {', '.join(abandoned) or 'none'}
Already in Series to Explore (DO NOT recommend these): {', '.join(series_to_explore_titles) or 'none'}

Generate exactly 3-5 TV series recommendations that are NOT any of the above shows.
For each, provide:
- title (exact official title)
- tmdb_id (numeric TMDB ID — be accurate)
- total_seasons (integer, current count)
- show_status ("Ended" or "Continuing")
- reason (2 sentences max: why it fits Paulo's taste profile specifically)

Return ONLY valid JSON array, no markdown, no explanation:
[
  {{
    "title": "...",
    "tmdb_id": 12345,
    "total_seasons": 3,
    "show_status": "Ended",
    "reason": "..."
  }}
]"""

    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    print("  Calling Claude for recommendations...")
    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = message.content[0].text.strip()
        # Strip any accidental markdown fences
        raw = re.sub(r'^```json\s*', '', raw)
        raw = re.sub(r'```$', '', raw)
        recommendations = json.loads(raw)
    except Exception as e:
        print(f"  Claude error: {e}")
        return

    # Validate, dedup, and assign IDs
    clean = []
    seen_titles = set(t.lower() for t in series_to_explore_titles)
    seen_titles.update(s.get('title', '').lower() for list_name in
                       ['watching_now','available_to_watch_next','waiting_for_next_season','finished_watching']
                       for s in db.get(list_name, []))

    for r in recommendations:
        title = r.get('title', '').strip()
        if not title:
            continue
        if title.lower() in seen_titles:
            print(f"  Dedup skip: {title}")
            continue
        show_id = title.lower().replace(r"[^a-z0-9]+", '-').strip('-')
        # Simple slug
        show_id = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
        clean.append({
            'id': show_id,
            'title': title,
            'tmdb_id': r.get('tmdb_id'),
            'total_seasons': r.get('total_seasons'),
            'show_status': r.get('show_status', 'Ended'),
            'reason': r.get('reason', '')
        })
        seen_titles.add(title.lower())

    db['claude_recommendations'] = clean[:5]
    print(f"  Generated {len(clean)} recommendations")
    for r in clean:
        print(f"    · {r['title']}: {r['reason'][:80]}...")

# ── MAIN ───────────────────────────────────────────────────────────────────

def main():
    print(f"=== SIGNAL Weekly Sync ({datetime.now().isoformat()}) ===")

    db = load_db()

    print("\n[1] Checking season updates for Waiting shows...")
    moved = check_season_updates(db)
    print(f"  → {moved} shows moved to Available Next")

    print("\n[2] Generating Claude recommendations...")
    generate_recommendations(db)

    save_db(db)
    print("\n=== Done ===")

if __name__ == '__main__':
    main()
