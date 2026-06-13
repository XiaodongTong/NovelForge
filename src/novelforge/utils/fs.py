"""Filesystem helpers: atomic write, hashing, listing."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Iterable, Union

PathLike = Union[str, os.PathLike]


def sha256_file(path: PathLike) -> str:
    """Return the hex SHA-256 digest of a file's bytes."""

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    """Return the hex SHA-256 digest of a string."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_write(path: PathLike, data: Union[str, bytes]) -> None:
    """Atomically write ``data`` to ``path``.

    Writes to a temporary file in the same directory, fsyncs the file and
    directory, then renames onto the target. This is resilient against
    crashes and partial writes.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    is_text = isinstance(data, str)
    payload = data.encode("utf-8") if is_text else data
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        # Best-effort fsync of the directory entry. Tolerate platforms/filesystems
        # where the parent dir is not directly fsync-able.
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except Exception:
        # Clean up the temp file on any failure to avoid leaving cruft behind.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def list_files(
    root: PathLike,
    patterns: Iterable[str] = ("*",),
    recursive: bool = True,
) -> list[Path]:
    """List files under ``root`` matching any glob pattern."""

    root = Path(root)
    if not root.exists():
        return []
    out: list[Path] = []
    if recursive:
        for pat in patterns:
            out.extend(sorted(root.rglob(pat)))
    else:
        for pat in patterns:
            out.extend(sorted(root.glob(pat)))
    return out


def ensure_dir(path: PathLike) -> Path:
    """Create ``path`` (including parents) and return it."""

    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p
