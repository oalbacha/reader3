"""Synthetic fixture-book helper.

Builds a minimal `Book` in memory -- no real `.epub` is parsed -- and pickles
it directly into `<book_id>_data/book.pkl`, matching the on-disk layout
`load_book_cached` expects. Used to exercise the reading-state HTTP endpoints
without touching the developer's real book library.
"""

import os
import pickle
from datetime import datetime, timezone

from reader3 import Book, BookMetadata, ChapterContent, TOCEntry

# Deliberately different, known text lengths so length-weighted progress
# math is checkable by hand: 100 + 200 + 300 = 600 total characters.
DEFAULT_CHAPTER_LENGTHS = (100, 200, 300)


def make_book(chapter_lengths=DEFAULT_CHAPTER_LENGTHS, title="Fixture Book", authors=("Fixture Author",)):
    """Build a `Book` with one spine chapter per length in `chapter_lengths`,
    each chapter's `text` being exactly that many characters long."""
    spine = []
    toc = []
    for i, length in enumerate(chapter_lengths):
        href = f"chap{i}.xhtml"
        text = "x" * length
        spine.append(ChapterContent(
            id=f"item_{i}",
            href=href,
            title=f"Chapter {i + 1}",
            content=f"<p>{text}</p>",
            text=text,
            order=i,
        ))
        toc.append(TOCEntry(
            title=f"Chapter {i + 1}",
            href=href,
            file_href=href,
            anchor="",
        ))
    metadata = BookMetadata(title=title, language="en", authors=list(authors))
    return Book(
        metadata=metadata,
        spine=spine,
        toc=toc,
        images={},
        source_file="fixture.epub",
        processed_at=datetime.now(timezone.utc).isoformat(),
    )


def write_book(books_dir, book_id, book) -> str:
    """Pickle `book` into `<books_dir>/<book_id>/book.pkl`. Returns the book's
    data directory."""
    book_dir = os.path.join(books_dir, book_id)
    os.makedirs(book_dir, exist_ok=True)
    with open(os.path.join(book_dir, "book.pkl"), "wb") as f:
        pickle.dump(book, f)
    return book_dir
