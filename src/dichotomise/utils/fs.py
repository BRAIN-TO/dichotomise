"""Filesystem helpers: file digests and the symlink check."""

from __future__ import annotations

import hashlib
from collections.abc import MutableMapping
from pathlib import Path

from dichotomise.errors import UnsafeSourceError


def file_digest(path: Path, cache: MutableMapping[Path, str] | None = None) -> str:
    """Return the SHA-256 digest of a file, optionally reusing a cached value."""
    if cache is not None and path in cache:
        return cache[path]
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if cache is not None:
        cache[path] = digest
    return digest


def reject_symlinks(source_dir: Path) -> None:
    """Raise UnsafeSourceError when a source tree contains a symbolic link."""
    if source_dir.is_symlink():
        raise UnsafeSourceError(f"{source_dir} is a symlink; refusing to copy it")
    for path in source_dir.rglob("*"):
        if path.is_symlink():
            raise UnsafeSourceError(f"{path} is a symlink; refusing to copy the source directory")
