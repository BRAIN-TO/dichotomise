"""make_tarball() and verify_archive(): tar.gz creation with a sha256 sidecar."""

from __future__ import annotations

import shutil
import subprocess
import tarfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from dichotomise.errors import ArchiveCreationError, ArchiveVerificationError
from dichotomise.utils import fs

ProgressCallback = Callable[[int, int], None]

# gzip's own default, kept the same whichever compressor ends up writing.
_GZIP_COMPRESSLEVEL = 9


def pigz_path() -> str | None:
    """Return the path to `pigz` if it's on PATH, else None.

    Exposed so a caller (e.g. the CLI) can report once, up front, whether
    the faster, parallel-gzip path will be used for this run's archives.
    """
    return shutil.which("pigz")


@dataclass(frozen=True)
class Archive:
    """An archive file and the checksum sidecar that verifies it."""

    path: Path
    checksum_path: Path


def _checksum_path_for(archive_path: Path) -> Path:
    name = archive_path.name
    if name.endswith(".tar.gz"):
        name = name[: -len(".tar.gz")]
    return archive_path.with_name(f"{name}.sha256")


def _sha256_of(path: Path) -> str:
    return fs.file_digest(path)


def _finish_archive(archive_path: Path) -> Archive:
    """Write the checksum sidecar and verify the archive before returning it."""
    checksum_path = _checksum_path_for(archive_path)
    checksum_path.write_text(_sha256_of(archive_path) + "\n")
    archive = Archive(path=archive_path, checksum_path=checksum_path)
    if not verify_archive(archive.path, archive.checksum_path):
        raise ArchiveVerificationError(f"Archive verification failed: {archive.path}")
    return archive


def make_tarball(
    source_dir: Path, archive_path: Path, *, on_progress: ProgressCallback | None = None
) -> Archive:
    """Compress everything under `source_dir` into `archive_path`, plus a checksum file.

    The checksum sidecar sits next to the archive, named after it with a
    `.sha256` extension in place of `.tar.gz`. `on_progress`, if given, is
    called after each file is added with (bytes added so far, total bytes
    to add). Progress is coarse-grained (per file, not per chunk), since tarfile adds
    a whole file in one call.
    """
    items = sorted(source_dir.rglob("*"))
    return _write_tarball(
        ((item, item.relative_to(source_dir)) for item in items), archive_path, on_progress
    )


def make_tarball_from_files(
    files: Sequence[Path],
    root: Path,
    archive_path: Path,
    *,
    on_progress: ProgressCallback | None = None,
) -> Archive:
    """Compress a scattered set of files, kept relative to `root`, into `archive_path`.

    Unlike `make_tarball()`, `files` need not fill all of `root` or even sit
    under one common folder beneath it; each is added individually, by its
    path relative to `root`, with no separate directory entries (a plain
    extract still recreates the needed folders). This lets one subject's
    files be archived straight from a shared, multi-subject source
    directory, without first copying that subject's files into a folder of
    their own.
    """
    sorted_files = sorted(files)
    return _write_tarball(
        ((path, path.relative_to(root)) for path in sorted_files), archive_path, on_progress
    )


def _add_entries(
    tar: tarfile.TarFile,
    entries: Sequence[tuple[Path, Path]],
    total_bytes: int,
    on_progress: ProgressCallback | None,
) -> None:
    """Add each (path, arcname) entry to an already-open `tar`.

    Entries that are directories count toward neither total nor progress
    bytes (they have no content of their own), so `on_progress` only ever
    reports real file bytes.
    """
    written_bytes = 0
    for path, arcname in entries:
        # recursive=False: each entry already comes from our own walk (files
        # and folders both), so letting tar.add() recurse into a folder
        # would add every file inside it a second time.
        tar.add(path, arcname=arcname, recursive=False)
        if path.is_file():
            written_bytes += path.stat().st_size
            if on_progress is not None:
                on_progress(written_bytes, total_bytes)


def _write_tarball_with_pigz(
    pigz: str,
    entries: Sequence[tuple[Path, Path]],
    archive_path: Path,
    total_bytes: int,
    on_progress: ProgressCallback | None,
) -> None:
    """Pipe an uncompressed tar stream through `pigz` (parallel gzip) to `archive_path`.

    `tarfile` writes the uncompressed tar stream straight into the `pigz`
    subprocess's stdin (mode "w|": no seeking, suitable for a pipe); `pigz`
    does the gzip compression itself, in parallel across CPU cores, with
    its stdout redirected straight to the archive file.
    """
    with archive_path.open("wb") as out_file:
        process = subprocess.Popen(
            [pigz, "-c", f"-{_GZIP_COMPRESSLEVEL}"], stdin=subprocess.PIPE, stdout=out_file
        )
        stdin = process.stdin
        assert stdin is not None  # guaranteed by stdin=subprocess.PIPE above
        try:
            with tarfile.open(fileobj=stdin, mode="w|") as tar:
                _add_entries(tar, entries, total_bytes, on_progress)
        finally:
            stdin.close()
            return_code = process.wait()
        if return_code != 0:
            raise ArchiveCreationError(
                f"pigz exited with status {return_code} while writing {archive_path}"
            )


def _write_tarball(
    entries: Iterable[tuple[Path, Path]],
    archive_path: Path,
    on_progress: ProgressCallback | None,
) -> Archive:
    """Write every (path, arcname) entry into a new tarball at `archive_path`.

    Prefers `pigz` for the gzip compression when it's on PATH. It
    parallelises across CPU cores, where the stdlib `gzip`/`zlib` codec
    `tarfile` otherwise uses is single-threaded. Without `pigz` it falls back to the
    stdlib path. Either way, the result is an ordinary .tar.gz.
    """
    sorted_entries = list(entries)
    total_bytes = sum(path.stat().st_size for path, _ in sorted_entries if path.is_file())
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    pigz = pigz_path()
    if pigz is not None:
        _write_tarball_with_pigz(pigz, sorted_entries, archive_path, total_bytes, on_progress)
    else:
        with tarfile.open(archive_path, "w:gz", compresslevel=_GZIP_COMPRESSLEVEL) as tar:
            _add_entries(tar, sorted_entries, total_bytes, on_progress)

    return _finish_archive(archive_path)


def verify_archive(archive_path: Path, checksum_path: Path) -> bool:
    """Return whether `archive_path`'s current contents match its recorded checksum."""
    expected = checksum_path.read_text().split()[0]
    return _sha256_of(archive_path) == expected
