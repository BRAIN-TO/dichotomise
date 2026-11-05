"""Stage 7: independently re-verify the output tree, then tar.gz + checksum it (archives/)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pydicom
from pydicom.errors import InvalidDicomError

from dichotomise.errors import FinaliseVerificationError
from dichotomise.pydcm.identify import file_digest
from dichotomise.pydcm.read import _metadata_from_dataset, _pixel_fingerprint
from dichotomise.run import Run
from dichotomise.stages.capture import CapturedSubject
from dichotomise.stages.rectify import (
    RectifyPlan,
    RectifyResult,
    RectifyReviewPlan,
    RectifyReviewResult,
)
from dichotomise.stages.sanitise import SanitiseResult, SanitiseReviewResult
from dichotomise.utils.archive import Archive, ProgressCallback, make_tarball
from dichotomise.utils.text import safe_filename_text


@dataclass(frozen=True)
class FinaliseResult:
    """The verified, archived, final output for one subject.

    `source_dir` is the already-verified tree that was archived (the
    sanitised tree if `--sanitise` was used, otherwise the rectified tree).
    It is kept here so a caller that also wants the unarchived files (for
    `--keep-unzipped`) doesn't need to re-derive which tree that was.
    """

    subject: CapturedSubject
    subject_label: str
    archive: Archive
    source_dir: Path


def _expected_uncompressed_pixel_bytes(dataset: pydicom.Dataset) -> int | None:
    """Return the expected uncompressed pixel-data length when it can be calculated."""
    if not hasattr(dataset, "PixelData"):
        return None
    file_meta = getattr(dataset, "file_meta", None)
    transfer_syntax = getattr(file_meta, "TransferSyntaxUID", None)
    if transfer_syntax is None or transfer_syntax.is_compressed:
        return None
    try:
        rows = int(dataset.Rows)
        columns = int(dataset.Columns)
        samples_per_pixel = int(getattr(dataset, "SamplesPerPixel", 1))
        bits_allocated = int(dataset.BitsAllocated)
        frames = int(getattr(dataset, "NumberOfFrames", 1))
    except (AttributeError, TypeError, ValueError):
        return None
    total_bits = rows * columns * samples_per_pixel * bits_allocated * frames
    return (total_bits + 7) // 8


def _verify_uncompressed_pixel_data(path: Path, dataset: pydicom.Dataset) -> None:
    """Reject an uncompressed image whose stored pixel data has the wrong length."""
    expected_bytes = _expected_uncompressed_pixel_bytes(dataset)
    if expected_bytes is None:
        return
    actual_bytes = len(dataset.PixelData)
    if actual_bytes not in (expected_bytes, expected_bytes + 1):
        raise FinaliseVerificationError(
            f"Pixel data length in {path} is {actual_bytes} byte(s); expected {expected_bytes}"
        )


def _verify_output_tree(
    expected_files: list[Path],
    output_dir: Path,
    expected_pixels: dict[Path, tuple[object, ...]] | None = None,
    expected_identities: dict[Path, tuple[str, str]] | None = None,
) -> None:
    """Independently re-check the finished output tree before it is archived.

    This deliberately re-reads the output tree from disk rather than trusting
    the file list the earlier stages produced, so that corruption introduced
    by a copy or rename step (a truncated file, a file that silently failed
    to write) is caught here rather than archived as if it were correct.
    """
    found_files = sorted(p for p in output_dir.rglob("*") if p.is_file())
    if set(found_files) != set(expected_files):
        raise FinaliseVerificationError(
            f"Expected {len(expected_files)} file(s) in the finished output, "
            f"but found {len(found_files)} in {output_dir}"
        )
    for path in found_files:
        try:
            dataset = pydicom.dcmread(path, force=False)
            _metadata_from_dataset(dataset, path)
        except (InvalidDicomError, OSError) as error:
            raise FinaliseVerificationError(
                f"{path} could not be read back after processing; the finished output "
                "may be corrupt"
            ) from error
        _verify_uncompressed_pixel_data(path, dataset)
        if expected_pixels is not None and _pixel_fingerprint(dataset) != expected_pixels[path]:
            raise FinaliseVerificationError(f"Pixel data differs from its source: {path}")
        if expected_identities is not None:
            _verify_identity(dataset, path, expected_identities[path])


def _verify_identity(dataset: pydicom.Dataset, path: Path, expected: tuple[str, str]) -> None:
    """Check a sanitised file's PatientID and SOP UID (dataset and file meta) are as written."""
    _, sop_uid = expected
    if (str(dataset.get("PatientID", "")), str(dataset.get("SOPInstanceUID", ""))) != expected:
        raise FinaliseVerificationError(
            f"Sanitised identity differs from the expected values: {path}"
        )
    file_meta = getattr(dataset, "file_meta", None)
    if (
        file_meta is not None
        and "MediaStorageSOPInstanceUID" in file_meta
        and str(file_meta.MediaStorageSOPInstanceUID) != sop_uid
    ):
        raise FinaliseVerificationError(f"File metadata has an inconsistent SOP UID: {path}")


def _verify_pixel_data(source: Path, output: Path) -> None:
    """Check that an output file's pixels and their interpretation match its source file."""
    original_dataset = pydicom.dcmread(source)
    finished = pydicom.dcmread(output)
    if _pixel_fingerprint(original_dataset) != _pixel_fingerprint(finished):
        raise FinaliseVerificationError(f"Pixel data differs from its source: {output}")


