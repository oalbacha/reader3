import os

import server
import upload_jobs
from tests.booklib import write_minimal_epub


def _epub_bytes(tmp_path, name="upload_source.epub", **kwargs):
    path = write_minimal_epub(str(tmp_path / name), **kwargs)
    with open(path, "rb") as f:
        return f.read()


def _upload(client, filename, content, confirm=None):
    data = {"confirm": "true"} if confirm else {}
    return client.post(
        "/api/upload",
        files={"file": (filename, content, "application/epub+zip")},
        data=data,
    )


def test_upload_new_book_processes_and_appears_in_library(client, books_dir, tmp_path):
    content = _epub_bytes(tmp_path, title="Uploaded Book")

    resp = _upload(client, "uploaded_book.epub", content)
    assert resp.status_code == 202
    body = resp.json()
    assert body["book_id"] == "uploaded_book_data"

    status = client.get(f"/api/upload-status/{body['job_id']}").json()
    assert status["status"] == "done"
    assert status["book_id"] == "uploaded_book_data"

    state = client.get("/api/state/uploaded_book_data")
    assert state.status_code == 200


def test_upload_saves_original_epub_in_library(client, books_dir, tmp_path):
    content = _epub_bytes(tmp_path, title="Kept Source")
    _upload(client, "kept_source.epub", content)

    assert os.path.exists(os.path.join(str(books_dir), "kept_source.epub"))


def test_upload_rejects_non_epub_extension(client, books_dir, tmp_path):
    resp = client.post(
        "/api/upload",
        files={"file": ("notabook.txt", b"just text", "text/plain")},
    )
    assert resp.status_code == 400


