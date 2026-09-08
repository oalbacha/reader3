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
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, NavigableString, Tag

# --- Configuration ---

DEFAULT_VAULT = os.path.expanduser("~/Documents/obsidian")
OBSIDIAN_VAULT = os.environ.get("OBSIDIAN_VAULT", DEFAULT_VAULT)
BOOK_NOTES_SUBDIR = os.environ.get("OBSIDIAN_BOOK_NOTES_DIR", "3. Resources/Book notes")

# Fenced code blocks fall back to this language when the source HTML doesn't
# provide a `data-code-language` hint.
DEFAULT_CODE_LANGUAGE = "ts"

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


# --- Position index (shared by TOC nesting + code detection) ---
#
# Highlights/notes store an `offset`: a character position into the chapter's
# flattened `.text` (see reader3.extract_plain_text -- get_text(separator=' ')
# with whitespace collapsed to single spaces). To reason about *where in the
# HTML* an offset falls (which TOC section it's under, whether it's inside a
# <pre>/<code>), we reparse the chapter's cleaned `.content` HTML and rebuild
# that exact same flattened text word-by-word, recording each word's [start,
# end) range in the flattened text plus its code-block/anchor context. As
# long as the chapter HTML hasn't changed since the highlight was captured,
# this lines up exactly with the stored offsets.

class _PositionIndex:
    __slots__ = ("word_spans", "anchor_offset")

    def __init__(self, word_spans: List[Tuple[int, int, bool, bool, Optional[str]]],
                 anchor_offset: Dict[str, int]):
        # word_spans: (start, end, in_pre, in_code_outside_pre, code_lang)
        self.word_spans = word_spans
        self.anchor_offset = anchor_offset


def _build_position_index(html: str) -> _PositionIndex:
    """Reparse one chapter's HTML and build a word-level position index.

    Returns a _PositionIndex with:
      - word_spans: one (start, end, in_pre, in_code, code_lang) tuple per
        whitespace-delimited "word" in document order, mirroring how
        extract_plain_text() flattens and collapses the chapter's text.
      - anchor_offset: element id -> the flattened-text offset where that
        element's content begins (used to place TOC entries, which point at
        `id="..."` anchors in the source HTML).
    """
    soup = BeautifulSoup(html or "", "html.parser")
    word_spans: List[Tuple[int, int, bool, bool, Optional[str]]] = []
    anchor_offset: Dict[str, int] = {}
    state = {"cursor": 0, "started": False}

    def visit(node):
        if isinstance(node, NavigableString):
            for word in str(node).split():
                if state["started"]:
                    state["cursor"] += 1
                start = state["cursor"]
                end = start + len(word)
                pre = node.find_parent("pre")
                in_pre = pre is not None
                code_ancestor = None if in_pre else node.find_parent("code")
                in_code = code_ancestor is not None
                lang = None
                if in_pre:
                    lang = pre.get("data-code-language") or DEFAULT_CODE_LANGUAGE
                word_spans.append((start, end, in_pre, in_code, lang))
                state["cursor"] = end
                state["started"] = True
            return
        if isinstance(node, Tag):
            elem_id = node.get("id")
            if elem_id and elem_id not in anchor_offset:
                anchor_offset[elem_id] = state["cursor"] + (1 if state["started"] else 0)
            for child in list(node.children):
                visit(child)

    for top in list(soup.contents):
        visit(top)

    return _PositionIndex(word_spans, anchor_offset)


def _classify_code(index: "_PositionIndex", offset: Optional[int], length: int):
    """Return ("block", lang), ("inline", None), or None for a [offset,
    offset+length) span, based on whether it falls FULLY inside a <pre> (or
    fully inside a stray <code> outside any <pre>). Any partial overlap, or
    a missing offset, returns None (render as a plain bullet)."""
    if offset is None or length <= 0:
        return None
    end = offset + length
    overlapping = [w for w in index.word_spans if w[0] < end and w[1] > offset]
    if not overlapping:
        return None
    if all(w[2] for w in overlapping):
        lang = overlapping[0][4] or DEFAULT_CODE_LANGUAGE
        return ("block", lang)
    if all(w[3] for w in overlapping):
        return ("inline", None)
    return None


# --- TOC hierarchy ---

def _find_chapter_entry(entries, href):
    """Find the (outer-most) TOC entry covering a spine file `href`.

    Mirrors server.py's chapter_title() search order: a top-level entry's
    own href is checked before descending into its children, so the entry
    returned is always the one that "owns" the whole chapter, not a nested
    sub-section that happens to share the same file.
    """
    for e in entries:
        if e.file_href == href:
            return e
        found = _find_chapter_entry(e.children, href)
        if found:
            return found
    return None


