# letterboxd-rss

A GitHub-based scraping project that archives a Letterboxd user's public RSS feed.
It runs automatically as a **GitHub Actions** workflow once a day, parses the feed,
and commits the data back to this repository in two formats: a human-diffable JSON
file and a SQLite database. The feed URL is configured in `sync.py` (or via the
`LETTERBOXD_RSS_URL` environment variable).

## Why accumulate?

The Letterboxd RSS feed only exposes the **~50 most recent diary entries** plus
recently updated lists — there is no pagination. So each run merges the current
feed into the existing dataset: new items are inserted, edited items (reviews,
lists) are updated in place, and nothing is ever deleted. Over time the repo
becomes a complete archive, and git history shows exactly what changed and when.

Output files are only rewritten when the data actually changes, so a run with no
new activity produces no commit.

## Files

| Path | Purpose |
|---|---|
| `sync.py` | The whole pipeline: fetch RSS → parse → upsert SQLite → export JSON |
| `backfill.py` | One-off importer: parses saved review pages from `exports/` into `data/backfill/page*.json` |
| `merge.py` | One-off merge: combines `data/backfill/page*.json` into SQLite + `data/letterboxd.json` |
| `data/letterboxd.json` | Full accumulated dataset as one pretty-printed JSON file (for version control / easy consumption) |
| `data/letterboxd.sqlite` | The same data as a relational SQLite database |
| `data/backfill/` | Parsed per-page backfill data (`page*.json`) and the combined `all.json` |
| `.github/workflows/daily-sync.yml` | Daily GitHub Actions workflow (04:17 UTC + manual trigger) |

## Data model

The feed contains two kinds of items, stored in three tables (mirrored in JSON):

- **`entries`** — diary entries, one row per logged watch. `type` is `review`
  (has review text) or `watch` (logged without a review). Columns: `guid` (PK),
  `title`, `film_title`, `film_year`, `tmdb_movie_id`, `member_rating` (0.5–5.0
  or null), `member_like`, `rewatch`, `watched_date`, `pub_date`, `link`,
  `poster_url`, `review_html`, `review_text`, plus `first_seen_at` /
  `updated_at` bookkeeping timestamps.
- **`lists`** — user-curated lists: `guid` (PK), `title`, `link`,
  `description_html`, `pub_date`, `first_seen_at`, `updated_at`.
- **`list_films`** — films inside each list, in list order:
  `(list_guid, position)` (PK), `film_title`, `film_url`.

In `data/letterboxd.json` the same data appears as `entries` (sorted newest
first) and `lists` (each with an inline `films` array).

## Running locally

Requires [uv](https://docs.astral.sh/uv/):

```bash
uv run sync.py
```

To archive a different user, override the feed URL:

```bash
LETTERBOXD_RSS_URL="https://letterboxd.com/<username>/rss/" uv run sync.py
```

## Automation

`.github/workflows/daily-sync.yml` runs daily and can also be triggered manually from
the Actions tab (`workflow_dispatch`). If the sync produced changes under
`data/`, the workflow commits and pushes them as `github-actions[bot]`.

Note: GitHub disables cron schedules in repos with no activity for 60 days;
the workflow's own data commits count as activity, so an active Letterboxd
account keeps it alive.

## Backfilling older history

Since the RSS feed cannot return entries older than the last ~50, older
reviews are imported from review pages saved manually in the browser
(`letterboxd.com/<user>/reviews/page/N/` → save as `exports/pageN.html`,
with every spoiler and every "…more" link expanded first). Then:

```bash
uv run backfill.py exports/page*.html   # parse pages -> data/backfill/page*.json
uv run merge.py                         # combine + upsert into SQLite + re-export JSON
```

`backfill.py` skips entries already present in `data/letterboxd.json` and
fails loudly on truncated or empty review bodies. Backfilled entries carry
two extra fields the feed does not provide (`like_count`, `comment_count`)
and lack two the page does not show (`pub_date`, `tmdb_movie_id`). Once
merged into SQLite they are preserved by the daily sync like any other entry.
