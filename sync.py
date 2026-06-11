"""Sync a Letterboxd RSS feed into SQLite and a JSON snapshot.

The feed only exposes the ~50 most recent diary entries and recent lists,
so this script accumulates data over time: new items are inserted, edited
items (reviews, lists) are updated in place, nothing is ever deleted.
Output files only change when the data changes, keeping git diffs clean.
"""

import html
import json
import logging
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TypeVar

import requests
from sqlalchemy import ForeignKey, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

FEED_URL = os.environ.get(
    "LETTERBOXD_RSS_URL", "https://letterboxd.com/bugfreedisco/rss/"
)
NS = {
    "letterboxd": "https://letterboxd.com",
    "tmdb": "https://themoviedb.org",
    "dc": "http://purl.org/dc/elements/1.1/",
}

root = Path(__file__).resolve().parent
data_dir = root / "data"
json_path = data_dir / "letterboxd.json"
db_path = data_dir / "letterboxd.sqlite"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__file__)


class Base(DeclarativeBase):
    pass


class Entry(Base):
    """A diary entry: a logged watch, with or without a review."""

    __tablename__ = "entries"

    guid: Mapped[str] = mapped_column(primary_key=True)
    type: Mapped[str]  # "review" | "watch"
    title: Mapped[str]
    film_title: Mapped[str]
    film_year: Mapped[int | None]
    tmdb_movie_id: Mapped[int | None]
    member_rating: Mapped[float | None]
    member_like: Mapped[bool]
    rewatch: Mapped[bool]
    watched_date: Mapped[str | None]
    pub_date: Mapped[str]
    link: Mapped[str]
    poster_url: Mapped[str | None]
    review_html: Mapped[str | None]
    review_text: Mapped[str | None]
    first_seen_at: Mapped[str]
    updated_at: Mapped[str]


class FilmList(Base):
    """A user-curated list."""

    __tablename__ = "lists"

    guid: Mapped[str] = mapped_column(primary_key=True)
    title: Mapped[str]
    link: Mapped[str]
    description_html: Mapped[str | None]
    pub_date: Mapped[str]
    first_seen_at: Mapped[str]
    updated_at: Mapped[str]


class ListFilm(Base):
    """A film inside a list, in list order."""

    __tablename__ = "list_films"

    list_guid: Mapped[str] = mapped_column(ForeignKey("lists.guid"), primary_key=True)
    position: Mapped[int] = mapped_column(primary_key=True)
    film_title: Mapped[str]
    film_url: Mapped[str]


TRow = TypeVar("TRow", Entry, FilmList)


def fetch_feed(url: str) -> str:
    """Download the RSS feed."""
    resp = requests.get(
        url, headers={"User-Agent": "letterboxd-rss-archiver/1.0"}, timeout=30
    )
    resp.raise_for_status()
    return resp.text


def text_of(item: ET.Element, tag: str) -> str | None:
    """Return stripped text of a child tag, or None if absent."""
    el = item.find(tag, NS)
    return el.text.strip() if el is not None and el.text else None


