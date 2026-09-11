def test_get_book_card_renders_same_markup_the_library_grid_uses(client, fixture_book):
    """GET /api/book/{id} renders via the same render_book_card macro as the
    library grid (templates/_book_card.html) -- assert on the same
    data-attributes a server-rendered card in the grid would carry."""
    book_id, _book, _lengths = fixture_book

    resp = client.get(f"/api/book/{book_id}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]

    html = resp.text
    assert f'data-book-id="{book_id}"' in html
    assert 'data-title="Fixture Book"' in html
    assert 'data-status="new"' in html
    assert f'data-href="/read/{book_id}/0"' in html
    assert "new-badge" in html and "NEW" in html
    assert 'data-role="menu-toggle"' in html


def test_get_book_card_reflects_progress_and_highlights(client, fixture_book):
    book_id, _book, _lengths = fixture_book

    client.post(f"/api/progress/{book_id}", json={"chapter_index": 0, "scroll_fraction": 0.5})
    client.post(f"/api/highlights/{book_id}", json={"chapter": 0, "text": "hello"})

    html = client.get(f"/api/book/{book_id}").text
    assert 'data-status="in_progress"' in html
    assert "hello" in html  # inside the forceescaped data-highlights JSON blob


def test_get_book_card_404_for_unknown_book(client, books_dir):
    resp = client.get("/api/book/does-not-exist_data")
    assert resp.status_code == 404


def test_get_book_card_marks_archived_state_for_upload_ui_to_check(client, fixture_book):
    """The upload UI (#16) uses this to avoid splicing an archived book's
    card into the non-archived library page after a confirmed reprocess."""
    book_id, _book, _lengths = fixture_book

    html = client.get(f"/api/book/{book_id}").text
    assert 'data-archived="false"' in html

    client.post(f"/api/archive/{book_id}", json={"archived": True})

    html = client.get(f"/api/book/{book_id}").text
    assert 'data-archived="true"' in html
