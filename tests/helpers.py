"""Builders shared by several test modules."""

from __future__ import annotations

from pathlib import Path

from dichotomise.stages.capture import CapturedSubject


def make_subject(working_dir: Path, source_root: Path | None = None) -> CapturedSubject:
    """A subject with no files, for stages that only need its folders and identifiers."""
    return CapturedSubject(
        subject_id="sub-01",
        patient_name="Doe^Jane^19900101",
        study_instance_uid="study-a",
        scan_date="20260101",
        scan_time="120000",
        files=[],
        source_root=source_root or working_dir,
        working_dir=working_dir,
    )
