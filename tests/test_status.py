def test_setting_status_read_is_reflected_in_state(client, fixture_book):
    book_id, _book, _lengths = fixture_book

    resp = client.post(f"/api/status/{book_id}", json={"status": "read"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "read"

    state = client.get(f"/api/state/{book_id}").json()
    assert state["status"] == "read"


def test_setting_status_to_null_resets_progress_and_chapter_progress(client, fixture_book):
    book_id, _book, _lengths = fixture_book

    client.post(f"/api/progress/{book_id}", json={"chapter_index": 0, "scroll_fraction": 0.7})
    client.post(f"/api/status/{book_id}", json={"status": "read"})

    resp = client.post(f"/api/status/{book_id}", json={"status": None})
    assert resp.status_code == 200
    body = resp.json()
    assert body["progress"] == 0.0
    assert body["status"] == "new"

    state = client.get(f"/api/state/{book_id}").json()
    assert state["progress"] == 0.0
    assert state["chapter_progress"] == {}


def test_invalid_status_value_is_rejected(client, fixture_book):
    book_id, _book, _lengths = fixture_book

    resp = client.post(f"/api/status/{book_id}", json={"status": "archived"})
    assert resp.status_code == 400
