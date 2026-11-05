"""Stage 6 (optional, --sanitise): replace PatientName/PatientID in a copy of the rectified tree."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pydicom

from dichotomise.pydcm.naming import with_new_sop_instance_uid
from dichotomise.pydcm.read import DicomMetadata
from dichotomise.pydcm.relabel import (
    FrozenCache,
    Policy,
    _apply_action,
    _sanitise_file,
    format_subject_name,
    sanitise_many,
)
from dichotomise.stages.capture import CapturedSubject
from dichotomise.stages.rectify import (
    RectifyPlan,
    RectifyResult,
    RectifyReviewPlan,
    RectifyReviewResult,
)
from dichotomise.utils.text import safe_filename_text


@dataclass(frozen=True)
class SanitiseResult:
    """A subject's rectified files, copied into a new tree with the policy applied."""

    subject: CapturedSubject
    subject_label: str
    sanitised_dir: Path
    files: list[Path]


def _policy_path(
    target: Path,
    policy: Policy,
    description: str,
    subject_label: str,
    scan_date: str,
    replacement_cache: dict[str, str],
) -> Path:
    """Apply the policy's SeriesDescription action to the series folder name.

    A "removed" description on a folder that is already just the series number
    (rectify() leaves it out when the policy withholds it) gets no "NA" suffix.
    """
    instruction = policy.actions.get("SeriesDescription", {})
    action = instruction.get("action", "keep")
    if action == "keep" or (action == "remove" and "-" not in target.parent.name):
        return target
    dataset = pydicom.Dataset()
    dataset.SeriesDescription = description
    _apply_action(
        dataset,
        "SeriesDescription",
        instruction,
        {
            "<subject-label>": subject_label,
            "<subject-name>": format_subject_name(subject_label),
            "<scan-date>": scan_date,
        },
        replacement_cache,
    )
    description = safe_filename_text(str(dataset.get("SeriesDescription", "")))
    series = target.parent.name.partition("-")[0]
    return target.parent.parent / f"{series}-{description}" / target.name


def _renamed_for_uid(target: Path, old_uid: str, new_uid: str) -> Path:
    """Rename a sanitised file if its SOP Instance UID was regenerated.

    Keeps the filename equal to the header and stops the original UID (which
    Siemens builds from the acquisition timestamp) surviving in a filename.
    """
    if not old_uid or not new_uid or new_uid == old_uid:
        return target
    renamed = with_new_sop_instance_uid(target, old_uid, new_uid)
    if renamed != target:
        if renamed.exists():
            raise FileExistsError(f"Sanitised output already exists: {renamed}")
        target.rename(renamed)
    return renamed


def sanitise(
    rectify_result: RectifyResult | RectifyPlan,
    *,
    policy: Policy,
    subject_label: str,
    replacement_cache: dict[str, str] | None = None,
    expected_pixels: dict[Path, tuple[object, ...]] | None = None,
    expected_identities: dict[Path, tuple[str, str]] | None = None,
) -> SanitiseResult:
    """Write a sanitised copy of every rectified file, next to (not over) the original.

    The same replacement identifiers are used for every file (`replacement_cache` is
    shared across the whole subject), so a UID that appears in more than one
    file regenerates to the same new value everywhere. Pass an external
    `replacement_cache` to also share it with `sanitise_review()`, so a UID
    common to a retained and a review file still gets the same replacement.

    `expected_pixels` and `expected_identities` are for the pipeline: when given, each file's
    pixel fingerprint and identity are recorded there and `finalise()` does the read-back
    check. Left as None (a direct call), every file is re-read and checked as it is saved.
    """
    sanitised_dir = rectify_result.subject.working_dir / "sanitise"
    replacement_cache = {} if replacement_cache is None else replacement_cache
    files = _sanitise_files(
        rectify_result.files,
        rectify_result.rectified_dir,
        sanitised_dir,
        rectify_result.sources if isinstance(rectify_result, RectifyPlan) else {},
        policy,
        subject_label,
        rectify_result.subject.scan_date,
        replacement_cache,
        expected_pixels,
        expected_identities,
    )

    return SanitiseResult(
        subject=rectify_result.subject,
        subject_label=subject_label,
        sanitised_dir=sanitised_dir,
        files=files,
    )


