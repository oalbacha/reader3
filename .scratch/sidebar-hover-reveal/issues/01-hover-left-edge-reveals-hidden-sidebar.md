# 01: Toggle the reader sidebar fully off, with hover-to-reveal on the left edge

**What to build:** The reader's sidebar toggle (`#sidebar-toggle` in `templates/reader.html`) currently only collapses the sidebar to a 56px icon-only rail (`#sidebar.collapsed`) — it's never fully hidden. Add a fully-hidden state: toggling off shrinks the sidebar (and its resizer) to zero width, giving the chapter content the full screen width. While hidden, hovering the mouse near the left edge of the viewport temporarily reveals the sidebar as an overlay (not shifting the document layout); moving the mouse away collapses it back to hidden. Clicking the toggle again while it's revealed-by-hover pins it back to the normal, always-visible docked state.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] The sidebar toggle gains a third state — fully hidden (0 width) — distinct from today's icon-only `collapsed` state and the normal docked state.
- [ ] While fully hidden, moving the mouse to within a small margin of the left edge of the viewport (e.g. the leftmost ~12px) reveals the sidebar as an overlay above the chapter content, without reflowing/shrinking the reading column.
- [ ] Moving the mouse away from the revealed sidebar (and not over it) hides it again after a short delay, matching common auto-hide-panel behavior (avoids flicker when moving directly from the edge onto the sidebar itself).
- [ ] The hover-revealed sidebar shows the full sidebar contents (TOC, notes, search — same as the normal docked state), not the icon-only rail.
- [ ] Clicking the sidebar toggle button while the sidebar is hover-revealed pins it open in the normal docked state (same as manually expanding today).
- [ ] The chosen state (docked / icon-collapsed / fully-hidden) persists across reloads via `localStorage`, consistent with the existing `sidebarCollapsed`/`sidebarWidth` persistence.
- [ ] Existing icon-only collapsed state and its behavior (click to expand, resizer hidden while collapsed) are unchanged — this adds a new state, it doesn't replace the existing one.
- [ ] Hovering the left edge does nothing when the sidebar is already docked or icon-collapsed (only applies in the new fully-hidden state).

## Comments

