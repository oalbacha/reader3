"""Reading state (status / progress / highlights) for a single book.

Per-book user state lives in `<book>_data/state.json`, right next to
book.pkl. `ReadingState` owns that dict entirely: route handlers in
server.py parse the request, call one named verb below, and shape the
response -- the invariants (monotonic per-chapter progress, the
status/progress coupling, resume-threshold scanning, one-time legacy
migration) live here instead of being a convention repeated at every call
site.

Deliberately decoupled from `Book`/pickle: a `ReadingState` is constructed
from `chapter_lengths` (plain ints, already computed by the book-loading
code) rather than reaching into a `Book` itself. State is read fresh on
every request (NOT lru_cached like the book) because it changes at
runtime, and each mutating verb persists immediately -- there is no
separate `save()` in the public interface, matching the per-request
atomic-write behavior this replaces.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

# Progress fraction at/above which a chapter (or the whole book) counts as "read".
READ_THRESHOLD = 0.95


def _safe_book_dir(books_dir: str, book_id: str) -> str:
    """Resolve a book folder inside books_dir, guarding against path escapes."""
    safe_id = os.path.basename(book_id)
    return os.path.join(books_dir, safe_id)


def _state_path(books_dir: str, book_id: str) -> str:
    return os.path.join(_safe_book_dir(books_dir, book_id), "state.json")


def _coerce_fraction(value) -> float:
    """Best-effort float in [0, 1]; malformed/missing input reads as 0."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(max(f, 0.0), 1.0)


