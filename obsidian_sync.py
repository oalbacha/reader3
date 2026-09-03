"""
Syncs reader3 highlights and notes into an Obsidian vault.

For each book that has at least one highlight or note, this writes (and keeps
up to date) a single Markdown document in the vault, with every entry listed
in chronological order and tagged with the chapter it came from.

Configuration (environment variables):
  OBSIDIAN_VAULT           Path to the Obsidian vault root.
                           Default: ~/Documents/obsidian
  OBSIDIAN_BOOK_NOTES_DIR  Folder inside the vault for the book notes.
                           Default: "3. Resources/Book notes"

If the vault path does not exist, syncing is skipped with a warning (reader3
keeps working normally; state.json remains the source of truth).
"""

import os
import re
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

# --- Configuration ---

DEFAULT_VAULT = os.path.expanduser("~/Documents/obsidian")
OBSIDIAN_VAULT = os.environ.get("OBSIDIAN_VAULT", DEFAULT_VAULT)
BOOK_NOTES_SUBDIR = os.environ.get("OBSIDIAN_BOOK_NOTES_DIR", "3. Resources/Book notes")

_warned_no_vault = False


# --- Helpers ---

def sanitize_filename(name: str) -> str:
    """Make a book title safe to use as a filename (no /, :, etc.)."""
    name = re.sub(r'[\\/:*?"<>|]', " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "Untitled"


def yaml_str(value: str) -> str:
    """Quote a string for safe use in YAML frontmatter."""
    return json.dumps(value or "", ensure_ascii=False)


# --- Rendering ---

def render_book_notes(book, highlights: list) -> str:
    """Render the full Markdown document for one book.

    Entries are grouped under ONE heading per chapter, with chapters in
    spine (book) order rather than creation order -- matching /api/export
    and the Library page -- so the note reads front-to-back like the book
    and the last entry is always the last highlight in the book, not just
    the most recently created one. Entries within a chapter are chronological.
    """
    title = book.metadata.title or "Untitled"
    author = ", ".join(book.metadata.authors)
    entries = sorted(
        highlights,
        key=lambda h: (h.get("ts") or "", h.get("chapter", 0)),
    )

    # Group by chapter, in spine order.
    by_chapter = {}
    for h in entries:
        by_chapter.setdefault(h.get("chapter", 0), []).append(h)
    by_chapter = dict(sorted(by_chapter.items()))

    lines = [
        "---",
        f"title: {yaml_str(title)}",
        f"author: {yaml_str(author)}",
        "source: reader3",
        f"highlights: {len(entries)}",
        f"last_synced: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "---",
        "",
        f"# {title}",
        "",
    ]
    if author:
        lines += [f"*{author}*", ""]

    if not entries:
        lines += ["_No highlights yet._"]
    else:
        for ch_idx, items in by_chapter.items():
            heading = items[0].get("chapter_title") or f"Section {ch_idx + 1}"
            lines += [f"## {heading}", ""]
            for h in items:
                text = (h.get("text") or "").replace("\n", " ")
                lines += [f"- {text}"]
                if h.get("note"):
                    lines += [f"    - **Note:** {h['note']}"]
            lines += [""]

    return "\n".join(lines).rstrip() + "\n"


# --- Sync ---

def sync_book_notes(book, highlights: list) -> Optional[str]:
    """Write/update the book's Markdown doc in the Obsidian vault.

    Books with no highlights or notes get NO document in the vault: if one
    exists from earlier (e.g. the last highlight was deleted), it is removed.

    Returns the written file path, or None if nothing was written.
    """
    global _warned_no_vault

    title = book.metadata.title or "Untitled"
    path = os.path.join(OBSIDIAN_VAULT, BOOK_NOTES_SUBDIR,
                        f"{sanitize_filename(title)}.md")

    if not highlights:
        # No highlights -> no note in Obsidian. Remove any stale doc.
        if os.path.isdir(OBSIDIAN_VAULT) and os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                print(f"[obsidian] Could not remove stale note {path}: {e}")
        return None

    if not os.path.isdir(OBSIDIAN_VAULT):
        if not _warned_no_vault:
            print(f"[obsidian] Vault not found at {OBSIDIAN_VAULT!r} — skipping sync "
                  f"(set OBSIDIAN_VAULT to enable)")
            _warned_no_vault = True
        return None

    out_dir = os.path.dirname(path)
    os.makedirs(out_dir, exist_ok=True)

    md = render_book_notes(book, highlights)

    # Atomic write so Obsidian never sees a half-written file.
    tmp = f"{path}.{uuid.uuid4().hex[:8]}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(md)
    os.replace(tmp, path)
    return path
