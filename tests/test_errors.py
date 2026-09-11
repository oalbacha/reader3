import pytest

# books_dir is included (unused directly) purely to isolate BOOKS_DIR/caches
# from the developer's real book library, even though these requests target
# a book id that was never written to disk.


@pytest.mark.parametrize("make_request", [
    lambda c, bid: c.get(f"/api/state/{bid}"),
    lambda c, bid: c.post(f"/api/progress/{bid}", json={"chapter_index": 0, "scroll_fraction": 0.1}),
    lambda c, bid: c.post(f"/api/status/{bid}", json={"status": "read"}),
    lambda c, bid: c.post(f"/api/archive/{bid}", json={"archived": True}),
    lambda c, bid: c.post(f"/api/highlights/{bid}", json={"chapter": 0, "text": "hi"}),
    lambda c, bid: c.post(f"/api/notes/{bid}", json={"chapter": 0, "text": "hi"}),
    lambda c, bid: c.delete(f"/api/highlights/{bid}/h_whatever"),
    lambda c, bid: c.get(f"/api/export/{bid}"),
])
def test_unknown_book_id_is_404(client, books_dir, book_id, make_request):
    resp = make_request(client, book_id)
    assert resp.status_code == 404