class ReadingState:
    """A single book's status/progress/highlights, persisted to
    `<book>_data/state.json` under `books_dir`."""

    def __init__(
        self,
        book_id: str,
        books_dir: str,
        chapter_lengths,
        *,
        status: Optional[str] = None,
        chapter_progress: Optional[dict] = None,
        highlights: Optional[list] = None,
        archived: bool = False,
    ):
        self._book_id = book_id
        self._books_dir = books_dir
        self._chapter_lengths = tuple(chapter_lengths)
        self.status = status
        self._chapter_progress = dict(chapter_progress or {})
        self._highlights = list(highlights or [])
        self._archived = bool(archived)

    # --- Loading / persistence ------------------------------------------

    @classmethod
    def load(cls, book_id: str, chapter_lengths, books_dir: str) -> "ReadingState":
        """Load state.json, filling in defaults for a never-seen book.

        Old-format files (no `chapter_progress` map) are migrated once, in
        place, the first time they're loaded -- see `_migrate_chapter_progress`
        -- and the migration is persisted immediately so it doesn't repeat
        on later loads.
        """
        path = _state_path(books_dir, book_id)
        stored = None
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    stored = json.load(f)
            except Exception as e:
                print(f"Error loading state {book_id}: {e}")
        stored = stored if isinstance(stored, dict) else {}

        had_chapter_progress = isinstance(stored.get("chapter_progress"), dict)
        if had_chapter_progress:
            chapter_progress = stored["chapter_progress"]
        else:
            chapter_progress = cls._migrate_chapter_progress(chapter_lengths, stored)

        state = cls(
            book_id,
            books_dir,
            chapter_lengths,
            status=stored.get("status"),
            chapter_progress=chapter_progress,
            highlights=stored.get("highlights") or [],
            archived=bool(stored.get("archived", False)),
        )

        if not had_chapter_progress and os.path.exists(path):
            # Persist the migration so it's a one-time event, not recomputed
            # on every load (and so a later manual state.json edit can't
            # undo it).
            try:
                state._save()
            except Exception as e:
                print(f"Error persisting migrated state {book_id}: {e}")

        return state

    @staticmethod
    def _migrate_chapter_progress(chapter_lengths, stored: dict) -> dict:
        """Derive a per-chapter progress map from an old-format state that
        only tracked a single last_chapter/last_scroll pointer, seeded so
        the previously displayed percentage doesn't regress: chapters
        before the old last_chapter count as fully read (1.0), and the old
        last_chapter itself is seeded with the old last_scroll fraction."""
        old_last_chapter = stored.get("last_chapter") or 0
        old_last_scroll = _coerce_fraction(stored.get("last_scroll"))
        migrated = {}
        for i in range(len(chapter_lengths)):
            if i < old_last_chapter:
                migrated[str(i)] = 1.0
            elif i == old_last_chapter:
                migrated[str(i)] = old_last_scroll
        return migrated

    def _save(self) -> None:
        """Atomically write state.json (temp file + os.replace)."""
        path = _state_path(self._books_dir, self._book_id)
        book_dir = os.path.dirname(path)
        if not os.path.isdir(book_dir):
            raise HTTPException(status_code=404, detail="Book not found")
        tmp = f"{path}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _to_dict(self) -> dict:
        return {
            "status": self.status,
            "progress": self.progress,
            "chapter_progress": self._chapter_progress,
            "highlights": self._highlights,
            "archived": self._archived,
        }

    # --- Read-only views --------------------------------------------------

    @property
    def progress(self) -> float:
        """Overall percent read = length-weighted sum of each chapter's own,
        independently-tracked fraction -- not an assumption that every
        chapter before the current one has been read in full."""
        lengths = self._chapter_lengths
        if not lengths:
            return 0.0
        total = sum(lengths) or 1
        read_len = sum(
            length * _coerce_fraction(self._chapter_progress.get(str(i), 0.0))
            for i, length in enumerate(lengths)
        )
        return read_len / total

    @property
    def chapter_progress(self) -> dict:
        return dict(self._chapter_progress)

    @property
    def highlights(self) -> list:
        return list(self._highlights)

    @property
    def archived(self) -> bool:
        return self._archived

    def derive_status(self) -> str:
        """Resolve the effective status: manual override, else from progress."""
        if self.status == "read":
            return "read"
        p = self.progress
        if p >= READ_THRESHOLD:
            return "read"
        if p > 0:
            return "in_progress"
        return "new"

    def resume_position(self) -> tuple:
        """Where "Continue" (library) and the reader's scroll-restore should
        land: the lowest-indexed chapter whose own fraction is still below
        READ_THRESHOLD (the same threshold used for book-level read/in_progress
        status), at that chapter's own saved scroll fraction. If every chapter
        is at/above the threshold, resume at the last chapter instead.

        Unlike simply tracking wherever the reader most recently opened
        (which a quick TOC preview of a later chapter would move past the
        earliest unfinished one), this never jumps past chapters that
        haven't actually been finished.
        """
        lengths = self._chapter_lengths
        if not lengths:
            return 0, 0.0
        for i in range(len(lengths)):
            frac = _coerce_fraction(self._chapter_progress.get(str(i), 0.0))
            if frac < READ_THRESHOLD:
                return i, frac
        last = len(lengths) - 1
        return last, _coerce_fraction(self._chapter_progress.get(str(last), 0.0))

    # --- Mutating verbs ----------------------------------------------------

    def record_progress(self, chapter_index: int, fraction: float) -> None:
        """Scrolling in `chapter_index` updates only that chapter's own
        stored fraction (monotonic -- never decreases for that chapter). It
        does not touch any other chapter, so briefly previewing a later
        chapter no longer implicitly credits the ones skipped in between."""
        lengths = self._chapter_lengths
        idx = max(0, min(chapter_index, len(lengths) - 1)) if lengths else 0
        frac = _coerce_fraction(fraction)
        prev = _coerce_fraction(self._chapter_progress.get(str(idx), 0.0))
        self._chapter_progress[str(idx)] = max(prev, frac)
        self._save()

    def set_status(self, status: Optional[str]) -> None:
        """Set the manual status override. Setting `None` also clears
        progress/chapter_progress; otherwise derive_status() would just
        re-derive "read" from a finished book's stale progress and the
        toggle would appear to do nothing."""
        self.status = status
        if status is None:
            self._chapter_progress = {}
        self._save()

    def set_archived(self, archived: bool) -> None:
        """Toggle the archived flag. Purely a visibility flag: this never
        touches progress, highlights, or status."""
        self._archived = bool(archived)
        self._save()

    def add_highlight(self, chapter: int, chapter_title: str, text: str, note: str, offset: Optional[int]) -> dict:
        # Deliberately not merged with add_note into one shared constructor:
        # a highlight carries `offset` (a position hint into the chapter
        # text) that a standalone note has no `text` position to derive, so
        # note entries omit that key entirely rather than storing a bogus
        # null.
        highlight = {
            "id": "h_" + uuid.uuid4().hex[:10],
            "chapter": chapter,
            "chapter_title": chapter_title,
            "text": text,
            "offset": offset,
            "note": note or "",
            "kind": "highlight",
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self._highlights.append(highlight)
        self._save()
        return highlight

    def add_note(self, chapter: int, chapter_title: str, text: str) -> dict:
        """A standalone free-text note, not tied to any quoted passage.

        Stored in the same list as highlights (kind="note") so it slots
        into the existing chapter grouping, Library panel, and export
        unchanged.
        """
        note = {
            "id": "n_" + uuid.uuid4().hex[:10],
            "chapter": chapter,
            "chapter_title": chapter_title,
            "text": text,
            "note": "",
            "kind": "note",
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self._highlights.append(note)
        self._save()
        return note

    def delete_highlight(self, highlight_id: str) -> bool:
        """Remove a highlight/note by id. Returns whether anything was
        removed; mapping that to a 404 stays the route's job."""
        before = len(self._highlights)
        self._highlights = [h for h in self._highlights if h.get("id") != highlight_id]
        removed = len(self._highlights) != before
        if removed:
            self._save()
        return removed
