"""The finalise stage: output verification and archives."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pydicom
import pytest
from helpers import force_pool, make_subject

from dichotomise.errors import FinaliseVerificationError
from dichotomise.pydcm.read import _pixel_fingerprint, read_metadata
from dichotomise.run import start_run
from dichotomise.stages.finalise import _verify_review_file, finalise, finalise_review
from dichotomise.stages.rectify import RectifyResult, RectifyReviewResult, rectify, rectify_review
from dichotomise.stages.sanitise import SanitiseResult, SanitiseReviewResult
from dichotomise.stages.sift import ReviewFile, SiftResult
from dichotomise.utils.archive import verify_archive
from dichotomise.utils.fs import file_digest


def _digests(result: RectifyReviewResult) -> dict[Path, str]:
    """The SHA-256 of each review file as rectified, as the pipeline records them."""
    return {item.path: file_digest(item.path) for item in result.files}


def test_finalise_archives_the_rectified_tree_when_not_sanitising(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    file_a = make_dicom_file(tmp_path / "source" / "1.dcm")
    metadata = read_metadata(file_a)
    subject = replace(make_subject(tmp_path / "capture"), files=[metadata])
    digests: dict[Path, str] = {}
    rectify_result = rectify(SiftResult(subject, [metadata], []), digests)
    run = start_run(tmp_path / "out", now=datetime(2026, 9, 22, 14, 30, 12, tzinfo=UTC))

    result = finalise(
        rectify_result,
        run,
        subject_label="sub-01",
        archive_token="a1b2c3",
        source_digests=digests,
    )

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
        finalise(rectify_result, run, subject_label="sub-01", source_digests={})


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
        finalise(
            rectified,
            run,
            subject_label="sub-01",
            sanitise_result=sanitised,
            source_digests={},
        )


def test_finalise_rejects_unexpected_file_even_when_count_matches(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    output_dir = tmp_path / "rectify"
    expected = make_dicom_file(output_dir / "expected.dcm")
    metadata = read_metadata(expected)
    expected.rename(output_dir / "unexpected.dcm")
    result = RectifyResult(make_subject(tmp_path), output_dir, [metadata])
    with pytest.raises(FinaliseVerificationError):
        finalise(
            result,
            start_run(tmp_path / "out"),
            subject_label="sub-01",
            source_digests={},
        )


def test_finalise_review_returns_none_when_there_are_no_review_files(tmp_path: Path) -> None:
    rectify_review_result = RectifyReviewResult(
        review_rectified_dir=tmp_path / "review-rectify", files=[], collision_count=0
    )
    run = start_run(tmp_path / "out", now=datetime(2026, 9, 22, 14, 30, 12, tzinfo=UTC))

    result = finalise_review(
        rectify_review_result,
        run,
        subject_label="sub-01",
        archive_token="a1b2c3",
        verified_digests={},
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
        finalise_review(
            result, run, subject_label="sub-01", archive_token="a1b2c3", verified_digests={}
        )


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
        rectify_review_result,
        run,
        subject_label="sub-01",
        archive_token="a1b2c3",
        verified_digests=_digests(rectify_review_result),
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
        verified_digests={},
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
    sanitised = SanitiseReviewResult(sanitised_dir, [changed])
    with pytest.raises(FinaliseVerificationError, match="Pixel data differs"):
        finalise_review(
            review,
            start_run(tmp_path / "out"),
            subject_label="sub-01",
            archive_token="a1b2c3",
            sanitise_review_result=sanitised,
            verified_digests={},
        )


def test_finalise_review_rejects_a_file_changed_after_rectification(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = make_dicom_file(tmp_path / "source" / "1.dcm")
    subject = make_subject(tmp_path / "working")
    digests: dict[Path, str] = {}
    review = rectify_review(
        SiftResult(subject, [], [ReviewFile(read_metadata(source), "duplicate")]), digests
    )
    with review.files[0].path.open("ab") as handle:
        handle.write(b"changed")
    with pytest.raises(FinaliseVerificationError, match="Review file differs"):
        finalise_review(
            review,
            start_run(tmp_path / "out"),
            subject_label="sub-01",
            archive_token="x",
            verified_digests=digests,
        )


def test_finalise_rejects_a_copy_that_differs_from_its_source(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = make_dicom_file(tmp_path / "source" / "1.dcm")
    metadata = read_metadata(source)
    subject = replace(make_subject(tmp_path / "capture"), files=[metadata])
    digests: dict[Path, str] = {}
    rectified = rectify(SiftResult(subject, [metadata], []), digests)
    with rectified.files[0].path.open("ab") as handle:
        handle.write(b"changed")
    with pytest.raises(FinaliseVerificationError, match="Copied file differs"):
        finalise(
            rectified,
            start_run(tmp_path / "out"),
            subject_label="sub-01",
            source_digests=digests,
        )


def test_finalise_review_rejects_a_file_with_no_recorded_digest(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    review_dir = tmp_path / "review"
    review = RectifyReviewResult(
        review_dir, [read_metadata(make_dicom_file(review_dir / "1.dcm"))], 0
    )
    with pytest.raises(FinaliseVerificationError, match="No source digest"):
        finalise_review(
            review,
            start_run(tmp_path / "out"),
            subject_label="sub-01",
            archive_token="x",
            verified_digests={},
        )


@pytest.mark.parametrize("corrupt", [False, True])
def test_finalise_checks_recorded_fingerprints_in_a_pool(
    tmp_path: Path,
    make_dicom_file: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    corrupt: bool,
) -> None:
    """The read-back of a sanitised tree runs in a pool and still fails closed."""
    sanitised_dir = tmp_path / "sanitise"
    files = [make_dicom_file(sanitised_dir / f"{n}.dcm", InstanceNumber=n) for n in range(1, 5)]
    datasets = {path: pydicom.dcmread(path) for path in files}
    pixels = {path: _pixel_fingerprint(dataset) for path, dataset in datasets.items()}
    identities = {
        path: (str(dataset.PatientID), str(dataset.SOPInstanceUID))
        for path, dataset in datasets.items()
    }
    if corrupt:
        pixels[files[2]] = ("not the real pixels",)
    subject = make_subject(tmp_path)
    pools = force_pool(monkeypatch)
    arguments = {
        "subject_label": "sub-01",
        "sanitise_result": SanitiseResult(subject, "sub-01", sanitised_dir, files),
        "source_digests": {},
        "expected_pixels": pixels,
        "expected_identities": identities,
    }
    rectified = RectifyResult(subject, tmp_path / "rectify", [])

    if corrupt:
        with pytest.raises(FinaliseVerificationError, match="Pixel data differs"):
            finalise(rectified, start_run(tmp_path / "out"), **arguments)
    else:
        finalise(rectified, start_run(tmp_path / "out"), **arguments)

    assert pools


def test_an_unreadable_sanitised_review_file_is_a_verification_error(tmp_path: Path) -> None:
    broken = tmp_path / "1.dcm"
    broken.write_bytes(b"not a dicom file")

    with pytest.raises(FinaliseVerificationError, match="could not be read back"):
        _verify_review_file((broken, (), None))
