"""Merge backfilled review pages into the main dataset.

Combines the per-page files produced by backfill.py (data/backfill/page*.json)
into one file (data/backfill/all.json), upserts the entries into the SQLite
database, and re-exports data/letterboxd.json. Backfilled entries have no
pub_date, so they sort by watched_date and end up after all feed entries.

Once merged, entries live in SQLite permanently: the daily sync.py run keeps
them in letterboxd.json alongside fresh feed data.

Usage: uv run merge.py
"""

import json
import logging
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backfill import USER, backfill_dir
from sync import Base, Entry, db_path, export_json, upsert

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__file__)


def combine_pages() -> list[dict[str, object]]:
    """Combine per-page files into one deduplicated, newest-first list."""
    by_guid: dict[str, dict[str, object]] = {}
    for path in sorted(backfill_dir.glob("page*.json")):
        for entry in json.loads(path.read_text(encoding="utf-8"))["entries"]:
            guid = str(entry["guid"])
            if guid in by_guid and by_guid[guid] != entry:
                raise ValueError(f"{path.name}: conflicting duplicate for {guid}")
            by_guid[guid] = entry
    viewing_id = lambda e: int(str(e["guid"]).rsplit("-", 1)[1])  # noqa: E731
    return sorted(by_guid.values(), key=viewing_id, reverse=True)


def main() -> int:
    """Combine page files, upsert into SQLite, re-export letterboxd.json."""
    entries = combine_pages()
    if not entries:
        logger.error("No backfill entries found under %s", backfill_dir)
        return 1
    all_path = backfill_dir / "all.json"
    all_path.write_text(
        json.dumps({"user": USER, "entries": entries}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    logger.info("Combined %d entries -> %s", len(entries), all_path)

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with Session(engine) as session:
        changed = sum(
            upsert(
                session,
                Entry,
                {
                    k: v
                    for k, v in e.items()
                    if k not in ("first_seen_at", "updated_at")
                },
                now,
            )
            for e in entries
        )
        session.commit()
        export_json(session, USER)
        total = len(session.scalars(select(Entry)).all())

    logger.info("Done: %d entries total (%d new/updated)", total, changed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
