import os
import pickle
import shutil
import uuid
from datetime import datetime
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
from reading_state import ReadingState

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Where are the book folders located?
BOOKS_DIR = "."

# Soft-deleted books are moved here rather than permanently erased.
TRASH_DIR = os.path.join(BOOKS_DIR, ".trash")

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
# Per-book user state lives in `<book>_data/state.json`, right next to
# book.pkl, owned entirely by the ReadingState class (reading_state.py).
# Deleting a book folder also deletes its state.

@lru_cache(maxsize=10)
def chapter_lengths(book_id: str) -> tuple:
    """Text length of each spine chapter (min 1), for weighting progress."""
    book = load_book_cached(book_id)
    if not book:
        return tuple()
    return tuple(max(len(ch.text or ""), 1) for ch in book.spine)


def _load_state(book_id: str) -> ReadingState:
    """Load `book_id`'s ReadingState, so callers can't forget to pair it with
    the right chapter_lengths/BOOKS_DIR."""
    return ReadingState.load(book_id, chapter_lengths(book_id), BOOKS_DIR)


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
                    state = _load_state(item)
                    if state.archived != archived:
                        continue
                    highlights = state.highlights
                    # Sort by book/spine order (not creation order) so the list
                    # reads front-to-back like the book, matching /api/export.
                    highlights_list = sorted(highlights, key=lambda h: h.get("chapter", 0))
                    cover_image = getattr(book, "cover_image", None)
                    resume_chapter, _resume_scroll = state.resume_position()
                    books.append({
                        "id": item,
                        "title": book.metadata.title,
                        "author": ", ".join(book.metadata.authors),
                        "chapters": len(book.spine),
                        "status": state.derive_status(),
                        "progress": round(state.progress * 100),
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

    state = _load_state(book_id)
    highlights = state.highlights
    chapter_highlights = [h for h in highlights if h.get("chapter") == chapter_index]

    # Some already-processed EPUBs bake hardcoded colors into chapter HTML
    # (e.g. inline style="color: #000000;"), which would override the theme
    # CSS and go invisible in dark mode. Strip those per-request rather than
    # mutating the cached chapter, since load_book_cached() reuses this object.
    chapter_soup = BeautifulSoup(current_chapter.content, "html.parser")
    chapter_html = str(strip_inline_colors(chapter_soup))

    resume_chapter, resume_scroll = state.resume_position()

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
        "status": state.derive_status(),
        "progress_pct": round(state.progress * 100),
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
    # Security check: ensure book_id/image_name are clean
    safe_image_name = os.path.basename(image_name)
    img_path = os.path.join(_book_dir(book_id), "images", safe_image_name)

    if not os.path.exists(img_path):
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(img_path)


# --- State / progress / highlights API ---

def _require_book(book_id: str) -> Book:
    book = load_book_cached(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    return book


def _book_dir(book_id: str) -> str:
    """Resolve a book folder inside BOOKS_DIR, guarding against path escapes."""
    return os.path.join(BOOKS_DIR, os.path.basename(book_id))


@app.get("/api/state/{book_id}")
async def get_state(book_id: str):
    _require_book(book_id)
    state = _load_state(book_id)
    resume_chapter, resume_scroll = state.resume_position()
    return {
        "status": state.derive_status(),
        "progress": state.progress,
        "chapter_progress": state.chapter_progress,
        "highlights": state.highlights,
        "archived": state.archived,
        "resume_chapter": resume_chapter,
        "resume_scroll": resume_scroll,
    }


@app.post("/api/progress/{book_id}")
async def update_progress(book_id: str, body: ProgressBody):
    _require_book(book_id)
    state = _load_state(book_id)
    state.record_progress(body.chapter_index, body.scroll_fraction)
    return {"progress": state.progress, "status": state.derive_status()}


@app.post("/api/status/{book_id}")
async def set_status(book_id: str, body: StatusBody):
    _require_book(book_id)
    if body.status not in (None, "read"):
        raise HTTPException(status_code=400, detail="status must be 'read' or null")
    state = _load_state(book_id)
    state.set_status(body.status)
    return {"progress": state.progress, "status": state.derive_status()}


@app.post("/api/archive/{book_id}")
async def set_archived(book_id: str, body: ArchiveBody):
    """Toggle the `archived` flag. Purely a visibility flag: unlike
    /api/remove, this never touches the book's folder, progress, highlights,
    or status."""
    _require_book(book_id)
    state = _load_state(book_id)
    state.set_archived(body.archived)
    return {"archived": state.archived}


@app.post("/api/highlights/{book_id}")
async def add_highlight(book_id: str, body: HighlightBody):
    book = _require_book(book_id)
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty highlight")
    state = _load_state(book_id)
    highlight = state.add_highlight(
        chapter=body.chapter,
        chapter_title=chapter_title(book, body.chapter),
        text=text,
        note=body.note or "",
        offset=body.offset,
    )
    _sync_obsidian(book, state.highlights)
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
    state = _load_state(book_id)
    note = state.add_note(
        chapter=body.chapter,
        chapter_title=chapter_title(book, body.chapter),
        text=text,
    )
    _sync_obsidian(book, state.highlights)
    return note


@app.delete("/api/highlights/{book_id}/{highlight_id}")
async def delete_highlight(book_id: str, highlight_id: str):
    book = _require_book(book_id)
    state = _load_state(book_id)
    if not state.delete_highlight(highlight_id):
        raise HTTPException(status_code=404, detail="Highlight not found")
    # Removing the last highlight removes the book's note from the vault.
    _sync_obsidian(book, state.highlights)
    return {"ok": True}


@app.get("/api/export/{book_id}")
async def export_highlights(book_id: str):
    book = _require_book(book_id)
    state = _load_state(book_id)
    highlights = state.highlights

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
    book_dir = _book_dir(book_id)
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

def _sync_obsidian(book: Book, highlights: list) -> None:
    """Best-effort sync of this book's highlights/notes to the Obsidian
    vault. Never let a vault problem break reading — log and move on."""
    try:
        sync_book_notes(book, highlights)
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
        state = _load_state(item)
        path = sync_book_notes(book, state.highlights)
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
