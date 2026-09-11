def test_highlight_create_and_delete_roundtrip(client, fixture_book):
    book_id, _book, _lengths = fixture_book

    resp = client.post(f"/api/highlights/{book_id}", json={
        "chapter": 0, "text": "Hello world", "note": "nb", "offset": 5,
    })
    assert resp.status_code == 200
    highlight = resp.json()
    assert highlight["chapter"] == 0
    assert highlight["chapter_title"] == "Chapter 1"
    assert highlight["text"] == "Hello world"
    assert highlight["note"] == "nb"
    assert highlight["kind"] == "highlight"
    assert highlight["id"].startswith("h_")

    state = client.get(f"/api/state/{book_id}").json()
    assert [h["id"] for h in state["highlights"]] == [highlight["id"]]

    resp = client.delete(f"/api/highlights/{book_id}/{highlight['id']}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    state = client.get(f"/api/state/{book_id}").json()
    assert state["highlights"] == []


def test_deleting_an_unknown_highlight_id_is_404(client, fixture_book):
    book_id, _book, _lengths = fixture_book

    resp = client.delete(f"/api/highlights/{book_id}/h_does_not_exist")
    assert resp.status_code == 404


def test_note_create_roundtrip(client, fixture_book):
    book_id, _book, _lengths = fixture_book

    resp = client.post(f"/api/notes/{book_id}", json={"chapter": 1, "text": "A standalone note"})
    assert resp.status_code == 200
    note = resp.json()
    assert note["chapter"] == 1
    assert note["chapter_title"] == "Chapter 2"
    assert note["text"] == "A standalone note"
    assert note["kind"] == "note"
    assert note["id"].startswith("n_")

    state = client.get(f"/api/state/{book_id}").json()
    assert [h["id"] for h in state["highlights"]] == [note["id"]]