def html_to_text(fragment: str) -> str:
    """Convert an HTML fragment to readable plain text."""
    s = re.sub(r"<br\s*/?>", "\n", fragment)
    s = re.sub(r"</p>\s*<p>", "\n\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def split_description(description: str | None) -> tuple[str | None, str | None]:
    """Split an entry description into (poster_url, review_html)."""
    if not description:
        return None, None
    poster = None
    m = re.search(r'<p><img src="([^"]+)"\s*/?\s*></p>', description)
    if m:
        poster = m.group(1)
        description = description.replace(m.group(0), "", 1)
    return poster, description.strip() or None


def parse_entry(item: ET.Element, guid: str) -> dict[str, object]:
    """Parse a diary item (review or watch) into a column dict."""
    entry_type = "review" if guid.startswith("letterboxd-review-") else "watch"
    rating = text_of(item, "letterboxd:memberRating")
    year = text_of(item, "letterboxd:filmYear")
    tmdb_id = text_of(item, "tmdb:movieId")
    poster_url, review_html = split_description(text_of(item, "description"))
    if entry_type == "watch":  # auto-generated "Watched on ..." text, not a review
        review_html = None
    return {
        "guid": guid,
        "type": entry_type,
        "title": text_of(item, "title") or "",
        "film_title": text_of(item, "letterboxd:filmTitle") or "",
        "film_year": int(year) if year else None,
        "tmdb_movie_id": int(tmdb_id) if tmdb_id else None,
        "member_rating": float(rating) if rating else None,
        "member_like": text_of(item, "letterboxd:memberLike") == "Yes",
        "rewatch": text_of(item, "letterboxd:rewatch") == "Yes",
        "watched_date": text_of(item, "letterboxd:watchedDate"),
        "pub_date": parsedate_to_datetime(text_of(item, "pubDate") or "").isoformat(),
        "link": text_of(item, "link") or "",
        "poster_url": poster_url,
        "review_html": review_html,
        "review_text": html_to_text(review_html) if review_html else None,
    }


def parse_list(item: ET.Element) -> tuple[dict[str, object], list[tuple[str, str]]]:
    """Parse a list item into (column dict, [(film_title, film_url), ...])."""
    description = text_of(item, "description") or ""
    films = [
        (html.unescape(m.group(2)).strip(), m.group(1))
        for m in re.finditer(r'<a href="([^"]+)">\s*([^<]+?)\s*</a>', description)
    ]
    columns: dict[str, object] = {
        "guid": text_of(item, "guid") or "",
        "title": text_of(item, "title") or "",
        "link": text_of(item, "link") or "",
        "description_html": description.strip() or None,
        "pub_date": parsedate_to_datetime(text_of(item, "pubDate") or "").isoformat(),
    }
    return columns, films


def upsert(
    session: Session,
    model: type[TRow],
    columns: dict[str, object],
    now: str,
) -> bool:
    """Insert or update a row; return True if anything changed."""
    row = session.get(model, columns["guid"])
    if row is None:
        session.add(model(**columns, first_seen_at=now, updated_at=now))
        return True
    changed = False
    for key, value in columns.items():
        if getattr(row, key) != value:
            setattr(row, key, value)
            changed = True
    if changed:
        row.updated_at = now
    return changed


def sync_list_films(session: Session, guid: str, films: list[tuple[str, str]]) -> bool:
    """Replace a list's films if they differ; return True if changed."""
    existing = session.scalars(
        select(ListFilm).where(ListFilm.list_guid == guid).order_by(ListFilm.position)
    ).all()
    if [(f.film_title, f.film_url) for f in existing] == films:
        return False
    for f in existing:
        session.delete(f)
    for position, (film_title, film_url) in enumerate(films, start=1):
        session.add(
            ListFilm(
                list_guid=guid,
                position=position,
                film_title=film_title,
                film_url=film_url,
            )
        )
    return True


def sync_feed(session: Session, xml_text: str) -> tuple[int, int]:
    """Upsert all feed items; return (changed_entries, changed_lists)."""
    channel = ET.fromstring(xml_text).find("channel")
    assert channel is not None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changed_entries = changed_lists = 0
    for item in channel.iter("item"):
        guid = text_of(item, "guid") or ""
        if guid.startswith("letterboxd-list-"):
            columns, films = parse_list(item)
            list_changed = upsert(session, FilmList, columns, now)
            films_changed = sync_list_films(session, guid, films)
            changed_lists += int(list_changed or films_changed)
        elif guid.startswith(("letterboxd-review-", "letterboxd-watch-")):
            changed_entries += int(upsert(session, Entry, parse_entry(item, guid), now))
        else:
            logger.warning("Skipping item with unknown guid: %s", guid)
    return changed_entries, changed_lists


def row_to_dict(row: Base, exclude: tuple[str, ...] = ()) -> dict[str, object]:
    """Serialize an ORM row to a plain dict in column order."""
    return {
        c.key: getattr(row, c.key)
        for c in row.__table__.columns
        if c.key not in exclude
    }


def export_json(session: Session, username: str) -> None:
    """Write the full accumulated dataset to a single JSON file."""
    sort_key = lambda row: (
        datetime.fromisoformat(row.pub_date),
        row.guid,
    )  # noqa: E731
    entries = sorted(session.scalars(select(Entry)), key=sort_key, reverse=True)
    lists = sorted(session.scalars(select(FilmList)), key=sort_key, reverse=True)
    payload = {
        "user": username,
        "feed_url": FEED_URL,
        "entries": [row_to_dict(e) for e in entries],
        "lists": [
            row_to_dict(lst)
            | {
                "films": [
                    {"position": f.position, "title": f.film_title, "url": f.film_url}
                    for f in session.scalars(
                        select(ListFilm)
                        .where(ListFilm.list_guid == lst.guid)
                        .order_by(ListFilm.position)
                    )
                ]
            }
            for lst in lists
        ],
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    """Fetch the feed, sync the database, export JSON."""
    data_dir.mkdir(exist_ok=True)
    logger.info("Fetching %s", FEED_URL)
    xml_text = fetch_feed(FEED_URL)
    username = FEED_URL.rstrip("/").split("/")[-2]

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        changed_entries, changed_lists = sync_feed(session, xml_text)
        session.commit()
        export_json(session, username)
        total_entries = len(session.scalars(select(Entry)).all())
        total_lists = len(session.scalars(select(FilmList)).all())

    logger.info(
        "Done: %d entries (%d new/updated), %d lists (%d new/updated)",
        total_entries,
        changed_entries,
        total_lists,
        changed_lists,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
