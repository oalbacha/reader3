import json
import os


def _write_legacy_state(books_dir, book_id, last_chapter, last_scroll):
    path = os.path.join(str(books_dir), book_id, "state.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"last_chapter": last_chapter, "last_scroll": last_scroll}, f)
    return path


def test_legacy_state_is_migrated_to_chapter_progress_once(client, books_dir, fixture_book):
    book_id, _book, _lengths = fixture_book
    path = _write_legacy_state(books_dir, book_id, last_chapter=1, last_scroll=0.4)

    resp = client.get(f"/api/state/{book_id}")
    assert resp.status_code == 200
    body = resp.json()

    # Chapters before the old last_chapter read as fully done; the old
    # last_chapter itself is seeded with the old last_scroll fraction.
    assert body["chapter_progress"] == {"0": 1.0, "1": 0.4}
    # read = 100*1.0 + 200*0.4 + 300*0.0 = 180; total = 600
    assert body["progress"] == 180 / 600

    with open(path, "r", encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["chapter_progress"] == {"0": 1.0, "1": 0.4}
    assert "last_chapter" not in on_disk
    assert "last_scroll" not in on_disk

    mtime_after_migration = os.path.getmtime(path)

    # A second load must not re-migrate (and therefore must not rewrite the
    # file again).
    resp = client.get(f"/api/state/{book_id}")
    assert resp.json()["chapter_progress"] == {"0": 1.0, "1": 0.4}
    assert os.path.getmtime(path) == mtime_after_migration
