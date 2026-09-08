# 01: Nest Obsidian export headings by TOC hierarchy (chapter → sub-chapter)

**What to build:** The Obsidian export currently groups every highlight/note under one flat heading per physical chapter file. Instead, it should nest headings following the book's own table-of-contents hierarchy: the top-level TOC entry covering a chapter is an h3, its nested sub-sections are h4, then h5, then h6 (TOC depths 0–3). A sub-section nested deeper than that (rare) renders as a **bold title** on its own line followed by a blank line, since Markdown/Obsidian headings stop at h6. A section heading prints even if it has no highlights directly under it, as long as some descendant section does, so the outline stays structurally sound. An entry with no usable position information (older highlights captured before position offsets existed, or standalone notes, which are never anchored to a specific passage) groups directly under its chapter's h3 heading only, exactly as today — no attempt at deeper placement.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [x] Highlights/notes are grouped and headed according to the TOC hierarchy of the book they belong to, not just the physical spine file.
- [x] Chapter-level TOC entries render as h3; each nested level below that renders one heading level deeper (h4, h5, h6) up to TOC depth 3.
- [x] TOC nesting deeper than depth 3 renders as a bold section title followed by a blank line, not a heading.
- [x] An ancestor section heading prints even when it has no highlights of its own, as long as a nested descendant section does — the resulting document never jumps from a shallower heading straight past an intermediate level without printing it.
- [x] A highlight or note with no position offset (including all standalone notes) is placed directly under its chapter's h3 heading, matching current behavior exactly.
- [x] Verify against at least one real book in the library with 2+ levels of TOC nesting (e.g. a technical book with Chapter → Section → Subsection) and confirm the rendered Markdown headings match that book's actual TOC structure.
- [x] Existing single-level books (flat TOC, no nesting) render exactly as they do today — only chapter-level h3 headings, no regression.

## Comments

Implemented in `obsidian_sync.py`'s `render_book_notes()`. Added a shared `_build_position_index()` helper that reparses each chapter's `.content` HTML and rebuilds its flattened text word-by-word (mirroring `reader3.extract_plain_text()`), recording each element `id="..."` attribute's start offset. `_find_chapter_entry()` locates the TOC entry covering a chapter (same search order as `server.py`'s `chapter_title()`); `_deepest_section()` walks that entry's children to find the deepest TOC section whose anchor offset is `<=` a highlight's offset. `_render_section()` recurses the TOC subtree depth-first, printing a heading (h3..h6, then bold past depth 3) whenever a node or any descendant has assigned items, then that node's own items, then its children.

Verified with throwaway scripts (not committed) against `Programming TypeScript by Boris Cherny_data/book.pkl` (3 real TOC levels: chapter → "The ABCs of Types" → "any"/"unknown"/"boolean" — confirmed rendered headings h3→h4→h5 match the book's actual TOC, including with that book's real previously-captured highlights), a fabricated 6-level-deep synthetic book (confirmed h3→h4→h5→h6 then bold fallback at depth 4 and 5), `The Death of Ivan Ilych_data` (a real, genuinely flat TOC book — confirmed only `### heading`, no nesting, no regression), and a fabricated flat two-chapter book plus a no-highlights book (both regression-clean). Also explicitly tested the "ancestor prints with zero own items" case (a highlight nested only under "boolean" still correctly prints the empty "The ABCs of Types" h4 above it) and a stale/out-of-range chapter index (degrades gracefully to the old `Section N` fallback without crashing).
