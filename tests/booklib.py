"""Synthetic fixture-book helper.

Builds a minimal `Book` in memory -- no real `.epub` is parsed -- and pickles
it directly into `<book_id>_data/book.pkl`, matching the on-disk layout
`load_book_cached` expects. Used to exercise the reading-state HTTP endpoints
without touching the developer's real book library.
"""

import os
import pickle
from datetime import datetime, timezone

from ebooklib import epub

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


def write_minimal_epub(path: str, title: str = "Test Title", chapters: int = 1) -> str:
    """Write a real, minimal, parseable .epub to `path` via ebooklib -- unlike
    `make_book`/`write_book` above, this exercises the actual parsing path
    (`reader3.process_epub`), for tests of the upload/processing pipeline
    rather than the reading-state endpoints. Returns `path`."""
    book = epub.EpubBook()
    book.set_identifier("test-fixture-id")
    book.set_title(title)
    book.set_language("en")
    book.add_author("Test Author")

    items = []
    for i in range(chapters):
        item = epub.EpubHtml(title=f"Chapter {i + 1}", file_name=f"chap_{i + 1}.xhtml", lang="en")
        item.content = f"<html><body><h1>Chapter {i + 1}</h1><p>Hello world.</p></body></html>"
        book.add_item(item)
        items.append(item)

    book.toc = tuple(epub.Link(item.file_name, item.title, f"chap{i + 1}") for i, item in enumerate(items))
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + items

    epub.write_epub(path, book)
    return path
