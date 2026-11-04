"""run_pipeline(): runs every stage in order and returns the finished Run."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from secrets import token_hex
from time import monotonic
from typing import TypeVar

from dichotomise.errors import RelabelError, UnsafeSourceError
from dichotomise.pydcm.names import generate_name
from dichotomise.pydcm.relabel import (
    Policy,
    load_policy,
    normalise_numeric_label,
    validate_label,
)
from dichotomise.run import Run, start_run
from dichotomise.stages.audit import AuditResult, _audit_files
from dichotomise.stages.capture import CapturedSubject, capture
from dichotomise.stages.finalise import _finalise, _finalise_review
from dichotomise.stages.rectify import (
    _plan_rectify,
    _plan_rectify_review,
    _rectify,
    _rectify_review,
)
from dichotomise.stages.reports import (
    pseudonymised_audit,
    pseudonymous_source_names,
    source_map_final_files,
    write_audit_report,
    write_finalise_report,
    write_retained_manifest,
    write_review_manifest,
    write_sift_report,
    write_source_manifest,
    write_source_map,
)
from dichotomise.stages.sanitise import _sanitise, _sanitise_review
from dichotomise.stages.sift import sift
from dichotomise.stages.source_archive import _source_archive
from dichotomise.utils.archive import Archive
from dichotomise.utils.text import safe_filename_text

_T = TypeVar("_T")
StageCallback = Callable[[str, str, float], None]
WarningCallback = Callable[[str], None]
ArchiveProgressCallback = Callable[[str, int, int], None]


def _run_stage(description: str, operation: Callable[[], _T], on_stage: StageCallback | None) -> _T:
    """Run a pipeline stage and report its lifecycle to an optional caller."""
    if on_stage is not None:
        on_stage("started", description, 0.0)
    started = monotonic()
    try:
        result = operation()
    except BaseException:
        if on_stage is not None:
            on_stage("failed", description, monotonic() - started)
        raise
    if on_stage is not None:
        on_stage("completed", description, monotonic() - started)
    return result


def _random_subject_label(used_labels: set[str]) -> str:
    """Generate a unique, filesystem-safe pseudonym for one subject in this run."""
    while len(used_labels) < 100_000:
        given_name, _, surname = generate_name().rpartition("_")
        label = f"{surname}_{given_name}"
        if label not in used_labels:
            used_labels.add(label)
            return label
    raise RelabelError("Could not generate a unique pseudonym for every subject in this run")


def _resolve_subject_label(
    subject: CapturedSubject,
    label_mode: str,
    subject_id: str | None,
    new_id: str | None,
    subject_mappings: Mapping[str, str] | None,
    used_labels: set[str],
) -> str:
    """Work out the replacement label a sanitised run should use for one subject."""
    if subject_mappings is not None:
        try:
            return validate_label(subject_mappings[subject.subject_id])
        except KeyError as error:
            raise RelabelError(
                f"No replacement ID was supplied for source subject {subject.subject_id!r}"
            ) from error
    if label_mode == "numerical":
        if not subject_id:
            raise RelabelError("Numerical replacement labels need a numerical subject ID")
        start_number = int(normalise_numeric_label(subject_id).removeprefix("sub-"))
        return normalise_numeric_label(str(start_number + subject.output_number - 1))
    if label_mode == "custom":
        if not new_id:
            raise RelabelError("Custom replacement labels need a replacement ID")
        label = validate_label(new_id)
        return label if label.startswith("sub-") else f"sub-{label}"
    if label_mode == "random":
        return _random_subject_label(used_labels)
    raise RelabelError(f"Unsupported replacement-label mode: {label_mode}")


def _random_output_token(used_tokens: set[str]) -> str:
    """Return a unique six-character hexadecimal token for one study output."""
    while len(used_tokens) < 16_777_216:
        token = token_hex(3)
        if token not in used_tokens:
            used_tokens.add(token)
            return token
    raise RelabelError("Could not create a unique output token for every study")


def _prepare_labels(
    subjects: list[CapturedSubject],
    sanitise_requested: bool,
    label_mode: str,
    subject_id: str | None,
    new_id: str | None,
    subject_mappings: Mapping[str, str] | None,
    used_labels: set[str],
) -> list[str]:
    if not sanitise_requested:
        return [subject.subject_id for subject in subjects]
    return [
        _resolve_subject_label(
            subject, label_mode, subject_id, new_id, subject_mappings, used_labels
        )
        for subject in subjects
    ]


def _reports_dir_for(run: Run, subject_label: str, output_token: str) -> Path:
    """Return this study's collision-safe directory under reports/."""
    return run.reports_dir / f"{safe_filename_text(subject_label)}_{output_token}"


