"""Serves files that users uploaded earlier."""

from pathlib import Path

UPLOAD_ROOT = Path("uploads")


def resolve_download(filename: str, root: Path = UPLOAD_ROOT) -> Path:
    return root / filename


def read_download(filename: str, root: Path = UPLOAD_ROOT) -> bytes:
    path = resolve_download(filename, root)
    if not path.is_file():
        raise FileNotFoundError(filename)
    return path.read_bytes()


def list_downloads(root: Path = UPLOAD_ROOT) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.is_file())
