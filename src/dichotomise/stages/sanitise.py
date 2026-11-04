"""Stage 6 (optional, --sanitise): replace PatientName/PatientID in a copy of the rectified tree."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dichotomise.pydcm.naming import with_new_sop_instance_uid
from dichotomise.pydcm.relabel import Policy, sanitise_file
from dichotomise.stages.capture import CapturedSubject
from dichotomise.stages.rectify import (
    RectifyResult,
    RectifyReviewResult,
    _RectifyPlan,
    _RectifyReviewPlan,
)
from dichotomise.utils.text import safe_filename_text


@dataclass(frozen=True)
class SanitiseResult:
    """A subject's rectified files, copied into a new tree with the policy applied."""

    subject: CapturedSubject
    subject_label: str
    sanitised_dir: Path
    files: list[Path]


def _policy_path(target: Path, policy: Policy) -> Path:
    """Apply the policy's SeriesDescription to the folder name.

    A folder that is already just the series number (rectify() leaves the
    description out when the policy withholds it) gets no "NA" suffix.
    """
    instruction = policy.actions.get("SeriesDescription", {})
    action = instruction.get("action", "keep")
    if action == "remove":
        if "-" not in target.parent.name:
            return target
        description = "NA"
    elif action in ("replace", "add"):
        description = safe_filename_text(str(instruction.get("value", "")))
    else:
        return target
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
    rectify_result: RectifyResult,
    *,
    policy: Policy,
    subject_label: str,
    replacement_cache: dict[str, str] | None = None,
) -> SanitiseResult:
    """Write a sanitised copy of every rectified file, next to (not over) the original.

    The same replacement identifiers are used for every file (`replacement_cache` is
    shared across the whole subject), so a UID that appears in more than one
    file regenerates to the same new value everywhere. Pass an external
    `replacement_cache` to also share it with `sanitise_review()`, so a UID
    common to a retained and a review file still gets the same replacement.
    """
    return _sanitise(
        rectify_result,
        policy=policy,
        subject_label=subject_label,
        replacement_cache=replacement_cache,
    )


def _sanitise(
    rectify_result: RectifyResult | _RectifyPlan,
    *,
    policy: Policy,
    subject_label: str,
    replacement_cache: dict[str, str] | None = None,
) -> SanitiseResult:
    sanitised_dir = rectify_result.subject.working_dir / "sanitise"
    replacement_cache = {} if replacement_cache is None else replacement_cache
    files: list[Path] = []

    for metadata in rectify_result.files:
        target = _policy_path(
            sanitised_dir / metadata.path.relative_to(rectify_result.rectified_dir), policy
        )
        if target.exists():
            raise FileExistsError(f"Sanitised output already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        new_uid = sanitise_file(
            (
                rectify_result.sources[metadata.path]
                if isinstance(rectify_result, _RectifyPlan)
                else metadata.path
            ),
            target,
            policy,
            subject_label=subject_label,
            scan_date=rectify_result.subject.scan_date,
            replacement_cache=replacement_cache,
        )
        files.append(_renamed_for_uid(target, metadata.sop_instance_uid, new_uid))

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
    rectify_review_result: RectifyReviewResult,
    *,
    policy: Policy,
    subject_label: str,
    scan_date: str,
    replacement_cache: dict[str, str],
) -> SanitiseReviewResult | None:
    """Write a sanitised copy of every rectified review file, or None if there are none.

    Filenames/paths are preserved as rectify_review() produced them, except
    that a regenerated SOP Instance UID replaces the original one in the
    filename; otherwise only DICOM tag content is scrubbed. Shares `replacement_cache` with the
    retained-files sanitise() call so a UID appearing in both a retained and
    a review file regenerates to the same new value.
    """
    return _sanitise_review(
        rectify_review_result,
        policy=policy,
        subject_label=subject_label,
        scan_date=scan_date,
        replacement_cache=replacement_cache,
    )


def _sanitise_review(
    rectify_review_result: RectifyReviewResult | _RectifyReviewPlan,
    *,
    policy: Policy,
    subject_label: str,
    scan_date: str,
    replacement_cache: dict[str, str],
) -> SanitiseReviewResult | None:
    if not rectify_review_result.files:
        return None
    sanitised_review_dir = rectify_review_result.review_rectified_dir.parent / "sanitise-review"
    files: list[Path] = []
    for metadata in rectify_review_result.files:
        target = _policy_path(
            sanitised_review_dir
            / metadata.path.relative_to(rectify_review_result.review_rectified_dir),
            policy,
        )
        if target.exists():
            raise FileExistsError(f"Sanitised output already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        new_uid = sanitise_file(
            (
                rectify_review_result.sources[metadata.path]
                if isinstance(rectify_review_result, _RectifyReviewPlan)
                else metadata.path
            ),
            target,
            policy,
            subject_label=subject_label,
            scan_date=scan_date,
            replacement_cache=replacement_cache,
        )
        files.append(_renamed_for_uid(target, metadata.sop_instance_uid, new_uid))
    return SanitiseReviewResult(sanitised_review_dir=sanitised_review_dir, files=files)
