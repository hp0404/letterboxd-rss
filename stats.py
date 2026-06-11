"""Print summary statistics for the archived Letterboxd data in SQLite."""

import sys
from collections import Counter

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from sync import Entry, FilmList, ListFilm, db_path


def stars(rating: float) -> str:
    """Render a 0.5-5.0 rating as star glyphs."""
    return "★" * int(rating) + ("½" if rating % 1 else "")


def main() -> int:
    """Read all diary entries and lists from SQLite and print metadata."""
    if not db_path.exists():
        print(f"Database not found: {db_path} — run `uv run sync.py` first.")
        return 1

    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        entries = session.scalars(select(Entry)).all()
        lists = session.scalars(select(FilmList)).all()
        list_films = session.scalars(select(ListFilm)).all()

    reviews = [e for e in entries if e.type == "review"]
    watched_dates = sorted(e.watched_date for e in entries if e.watched_date)
    rated = [e.member_rating for e in entries if e.member_rating is not None]

    print("=== Diary entries ===")
    print(f"Total:        {len(entries)}")
    print(f"  reviews:    {len(reviews)}")
    print(f"  watches:    {len(entries) - len(reviews)} (logged without review)")
    print(f"Rewatches:    {sum(e.rewatch for e in entries)}")
    print(f"Likes:        {sum(e.member_like for e in entries)}")
    if watched_dates:
        print(f"Oldest watch: {watched_dates[0]}")
        print(f"Newest watch: {watched_dates[-1]}")

    liked = sorted(
        (e for e in entries if e.member_like),
        key=lambda e: (e.member_rating or 0, e.watched_date or ""),
        reverse=True,
    )
    if liked:
        print(f"\n=== Liked films (top {min(len(liked), 10)} of {len(liked)}) ===")
        for e in liked[:10]:
            label = stars(e.member_rating) if e.member_rating else "unrated"
            print(
                f"  {label:<6} {e.film_title} ({e.film_year}), watched {e.watched_date}"
            )

    if rated:
        print("\n=== Ratings ===")
        print(f"Rated:        {len(rated)} of {len(entries)}")
        print(f"Average:      {sum(rated) / len(rated):.2f}")
        histogram = Counter(rated)
        for rating in sorted(histogram, reverse=True):
            count = histogram[rating]
            print(f"  {stars(rating):<6} {rating:>3.1f}  {'#' * count} {count}")

    film_years = Counter(e.film_year for e in entries if e.film_year)
    if film_years:
        print("\n=== Films by release year (top 10) ===")
        for year, count in film_years.most_common(10):
            print(f"  {year}: {count}")

    print("\n=== Lists ===")
    print(f"Total:        {len(lists)} ({len(list_films)} films across all lists)")
    for lst in sorted(lists, key=lambda x: x.pub_date, reverse=True):
        films_in_list = sum(f.list_guid == lst.guid for f in list_films)
        print(f"  {lst.title} ({films_in_list} films, updated {lst.pub_date[:10]})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
