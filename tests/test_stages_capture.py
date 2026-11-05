"""The capture stage: grouping files by subject without copying them."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dichotomise.errors import NoDicomFilesFoundError, UnsafeSourceError
from dichotomise.run import start_run
from dichotomise.stages.capture import capture


def _run(tmp_path: Path):
    """A run in `tmp_path/out` with a fixed timestamp, so names are predictable."""
    out_dir = tmp_path / "out"
    return start_run(out_dir, now=datetime(2026, 9, 22, 14, 30, 12, tzinfo=UTC))


def test_capture_groups_files_by_patient_and_study_without_copying(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    file_1 = make_dicom_file(
        source / "DWI_21_MR" / "1.dcm",
        PatientID="sub-01",
        StudyInstanceUID="study-a",
        StudyDate="20260101",
        StudyTime="120000",
    )
    file_2 = make_dicom_file(
        source / "DWI_21_MR" / "2.dcm",
        PatientID="sub-01",
        StudyInstanceUID="study-a",
        StudyDate="20260101",
        StudyTime="120000",
    )
    file_3 = make_dicom_file(
        source / "other_subject" / "1.dcm",
        PatientID="sub-02",
        StudyInstanceUID="study-b",
        StudyDate="20260102",
    )
    run = _run(tmp_path)

    subjects = capture(source, run)

    assert {subject.subject_id for subject in subjects} == {"sub-01", "sub-02"}
    sub01 = next(subject for subject in subjects if subject.subject_id == "sub-01")
    assert sub01.scan_date == "20260101"
    assert sub01.scan_time == "120000"
    assert {metadata.path for metadata in sub01.files} == {file_1, file_2}
    assert sub01.source_root == source

    # Nothing was copied anywhere: the files are exactly where they started.
    assert sorted(source.rglob("*.dcm")) == sorted([file_1, file_2, file_3])
    assert not run.working_dir.exists()


def test_capture_ignores_non_dicom_files(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm", PatientID="sub-01", StudyInstanceUID="study-a")
    (source / "series" / "readme.txt").write_text("not a scan file")
    run = _run(tmp_path)

    subjects = capture(source, run)

    all_files = [metadata.path for subject in subjects for metadata in subject.files]
    assert all(p.suffix == ".dcm" for p in all_files)


def test_capture_raises_when_no_dicom_files_are_found(tmp_path: Path) -> None:
    source = tmp_path / "export"
    source.mkdir()
    (source / "readme.txt").write_text("nothing to see here")
    run = _run(tmp_path)

    with pytest.raises(NoDicomFilesFoundError):
        capture(source, run)


def test_capture_refuses_a_source_directory_containing_a_symlink(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    real_dir = tmp_path / "elsewhere"
    real_dir.mkdir()
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm", PatientID="sub-01", StudyInstanceUID="study-a")
    (source / "series" / "link").symlink_to(real_dir)
    run = _run(tmp_path)

    with pytest.raises(UnsafeSourceError):
        capture(source, run)


def test_capture_stops_on_a_source_file_it_cannot_open(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    """An unreadable file is a source problem, not a stray non-DICOM file to skip."""
    source = tmp_path / "export"
    make_dicom_file(source / "1.dcm")
    locked = source / "2.dcm"
    locked.write_bytes(b"x")
    locked.chmod(0)
    if os.access(locked, os.R_OK):  # e.g. running as root: nothing to test
        pytest.skip("file permissions are not enforced for this user")
    try:
        with pytest.raises(UnsafeSourceError, match="no longer accessible"):
            capture(source, _run(tmp_path))
    finally:
        locked.chmod(0o600)
