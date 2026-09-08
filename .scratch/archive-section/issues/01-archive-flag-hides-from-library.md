# 01: Archive a book to hide it from the library

**What to build:** Each book's `state.json` gets an `archived` flag. A new "Archive" action in the book's "..." menu (alongside the existing "Remove" and "Mark read/unread" actions) sets this flag, and archived books immediately drop out of the main library list. This is a distinct, reversible action from the existing "Remove" (which soft-deletes into `.trash/` with no browse/restore UI) — archiving never touches the book's folder, progress, highlights, or status.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [x] `state.json` supports an `archived` boolean (default `false` for existing and new books).
- [x] A new endpoint (or extension of an existing one) toggles the flag for a given book.
- [x] The library listing excludes archived books entirely (not grayed out, not shown behind a filter toggle).
- [x] The book's "..." menu gains an "Archive" action distinct from "Remove"; archiving a book does not move its folder, clear its progress, or touch its highlights/status.
- [x] Archiving a book removes it from the library view without a full page reload (matching how the existing menu actions update the card in place).
- [x] Toggling the flag back to `false` (e.g. via direct API call, ahead of ticket 02's UI) makes the book reappear in the library with its prior progress/highlights/status intact.

## Comments

Implemented in `server.py` and `templates/library.html`:

- `default_state()` now includes `"archived": False`; `load_state()` coerces any stored value to `bool` so existing books without the key default to `false`.
- New `POST /api/archive/{book_id}` endpoint (body: `{"archived": bool}`), modeled on the existing `/api/status/{book_id}` pattern. It only flips `state["archived"]` and saves — it never touches the book's folder, `progress`, `highlights`, or `status`, and is entirely separate from `/api/remove` (untouched).
- The book-listing loop (extracted into a shared `_scan_books(archived: bool)` helper, reused by ticket 02's `/archive` route) now skips any book whose `archived` flag doesn't match, so archived books are fully excluded from `/` rather than filtered client-side.
- The card "..." menu gained an "Archive Book" item, wired to `POST /api/archive` with `archived: true`; on success the card is removed from the DOM in place (`card.remove()`), matching the existing Remove/Mark-read pattern — no page reload.
- Verified manually end-to-end (see ticket 02's comments for the shared round-trip verification, run together since 02 was implemented immediately after).
