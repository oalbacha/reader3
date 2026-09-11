import os
import json
import pickle
import shutil
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from obsidian_sync import sync_book_notes
from reader3 import Book, BookMetadata, ChapterContent, TOCEntry, strip_inline_colors

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Where are the book folders located?
BOOKS_DIR = "."

# Soft-deleted books are moved here rather than permanently erased.
TRASH_DIR = os.path.join(BOOKS_DIR, ".trash")

# Progress fraction at/above which a book counts as "read".
READ_THRESHOLD = 0.95

@lru_cache(maxsize=10)
def load_book_cached(folder_name: str) -> Optional[Book]:
    """
    Loads the book from the pickle file.
    Cached so we don't re-read the disk on every click.
    """
    file_path = os.path.join(BOOKS_DIR, folder_name, "book.pkl")
    if not os.path.exists(file_path):
        return None

    try:
        with open(file_path, "rb") as f:
            book = pickle.load(f)
        return book
    except Exception as e:
        print(f"Error loading book {folder_name}: {e}")
        return None


# --- Reading state (status / progress / highlights) ---
#
# Per-book user state lives in `<book>_data/state.json`, right next to book.pkl.
# It is read fresh on every request (NOT lru_cached like the book) because it
# changes at runtime. Deleting a book folder also deletes its state.

def _safe_book_dir(book_id: str) -> str:
    """Resolve a book folder inside BOOKS_DIR, guarding against path escapes."""
    safe_id = os.path.basename(book_id)
    return os.path.join(BOOKS_DIR, safe_id)


def state_path(book_id: str) -> str:
    return os.path.join(_safe_book_dir(book_id), "state.json")


def default_state() -> dict:
    return {
        "status": None,
        "progress": 0.0,
        # Per-chapter max scroll fraction ever reached, keyed by str(chapter_index).
        # Monotonic per chapter. Drives the overall "percent read" (a length-weighted
        # sum of each chapter's own fraction) and the "resume at earliest unfinished
        # chapter" logic, instead of assuming every chapter before the current one
        # has been fully read.
        "chapter_progress": {},
        "highlights": [],
        "archived": False,
    }


