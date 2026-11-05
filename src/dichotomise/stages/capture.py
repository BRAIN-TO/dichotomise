"""Stage 1: group the source directory's DICOMs by subject, without copying them."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from dichotomise.errors import NoDicomFilesFoundError, UnsafeSourceError
from dichotomise.pydcm import read as dicom_read
from dichotomise.pydcm.read import DicomMetadata
from dichotomise.run import Run
from dichotomise.utils.fs import reject_symlinks
from dichotomise.utils.text import safe_filename_text


@dataclass(frozen=True)
class CapturedSubject:
    """One subject's files, found in place under the source directory.

    `files` point at the original, untouched source files; nothing has been
    copied yet. `source_root` is the source directory they were found
    under, used to keep archived/report paths relative to it. `working_dir`
    is this subject's own folder under the run's working/ directory, where
    later stages (rectify, sanitise) write their output. This is the one place in
    the pipeline real copies are made.
    """

    subject_id: str
    patient_name: str
    study_instance_uid: str
    scan_date: str
    scan_time: str
    files: list[DicomMetadata]
    source_root: Path
    working_dir: Path
    output_number: int = 1


def _subject_folder_name(metadata: DicomMetadata, used_names: dict[str, int]) -> str:
    base_name = (
        f"{safe_filename_text(metadata.patient_id)}_{safe_filename_text(metadata.study_date)}"
    )
    occurrence = used_names.get(base_name, 0) + 1
    used_names[base_name] = occurrence
    return base_name if occurrence == 1 else f"{base_name}-{occurrence:03d}"


def group_by_subject(files: Iterable[DicomMetadata]) -> dict[tuple[str, str], list[DicomMetadata]]:
    """Group files by (PatientID, StudyInstanceUID), in sorted key order.

    The folder layout plays no part: one folder holding several subjects and
    one subject spread over many folders both group correctly.
    """
    files_by_subject: dict[tuple[str, str], list[DicomMetadata]] = defaultdict(list)
    for metadata in files:
        files_by_subject[(metadata.patient_id, metadata.study_instance_uid)].append(metadata)
    return {key: files_by_subject[key] for key in sorted(files_by_subject)}


def subject_from_files(
    files: list[DicomMetadata], *, source_root: Path, working_dir: Path, output_number: int
) -> CapturedSubject:
    """Build a subject from its files, taking the identity fields from the first file."""
    first = files[0]
    return CapturedSubject(
        subject_id=first.patient_id,
        patient_name=first.patient_name,
        study_instance_uid=first.study_instance_uid,
        scan_date=first.study_date,
        scan_time=first.study_time,
        files=files,
        source_root=source_root,
        working_dir=working_dir,
        output_number=output_number,
    )


def capture(source_dir: Path, run: Run) -> list[CapturedSubject]:
    """Group every scan file under `source_dir` by subject, without copying any of them.

    Subjects are identified from each file's PatientID and StudyInstanceUID,
    never from folder names, so a messy or misnamed export still ends up
    split correctly. A file that cannot be read as DICOM is left out of
    every subject's file list.
    """
    return _capture(source_dir, run)[0]


Fingerprint = tuple[int, int, int, int, int]


def _fingerprint(path: Path) -> Fingerprint:
    """Identify a file's on-disk state, so a later change can be detected."""
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _fingerprints(paths: list[Path]) -> dict[Path, Fingerprint]:
    try:
        return {path: _fingerprint(path) for path in paths}
    except OSError as error:
        raise UnsafeSourceError(f"Source is no longer accessible: {error.filename}") from error


def _capture(source_dir: Path, run: Run) -> tuple[list[CapturedSubject], dict[Path, Fingerprint]]:
    """capture(), plus a fingerprint of every DICOM source file for later change checks.

    Every file is fingerprinted before its header is read and again afterwards,
    so one that changes or disappears mid-capture stops the run instead of
    being half-read.
    """
    reject_symlinks(source_dir)
    paths = sorted(path for path in source_dir.rglob("*") if path.is_file())
    before = _fingerprints(paths)
    metadata = [item for item in dicom_read._read_all(paths) if item is not None]
    after = _fingerprints(paths)
    for path in paths:
        if before[path] != after[path]:
            raise UnsafeSourceError(f"Source changed during capture: {path}")
    # A file that is not DICOM is skipped, but one that cannot be opened at all is not.
    dicom_paths = {item.path for item in metadata}
    for path in paths:
        if path not in dicom_paths:
            try:
                path.open("rb").close()
            except OSError as error:
                raise UnsafeSourceError(f"Source is no longer accessible: {path}") from error
    files_by_subject = group_by_subject(metadata)

    if not files_by_subject:
        raise NoDicomFilesFoundError(f"No DICOM files were found under {source_dir}")

    used_names: dict[str, int] = {}
    captured: list[CapturedSubject] = []
    for output_number, key in enumerate(files_by_subject, start=1):
        files = sorted(files_by_subject[key], key=lambda metadata: metadata.path)
        folder_name = _subject_folder_name(files[0], used_names)
        captured.append(
            subject_from_files(
                files,
                source_root=source_dir,
                working_dir=run.working_dir / folder_name,
                output_number=output_number,
            )
        )
    return captured, {item.path: before[item.path] for item in metadata}
