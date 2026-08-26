import os
import json
import pickle
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

from reader3 import Book, BookMetadata, ChapterContent, TOCEntry, strip_inline_colors

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Where are the book folders located?
BOOKS_DIR = "."

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
    return {"status": None, "progress": 0.0, "last_chapter": 0, "last_scroll": 0.0, "highlights": []}


def load_state(book_id: str) -> dict:
    """Load state.json, filling in any missing keys with defaults."""
    path = state_path(book_id)
    state = default_state()
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
    try:
        state["progress"] = float(state.get("progress") or 0.0)
    except (TypeError, ValueError):
        state["progress"] = 0.0
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


class HighlightBody(BaseModel):
    chapter: int
    text: str
    note: str = ""


class NoteBody(BaseModel):
    chapter: int
    text: str


@app.get("/", response_class=HTMLResponse)
async def library_view(request: Request):
    """Lists all available processed books."""
    books = []

    # Scan directory for folders ending in '_data' that have a book.pkl
    if os.path.exists(BOOKS_DIR):
        for item in os.listdir(BOOKS_DIR):
            if item.endswith("_data") and os.path.isdir(item):
                # Try to load it to get the title
                book = load_book_cached(item)
                if book:
                    state = load_state(item)
                    highlights = state.get("highlights") or []
                    books.append({
                        "id": item,
                        "title": book.metadata.title,
                        "author": ", ".join(book.metadata.authors),
                        "chapters": len(book.spine),
                        "status": derive_status(state),
                        "progress": round((state.get("progress") or 0.0) * 100),
                        "highlights": len(highlights),
                        "highlights_list": highlights,
                        "last_chapter": state.get("last_chapter", 0),
                    })

    # Show in-progress books first, then new, then finished.
    order = {"in_progress": 0, "new": 1, "read": 2}
    books.sort(key=lambda b: (order.get(b["status"], 1), b["title"].lower()))

    return templates.TemplateResponse("library.html", {"request": request, "books": books})

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
        "last_chapter": state.get("last_chapter", 0),
        "last_scroll": state.get("last_scroll", 0.0),
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
    return {**state, "status": derive_status(state)}


@app.post("/api/progress/{book_id}")
async def update_progress(book_id: str, body: ProgressBody):
    _require_book(book_id)
    lengths = chapter_lengths(book_id)
    total = sum(lengths) or 1
    idx = max(0, min(body.chapter_index, len(lengths) - 1))
    frac = min(max(body.scroll_fraction, 0.0), 1.0)

    read_len = sum(lengths[:idx]) + frac * lengths[idx]
    computed = read_len / total

    state = load_state(book_id)
    # Progress is monotonic: back-scrolling never lowers it.
    state["progress"] = max(state.get("progress") or 0.0, computed)
    # last_chapter/last_scroll track the ACTUAL current position (can move back)
    # so we can resume exactly where the reader left off.
    state["last_chapter"] = idx
    state["last_scroll"] = frac
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
        state["last_chapter"] = 0
        state["last_scroll"] = 0.0
    save_state(book_id, state)
    return {"progress": state.get("progress", 0.0), "status": derive_status(state)}


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
        "note": body.note or "",
        "kind": "highlight",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    state = load_state(book_id)
    state["highlights"].append(highlight)
    save_state(book_id, state)
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
    return note


@app.delete("/api/highlights/{book_id}/{highlight_id}")
async def delete_highlight(book_id: str, highlight_id: str):
    _require_book(book_id)
    state = load_state(book_id)
    before = len(state["highlights"])
    state["highlights"] = [h for h in state["highlights"] if h.get("id") != highlight_id]
    if len(state["highlights"]) == before:
        raise HTTPException(status_code=404, detail="Highlight not found")
    save_state(book_id, state)
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


if __name__ == "__main__":
    import uvicorn
    print("Starting server at http://127.0.0.1:8123")
    uvicorn.run(app, host="127.0.0.1", port=8123)