def _final_dir_for(run: Run, subject_label: str, output_token: str) -> Path:
    """Return this study's collision-safe directory under final/ (--keep-unzipped)."""
    return run.final_dir / f"{safe_filename_text(subject_label)}_{output_token}"


def _archive_source_subject(
    subject: CapturedSubject,
    run: Run,
    description: str,
    on_archive_progress: ArchiveProgressCallback | None,
) -> tuple[Archive, dict[Path, str], dict[Path, tuple[int, int, int, int, int]]]:
    """Archive one captured study before it is audited or transformed."""
    on_progress = (
        (lambda done, total: on_archive_progress(description, done, total))
        if on_archive_progress is not None
        else None
    )
    fingerprints = {item.path: _fingerprint(item.path) for item in subject.files}
    digests: dict[Path, str] = {}
    archive = _source_archive(subject, run, on_progress=on_progress, source_digests=digests)
    _check_sources(fingerprints)
    return archive, digests, fingerprints


def _source_map_path(source_archive_path: Path) -> Path:
    """Name the source map after the source archive it sits beside."""
    stem = source_archive_path.name.removesuffix(".tar.gz")
    return source_archive_path.with_name(stem.replace("_source-archive_", "_source-map_") + ".csv")


def _fingerprint(path: Path) -> tuple[int, int, int, int, int]:
    """Identify a file's on-disk state, so a later change can be detected."""
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _check_sources(fingerprints: dict[Path, tuple[int, int, int, int, int]]) -> None:
    """Stop the run if any source file changed since it was archived."""
    for path, expected in fingerprints.items():
        try:
            actual = _fingerprint(path)
        except OSError as error:
            raise UnsafeSourceError(f"Source is no longer accessible: {path}") from error
        if actual != expected:
            raise UnsafeSourceError(f"Source changed during processing: {path}")


