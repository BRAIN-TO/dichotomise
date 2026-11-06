"""Stage 5: copy retained files into metadata-named, series-ordered folders.

This is the pipeline's first and only copy of each file's bytes: capture()
and sift() work from the original source files in place, so rectify() (and
rectify_review() below) read straight from those original paths.

Each stage first *plans* (decides every target name, no file touched) and then
copies the plan, so the sanitise stage can write straight from the source files.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path

from dichotomise.errors import FinaliseVerificationError, NamingCollisionError
from dichotomise.pydcm.identify import file_digest
from dichotomise.pydcm.naming import rectified_path
from dichotomise.pydcm.read import DicomMetadata
from dichotomise.stages.capture import CapturedSubject
from dichotomise.stages.sift import SiftResult


@dataclass(frozen=True)
class RectifyResult:
    """A subject's retained files, copied into their sorted, clearly named tree."""

    subject: CapturedSubject
    rectified_dir: Path
    files: list[DicomMetadata]
    merged_identical_count: int = 0
    target_by_source: dict[Path, Path] = field(default_factory=dict)


@dataclass(frozen=True)
class RectifyPlan:
    """The decided names for a subject's retained files; nothing is copied yet."""

    subject: CapturedSubject
    rectified_dir: Path
    files: list[DicomMetadata]
    sources: dict[Path, Path]  # target -> the source file to copy there
    merged_identical_count: int
    target_by_source: dict[Path, Path]


def plan_rectify(
    sift_result: SiftResult,
    digests: dict[Path, str],
    *,
    describe_series: bool = True,
    rectified_dir: Path | None = None,
) -> RectifyPlan:
    """Decide each retained file's target name; merge identical repeats, refuse real clashes.

    The names go under `rectified_dir`, by default the subject's own `rectify/` working folder.
    """
    rectified_dir = rectified_dir or sift_result.subject.working_dir / "rectify"
    sources: dict[Path, Path] = {}
    files: list[DicomMetadata] = []
    target_by_source: dict[Path, Path] = {}
    merged_identical_count = 0
    for metadata in sift_result.retained:
        target = rectified_path(rectified_dir, metadata, describe_series=describe_series)
        if target in sources:
            if file_digest(sources[target], digests) != file_digest(metadata.path, digests):
                raise NamingCollisionError(
                    f"Two different scan files would both be named {target.name}: "
                    f"{sources[target]} and {metadata.path}"
                )
            merged_identical_count += 1
            target_by_source[metadata.path] = target
            continue
        sources[target] = metadata.path
        target_by_source[metadata.path] = target
        files.append(replace(metadata, path=target))
    return RectifyPlan(
        sift_result.subject, rectified_dir, files, sources, merged_identical_count, target_by_source
    )


def copy_planned(sources: dict[Path, Path], digests: dict[Path, str]) -> None:
    """Copy each planned file and check the copy matches its source."""
    for target, source in sources.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if file_digest(source, digests) != file_digest(target):
            raise FinaliseVerificationError(f"Copied file differs from its source: {target}")
        digests[target] = digests[source]


def rectify(
    sift_result: SiftResult,
    digests: dict[Path, str],
    *,
    describe_series: bool = True,
) -> RectifyResult:
    """Copy every retained file into its rectified name and folder.

    A byte-identical file that lands on a name already written is the same
    file again (for example a copy of an export in another folder), so it is
    not written or listed a second time (`merged_identical_count` counts
    them). Two different scan files that would land on the exact same
    rectified name is a real ambiguity about which file the name belongs to,
    so it stops the run rather than silently picking one or numbering around it.

    `digests` holds source-file SHA-256 digests (the pipeline passes the ones taken while
    archiving the source, and every copy is added), so finalise can check the copies against
    them; finalise reads them back from `target_by_source`.
    """
    plan = plan_rectify(sift_result, digests, describe_series=describe_series)
    copy_planned(plan.sources, digests)
    return RectifyResult(
        plan.subject,
        plan.rectified_dir,
        plan.files,
        plan.merged_identical_count,
        plan.target_by_source,
    )


@dataclass(frozen=True)
class RectifyReviewResult:
    """A subject's review files, renamed the same way retained files are."""

    review_rectified_dir: Path
    files: list[DicomMetadata]
    collision_count: int
    merged_identical_count: int = 0
    target_by_source: dict[Path, Path] = field(default_factory=dict)


@dataclass(frozen=True)
class RectifyReviewPlan:
    """The decided names for a subject's review files; nothing is copied yet."""

    review_rectified_dir: Path
    files: list[DicomMetadata]
    collision_count: int
    merged_identical_count: int
    sources: dict[Path, Path]  # target -> the source file to copy there
    target_by_source: dict[Path, Path]


def plan_rectify_review(
    sift_result: SiftResult, digests: dict[Path, str], *, describe_series: bool = True
) -> RectifyReviewPlan:
    """Decide every review file's target name, numbering clashes and merging identical repeats."""
    review_rectified_dir = sift_result.subject.working_dir / "review-rectify"
    written: dict[Path, dict[str, Path]] = {}  # name -> {content digest: final target}
    sources: dict[Path, Path] = {}
    files: list[DicomMetadata] = []
    target_by_source: dict[Path, Path] = {}
    collision_count = 0
    merged_identical_count = 0
    for item in sift_result.review:
        target = rectified_path(
            review_rectified_dir, item.metadata, describe_series=describe_series
        )
        digest = file_digest(item.metadata.path, digests)
        previous = written.setdefault(target, {})
        if digest in previous:
            merged_identical_count += 1
            target_by_source[item.metadata.path] = previous[digest]
            continue
        occurrence = len(previous)
        if occurrence:
            collision_count += 1
            target = target.with_name(f"{target.stem}-dup{occurrence:02d}{target.suffix}")
        previous[digest] = target
        sources[target] = item.metadata.path
        target_by_source[item.metadata.path] = target
        files.append(replace(item.metadata, path=target))
    return RectifyReviewPlan(
        review_rectified_dir,
        files,
        collision_count,
        merged_identical_count,
        sources,
        target_by_source,
    )


def rectify_review(
    sift_result: SiftResult, digests: dict[Path, str], *, describe_series: bool = True
) -> RectifyReviewResult:
    """Copy every review file into its rectified name, tolerating collisions.

    Uses the same naming as rectify() so a reviewer can see immediately
    which series/instance a review file's metadata claims to belong to --
    including when that claim is wrong (misfiled) or shared with another
    file (duplicate). A byte-identical file that lands on a name already
    written is the same file again, so it is not written a second time
    (`merged_identical_count` counts them). A name shared by files that
    differ keeps a disambiguated name (`-dup01`, `-dup02`, ...) instead of
    being silently dropped or overwritten. `digests` is the same source-digest cache that
    `rectify()` takes.
    """
    plan = plan_rectify_review(sift_result, digests, describe_series=describe_series)
    copy_planned(plan.sources, digests)
    return RectifyReviewResult(
        plan.review_rectified_dir,
        plan.files,
        plan.collision_count,
        plan.merged_identical_count,
        plan.target_by_source,
    )
