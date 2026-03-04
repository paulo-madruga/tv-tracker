#!/usr/bin/env python3
"""
Weekly sync script for SIGNAL TV Tracker.
1. Checks TMDB for new seasons on shows in waiting_for_next_season
   - Verifies air date is in the past before moving (avoids announced-but-unaired seasons)
2. Moves newly available shows to available_to_watch_next
3. Generates 3-5 Claude recommendations based on finished_watching ratings
4. Dedups recommendations against series_to_explore
"""

import json
import os
import time
import re
import requests
from datetime import datetime, timezone, date

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

def season_has_aired(tmdb_id, season_number):
    """
    Fetch TMDB season detail and confirm air_date is today or in the past.
    TMDB lists announced but unaired seasons -- must verify the actual air date.
    """
    data = tmdb_get(f"/tv/{tmdb_id}/season/{season_number}")
    time.sleep(0.25)
    if not data:
        return False
    air_date_str = data.get('air_date') or ''
    if not air_date_str:
        print(f"      No air date for S{season_number} -- treating as not yet aired")
        return False
    try:
        aired = datetime.strptime(air_date_str, '%Y-%m-%d').date()
        today = datetime.now(timezone.utc).date()
        print(f"      S{season_number} air date: {air_date_str} | Today: {today}")
        return aired <= today
    except Exception as e:
        print(f"      Could not parse air date '{air_date_str}': {e}")
        return False

def check_season_updates(db):
    waiting = db.get('waiting_for_next_season', [])
    still_waiting = []
    moved_to_available = []

    for show in waiting:
        tmdb_id = show.get('tmdb_id')
        if not tmdb_id:
            print(f"  Skipping {show['title']} -- no TMDB ID")
            still_waiting.append(show)
            continue

        print(f"  Checking: {show['title']} (TMDB {tmdb_id})")
        data = tmdb_get(f"/tv/{tmdb_id}")
        time.sleep(0.25)

        if not data:
            still_waiting.append(show)
            continue

        tmdb_total = data.get('number_of_seasons', show.get('total_seasons', 1))
        tmdb_status = data.get('status', show.get('show_status', 'Continuing'))
        seasons_watched = show.get('seasons_watched', 1)
        next_season = seasons_watched + 1

        show['total_seasons'] = tmdb_total
        show['show_status'] = tmdb_status

        if tmdb_total >= next_season:
            print(f"    -> TMDB shows S{next_season} exists. Verifying air date...")
            if season_has_aired(tmdb_id, next_season):
                print(f"    -> Confirmed aired. Moving to Available Next.")
                moved_to_available.append({
                    'id': show['id'],
                    'title': show['title'],
                    'tmdb_id': tmdb_id,
                    'next_season': next_season,
                    'total_seasons': tmdb_total,
                    'show_status': tmdb_status,
                    'network': show.get('network', ''),
                    'notes': show.get('notes', '')
                })
            else:
                print(f"    -> S{next_season} not yet aired. Keeping in Waiting.")
                still_waiting.append(show)
        else:
            print(f"    -> No new season yet. Watched {seasons_watched}/{tmdb_total}")
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
- Every episode advances main arc -- subplots feed the central narrative
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

