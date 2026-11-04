from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dichotomise.errors import FinaliseVerificationError
from dichotomise.pydcm.read import read_metadata
from dichotomise.run import start_run
from dichotomise.stages.capture import CapturedSubject
from dichotomise.stages.finalise import finalise, finalise_review
from dichotomise.stages.rectify import RectifyResult, RectifyReviewResult
from dichotomise.stages.sanitise import SanitiseResult, SanitiseReviewResult
from dichotomise.utils.archive import verify_archive


def _subject(working_dir: Path) -> CapturedSubject:
    return CapturedSubject(
        subject_id="sub-01",
        patient_name="Doe^Jane^19900101",
        study_instance_uid="study-a",
        scan_date="20260101",
        scan_time="120000",
        files=[],
        source_root=working_dir,
        working_dir=working_dir,
    )


def test_finalise_archives_the_rectified_tree_when_not_sanitising(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    rectified_dir = tmp_path / "rectify"
    file_a = make_dicom_file(rectified_dir / "021-DWI" / "1.dcm")
    subject = _subject(tmp_path / "capture")
    rectify_result = RectifyResult(
        subject=subject, rectified_dir=rectified_dir, files=[read_metadata(file_a)]
    )
    run = start_run(tmp_path / "out", now=datetime(2026, 9, 22, 14, 30, 12, tzinfo=UTC))

    result = finalise(rectify_result, run, subject_label="sub-01", archive_token="a1b2c3")

    expected_name = "sub-01_a1b2c3_dichotomised-archive_20260922T143012Z"
    assert result.archive.path == run.archives_dir / f"{expected_name}.tar.gz"
    assert verify_archive(result.archive.path, result.archive.checksum_path) is True


def test_finalise_raises_when_the_output_tree_is_missing_an_expected_file(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    rectified_dir = tmp_path / "rectify"
    file_a = make_dicom_file(rectified_dir / "021-DWI" / "1.dcm")
    metadata_a = read_metadata(file_a)
    subject = _subject(tmp_path / "capture")
    # Claim a second file that was never actually written.
    missing = rectified_dir / "021-DWI" / "2.dcm"
    from dataclasses import replace

    rectify_result = RectifyResult(
        subject=subject,
        rectified_dir=rectified_dir,
        files=[metadata_a, replace(metadata_a, path=missing)],
    )
    run = start_run(tmp_path / "out", now=datetime(2026, 9, 22, 14, 30, 12, tzinfo=UTC))

    with pytest.raises(FinaliseVerificationError):
        finalise(rectify_result, run, subject_label="sub-01")


def test_finalise_rejects_changed_sanitised_pixel_data(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    rectified_dir = tmp_path / "rectify"
    sanitised_dir = tmp_path / "sanitise"
    original = make_dicom_file(rectified_dir / "1.dcm", PixelData=b"\x01\x02")
    changed = make_dicom_file(sanitised_dir / "1.dcm", PixelData=b"\x03\x04")
    subject = _subject(tmp_path)
    rectified = RectifyResult(subject, rectified_dir, [read_metadata(original)])
    sanitised = SanitiseResult(subject, "sub-01", sanitised_dir, [changed])
    run = start_run(tmp_path / "out")

    with pytest.raises(FinaliseVerificationError, match="Pixel data differs"):
        finalise(rectified, run, subject_label="sub-01", sanitise_result=sanitised)


def test_finalise_rejects_unexpected_file_even_when_count_matches(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    output_dir = tmp_path / "rectify"
    expected = make_dicom_file(output_dir / "expected.dcm")
    metadata = read_metadata(expected)
    expected.rename(output_dir / "unexpected.dcm")
    result = RectifyResult(_subject(tmp_path), output_dir, [metadata])
    with pytest.raises(FinaliseVerificationError):
        finalise(result, start_run(tmp_path / "out"), subject_label="sub-01")


def test_finalise_review_returns_none_when_there_are_no_review_files(tmp_path: Path) -> None:
    rectify_review_result = RectifyReviewResult(
        review_rectified_dir=tmp_path / "review-rectify", files=[], collision_count=0
    )
    run = start_run(tmp_path / "out", now=datetime(2026, 9, 22, 14, 30, 12, tzinfo=UTC))

    result = finalise_review(
        rectify_review_result, run, subject_label="sub-01", archive_token="a1b2c3"
    )

    assert result is None
    assert not run.archives_dir.exists() or not list(run.archives_dir.glob("*"))


def test_finalise_review_rejects_a_missing_file(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    review_dir = tmp_path / "review"
    source = make_dicom_file(review_dir / "1.dcm")
    metadata = read_metadata(source)
    source.unlink()
    result = RectifyReviewResult(
        review_rectified_dir=review_dir, files=[metadata], collision_count=0
    )
    run = start_run(tmp_path / "out")

    with pytest.raises(FinaliseVerificationError):
        finalise_review(result, run, subject_label="sub-01", archive_token="a1b2c3")


def test_finalise_review_archives_the_renamed_review_tree_when_not_sanitised(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    review_rectified_dir = tmp_path / "review-rectify"
    file_a = make_dicom_file(review_rectified_dir / "021-DWI" / "1.dcm")
    rectify_review_result = RectifyReviewResult(
        review_rectified_dir=review_rectified_dir,
        files=[read_metadata(file_a)],
        collision_count=0,
    )
    run = start_run(tmp_path / "out", now=datetime(2026, 9, 22, 14, 30, 12, tzinfo=UTC))

    result = finalise_review(
        rectify_review_result, run, subject_label="sub-01", archive_token="a1b2c3"
    )

    assert result is not None
    expected_name = "sub-01_a1b2c3_review-archive_20260922T143012Z"
    assert result.path == run.archives_dir / f"{expected_name}.tar.gz"
    assert verify_archive(result.path, result.checksum_path) is True

    import tarfile

    with tarfile.open(result.path, "r:gz") as tar:
        assert any(name.endswith("1.dcm") for name in tar.getnames())


def test_finalise_review_archives_the_sanitised_tree_when_sanitised(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    review_rectified_dir = tmp_path / "review-rectify"
    raw_file = make_dicom_file(review_rectified_dir / "021-DWI" / "1.dcm")
    rectify_review_result = RectifyReviewResult(
        review_rectified_dir=review_rectified_dir,
        files=[read_metadata(raw_file)],
        collision_count=0,
    )
    sanitised_review_dir = tmp_path / "sanitise-review"
    sanitised_file = make_dicom_file(sanitised_review_dir / "021-DWI" / "1.dcm")
    sanitise_review_result = SanitiseReviewResult(
        sanitised_review_dir=sanitised_review_dir, files=[sanitised_file]
    )
    run = start_run(tmp_path / "out", now=datetime(2026, 9, 22, 14, 30, 12, tzinfo=UTC))

    result = finalise_review(
        rectify_review_result,
        run,
        subject_label="sub-01",
        archive_token="a1b2c3",
        sanitise_review_result=sanitise_review_result,
    )

    assert result is not None
    import tarfile

    with tarfile.open(result.path, "r:gz") as tar:
        contents = {
            member.name: tar.extractfile(member).read()  # type: ignore[union-attr]
            for member in tar.getmembers()
            if member.isfile()
        }
    assert contents == {"021-DWI/1.dcm": sanitised_file.read_bytes()}