def _process_subject(
    subject: CapturedSubject,
    run: Run,
    source_archive_path: Path,
    *,
    sanitise_requested: bool,
    sanitise_level: str,
    policy: Policy | None,
    source_digests: dict[Path, str],
    source_fingerprints: dict[Path, tuple[int, int, int, int, int]],
    used_output_tokens: set[str],
    resolved_label: str,
    keep_working_files: bool,
    keep_unzipped: bool,
    on_audit: Callable[[AuditResult], None] | None,
    on_stage: StageCallback | None,
    on_archive_progress: ArchiveProgressCallback | None,
) -> None:
    # The replacement label depends only on `subject` and the CLI's own
    # inputs, not on anything audit/sift/rectify produce, so it can be
    # resolved upfront and every report written straight to its final
    # location, rather than staged under a temporary name and renamed later.
    subject_label = resolved_label
    output_token = _random_output_token(used_output_tokens)
    reports_dir = _reports_dir_for(run, subject_label, output_token)
    # A sanitised run's reports name files by position, never by the source
    # filename, folder or UID, which can carry a participant code or scan time.
    names = pseudonymous_source_names(subject.files) if sanitise_requested else None
    # A policy that removes or replaces the series description must not leave it in
    # folder names or reports either.
    describe_series = policy is None or policy.keeps("SeriesDescription")
    write_source_manifest(subject, reports_dir, names=names)

    def _bind_progress(description: str) -> Callable[[int, int], None] | None:
        if on_archive_progress is None:
            return None
        return lambda done, total: on_archive_progress(description, done, total)

    subject_prefix = f"Study {subject.output_number}"
    _check_sources(source_fingerprints)
    audit_result = _run_stage(
        f"{subject_prefix}: auditing",
        lambda: _audit_files(subject.files, subject, source_digests),
        on_stage,
    )
    if on_audit is not None:
        on_audit(audit_result)
    write_audit_report(
        pseudonymised_audit(audit_result, names, hide_series_descriptions=not describe_series)
        if names
        else audit_result,
        reports_dir,
        subject_label=subject_label,
    )

    sift_result = _run_stage(
        f"{subject_prefix}: sorting review files", lambda: sift(audit_result), on_stage
    )
    write_retained_manifest(sift_result, reports_dir, names=names)
    write_review_manifest(sift_result, reports_dir, names=names)
    source_map_path = _source_map_path(source_archive_path)
    if names:
        write_source_map(source_map_path, sift_result, names)

    _check_sources(source_fingerprints)
    direct = sanitise_requested and not keep_working_files
    rectify_result = _run_stage(
        f"{subject_prefix}: rectifying retained files",
        lambda: (
            _plan_rectify(sift_result, source_digests, describe_series=describe_series)
            if direct
            else _rectify(sift_result, source_digests, describe_series=describe_series)
        ),
        on_stage,
    )
    rectify_review_result = _run_stage(
        f"{subject_prefix}: renaming review files",
        lambda: (
            _plan_rectify_review(sift_result, source_digests, describe_series=describe_series)
            if direct
            else _rectify_review(sift_result, source_digests, describe_series=describe_series)
        ),
        on_stage,
    )

    sanitise_result = None
    sanitise_review_result = None
    if policy is not None:
        replacement_cache: dict[str, str] = {}
        sanitise_result = _run_stage(
            f"{subject_prefix}: sanitising files",
            lambda: _sanitise(
                rectify_result,
                policy=policy,
                subject_label=subject_label,
                replacement_cache=replacement_cache,
            ),
            on_stage,
        )
        sanitise_review_result = _run_stage(
            f"{subject_prefix}: sanitising review files",
            lambda: _sanitise_review(
                rectify_review_result,
                policy=policy,
                subject_label=subject_label,
                scan_date=subject.scan_date,
                replacement_cache=replacement_cache,
            ),
            on_stage,
        )

    review_archive_description = f"{subject_prefix}: archiving review files"
    review_archive = _run_stage(
        review_archive_description,
        lambda: _finalise_review(
            rectify_review_result,
            run,
            subject_label=subject_label,
            archive_token=output_token,
            sanitise_review_result=sanitise_review_result,
            on_progress=_bind_progress(review_archive_description),
        ),
        on_stage,
    )
    write_sift_report(
        sift_result,
        reports_dir,
        rectify_result=rectify_result,
        rectify_review_result=rectify_review_result,
        review_archive=review_archive,
        review_sanitised=sanitise_requested,
        names=names,
    )

    finalise_description = f"{subject_prefix}: creating verified archive"
    _check_sources(source_fingerprints)
    finalise_result = _run_stage(
        finalise_description,
        lambda: _finalise(
            rectify_result,
            run,
            subject_label=subject_label,
            sanitise_result=sanitise_result,
            archive_token=output_token,
            on_progress=_bind_progress(finalise_description),
            source_digests=source_digests,
        ),
        on_stage,
    )
    file_count = len(sanitise_result.files) if sanitise_result else len(rectify_result.files)
    write_finalise_report(
        finalise_result,
        reports_dir,
        file_count=file_count,
        sanitised=sanitise_requested,
        sanitise_level=sanitise_level if sanitise_requested else None,
        audit_result=audit_result,
        rectify_result=rectify_result,
        sanitise_result=sanitise_result,
        names=names,
        describe_series=describe_series,
    )
    if names:
        write_source_map(
            source_map_path,
            sift_result,
            names,
            source_map_final_files(
                sift_result,
                rectify_result,
                rectify_review_result,
                sanitise_result,
                sanitise_review_result,
            ),
        )

    if keep_unzipped:
        final_dir = _final_dir_for(run, subject_label, output_token)
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        _run_stage(
            f"{subject_prefix}: keeping the final files unzipped",
            lambda: (
                shutil.copytree(finalise_result.source_dir, final_dir)
                if keep_working_files
                else finalise_result.source_dir.rename(final_dir)
            ),
            on_stage,
        )


