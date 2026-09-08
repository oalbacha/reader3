# 02: Archive section accessible from the home page, with restore-to-library

**What to build:** A link on the home/library page (e.g. "Archive (N)", showing the archived count) opens a page listing every archived book, using the same card style as the library. Each archived card has a "Move to Library" action that clears the `archived` flag (from ticket 01) and sends the book back to the main library list. Archiving a book from the library and then restoring it from the Archive page round-trips it back exactly where it left off (progress, highlights, status all intact, since ticket 01 guarantees archiving never touches them).

**Blocked by:** 01 (needs the `archived` flag and toggle endpoint)

**Status:** ready-for-agent

- [x] The home page shows a link/button to the Archive section, visible whenever at least one book is archived (or always visible with a count, either is fine — pick whichever matches the existing UI's handling of empty states).
- [x] The Archive page lists all and only archived books, in the same card style as the library (title, author, cover, progress/status).
- [x] Each archived card has a "Move to Library" action that clears the `archived` flag; the book disappears from the Archive page and reappears in the main library.
- [x] Round-trip check: archive a book from the library, confirm it's on the Archive page, restore it, confirm it's back in the library with its prior reading progress, status, and highlights unchanged.
- [x] The Archive page is reachable only from the home page (no separate top-level route users would need to know about ahead of time).

## Comments

Implemented in `server.py` and `templates/library.html` (same commit as ticket 01):

- Chose "visible only when N > 0" for the home-page link: `/` renders `Archive ({{ archived_count }})` next to the "Library" heading only when `archived_count > 0`, matching the library's existing pattern of omitting UI for empty states (e.g. the "no processed books" message only appears when the list is empty).
- New `GET /archive` route (`archive_view`) reuses `templates/library.html` with an `archive_mode=True` context flag and the same `_scan_books()` helper (filtered to `archived=True`), so the archive page renders the identical card markup/CSS as the library — title, author (via cover alt-less card), cover image, and progress/status badge are all identical code paths, just a different filter.
- Each archived card's "..." menu shows "Move to Library" instead of "Archive Book" (`{% if archive_mode %}` branch); it POSTs `{"archived": false}` to the same `/api/archive/{book_id}` endpoint from ticket 01, then removes the card from the DOM — same reversible, no-side-effects toggle, just the opposite direction.
- The archive page also has a "← Library" link back to `/`; `/archive` itself is otherwise only linked from the home page's counter link — no nav/menu elsewhere references it.

Manual verification (server run from the worktree, `python3 server.py` executed directly so the pickled `Book` class resolves under `__main__` — running via `uvicorn server:app` from the CLI breaks unpickling because `book.pkl` was created while the module was `__main__`, this is pre-existing and unrelated to this change):

- Copied three real book folders from the main checkout into the worktree for testing (`Deep Work by Cal Newport_data` — no prior `state.json`; `The Lean Startup by Eric Ries_data` — progress only; `Customer Success Nick Mehta_data` — progress 0.79, 779 highlights, used for the round-trip check), and removed them again after testing.
- `GET /` listed all three books; `POST /api/archive/<Customer Success...>` with `{"archived": true}` returned `{"archived": true}`, after which `GET /` no longer listed it (down to 2 books) and `GET /` showed a new "Archive (1)" link, and `GET /archive` listed exactly that one book with the "Move to Library" menu item.
- `POST /api/archive/<...>` with `{"archived": false}` restored it: `GET /archive` returned to "No archived books.", `GET /` listed all three again, and the "Archive (1)" link disappeared.
- Round-trip integrity: diffed the book's `state.json` before archiving vs. after the archive+restore round trip at the JSON-field level — `status`, `progress`, `last_chapter`, `last_scroll`, and all 779 `highlights` entries were identical. The only difference in the file is the addition of the new `"archived": false` key (expected/required by ticket 01, since `state.json` must now support the flag) — not a literal byte-for-byte identical file, but every pre-existing field is unchanged.
- Edge cases: `POST /api/archive/<nonexistent>` → 404; `POST /api/archive/<book>` with an empty body (missing required `archived` field) → 422; `GET /api/state/<book>` includes `"archived": false` for a book with no prior `state.json`.
- Not verified: real browser click-through of the "..." menu (only the underlying fetch/DOM-removal logic was exercised via curl + reading the rendered HTML/JS, since this environment has no browser). The JS mirrors the existing, working Remove-book handler pattern exactly.
