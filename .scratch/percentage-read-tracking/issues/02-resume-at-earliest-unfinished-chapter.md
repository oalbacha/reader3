# 02: "Continue" resumes at your earliest unfinished chapter, not the last chapter you merely opened

**What to build:** The library's "Continue" button and the reader's scroll-restore-on-load feature use the per-chapter progress map (from ticket 01) to find the lowest-indexed chapter that isn't yet fully read, and reopen it at that chapter's own saved scroll position. Previewing a later chapter via the table of contents (or prev/next) no longer moves your resume point past chapters you haven't finished reading. Directly navigating to any chapter still opens exactly that chapter — this only changes what "Continue" / library resume targets.

**Blocked by:** 01 (needs the per-chapter progress map to know which chapters are unfinished)

**Status:** ready-for-agent

- [x] "Fully read" for a chapter uses the same threshold already applied to the book-level read/in_progress/new status (i.e. a chapter counts as done once its fraction reaches that threshold, not only at exactly 1.0).
- [x] Resume chapter = the lowest-indexed chapter whose fraction is below that threshold; if every chapter is at/above it, resume at the last chapter.
- [x] The scroll position restored within the resume chapter matches that chapter's own stored fraction.
- [x] Reproduce the reported bug and confirm it's fixed: read through chapter 3 (leaving it unfinished), open chapter 9 once via the TOC without reading chapters 4–8, then use "Continue" from the library — it reopens chapter 3 at its saved scroll position, not chapter 9.
- [x] A chapter you've already fully read is never re-selected as the resume point just because you scroll back into it later (resume only ever looks for the earliest *unfinished* chapter).
- [x] Manual navigation (TOC, prev/next links) is unaffected by this change.

## Comments

Implemented `resume_position(book_id, state)` in `server.py`, reusing the existing `READ_THRESHOLD = 0.95` constant (the same one `derive_status()` already used for book-level status) rather than requiring exact `1.0`. It scans the per-chapter map from ticket 01 in chapter order and returns the first chapter below the threshold (with that chapter's own stored fraction), or the last chapter if every chapter is at/above it.

Wired into three places: `library_view()` — the "Continue" card's `data-href` now uses `book.resume_chapter` (via `templates/library.html`'s `{% set start = book.resume_chapter if book.status == "in_progress" else 0 %}`) instead of `book.last_chapter`; `read_chapter()` — passes new `resume_chapter`/`resume_scroll` template fields, and `templates/reader.html`'s `restoreScroll()` now compares `CHAPTER_INDEX === RESUME_CHAPTER` (replacing the old `LAST_CHAPTER`/`LAST_SCROLL` comparison, which are now unused and were removed) so it only restores scroll when landing on the earliest-unfinished chapter; `get_state()` (`/api/state/{book_id}`) also now returns `resume_chapter`/`resume_scroll` for API consumers. `last_chapter`/`last_scroll` are kept in the state schema and API responses (still reflecting whatever was actually last opened) since other code/tests may still reference them, per the constraint not to remove fields without checking every reference.

Verified against a real book's data via the live server: after marking chapters 0–2 fully read, chapter 3 partially read (60%), and chapter 9 merely previewed (10%), `/api/state` returned `resume_chapter: 3, resume_scroll: 0.6`, and the library page's `data-href` pointed at `/read/.../3`. Directly requesting `/read/{book_id}/9` still opened chapter 9 (`CHAPTER_INDEX = 9`) while `RESUME_CHAPTER` stayed `3`, confirming manual navigation is unaffected and only the reader's own scroll-restore-on-load and the library's Continue target changed. Marking chapter 3 fully read afterward correctly advanced the resume target past it to the next actually-unfinished chapter, confirming a finished chapter is never re-selected just because it's later revisited.
