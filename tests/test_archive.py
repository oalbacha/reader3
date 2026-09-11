def test_archive_flag_toggles_independently_of_progress_status_and_highlights(client, fixture_book):
    book_id, _book, _lengths = fixture_book

    client.post(f"/api/progress/{book_id}", json={"chapter_index": 0, "scroll_fraction": 0.5})
    client.post(f"/api/status/{book_id}", json={"status": "read"})
    client.post(f"/api/highlights/{book_id}", json={"chapter": 0, "text": "keep me"})
    baseline = client.get(f"/api/state/{book_id}").json()

    resp = client.post(f"/api/archive/{book_id}", json={"archived": True})
    assert resp.status_code == 200
    assert resp.json() == {"archived": True}

    state = client.get(f"/api/state/{book_id}").json()
    assert state["archived"] is True
    assert state["progress"] == baseline["progress"]
    assert state["status"] == baseline["status"]
    assert state["highlights"] == baseline["highlights"]

    resp = client.post(f"/api/archive/{book_id}", json={"archived": False})
    assert resp.json() == {"archived": False}
    state = client.get(f"/api/state/{book_id}").json()
    assert state["archived"] is False
