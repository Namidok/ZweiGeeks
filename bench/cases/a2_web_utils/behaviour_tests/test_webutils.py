import pytest

from webutils import downloads, passwords, query, sessions


def test_session_survives_a_cookie_round_trip():
    session = sessions.Session(user_id=7, roles=["admin"], created_at=1000.0)
    restored = sessions.session_from_cookie(sessions.session_to_cookie(session))
    assert restored == session
    assert restored.is_admin()


def test_session_freshness():
    session = sessions.Session(user_id=1, created_at=1000.0)
    assert sessions.is_fresh(session, now=1000.0 + 60)
    assert not sessions.is_fresh(session, now=1000.0 + 7200)


def test_password_round_trip():
    stored = passwords.hash_password("correct horse 42")
    assert passwords.verify_password("correct horse 42", stored)
    assert not passwords.verify_password("wrong horse 42", stored)


def test_password_strength_rules():
    assert passwords.password_is_strong("abcdefgh12")
    assert not passwords.password_is_strong("short1")
    assert not passwords.password_is_strong("onlyletterslong")


def test_downloads_read_and_list(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    (root / "invoice.pdf").write_bytes(b"%PDF-1.4")
    (root / "notes.txt").write_bytes(b"hello")
    assert downloads.read_download("notes.txt", root) == b"hello"
    assert downloads.list_downloads(root) == ["invoice.pdf", "notes.txt"]
    with pytest.raises(FileNotFoundError):
        downloads.read_download("missing.txt", root)


def test_query_parsing():
    assert query.parse_limit(None) == 20
    assert query.parse_limit("10") == 10
    assert query.parse_limit("500") == 100
    assert query.parse_sort("-created_at") == ("created_at", True)
    assert query.parse_sort(None) == ("name", False)
    with pytest.raises(ValueError):
        query.parse_sort("password")
