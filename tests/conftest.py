import uuid

import pytest
from fastapi.testclient import TestClient

import obsidian_sync
import server
import upload_jobs
from tests.booklib import DEFAULT_CHAPTER_LENGTHS, make_book, write_book


@pytest.fixture(autouse=True)
def _clean_upload_jobs():
    """upload_jobs' job store and in-flight set are module-level globals, so
    without this they'd leak across tests within the same pytest process --
    e.g. a book_id left "in flight" by one test would make try_create_job
    spuriously refuse a same-named job in the next."""
    upload_jobs._jobs.clear()
    upload_jobs._in_flight_book_ids.clear()
    yield
    upload_jobs._jobs.clear()
    upload_jobs._in_flight_book_ids.clear()


@pytest.fixture
def books_dir(tmp_path, monkeypatch):
    """Redirect BOOKS_DIR (and the Obsidian vault) at a throwaway temp
    directory for the duration of the test, and make sure the module-level
    lru_caches on book loading don't leak state in or out of it."""
    monkeypatch.setattr(server, "BOOKS_DIR", str(tmp_path))
    monkeypatch.setattr(server, "TRASH_DIR", str(tmp_path / ".trash"))
    # Never touch the developer's real Obsidian vault: point at a path that
    # doesn't exist, which sync_book_notes treats as "vault not found" and
    # silently skips.
    monkeypatch.setattr(obsidian_sync, "OBSIDIAN_VAULT", str(tmp_path / "_no_vault"))

    server.load_book_cached.cache_clear()
    server.chapter_lengths.cache_clear()
    yield tmp_path
    server.load_book_cached.cache_clear()
    server.chapter_lengths.cache_clear()


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture
def book_id():
    """A fresh, unique book id per test so cache keys never collide even if
    a cache-clear were ever missed."""
    return f"fixture-{uuid.uuid4().hex[:8]}_data"


@pytest.fixture
def fixture_book(books_dir, book_id):
    """A pickled fixture book with three chapters of known, distinct text
    lengths (100/200/300 chars, 600 total) ready to serve via the API.

    Returns (book_id, book, chapter_lengths).
    """
    book = make_book(DEFAULT_CHAPTER_LENGTHS, title="Fixture Book")
    write_book(str(books_dir), book_id, book)
    return book_id, book, DEFAULT_CHAPTER_LENGTHS
