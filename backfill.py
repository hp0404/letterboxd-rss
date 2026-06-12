"""One-off backfill of historical reviews from saved Letterboxd HTML pages.

The RSS feed only exposes the ~50 most recent entries, so older reviews were
saved manually from the browser (letterboxd.com/<user>/reviews/page/N/) into
exports/pageN.html. This script parses those pages into per-page JSON files
under data/backfill/, using the same entry shape as data/letterboxd.json plus
two extra fields: like_count and comment_count.

Entries whose guid already exists in data/letterboxd.json are skipped, so the
per-page files contain only new (historical) data. Merging the per-page files
into the main dataset is a separate, later step.

Usage: uv run backfill.py exports/page5.html [exports/page6.html ...]
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from sync import html_to_text

USER = "bugfreedisco"
SPOILER_PATTERN = "I can handle the truth"

root = Path(__file__).resolve().parent
json_path = root / "data" / "letterboxd.json"
backfill_dir = root / "data" / "backfill"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__file__)


def stars_to_rating(stars: str) -> float:
    """Convert a star string like '★★★½' to a numeric rating."""
    return stars.count("★") + (0.5 if "½" in stars else 0.0)


def check_review_body(article: Tag, guid: str) -> None:
    """Fail if a review body is missing or empty.

    An unrevealed spoiler ('I can handle the truth' link still active) is
    harmless: the full text is in the hidden js-review-body either way, so
    it only warrants a warning.
    """
    spoiler = article.select_one("p.js-spoiler-container a[data-js-trigger]")
    if spoiler is not None and not spoiler.has_attr("hidden"):
        logger.warning(
            "%s: spoiler not revealed ('%s'), using hidden body text",
            guid,
            SPOILER_PATTERN,
        )
    body = article.select_one("div.js-review-body")
    if body is None or not body.get_text(strip=True):
        raise ValueError(f"{guid}: review body is missing or empty")


def review_html_of(article: Tag, guid: str) -> str:
    """Return a review's full body HTML.

    A div.collapsed-text wrapper means the page was saved with the review
    still folded behind the '…more' link, so the export only contains a
    truncated text (the full-text endpoint is behind Cloudflare and cannot
    be fetched by a script). The page must be re-saved with the review
    expanded.
    """
    body = article.select_one("div.js-review-body")
    assert body is not None  # guaranteed by check_review_body
    if body.select_one("div.collapsed-text") is not None:
        raise ValueError(
            f"{guid}: body truncated in export — expand '…more' and re-save the page"
        )
    return body.decode_contents().strip()


def guid_of(article: Tag) -> str:
    """Build the RSS-style guid, e.g. 'letterboxd-review-1281708855'."""
    object_id = str(article["data-object-id"])  # e.g. "viewing:1281708855"
    entry_type = str(article["data-object-name"])  # "review"
    return f"letterboxd-{entry_type}-{object_id.removeprefix('viewing:')}"


def parse_article(article: Tag, guid: str) -> dict[str, object]:
    """Parse one review article into the letterboxd.json entry shape."""
    entry_type = str(article["data-object-name"])  # "review"
    check_review_body(article, guid)

    name_link = article.select_one("h2.primaryname a")
    assert name_link is not None, f"{guid}: film title not found"
    film_title = name_link.get_text(strip=True)
    link = str(name_link["href"])
    year_el = article.select_one("span.releasedate a")
    film_year = int(year_el.get_text(strip=True)) if year_el else None

    rating_el = article.select_one("svg.glyph.-rating")
    stars = str(rating_el["aria-label"]) if rating_el else None
    title = f"{film_title}, {film_year}" + (f" - {stars}" if stars else "")

    context_el = article.select_one("a.context")
    assert context_el is not None, f"{guid}: watched/rewatched context not found"
    date_el = article.select_one("time.timestamp")
    raw_date = str(date_el["datetime"]) if date_el else None
    # Reviews without a diary date (context "Added") carry a full publication
    # timestamp instead of a watched date, e.g. "2024-05-03T12:39:08.072Z".
    watched_date = pub_date = None
    if raw_date and "T" in raw_date:
        pub_date = raw_date.replace("Z", "+00:00")
    else:
        watched_date = raw_date

    poster_img = article.select_one("div.film-poster img")
    poster_url = None
    if poster_img is not None and poster_img.has_attr("srcset"):
        poster_url = str(poster_img["srcset"]).split()[0]

    review_html = review_html_of(article, guid)

    like_el = article.select_one("p.like-link-target[data-count]")
    comment_el = article.select_one('a.metadata[href*="#comments"] span.label')

    return {
        "guid": guid,
        "type": entry_type,
        "title": title,
        "film_title": film_title,
        "film_year": film_year,
        "tmdb_movie_id": None,  # not present in the HTML exports
        "member_rating": stars_to_rating(stars) if stars else None,
        "member_like": article.select_one("svg.inline-liked") is not None,
        "rewatch": context_el.get_text(strip=True) == "Rewatched",
        "watched_date": watched_date,
        "pub_date": pub_date,  # only present for entries without a diary date
        "link": link,
        "poster_url": poster_url,
        "review_html": review_html,
        "review_text": html_to_text(review_html),
        "like_count": int(str(like_el["data-count"])) if like_el else 0,
        "comment_count": int(comment_el.get_text(strip=True)) if comment_el else 0,
    }


def parse_page(
    path: Path, known_guids: set[str]
) -> tuple[list[dict[str, object]], int]:
    """Parse a page's review articles, skipping known guids; collect errors."""
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    entries = []
    errors = 0
    for article in soup.select("article.production-viewing[data-object-id]"):
        guid = guid_of(article)
        if guid in known_guids:
            logger.info("Skipping %s (already in %s)", guid, json_path.name)
            continue
        try:
            entries.append(parse_article(article, guid))
        except ValueError as exc:
            logger.error("%s: %s", path.name, exc)
            errors += 1
    return entries, errors


def main() -> int:
    """Parse the given export pages into per-page JSON files."""
    paths = [Path(arg) for arg in sys.argv[1:]]
    if not paths:
        logger.error("Usage: uv run backfill.py exports/page5.html [...]")
        return 1

    known_guids = {
        e["guid"] for e in json.loads(json_path.read_text(encoding="utf-8"))["entries"]
    }
    backfill_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    total_errors = 0
    for path in paths:
        entries, errors = parse_page(path, known_guids)
        total_errors += errors
        for entry in entries:
            entry["first_seen_at"] = now
            entry["updated_at"] = now
        out_path = backfill_dir / f"{path.stem}.json"
        out_path.write_text(
            json.dumps(
                {"user": USER, "source": path.name, "entries": entries},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        logger.info("%s: %d entries -> %s", path.name, len(entries), out_path)
    if total_errors:
        logger.error("%d entries could not be parsed, see errors above", total_errors)
    return 1 if total_errors else 0


if __name__ == "__main__":
    sys.exit(main())
