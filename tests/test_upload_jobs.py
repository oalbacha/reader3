import upload_jobs


def test_derive_book_id_matches_reader3_cli_convention():
    assert upload_jobs.derive_book_id("dracula.epub") == "dracula_data"


def test_derive_book_id_preserves_spaces_like_existing_library_folders():
    assert upload_jobs.derive_book_id("A Philosophy of Software Design.epub") == \
        "A Philosophy of Software Design_data"


def test_derive_book_id_strips_directory_components():
    assert upload_jobs.derive_book_id("../../etc/dracula.epub") == "dracula_data"


def test_derive_book_id_empty_for_extensionless_stem():
    assert upload_jobs.derive_book_id(".epub") == ""


def test_validate_filename_accepts_epub():
    assert upload_jobs.validate_filename("dracula.epub") is None


def test_validate_filename_rejects_non_epub():
    assert upload_jobs.validate_filename("dracula.pdf") is not None


def test_validate_filename_rejects_empty():
    assert upload_jobs.validate_filename("") is not None


def test_validate_filename_rejects_degenerate_stem():
    assert upload_jobs.validate_filename(".epub") is not None


def test_job_lifecycle_starts_processing():
    job = upload_jobs.try_create_job(book_id="dracula_data", filename="dracula.epub")
    fetched = upload_jobs.get_job(job.id)
    assert fetched.status == "processing"
    assert fetched.book_id == "dracula_data"
    assert fetched.error is None


def test_mark_done_updates_status():
    job = upload_jobs.try_create_job(book_id="dracula_data", filename="dracula.epub")
    upload_jobs.mark_done(job.id)
    assert upload_jobs.get_job(job.id).status == "done"


def test_mark_error_records_message():
    job = upload_jobs.try_create_job(book_id="dracula_data", filename="dracula.epub")
    upload_jobs.mark_error(job.id, "not a valid epub")
    fetched = upload_jobs.get_job(job.id)
    assert fetched.status == "error"
    assert fetched.error == "not a valid epub"


def test_get_job_returns_none_for_unknown_id():
    assert upload_jobs.get_job("does-not-exist") is None


def test_try_create_job_rejects_second_job_for_same_in_flight_book_id():
    first = upload_jobs.try_create_job(book_id="busy_data", filename="busy.epub")
    assert first is not None

    second = upload_jobs.try_create_job(book_id="busy_data", filename="busy.epub")
    assert second is None


def test_try_create_job_allows_new_job_once_previous_one_finishes():
    first = upload_jobs.try_create_job(book_id="freed_data", filename="freed.epub")
    upload_jobs.mark_done(first.id)

    second = upload_jobs.try_create_job(book_id="freed_data", filename="freed.epub")
    assert second is not None


def test_try_create_job_allows_new_job_after_previous_one_errors():
    first = upload_jobs.try_create_job(book_id="errored_data", filename="errored.epub")
    upload_jobs.mark_error(first.id, "boom")

    second = upload_jobs.try_create_job(book_id="errored_data", filename="errored.epub")
    assert second is not None


def test_try_create_job_treats_case_variants_as_the_same_in_flight_target():
    """Book_data and book_data would land in the same physical directory on
    a case-insensitive filesystem (the default on macOS/Windows), so the
    in-flight lock must not let both race process_epub concurrently."""
    first = upload_jobs.try_create_job(book_id="Casey_data", filename="Casey.epub")
    assert first is not None

    second = upload_jobs.try_create_job(book_id="casey_data", filename="casey.epub")
    assert second is None

    upload_jobs.mark_done(first.id)
    third = upload_jobs.try_create_job(book_id="CASEY_data", filename="CASEY.epub")
    assert third is not None