def _deepest_section(entries, href, offset, anchor_offset):
    """Given a list of sibling TOCEntry objects (all nested under some
    section of chapter `href`), return the chain of entries -- from
    shallowest to deepest -- whose section contains `offset`, per each
    entry's anchor position in the chapter's flattened text.

    Entries are assumed to appear in document order, so among siblings the
    section containing `offset` is the *last* one whose start position is
    still <= offset.
    """
    chosen = None
    for e in entries:
        if e.file_href != href:
            continue
        start = 0 if not e.anchor else anchor_offset.get(e.anchor)
        if start is None:
            # Anchor doesn't resolve in this chapter's HTML (stale/broken
            # link) -- skip it; never place a highlight under a section we
            # can't actually locate.
            continue
        if start <= offset:
            chosen = e
        else:
            break
    if chosen is None:
        return []
    return [chosen] + _deepest_section(chosen.children, href, offset, anchor_offset)


def _subtree_has_items(entry, items_by_key) -> bool:
    if items_by_key.get(id(entry)):
        return True
    return any(_subtree_has_items(child, items_by_key) for child in entry.children)


def _emit_heading(title: str, depth: int, lines: list) -> None:
    """depth 0 is the chapter-level heading (h3); depth 1-3 nest deeper (h4,
    h5, h6). Depth 4+ (TOC nesting deeper than Markdown headings support)
    falls back to a bold line."""
    level = depth + 3
    if level <= 6:
        lines.append(f"{'#' * level} {title}")
        lines.append("")
    else:
        lines.append(f"**{title}**")
        lines.append("")


def _emit_entry(h: dict, index: Optional["_PositionIndex"], lines: list) -> None:
    text = (h.get("text") or "").replace("\n", " ")
    offset = h.get("offset")
    code = _classify_code(index, offset, len(h.get("text") or "")) if index is not None else None

    if code and code[0] == "block":
        _, lang = code
        lines.append(f"```{lang}")
        lines.append(text)
        lines.append("```")
        if h.get("note"):
            lines.append(f"**Note:** {h['note']}")
        lines.append("")
    elif code and code[0] == "inline":
        lines.append(f"- `{text}`")
        if h.get("note"):
            lines.append(f"    - **Note:** {h['note']}")
    else:
        lines.append(f"- {text}")
        if h.get("note"):
            lines.append(f"    - **Note:** {h['note']}")


def _render_section(entry, depth: int, heading_title: Optional[str],
                     items_by_key, index: Optional["_PositionIndex"], lines: list) -> None:
    """Recursively render one TOC entry's heading (if it or any descendant
    has items) followed by its own items, then its children's sections."""
    if not _subtree_has_items(entry, items_by_key):
        return
    _emit_heading(heading_title if heading_title is not None else entry.title, depth, lines)
    for h in items_by_key.get(id(entry), []):
        _emit_entry(h, index, lines)
    lines.append("")
    for child in entry.children:
        _render_section(child, depth + 1, None, items_by_key, index, lines)


# --- Rendering ---

def render_book_notes(book, highlights: list) -> str:
    """Render the full Markdown document for one book.

    Entries are grouped by chapter in spine (book) order rather than
    creation order -- matching /api/export and the Library page -- so the
    note reads front-to-back like the book and the last entry is always the
    last highlight in the book, not just the most recently created one.

    Within a chapter, headings nest following the book's own TOC hierarchy:
    the TOC entry covering the whole chapter is an h3, and each nested TOC
    level below it goes one heading level deeper (h4, h5, h6), falling back
    to a bold line past that. A highlight/note is placed under the deepest
    TOC section whose anchor position (in the chapter's flattened text) is
    at or before the highlight's own offset; one with no offset -- older
    data, or any standalone note -- goes directly under the chapter's own
    h3 heading, exactly as before. A section heading is printed even with
    no highlights of its own, as long as some nested descendant has one, so
    the outline never silently skips a level. Entries within a section are
    chronological.
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
            chapter = book.spine[ch_idx] if 0 <= ch_idx < len(book.spine) else None
            href = chapter.href if chapter else None
            chapter_entry = _find_chapter_entry(book.toc, href) if href else None
            fallback_title = items[0].get("chapter_title") or f"Section {ch_idx + 1}"

            index = _build_position_index(chapter.content) if chapter is not None else None

            if chapter_entry is None:
                # No TOC entry covers this chapter (malformed/fallback TOC,
                # or a stale chapter index) -- flat rendering, same as before.
                lines += [f"### {fallback_title}", ""]
                for h in items:
                    _emit_entry(h, index, lines)
                lines.append("")
                continue

            items_by_key = defaultdict(list)
            for h in items:
                offset = h.get("offset")
                target = chapter_entry
                if offset is not None:
                    chain = _deepest_section(chapter_entry.children, href, offset,
                                              index.anchor_offset)
                    if chain:
                        target = chain[-1]
                items_by_key[id(target)].append(h)

            _render_section(chapter_entry, 0, fallback_title, items_by_key, index, lines)

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