def _coerce_fraction(value) -> float:
    """Best-effort float in [0, 1]; malformed/missing input reads as 0."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(max(f, 0.0), 1.0)


def _migrate_chapter_progress(book_id: str, state: dict) -> dict:
    """Derive a per-chapter progress map from an old-format state that only
    tracked a single last_chapter/last_scroll pointer, seeded so the
    previously displayed percentage doesn't regress: chapters before the old
    last_chapter count as fully read (1.0), and the old last_chapter itself
    is seeded with the old last_scroll fraction."""
    lengths = chapter_lengths(book_id)
    old_last_chapter = state.get("last_chapter") or 0
    old_last_scroll = _coerce_fraction(state.get("last_scroll"))
    migrated = {}
    for i in range(len(lengths)):
        if i < old_last_chapter:
            migrated[str(i)] = 1.0
        elif i == old_last_chapter:
            migrated[str(i)] = old_last_scroll
    return migrated


def compute_overall_progress(book_id: str, chapter_progress: dict) -> float:
    """Overall percent read = length-weighted sum of each chapter's own,
    independently-tracked fraction -- not an assumption that every chapter
    before the current one has been read in full."""
    lengths = chapter_lengths(book_id)
    if not lengths:
        return 0.0
    total = sum(lengths) or 1
    chapter_progress = chapter_progress or {}
    read_len = sum(
        length * _coerce_fraction(chapter_progress.get(str(i), 0.0))
        for i, length in enumerate(lengths)
    )
    return read_len / total


def load_state(book_id: str) -> dict:
    """Load state.json, filling in any missing keys with defaults.

    Old-format files (no `chapter_progress` map) are migrated once, in
    place, the first time they're loaded -- see `_migrate_chapter_progress` --
    and the migration is saved immediately so it doesn't repeat on later loads.
    """
    path = state_path(book_id)
    state = default_state()
    stored = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                state.update(stored)
        except Exception as e:
            print(f"Error loading state {book_id}: {e}")
    # Normalize types
    state["highlights"] = state.get("highlights") or []
    state["archived"] = bool(state.get("archived", False))

    had_chapter_progress = isinstance(stored, dict) and isinstance(stored.get("chapter_progress"), dict)
    if had_chapter_progress:
        state["chapter_progress"] = stored["chapter_progress"]
    else:
        state["chapter_progress"] = _migrate_chapter_progress(book_id, state)

    # Retired in favor of chapter_progress/resume_position; drop them from any
    # legacy file that still has them (state.update(stored) above would have
    # carried them over) rather than keep resaving dead fields forever.
    state.pop("last_chapter", None)
    state.pop("last_scroll", None)

    state["progress"] = compute_overall_progress(book_id, state["chapter_progress"])

    if not had_chapter_progress and os.path.exists(path):
        # Persist the migration so it's a one-time event, not recomputed on
        # every load (and so a later manual state.json edit can't undo it).
        try:
            save_state(book_id, state)
        except Exception as e:
            print(f"Error persisting migrated state {book_id}: {e}")

    return state


def save_state(book_id: str, state: dict) -> None:
    """Atomically write state.json (temp file + os.replace)."""
    path = state_path(book_id)
    book_dir = os.path.dirname(path)
    if not os.path.isdir(book_dir):
        raise HTTPException(status_code=404, detail="Book not found")
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


@lru_cache(maxsize=10)
def chapter_lengths(book_id: str) -> tuple:
    """Text length of each spine chapter (min 1), for weighting progress."""
    book = load_book_cached(book_id)
    if not book:
        return tuple()
    return tuple(max(len(ch.text or ""), 1) for ch in book.spine)


def derive_status(state: dict) -> str:
    """Resolve the effective status: manual override, else from progress."""
    if state.get("status") == "read":
        return "read"
    p = state.get("progress") or 0.0
    if p >= READ_THRESHOLD:
        return "read"
    if p > 0:
        return "in_progress"
    return "new"


def resume_position(book_id: str, state: dict) -> tuple:
    """Where "Continue" (library) and the reader's scroll-restore should land:
    the lowest-indexed chapter whose own fraction is still below
    READ_THRESHOLD (the same threshold used for book-level read/in_progress
    status), at that chapter's own saved scroll fraction. If every chapter is
    at/above the threshold, resume at the last chapter instead.

    Unlike simply tracking wherever the reader most recently opened
    (which a quick TOC preview of a later chapter would move past the
    earliest unfinished one), this never jumps past chapters that
    haven't actually been finished.
    """
    lengths = chapter_lengths(book_id)
    if not lengths:
        return 0, 0.0
    chapter_progress = state.get("chapter_progress") or {}
    for i in range(len(lengths)):
        frac = _coerce_fraction(chapter_progress.get(str(i), 0.0))
        if frac < READ_THRESHOLD:
            return i, frac
    last = len(lengths) - 1
    return last, _coerce_fraction(chapter_progress.get(str(last), 0.0))


def chapter_title(book: Book, idx: int) -> str:
    """Best human label for a spine chapter: matching TOC title, else fallback."""
    if idx < 0 or idx >= len(book.spine):
        return f"Section {idx + 1}"
    href = book.spine[idx].href

    def search(entries):
        for e in entries:
            if e.file_href == href and e.title:
                return e.title
            found = search(e.children)
            if found:
                return found
        return None

    return search(book.toc) or book.spine[idx].title or f"Section {idx + 1}"


# --- Request bodies ---

class ProgressBody(BaseModel):
    chapter_index: int
    scroll_fraction: float = 0.0


class StatusBody(BaseModel):
    status: Optional[str] = None


class ArchiveBody(BaseModel):
    archived: bool


class HighlightBody(BaseModel):
    chapter: int
    text: str
    note: str = ""
    # Character offset of `text` within the chapter's flattened raw text at
    # the moment of highlighting. A position hint only (chapter content is
    # static, so it should stay valid) -- used to disambiguate which
    # occurrence to re-mark when `text` appears more than once.
    offset: Optional[int] = None


class NoteBody(BaseModel):
    chapter: int
    text: str


def _scan_books(archived: bool) -> list:
    """Scan BOOKS_DIR for processed book folders and build the card list,
    keeping only books whose `archived` flag matches. Shared by the library
    and archive views so they render identical card data from one source of
    truth."""
    books = []

    if os.path.exists(BOOKS_DIR):
        for item in os.listdir(BOOKS_DIR):
            if item.endswith("_data") and os.path.isdir(item):
                # Try to load it to get the title
                book = load_book_cached(item)
                if book:
                    state = load_state(item)
                    if bool(state.get("archived", False)) != archived:
                        continue
                    highlights = state.get("highlights") or []
                    # Sort by book/spine order (not creation order) so the list
                    # reads front-to-back like the book, matching /api/export.
                    highlights_list = sorted(highlights, key=lambda h: h.get("chapter", 0))
                    cover_image = getattr(book, "cover_image", None)
                    resume_chapter, _resume_scroll = resume_position(item, state)
                    books.append({
                        "id": item,
                        "title": book.metadata.title,
                        "author": ", ".join(book.metadata.authors),
                        "chapters": len(book.spine),
                        "status": derive_status(state),
                        "progress": round((state.get("progress") or 0.0) * 100),
                        "highlights": len(highlights),
                        "highlights_list": highlights_list,
                        # Where "Continue" should reopen: the earliest chapter that
                        # isn't fully read yet, not just wherever was last opened.
                        "resume_chapter": resume_chapter,
                        "cover_url": f"/read/{item}/{cover_image}" if cover_image else None,
                    })

    # Show in-progress books first, then new, then finished.
    order = {"in_progress": 0, "new": 1, "read": 2}
    books.sort(key=lambda b: (order.get(b["status"], 1), b["title"].lower()))
    return books


@app.get("/", response_class=HTMLResponse)
async def library_view(request: Request):
    """Lists all non-archived processed books."""
    books = _scan_books(archived=False)
    archived_count = len(_scan_books(archived=True))
    return templates.TemplateResponse("library.html", {
        "request": request,
        "books": books,
        "archive_mode": False,
        "archived_count": archived_count,
    })


@app.get("/archive", response_class=HTMLResponse)
async def archive_view(request: Request):
    """Lists archived books, reusing the library card template/styles.
    Reachable only via the "Archive" link on the home page."""
    books = _scan_books(archived=True)
    return templates.TemplateResponse("library.html", {
        "request": request,
        "books": books,
        "archive_mode": True,
        "archived_count": len(books),
    })

@app.get("/read/{book_id}", response_class=HTMLResponse)
async def redirect_to_first_chapter(book_id: str):
    """Helper to just go to chapter 0."""
    return await read_chapter(book_id=book_id, chapter_index=0)

@app.get("/read/{book_id}/{chapter_index}", response_class=HTMLResponse)
async def read_chapter(request: Request, book_id: str, chapter_index: int):
    """The main reader interface."""
    book = load_book_cached(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    if chapter_index < 0 or chapter_index >= len(book.spine):
        raise HTTPException(status_code=404, detail="Chapter not found")

    current_chapter = book.spine[chapter_index]

    # Calculate Prev/Next links
    prev_idx = chapter_index - 1 if chapter_index > 0 else None
    next_idx = chapter_index + 1 if chapter_index < len(book.spine) - 1 else None

    state = load_state(book_id)
    highlights = state.get("highlights") or []
    chapter_highlights = [h for h in highlights if h.get("chapter") == chapter_index]

    # Some already-processed EPUBs bake hardcoded colors into chapter HTML
    # (e.g. inline style="color: #000000;"), which would override the theme
    # CSS and go invisible in dark mode. Strip those per-request rather than
    # mutating the cached chapter, since load_book_cached() reuses this object.
    chapter_soup = BeautifulSoup(current_chapter.content, "html.parser")
    chapter_html = str(strip_inline_colors(chapter_soup))

    resume_chapter, resume_scroll = resume_position(book_id, state)

    return templates.TemplateResponse("reader.html", {
        "request": request,
        "book": book,
        "current_chapter": current_chapter,
        "chapter_html": chapter_html,
        "chapter_index": chapter_index,
        "book_id": book_id,
        "prev_idx": prev_idx,
        "next_idx": next_idx,
        "total_chapters": len(book.spine),
        "status": derive_status(state),
        "progress_pct": round((state.get("progress") or 0.0) * 100),
        "highlights": highlights,
        "chapter_highlights": chapter_highlights,
        # Scroll-restore-on-load targets the earliest unfinished chapter (ticket 02),
        # not merely whatever chapter was last opened.
        "resume_chapter": resume_chapter,
        "resume_scroll": resume_scroll,
    })

@app.get("/read/{book_id}/images/{image_name}")
async def serve_image(book_id: str, image_name: str):
    """
    Serves images specifically for a book.
    The HTML contains <img src="images/pic.jpg">.
    The browser resolves this to /read/{book_id}/images/pic.jpg.
    """
    # Security check: ensure book_id is clean
    safe_book_id = os.path.basename(book_id)
    safe_image_name = os.path.basename(image_name)

    img_path = os.path.join(BOOKS_DIR, safe_book_id, "images", safe_image_name)

    if not os.path.exists(img_path):
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(img_path)


# --- State / progress / highlights API ---

def _require_book(book_id: str) -> Book:
    book = load_book_cached(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    return book


@app.get("/api/state/{book_id}")
async def get_state(book_id: str):
    _require_book(book_id)
    state = load_state(book_id)
    resume_chapter, resume_scroll = resume_position(book_id, state)
    return {
        **state,
        "status": derive_status(state),
        "resume_chapter": resume_chapter,
        "resume_scroll": resume_scroll,
    }


@app.post("/api/progress/{book_id}")
async def update_progress(book_id: str, body: ProgressBody):
    _require_book(book_id)
    lengths = chapter_lengths(book_id)
    idx = max(0, min(body.chapter_index, len(lengths) - 1)) if lengths else 0
    frac = min(max(body.scroll_fraction, 0.0), 1.0)

    state = load_state(book_id)
    chapter_progress = state.get("chapter_progress") or {}
    # Scrolling in chapter `idx` updates only that chapter's own stored
    # fraction (monotonic -- never decreases for that chapter). It does not
    # touch any other chapter, so briefly previewing a later chapter no
    # longer implicitly credits the ones skipped in between.
    prev = _coerce_fraction(chapter_progress.get(str(idx), 0.0))
    chapter_progress[str(idx)] = max(prev, frac)
    state["chapter_progress"] = chapter_progress
    # Overall percentage is the length-weighted sum of each chapter's own
    # fraction -- naturally monotonic, since no per-chapter fraction ever decreases.
    state["progress"] = compute_overall_progress(book_id, chapter_progress)
    save_state(book_id, state)
    return {"progress": state["progress"], "status": derive_status(state)}


@app.post("/api/status/{book_id}")
async def set_status(book_id: str, body: StatusBody):
    _require_book(book_id)
    if body.status not in (None, "read"):
        raise HTTPException(status_code=400, detail="status must be 'read' or null")
    state = load_state(book_id)
    state["status"] = body.status
    # Marking unread must also clear progress; otherwise derive_status() would
    # re-derive "read" from a finished book's progress and the toggle would appear to do nothing.
    if body.status is None:
        state["progress"] = 0.0
        state["chapter_progress"] = {}
    save_state(book_id, state)
    return {"progress": state.get("progress", 0.0), "status": derive_status(state)}


@app.post("/api/archive/{book_id}")
async def set_archived(book_id: str, body: ArchiveBody):
    """Toggle the `archived` flag. Purely a visibility flag: unlike
    /api/remove, this never touches the book's folder, progress, highlights,
    or status."""
    _require_book(book_id)
    state = load_state(book_id)
    state["archived"] = body.archived
    save_state(book_id, state)
    return {"archived": state["archived"]}


@app.post("/api/highlights/{book_id}")
async def add_highlight(book_id: str, body: HighlightBody):
    book = _require_book(book_id)
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty highlight")
    highlight = {
        "id": "h_" + uuid.uuid4().hex[:10],
        "chapter": body.chapter,
        "chapter_title": chapter_title(book, body.chapter),
        "text": text,
        "offset": body.offset,
        "note": body.note or "",
        "kind": "highlight",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    state = load_state(book_id)
    state["highlights"].append(highlight)
    save_state(book_id, state)
    _sync_obsidian(book, state)
    return highlight


@app.post("/api/notes/{book_id}")
async def add_note(book_id: str, body: NoteBody):
    """A standalone free-text note, not tied to any quoted passage.

    Stored in the same list as highlights (kind="note") so it slots into
    the existing chapter grouping, Library panel, and export unchanged.
    """
    book = _require_book(book_id)
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty note")
    note = {
        "id": "n_" + uuid.uuid4().hex[:10],
        "chapter": body.chapter,
        "chapter_title": chapter_title(book, body.chapter),
        "text": text,
        "note": "",
        "kind": "note",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    state = load_state(book_id)
    state["highlights"].append(note)
    save_state(book_id, state)
    _sync_obsidian(book, state)
    return note


@app.delete("/api/highlights/{book_id}/{highlight_id}")
async def delete_highlight(book_id: str, highlight_id: str):
    book = _require_book(book_id)
    state = load_state(book_id)
    before = len(state["highlights"])
    state["highlights"] = [h for h in state["highlights"] if h.get("id") != highlight_id]
    if len(state["highlights"]) == before:
        raise HTTPException(status_code=404, detail="Highlight not found")
    save_state(book_id, state)
    # Removing the last highlight removes the book's note from the vault.
    _sync_obsidian(book, state)
    return {"ok": True}


@app.get("/api/export/{book_id}")
async def export_highlights(book_id: str):
    book = _require_book(book_id)
    state = load_state(book_id)
    highlights = state.get("highlights") or []

    title = book.metadata.title or "Untitled"
    author = ", ".join(book.metadata.authors)
    today = datetime.now().strftime("%Y-%m-%d")

    lines = [f"# {title} — Highlights", ""]
    byline = f"_{author} · exported {today}_" if author else f"_exported {today}_"
    lines += [byline, ""]

    if not highlights:
        lines.append("_No highlights yet._")
    else:
        # Group by chapter, in spine order.
        by_chapter = {}
        for h in highlights:
            by_chapter.setdefault(h.get("chapter", 0), []).append(h)
        for ch_idx in sorted(by_chapter):
            items = by_chapter[ch_idx]
            heading = items[0].get("chapter_title") or chapter_title(book, ch_idx)
            lines += [f"## {heading}", ""]
            for h in items:
                if h.get("kind") == "note":
                    lines += [h.get("text") or "", ""]
                    continue
                quote = (h.get("text") or "").replace("\n", "\n> ")
                lines += [f"> {quote}", ""]
                if h.get("note"):
                    lines += [f"  {h['note']}", ""]

    md = "\n".join(lines).rstrip() + "\n"
    safe_name = "".join(c for c in title if c.isalnum() or c in " -_").strip() or "highlights"
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe_name} highlights.md"'},
    )


@app.post("/api/remove/{book_id}")
async def remove_book(book_id: str):
    """Soft-delete: move the book's data folder into .trash/ instead of
    permanently erasing it, so it stays recoverable. The source .epub file
    (which lives outside this folder) is never touched.
    """
    book_dir = _safe_book_dir(book_id)
    if not os.path.isdir(book_dir):
        raise HTTPException(status_code=404, detail="Book not found")
    os.makedirs(TRASH_DIR, exist_ok=True)
    safe_id = os.path.basename(book_id)
    dest = os.path.join(TRASH_DIR, safe_id)
    if os.path.exists(dest):
        dest = os.path.join(TRASH_DIR, f"{safe_id}__{uuid.uuid4().hex[:8]}")
    shutil.move(book_dir, dest)
    load_book_cached.cache_clear()
    return {"ok": True}


# --- Obsidian sync ---

def _sync_obsidian(book: Book, state: dict) -> None:
    """Best-effort sync of this book's highlights/notes to the Obsidian
    vault. Never let a vault problem break reading — log and move on."""
    try:
        sync_book_notes(book, state.get("highlights") or [])
    except Exception as e:
        print(f"[obsidian] Sync failed for {getattr(book.metadata, 'title', '?')}: {e}")


def sync_all_obsidian_notes() -> int:
    """Backfill: sync every book in the library to the vault. Books with no
    highlights produce no vault document (stale ones are removed). Returns
    the number of notes written."""
    written = 0
    for item in os.listdir(BOOKS_DIR):
        if not (item.endswith("_data") and os.path.isdir(item)):
            continue
        book = load_book_cached(item)
        if not book:
            continue
        state = load_state(item)
        path = sync_book_notes(book, state.get("highlights") or [])
        if path:
            written += 1
    return written


if __name__ == "__main__":
    import uvicorn
    # Backfill on startup so existing highlights/notes land in the vault.
    try:
        w = sync_all_obsidian_notes()
        print(f"[obsidian] Backfill complete: {w} book note(s) synced")
    except Exception as e:
        print(f"[obsidian] Backfill failed: {e}")
    print("Starting server at http://127.0.0.1:8123")
    uvicorn.run(app, host="127.0.0.1", port=8123)