def test_upload_rejects_oversized_file(client, books_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(upload_jobs, "MAX_UPLOAD_BYTES", 10)
    resp = _upload(client, "toobig.epub", b"x" * 100)
    assert resp.status_code == 400


def test_upload_collision_without_confirm_is_rejected(client, books_dir, tmp_path):
    content = _epub_bytes(tmp_path, title="Original")
    first = _upload(client, "collide.epub", content)
    assert first.status_code == 202

    second = _upload(client, "collide.epub", content)
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["book_id"] == "collide_data"
    assert detail["title"] == "Original"


def test_upload_collision_with_confirm_reprocesses_in_place_and_keeps_state(client, books_dir, tmp_path):
    original = _epub_bytes(tmp_path, name="v1.epub", title="Original Title")
    first = _upload(client, "reprocess_me.epub", original)
    book_id = first.json()["book_id"]

    client.post(f"/api/highlights/{book_id}", json={"chapter": 0, "text": "keep me"})
    client.post(f"/api/progress/{book_id}", json={"chapter_index": 0, "scroll_fraction": 0.5})

    updated = _epub_bytes(tmp_path, name="v2.epub", title="Updated Title")
    second = _upload(client, "reprocess_me.epub", updated, confirm=True)
    assert second.status_code == 202

    status = client.get(f"/api/upload-status/{second.json()['job_id']}").json()
    assert status["status"] == "done"

    state = client.get(f"/api/state/{book_id}").json()
    assert len(state["highlights"]) == 1
    assert state["highlights"][0]["text"] == "keep me"


def test_upload_reprocess_updates_book_metadata(client, books_dir, tmp_path):
    v1 = _epub_bytes(tmp_path, name="v1.epub", title="Before")
    first = _upload(client, "changes.epub", v1)
    book_id = first.json()["book_id"]

    v2 = _epub_bytes(tmp_path, name="v2.epub", title="After")
    _upload(client, "changes.epub", v2, confirm=True)

    # /read/{book_id}/0 (not the bare /read/{book_id} redirect, which has a
    # pre-existing unrelated bug -- see final report) still confirms the
    # reprocess actually happened by checking the reader page picked up v2's title.
    resp = client.get(f"/read/{book_id}/0")
    assert resp.status_code == 200
    assert "After" in resp.text


def test_upload_status_for_unknown_job_is_404(client, books_dir):
    resp = client.get("/api/upload-status/does-not-exist")
    assert resp.status_code == 404


def test_upload_job_marks_error_for_corrupt_epub(client, books_dir, tmp_path):
    resp = _upload(client, "corrupt.epub", b"not a real epub file")
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    status = client.get(f"/api/upload-status/{job_id}").json()
    assert status["status"] == "error"
    assert status["error"]


def test_failed_new_upload_removes_orphaned_source_file(client, books_dir, tmp_path):
    """A brand-new upload that fails never gets a book_dir/book.pkl, so
    nothing in the library scan or UI can ever surface or clean up its source
    .epub -- it must not be left behind to accumulate."""
    resp = _upload(client, "junk.epub", b"not a real epub file")
    job_id = resp.json()["job_id"]
    client.get(f"/api/upload-status/{job_id}")  # background task runs synchronously under TestClient

    assert not os.path.exists(os.path.join(str(books_dir), "junk.epub"))


def test_swap_failure_marks_job_error_and_clears_in_flight_guard(client, books_dir, tmp_path, monkeypatch):
    """If the post-processing swap itself fails (disk full, permission error)
    after process_epub/save_to_pickle already succeeded in the scratch dir,
    the job must still end up 'error' (not stuck 'processing' forever) and
    the in-flight guard must still release -- otherwise every future upload
    for this book_id 429s until the server restarts."""
    real_move = server.shutil.move

    def boom(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(server.shutil, "move", boom)

    content = _epub_bytes(tmp_path, title="Swap Failure")
    resp = _upload(client, "swap_fail.epub", content)
    job_id = resp.json()["job_id"]

    status = client.get(f"/api/upload-status/{job_id}").json()
    assert status["status"] == "error"

    monkeypatch.setattr(server.shutil, "move", real_move)
    retry = _upload(client, "swap_fail.epub", content)
    assert retry.status_code == 202


def test_failed_reprocess_leaves_existing_book_dir_untouched(client, books_dir, tmp_path, monkeypatch):
    """A confirmed reprocess that fails mid-parse must not corrupt the
    already-working book: process_epub() wipes book_dir/images/ before it has
    finished validating the new file, so processing has to happen in a
    scratch directory and only swap in on success."""
    good = _epub_bytes(tmp_path, name="v1.epub", title="Still Good")
    first = _upload(client, "fragile.epub", good)
    book_id = first.json()["book_id"]
    book_pkl_path = os.path.join(str(books_dir), book_id, "book.pkl")
    with open(book_pkl_path, "rb") as f:
        before = f.read()

    def boom(*args, **kwargs):
        raise ValueError("simulated parse failure")

    monkeypatch.setattr(server, "process_epub", boom)

    second = _upload(client, "fragile.epub", good, confirm=True)
    assert second.status_code == 202
    status = client.get(f"/api/upload-status/{second.json()['job_id']}").json()
    assert status["status"] == "error"

    with open(book_pkl_path, "rb") as f:
        after = f.read()
    assert after == before


def test_reprocessing_an_archived_book_leaves_it_archived(client, books_dir, tmp_path):
    """Reprocess-in-place must never touch state.json's archived flag --
    the upload UI (#16) relies on /api/book/{id} still reporting archived
    after a confirmed reprocess to avoid showing it on the main library page."""
    original = _epub_bytes(tmp_path, name="v1.epub", title="Original")
    first = _upload(client, "archived_book.epub", original)
    book_id = first.json()["book_id"]

    client.post(f"/api/archive/{book_id}", json={"archived": True})

    updated = _epub_bytes(tmp_path, name="v2.epub", title="Updated")
    second = _upload(client, "archived_book.epub", updated, confirm=True)
    assert second.status_code == 202
    status = client.get(f"/api/upload-status/{second.json()['job_id']}").json()
    assert status["status"] == "done"

    html = client.get(f"/api/book/{book_id}").text
    assert 'data-archived="true"' in html
    assert 'data-title="Updated"' in html


def test_upload_for_book_id_already_in_flight_is_rejected(client, books_dir, tmp_path):
    """Guards against two concurrent uploads (e.g. a double-submitted
    confirm, or two tabs) racing process_epub/save_to_pickle against the same
    book_dir at once."""
    upload_jobs.try_create_job(book_id="busy_data", filename="busy.epub")

    content = _epub_bytes(tmp_path, title="Busy")
    resp = _upload(client, "busy.epub", content)
    assert resp.status_code == 429