def run_pipeline(
    source_dir: Path,
    out_dir: Path,
    *,
    sanitise_requested: bool = False,
    sanitise_level: str = "minimal",
    label_mode: str = "random",
    subject_id: str | None = None,
    new_id: str | None = None,
    subject_mappings: Mapping[str, str] | None = None,
    keep_working_files: bool = False,
    keep_unzipped: bool = False,
    on_audit: Callable[[AuditResult], None] | None = None,
    on_stage: StageCallback | None = None,
    on_warning: WarningCallback | None = None,
    on_info: WarningCallback | None = None,
    on_archive_progress: ArchiveProgressCallback | None = None,
    on_run_start: Callable[[Run], None] | None = None,
) -> Run:
    """Run the full dichotomise pipeline over every subject found under `source_dir`.

    `sanitise_level` selects a policy filename (see docs/sanitise-policies.md).
    The command line chooses the internal label mode from the selected policy
    and any numerical, exact, or mapped replacement ID supplied.

    `keep_unzipped`, independent of `keep_working_files`, additionally copies
    each subject's final (optionally sanitised) tree into `final/`,
    unarchived, e.g. for a BIDS conversion that wants plain files on disk
    rather than a tarball.

    `on_run_start`, if given, is called with the `Run` immediately once its
    timestamped output folder exists, before anything else, so a caller
    can report the actual created directory rather than the `out_dir` it
    will be created under.
    """
    run = start_run(out_dir)
    if on_run_start is not None:
        on_run_start(run)
    try:
        subjects = _run_stage(
            "Capturing DICOMs by study", lambda: capture(source_dir, run), on_stage
        )
        if on_info is not None:
            total_files = sum(len(subject.files) for subject in subjects)
            subject_word = "subject" if len(subjects) == 1 else "subjects"
            file_word = "file" if total_files == 1 else "files"
            on_info(f"Found {len(subjects)} {subject_word}, {total_files} DICOM {file_word}")
        if new_id is not None and len(subjects) != 1:
            raise RelabelError("--new-id may only be used when the source contains one subject.")
        if subject_id is not None and len(subjects) != 1 and on_warning is not None:
            on_warning(
                "Multiple studies found with --subj-id; numbering replacement IDs sequentially."
            )
        if subject_mappings is not None:
            source_ids = {subject.subject_id for subject in subjects}
            unknown_ids = set(subject_mappings) - source_ids
            if unknown_ids:
                raise RelabelError(
                    "Mappings were supplied for source IDs not found in this export: "
                    + ", ".join(sorted(unknown_ids))
                )
        used_labels: set[str] = set()
        used_output_tokens: set[str] = set()
        labels = _prepare_labels(
            subjects,
            sanitise_requested,
            label_mode,
            subject_id,
            new_id,
            subject_mappings,
            used_labels,
        )
        policy = load_policy(sanitise_level) if sanitise_requested else None
        for subject, resolved_label in zip(subjects, labels, strict=True):
            archive_source_description = f"Study {subject.output_number}: archiving source DICOMs"
            source_archive_result, source_digests, source_fingerprints = _run_stage(
                archive_source_description,
                partial(
                    _archive_source_subject,
                    subject,
                    run,
                    archive_source_description,
                    on_archive_progress,
                ),
                on_stage,
            )
            _process_subject(
                subject,
                run,
                source_archive_result.path,
                sanitise_requested=sanitise_requested,
                sanitise_level=sanitise_level,
                policy=policy,
                source_digests=source_digests,
                source_fingerprints=source_fingerprints,
                used_output_tokens=used_output_tokens,
                resolved_label=resolved_label,
                keep_working_files=keep_working_files,
                keep_unzipped=keep_unzipped,
                on_audit=on_audit,
                on_stage=on_stage,
                on_archive_progress=on_archive_progress,
            )
        if not keep_working_files:
            _run_stage("Cleaning working files", lambda: shutil.rmtree(run.working_dir), on_stage)
    except BaseException:
        run.set_status("failed")
        raise
    run.set_status("complete")
    return run