@dataclass(frozen=True)
class SanitiseReviewResult:
    """A subject's rectified review files, copied into a new tree with the policy applied."""

    sanitised_review_dir: Path
    files: list[Path]


def sanitise_review(
    rectify_review_result: RectifyReviewResult | RectifyReviewPlan,
    *,
    policy: Policy,
    subject_label: str,
    scan_date: str,
    replacement_cache: dict[str, str],
    expected_pixels: dict[Path, tuple[object, ...]] | None = None,
    expected_identities: dict[Path, tuple[str, str]] | None = None,
) -> SanitiseReviewResult | None:
    """Write a sanitised copy of every rectified review file, or None if there are none.

    Filenames/paths are preserved as rectify_review() produced them, except
    that a regenerated SOP Instance UID replaces the original one in the
    filename; otherwise only DICOM tag content is scrubbed. Shares `replacement_cache` with the
    retained-files sanitise() call so a UID appearing in both a retained and
    a review file regenerates to the same new value. `expected_pixels` and
    `expected_identities` work as in `sanitise()`.
    """
    if not rectify_review_result.files:
        return None
    sanitised_review_dir = rectify_review_result.review_rectified_dir.parent / "sanitise-review"
    files = _sanitise_files(
        rectify_review_result.files,
        rectify_review_result.review_rectified_dir,
        sanitised_review_dir,
        rectify_review_result.sources
        if isinstance(rectify_review_result, RectifyReviewPlan)
        else {},
        policy,
        subject_label,
        scan_date,
        replacement_cache,
        expected_pixels,
        expected_identities,
    )
    return SanitiseReviewResult(sanitised_review_dir=sanitised_review_dir, files=files)


def _sanitise_files(
    metadata_files: list[DicomMetadata],
    source_dir: Path,
    target_dir: Path,
    sources: dict[Path, Path],
    policy: Policy,
    subject_label: str,
    scan_date: str,
    replacement_cache: dict[str, str],
    expected_pixels: dict[Path, tuple[object, ...]] | None,
    expected_identities: dict[Path, tuple[str, str]] | None,
) -> list[Path]:
    """Sanitise a list of rectified files into `target_dir` and return the output paths.

    Reads from `sources` when the rectify stage only planned the names (no intermediate copy),
    and records pixel fingerprints and identities in the optional `expected_*` dicts for finalise.
    """
    # Names are decided here, in order, before any file is written.
    targets: list[Path] = []
    for metadata in metadata_files:
        target = _policy_path(
            target_dir / metadata.path.relative_to(source_dir),
            policy,
            metadata.series_description,
            subject_label,
            scan_date,
            replacement_cache,
        )
        if target.exists() or target in targets:
            raise FileExistsError(f"Sanitised output already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        targets.append(target)
    tasks = [
        (sources.get(metadata.path, metadata.path), target)
        for metadata, target in zip(metadata_files, targets, strict=True)
    ]
    verify = expected_pixels is None
    if isinstance(replacement_cache, FrozenCache):
        results = sanitise_many(
            tasks,
            policy,
            subject_label=subject_label,
            scan_date=scan_date,
            cache=replacement_cache,
            verify=verify,
        )
    else:  # a direct call with a live cache: values are generated as the files go by
        results = [
            _sanitise_file(
                source,
                target,
                policy,
                subject_label=subject_label,
                scan_date=scan_date,
                replacement_cache=replacement_cache,
                verify=verify,
            )
            for source, target in tasks
        ]
    files: list[Path] = []
    for metadata, target, (new_uid, fingerprint, patient_id) in zip(
        metadata_files, targets, results, strict=True
    ):
        target = _renamed_for_uid(target, metadata.sop_instance_uid, new_uid)
        files.append(target)
        if expected_pixels is not None:
            expected_pixels[target] = fingerprint
        if expected_identities is not None:
            expected_identities[target] = (patient_id, new_uid)
    return files
