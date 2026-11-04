"""Stage 2: tar.gz + checksum the untouched, original source files (archives/).

Archives each subject's files straight from the source directory after
capture has read their headers. Source files are not modified.
"""

from __future__ import annotations

from pathlib import Path
from secrets import token_hex

from dichotomise.run import Run
from dichotomise.stages.capture import CapturedSubject
from dichotomise.utils.archive import Archive, ProgressCallback, _write_tarball
from dichotomise.utils.text import safe_filename_text


def _study_archive_path(subject: CapturedSubject, run: Run) -> Path:
    """Return an unused, concise source-archive path for one captured study."""
    prefix = safe_filename_text(subject.subject_id)
    for _ in range(100):
        archive_stem = f"{prefix}_{token_hex(3)}_source-archive_{run.timestamp}"
        archive_path = run.archives_dir / f"{archive_stem}.tar.gz"
        checksum_path = run.archives_dir / f"{archive_stem}.sha256"
        if not archive_path.exists() and not checksum_path.exists():
            return archive_path
    raise FileExistsError(f"Could not create a unique source archive name for {subject.subject_id}")


def source_archive(
    subject: CapturedSubject, run: Run, *, on_progress: ProgressCallback | None = None
) -> Archive:
    """Archive one subject's original source files, by their path under `source_root`."""
    return _source_archive(subject, run, on_progress=on_progress)


def _source_archive(
    subject: CapturedSubject,
    run: Run,
    *,
    on_progress: ProgressCallback | None = None,
    source_digests: dict[Path, str] | None = None,
) -> Archive:
    paths = sorted(metadata.path for metadata in subject.files)
    entries = ((path, path.relative_to(subject.source_root)) for path in paths)
    return _write_tarball(entries, _study_archive_path(subject, run), on_progress, source_digests)