def generate_recommendations(db):
    if not ANTHROPIC_KEY:
        print("  SKIP (no Anthropic key)")
        return

    existing_recos = db.get('claude_recommendations', [])
    slots_needed = 5 - len(existing_recos)

    if slots_needed <= 0:
        print("  Already have 5 recommendations — skipping.")
        return

    print(f"  Have {len(existing_recos)} existing recos. Need {slots_needed} more.")

    finished = db.get('finished_watching', [])
    excellent = [s['title'] for s in finished if s.get('rating') == 'Excellent']
    good = [s['title'] for s in finished if s.get('rating') == 'Good']
    abandoned = [s['title'] for s in finished if s.get('rating') == 'Abandoned Halfway']

    # Build full exclusion set — everything already tracked
    all_titles = set()
    for list_name in ['watching_now', 'available_to_watch_next', 'waiting_for_next_season',
                      'series_to_explore', 'claude_recommendations', 'finished_watching',
                      'dismissed_recommendations']:
        for show in db.get(list_name, []):
            all_titles.add(show.get('title', '').lower())

    # Build dismissed context grouped by reason
    dismissed = db.get('dismissed_recommendations', [])
    by_reason = {}
    for s in dismissed:
        r = s.get('dismiss_reason', 'other')
        by_reason.setdefault(r, []).append(s['title'])

    dismissed_lines = []
    reason_instructions = {
        'not_interested': 'Previously recommended but REJECTED — adjust taste model away from these shows and similar ones',
        'no_service':     'Dismissed — unavailable on user subscriptions (Apple TV+, HBO/Max, Netflix, Prime Video, Disney+)',
        'wrong_genre':    'Dismissed — wrong genre or tone — use to refine taste model away from similar shows',
        'too_long':       'Dismissed — too many seasons — prefer shows under 5 seasons where possible',
        'seen_it':        'Already seen — do not recommend',
        'other':          'Dismissed for other reasons — do not re-recommend',
    }
    for reason, titles in by_reason.items():
        instruction = reason_instructions.get(reason, 'Dismissed — do not re-recommend')
        dismissed_lines.append(f"{instruction}: {', '.join(titles)}")

    dismissed_context = ('\n' + '\n'.join(dismissed_lines)) if dismissed_lines else ''

    prompt = (
        TASTE_PROFILE +
        "\n\nCURRENT DATA:"
        "\nExcellent: " + (', '.join(excellent) or 'none') +
        "\nGood: " + (', '.join(good) or 'none') +
        "\nAbandoned: " + (', '.join(abandoned) or 'none') +
        dismissed_context +
        "\nAlready tracked (DO NOT recommend any of these): " + (', '.join(sorted(all_titles)) or 'none') +
        "\n\nGenerate exactly " + str(slots_needed) + " TV series recommendation(s) that are NOT any of the above shows."
        "\nFor each, provide:"
        "\n- title (exact official title)"
        "\n- tmdb_id (numeric TMDB ID -- be accurate)"
        "\n- total_seasons (integer, current count)"
        "\n- show_status (\"Ended\" or \"Continuing\")"
        "\n- network (streaming service, e.g. \"Apple TV+\", \"HBO\", \"Netflix\", \"Prime Video\", \"Disney+\", \"Hulu\")"
        "\n- reason (2 sentences max: why it fits Paulo's taste profile specifically)"
        "\n\nReturn ONLY valid JSON array, no markdown, no explanation:"
        "\n["
        "\n  {"
        "\n    \"title\": \"...\","
        "\n    \"tmdb_id\": 12345,"
        "\n    \"total_seasons\": 3,"
        "\n    \"show_status\": \"Ended\","
        "\n    \"network\": \"Netflix\","
        "\n    \"reason\": \"...\""
        "\n  }"
        "\n]"
    )

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
        raw = re.sub(r'^```json\s*', '', raw)
        raw = re.sub(r'```$', '', raw)
        recommendations = json.loads(raw)
    except Exception as e:
        print(f"  Claude error: {e}")
        return

    seen = set(all_titles)
    new_recos = []

    for r in recommendations:
        title = r.get('title', '').strip()
        if not title or title.lower() in seen:
            print(f"  Dedup skip: {title}")
            continue
        show_id = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
        new_recos.append({
            'id': show_id,
            'title': title,
            'tmdb_id': r.get('tmdb_id'),
            'total_seasons': r.get('total_seasons'),
            'show_status': r.get('show_status', 'Ended'),
            'network': r.get('network', ''),
            'reason': r.get('reason', '')
        })
        seen.add(title.lower())
        if len(new_recos) >= slots_needed:
            break

    # Append new recos to existing ones (preserve undismissed)
    db['claude_recommendations'] = existing_recos + new_recos
    print(f"  Added {len(new_recos)} new recommendations (total: {len(db['claude_recommendations'])})")
    for r in new_recos:
        print(f"    . {r['title']} ({r.get('network','')}): {r['reason'][:80]}...")

# ── MAIN ───────────────────────────────────────────────────────────────────

def main():
    print(f"=== SIGNAL Weekly Sync ({datetime.now().isoformat()}) ===")
    db = load_db()

    print("\n[1] Checking season updates for Waiting shows...")
    moved = check_season_updates(db)
    print(f"  -> {moved} shows moved to Available Next")

    print("\n[2] Generating Claude recommendations...")
    generate_recommendations(db)

    save_db(db)
    print("\n=== Done ===")

if __name__ == '__main__':
    main()
