import pytest

from webutils import downloads


def test_read_download_refuses_paths_outside_the_root(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("db password")
    with pytest.raises((ValueError, PermissionError)):
        downloads.read_download("../secret.txt", root)
