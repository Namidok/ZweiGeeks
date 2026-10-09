"""What the inventory service does today, bugs aside. load_settings has no
test here: with PyYAML 6, `yaml.load(f)` without a Loader raises TypeError,
so it can't pass on the unmodified repo; bug_tests/ covers it instead."""

import sqlite3

import app


class _FakeResponse:
    def json(self):
        return {"price": 4.25}


def test_fetch_price_returns_the_price_field(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _FakeResponse()

    monkeypatch.setattr(app.requests, "get", fake_get)
    assert app.fetch_price("https://prices.example/apple") == 4.25
    assert calls == ["https://prices.example/apple"]


def test_archive_logs_creates_the_archive(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "app.log").write_text("started\n")
    assert app.archive_logs("logs") == 0
    assert (tmp_path / "logs.tgz").is_file()


def test_find_item_returns_matching_rows():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE items (id INTEGER, name TEXT, price REAL)")
    conn.executemany("INSERT INTO items VALUES (?, ?, ?)", [(1, "apple", 2.5), (2, "pear", 3.0)])
    assert app.find_item(conn, "apple") == [(1, "apple", 2.5)]
    assert app.find_item(conn, "plum") == []


def test_connect_opens_the_inventory_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = app.connect()
    assert isinstance(result[0], sqlite3.Connection)
    result[0].close()
