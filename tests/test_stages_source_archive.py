"""The source-archive stage: naming and contents of the untouched-source archive."""

from __future__ import annotations

import re
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from dichotomise.pydcm.read import read_metadata
from dichotomise.run import start_run
from dichotomise.stages.capture import CapturedSubject
from dichotomise.stages.source_archive import source_archive
from dichotomise.utils.archive import verify_archive


def test_source_archive_names_the_archive_from_run_subject_and_scan_datetime(
    tmp_path: Path, make_dicom_file
) -> None:
    source_dir = tmp_path / "export"
    dicom_file = make_dicom_file(source_dir / "series" / "1.dcm")

    run = start_run(tmp_path / "out", now=datetime(2026, 9, 22, 14, 30, 12, tzinfo=UTC))
    subject = CapturedSubject(
        subject_id="sub-01",
        patient_name="Doe^Jane^19900101",
        study_instance_uid="study-a",
        scan_date="20260101",
        scan_time="120000",
        files=[read_metadata(dicom_file)],
        source_root=source_dir,
        working_dir=run.working_dir / "sub-01",
    )

    archive = source_archive(subject, run)

    pattern = r"sub-01_[0-9a-f]{6}_source-archive_20260922T143012Z"
    assert re.fullmatch(pattern + r"\.tar\.gz", archive.path.name)
    assert re.fullmatch(pattern + r"\.sha256", archive.checksum_path.name)
    assert archive.path.exists()
    assert verify_archive(archive.path, archive.checksum_path) is True

    with tarfile.open(archive.path, "r:gz") as tar:
        names = [member.name for member in tar.getmembers() if member.isfile()]
    assert names == ["series/1.dcm"]

    # The original source file was only read, never moved or modified.
    assert dicom_file.exists()
