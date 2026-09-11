"""In-memory tracking for background book-upload/processing jobs.

Jobs are transient: nothing here is persisted, and the store is empty again
on server restart. That's fine -- job status only matters to the browser tab
that kicked off the upload, and once a job finishes the durable result lives
in the book folder itself (book.pkl / state.json), not here.
"""

import os
import threading
import uuid
from dataclasses import dataclass
from typing import Dict, Optional

VALID_EXTENSION = ".epub"
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # ~100MB soft cap


@dataclass
class UploadJob:
    id: str
    filename: str
    book_id: str
    status: str = "processing"  # "processing" | "done" | "error"
    error: Optional[str] = None


_jobs: Dict[str, UploadJob] = {}
_in_flight_book_ids: set = set()
_lock = threading.Lock()


def derive_book_id(filename: str) -> str:
    """Book folder name for an uploaded epub filename, matching reader3.py's
    CLI convention: <epub-stem>_data, living directly under BOOKS_DIR. Empty
    if `filename` has no usable stem (e.g. ".epub").

    Strips the ".epub" suffix directly rather than via os.path.splitext,
    since splitext treats a leading dot as making the whole name a stem with
    no extension (splitext(".epub") == (".epub", "")), not what we want here.
    """
    basename = os.path.basename(filename or "")
    if not basename.lower().endswith(VALID_EXTENSION):
        return ""
    stem = basename[: -len(VALID_EXTENSION)]
    return f"{stem}_data" if stem else ""


def validate_filename(filename: str) -> Optional[str]:
    """Return an error message if `filename` can't be accepted, else None.
    Checks shape only (extension, non-empty stem) -- size is checked
    separately while streaming the upload, since that requires reading it."""
    if not filename or not filename.lower().endswith(VALID_EXTENSION):
        return "Only .epub files are supported."
    if not derive_book_id(filename):
        return "Invalid filename."
    return None


def try_create_job(book_id: str, filename: str) -> Optional[UploadJob]:
    """Atomically start a job for `book_id`, unless one is already in flight
    for it -- returns None in that case. Without this, two uploads racing for
    the same book_id (a double-submitted reprocess, or two brand-new uploads
    that happen to share a filename) could both pass the caller's collision
    check and then run process_epub/save_to_pickle against the same book_dir
    concurrently, corrupting it."""
    with _lock:
        if book_id in _in_flight_book_ids:
            return None
        job = UploadJob(id=uuid.uuid4().hex, filename=filename, book_id=book_id)
        _jobs[job.id] = job
        _in_flight_book_ids.add(book_id)
        return job


def get_job(job_id: str) -> Optional[UploadJob]:
    with _lock:
        return _jobs.get(job_id)


def mark_done(job_id: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job:
            job.status = "done"
            _in_flight_book_ids.discard(job.book_id)


def mark_error(job_id: str, message: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job:
            job.status = "error"
            job.error = message
            _in_flight_book_ids.discard(job.book_id)
