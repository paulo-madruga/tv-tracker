# SIGNAL // TV Tracker

Personal TV series tracker. State-machine-based show management with GitHub as the data layer.

## Architecture

- **Frontend**: Single-file PWA (installable, offline-capable)
- **Data**: `shows.json` in this repo — read/written via GitHub API directly from the app
- **Automation**: GitHub Action runs every Monday, checks TMDB for new seasons and generates Claude recommendations

## Setup

### 1. Fork or create this repo

Keep it private if you want your watch history private.

### 2. Add repository secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
|--------|-------|
| `TMDB_TOKEN` | TMDB Read Access Token (free at themoviedb.org → Settings → API) |
| `ANTHROPIC_API_KEY` | Your Anthropic API key (sk-ant-...) |

### 3. Enable GitHub Pages

Go to **Settings → Pages** → Source: `Deploy from a branch` → Branch: `main` → Folder: `/` (root).

Your PWA will be live at `https://YOUR_USERNAME.github.io/REPO_NAME/`

### 4. Create a Personal Access Token (PAT)

Go to **GitHub Settings → Developer settings → Personal access tokens → Fine-grained tokens**.

Permissions needed:
- **Contents**: Read and write (to update shows.json)

Copy the token — you'll paste it into the app's Config screen.

### 5. Configure the app

Open the PWA → click **CONFIG** → fill in:
- **Owner / Repo**: `your-username/your-repo`
- **JSON File Path**: `shows.json`
- **PAT**: the token from step 4
- **TMDB API Key**: your TMDB Read Access Token
- **Anthropic Key**: not used client-side, only needed in GitHub secrets

## Show State Machine

```
Series to Explore ──────────────────► Watching Now (S1)
                                              │
Claude Recommendations ─────────────►        │
                                    ┌────────┴──────────┐
                                    ▼                   ▼
                          More seasons available?   Final season + show ended?
                                    │                   │
                                    ▼                   ▼
                        Available to Watch Next    Finished Watching
                             (next season)    (Excellent/Good/Not My Favorites)
                                    │
                                    ▼
                             Watching Now
                                    │
                            Current with latest,
                            show still in production
                                    │
                                    ▼
                        Waiting for Next Season
                                    │
                        [GitHub Action detects new season]
                                    │
                                    ▼
                        Available to Watch Next
```

**Abandoned**: can happen from Watching Now at any point → Finished Watching (Abandoned Halfway). No revive.

## Weekly Automation

The GitHub Action runs every Monday at 9am UTC and does two things:

1. **Season check**: For every show in "Waiting for Next Season", queries TMDB. If a new season is available, moves the show to "Available to Watch Next".

2. **Recommendations**: Sends your Finished Watching ratings to Claude, which returns 3-5 new show recommendations tailored to your taste profile. Deduped against Series to Explore.

You can also trigger it manually via **Actions → Weekly TV Tracker Sync → Run workflow**.

## Data Schema

`shows.json` is the single source of truth. Key fields per show:

```json
{
  "id": "show-slug",
  "title": "Show Title",
  "tmdb_id": 12345,
  "current_season": 2,        // watching_now
  "next_season": 3,           // available_to_watch_next
  "seasons_watched": 2,       // waiting / finished
  "total_seasons": 4,
  "show_status": "Continuing", // or "Ended"
  "rating": "Excellent",       // finished_watching only
  "reason": "Why Claude recommended it", // recommendations only
  "blurb": "Show description",  // series_to_explore only
  "notes": "Your notes",
  "date_finished": "2026-02-24"
}
```
