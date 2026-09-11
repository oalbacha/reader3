import server

# Fixture book chapters are 100/200/300 chars (see tests/booklib.py), so the
# math below is checkable by hand.


def test_progress_never_regresses_a_chapters_stored_fraction(client, fixture_book):
    book_id, _book, _lengths = fixture_book

    resp = client.post(f"/api/progress/{book_id}", json={"chapter_index": 0, "scroll_fraction": 0.6})
    assert resp.status_code == 200
    high_water = resp.json()["progress"]
    assert high_water == 100 * 0.6 / 600

    # A later, lower fraction for the same chapter must not regress it.
    resp = client.post(f"/api/progress/{book_id}", json={"chapter_index": 0, "scroll_fraction": 0.2})
    assert resp.status_code == 200
    assert resp.json()["progress"] == high_water

    state = client.get(f"/api/state/{book_id}").json()
    assert state["chapter_progress"]["0"] == 0.6


def test_overall_progress_is_length_weighted_across_chapters(client, fixture_book):
    book_id, _book, _lengths = fixture_book

    client.post(f"/api/progress/{book_id}", json={"chapter_index": 0, "scroll_fraction": 1.0})
    resp = client.post(f"/api/progress/{book_id}", json={"chapter_index": 1, "scroll_fraction": 0.5})

    # read = 100*1.0 + 200*0.5 + 300*0.0 = 200; total = 600
    assert resp.json()["progress"] == 200 / 600


def test_resume_position_is_the_earliest_chapter_below_threshold(client, fixture_book):
    book_id, _book, _lengths = fixture_book
    assert server.READ_THRESHOLD == 0.95

    client.post(f"/api/progress/{book_id}", json={"chapter_index": 0, "scroll_fraction": 1.0})
    client.post(f"/api/progress/{book_id}", json={"chapter_index": 1, "scroll_fraction": 0.9})

    state = client.get(f"/api/state/{book_id}").json()
    assert state["resume_chapter"] == 1
    assert state["resume_scroll"] == 0.9


def test_resume_position_falls_back_to_last_chapter_when_all_are_done(client, fixture_book):
    book_id, _book, _lengths = fixture_book

    for idx in range(3):
        client.post(f"/api/progress/{book_id}", json={"chapter_index": idx, "scroll_fraction": 1.0})

    state = client.get(f"/api/state/{book_id}").json()
    assert state["resume_chapter"] == 2
    assert state["resume_scroll"] == 1.0
