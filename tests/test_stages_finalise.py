"""The finalise stage: output verification, archives and the standalone provenance checks."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers import make_subject

from dichotomise.errors import FinaliseVerificationError
from dichotomise.pydcm.read import read_metadata
from dichotomise.run import start_run
from dichotomise.stages.finalise import finalise, finalise_review
from dichotomise.stages.rectify import RectifyResult, RectifyReviewResult, rectify, rectify_review
from dichotomise.stages.sanitise import SanitiseResult, SanitiseReviewResult
from dichotomise.stages.sift import ReviewFile, SiftResult
from dichotomise.utils.archive import verify_archive
from dichotomise.utils.fs import file_digest


def _review_manifest(result: RectifyReviewResult) -> None:
    """Write the private digest manifest that standalone review finalisation requires."""
    manifest = result.review_rectified_dir.parent / ".review-rectify-digests.json"
    manifest.write_text(
        json.dumps(
            {
                str(item.path.relative_to(result.review_rectified_dir)): file_digest(item.path)
                for item in result.files
            }
        )
    )


def test_finalise_archives_the_rectified_tree_when_not_sanitising(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    file_a = make_dicom_file(tmp_path / "source" / "1.dcm")
    metadata = read_metadata(file_a)
    subject = replace(make_subject(tmp_path / "capture"), files=[metadata])
    rectify_result = rectify(SiftResult(subject, [metadata], []))
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
    subject = make_subject(tmp_path / "capture")
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
    subject = make_subject(tmp_path)
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
    result = RectifyResult(make_subject(tmp_path), output_dir, [metadata])
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
    _review_manifest(rectify_review_result)
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
    _review_manifest(rectify_review_result)
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


def test_finalise_review_rejects_changed_pixels(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    original_dir = tmp_path / "review"
    sanitised_dir = tmp_path / "sanitised"
    original = make_dicom_file(original_dir / "1.dcm", PixelData=b"\x01\x02")
    changed = make_dicom_file(sanitised_dir / "1.dcm", PixelData=b"\x03\x04")
    review = RectifyReviewResult(original_dir, [read_metadata(original)], 0)
    _review_manifest(review)
    sanitised = SanitiseReviewResult(sanitised_dir, [changed])
    with pytest.raises(FinaliseVerificationError, match="Pixel data differs"):
        finalise_review(
            review,
            start_run(tmp_path / "out"),
            subject_label="sub-01",
            archive_token="a1b2c3",
            sanitise_review_result=sanitised,
        )


def test_standalone_review_rejects_changes_after_rectification(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = make_dicom_file(tmp_path / "source" / "1.dcm")
    metadata = read_metadata(source)
    subject = make_subject(tmp_path / "working")
    review = rectify_review(SiftResult(subject, [], [ReviewFile(metadata, "duplicate")]))
    with review.files[0].path.open("ab") as handle:
        handle.write(b"changed")
    with pytest.raises(FinaliseVerificationError, match="Review file differs"):
        finalise_review(
            review, start_run(tmp_path / "out"), subject_label="sub-01", archive_token="x"
        )


def test_standalone_review_requires_provenance(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = make_dicom_file(tmp_path / "review" / "1.dcm")
    review = RectifyReviewResult(source.parent, [read_metadata(source)], 0)
    with pytest.raises(FinaliseVerificationError, match="Invalid review provenance"):
        finalise_review(
            review, start_run(tmp_path / "out"), subject_label="sub-01", archive_token="x"
        )


def test_standalone_review_archives_verified_rectification(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = make_dicom_file(tmp_path / "source" / "1.dcm")
    review = rectify_review(
        SiftResult(
            make_subject(tmp_path / "working"), [], [ReviewFile(read_metadata(source), "duplicate")]
        )
    )
    result = finalise_review(
        review, start_run(tmp_path / "out"), subject_label="sub-01", archive_token="x"
    )
    assert result is not None
    import tarfile

    with tarfile.open(result.path) as archive:
        assert all("digests" not in entry.name for entry in archive.getmembers())


@pytest.mark.parametrize("manifest_data", [None, "not-json", "{}"])
def test_standalone_review_rejects_missing_or_invalid_manifest(
    tmp_path: Path, make_dicom_file: Callable[..., Path], manifest_data: str | None
) -> None:
    source = make_dicom_file(tmp_path / "source" / "1.dcm")
    review = rectify_review(
        SiftResult(
            make_subject(tmp_path / "working"), [], [ReviewFile(read_metadata(source), "duplicate")]
        )
    )
    manifest = review.review_rectified_dir.parent / ".review-rectify-digests.json"
    if manifest_data is None:
        manifest.unlink()
    else:
        manifest.write_text(manifest_data)
    with pytest.raises(FinaliseVerificationError, match="Invalid review provenance"):
        finalise_review(
            review, start_run(tmp_path / "out"), subject_label="sub-01", archive_token="x"
        )


def test_standalone_retained_finalisation_requires_provenance(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    file = make_dicom_file(tmp_path / "rectify" / "1.dcm")
    result = RectifyResult(make_subject(tmp_path), file.parent, [read_metadata(file)])
    with pytest.raises(FinaliseVerificationError, match="Invalid retained provenance"):
        finalise(result, start_run(tmp_path / "out"), subject_label="sub-01")