def finalise(
    rectify_result: RectifyResult | RectifyPlan,
    run: Run,
    *,
    subject_label: str,
    sanitise_result: SanitiseResult | None = None,
    archive_token: str | None = None,
    on_progress: ProgressCallback | None = None,
    source_digests: dict[Path, str],
    expected_pixels: dict[Path, tuple[object, ...]] | None = None,
    expected_identities: dict[Path, tuple[str, str]] | None = None,
) -> FinaliseResult:
    """Verify the finished output tree file by file, then archive it into `run.archives_dir`.

    Archives the sanitised tree if `--sanitise` was used, otherwise the rectified tree. Named
    with the run's timestamp, `subject_label` (the real PatientID, or the replacement label if
    sanitised), scan date/time and a per-run study number, so one study never replaces another.

    The checks depend on the run: copies are compared with the source digests and
    the source of each rectified file (`target_by_source`); sanitised files with the pixel
    fingerprints and identities recorded while sanitising.
    """
    source_dir = sanitise_result.sanitised_dir if sanitise_result else rectify_result.rectified_dir
    expected_files = (
        sanitise_result.files if sanitise_result else [item.path for item in rectify_result.files]
    )
    _verify_output_tree(expected_files, source_dir, expected_pixels, expected_identities)

    if sanitise_result is None:
        source_of = {target: source for source, target in rectify_result.target_by_source.items()}
        for item in rectify_result.files:
            original = source_of.get(item.path)
            if original is None:
                raise FinaliseVerificationError(f"No source provenance for {item.path}")
            if original not in source_digests:
                raise FinaliseVerificationError(f"No source digest for {original}")
            if file_digest(original, source_digests) != file_digest(item.path):
                raise FinaliseVerificationError(f"Copied file differs from its source: {item.path}")
    elif expected_pixels is None:
        for item, output in zip(rectify_result.files, sanitise_result.files, strict=True):
            source = (
                rectify_result.sources[item.path]
                if isinstance(rectify_result, RectifyPlan)
                else item.path
            )
            _verify_pixel_data(source, output)

    subject = rectify_result.subject
    scan_datetime = (
        f"{safe_filename_text(subject.scan_date)}-{safe_filename_text(subject.scan_time)}"
    )
    if archive_token is None:
        name = (
            f"{run.timestamp}_{safe_filename_text(subject_label)}_{scan_datetime}"
            f"_study-{subject.output_number:03d}_dichotomised-archive.tar.gz"
        )
    else:
        name = (
            f"{safe_filename_text(subject_label)}_{safe_filename_text(archive_token)}"
            f"_dichotomised-archive_{run.timestamp}.tar.gz"
        )
    archive_path = run.archives_dir / name
    archive = make_tarball(source_dir, archive_path, on_progress=on_progress)

    return FinaliseResult(
        subject=subject, subject_label=subject_label, archive=archive, source_dir=source_dir
    )


def _verify_sanitised_review(
    rectify_review_result: RectifyReviewResult | RectifyReviewPlan,
    sanitise_review_result: SanitiseReviewResult,
    expected_pixels: dict[Path, tuple[object, ...]] | None,
    expected_identities: dict[Path, tuple[str, str]] | None,
) -> None:
    """Check each sanitised review file against its source or the recorded fingerprints."""
    for item, output in zip(rectify_review_result.files, sanitise_review_result.files, strict=True):
        source = (
            rectify_review_result.sources[item.path]
            if isinstance(rectify_review_result, RectifyReviewPlan)
            else item.path
        )
        if expected_pixels is None:
            _verify_pixel_data(source, output)
        else:
            dataset = pydicom.dcmread(output)
            if _pixel_fingerprint(dataset) != expected_pixels[output]:
                raise FinaliseVerificationError(f"Pixel data differs from its source: {output}")
            if expected_identities is not None:
                _verify_identity(dataset, output, expected_identities[output])


def finalise_review(
    rectify_review_result: RectifyReviewResult | RectifyReviewPlan,
    run: Run,
    *,
    subject_label: str,
    archive_token: str,
    sanitise_review_result: SanitiseReviewResult | None = None,
    on_progress: ProgressCallback | None = None,
    expected_pixels: dict[Path, tuple[object, ...]] | None = None,
    expected_identities: dict[Path, tuple[str, str]] | None = None,
    verified_digests: dict[Path, str],
) -> Archive | None:
    """Verify the review tree and archive it, or return None when there are no review files.

    Archives the sanitised copy when `--sanitise` was used; otherwise the renamed but
    unsanitised review tree, whose files must match `verified_digests`. Runs unconditionally,
    independently of `--keep-working-files`.
    """
    if not rectify_review_result.files:
        return None
    source_dir = (
        sanitise_review_result.sanitised_review_dir
        if sanitise_review_result is not None
        else rectify_review_result.review_rectified_dir
    )
    expected = (
        sanitise_review_result.files
        if sanitise_review_result is not None
        else [item.path for item in rectify_review_result.files]
    )
    found = {path for path in source_dir.rglob("*") if path.is_file()}
    if found != set(expected):
        raise FinaliseVerificationError(
            f"Review output files do not match the expected files in {source_dir}"
        )
    if sanitise_review_result is not None:
        _verify_sanitised_review(
            rectify_review_result, sanitise_review_result, expected_pixels, expected_identities
        )
    else:
        for path in expected:
            if path not in verified_digests:
                raise FinaliseVerificationError(f"No source digest for {path}")
            if file_digest(path) != verified_digests[path]:
                raise FinaliseVerificationError(f"Review file differs from its source: {path}")
    name = (
        f"{safe_filename_text(subject_label)}_{safe_filename_text(archive_token)}"
        f"_review-archive_{run.timestamp}.tar.gz"
    )
    return make_tarball(source_dir, run.archives_dir / name, on_progress=on_progress)
