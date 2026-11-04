"""Writes the three numbered, plain-language JSON reports for one subject.

Named by pipeline order (stage-01/02/03), not by internal stage names, so a
report can be found without knowing what "audit"/"sift" mean. See
docs/sanitise-policies.md and README.md for the reports/ folder layout.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pydicom

from dichotomise.pydcm.acquisition import infer_ti_index, summarise_acquisition
from dichotomise.pydcm.read import DicomMetadata
from dichotomise.stages.audit import (
    AuditedFile,
    AuditResult,
    describe_findings,
    metadata_inconsistencies,
)
from dichotomise.stages.capture import CapturedSubject
from dichotomise.stages.finalise import FinaliseResult
from dichotomise.stages.rectify import (
    RectifyResult,
    RectifyReviewResult,
    _RectifyPlan,
    _RectifyReviewPlan,
)
from dichotomise.stages.sanitise import SanitiseResult, SanitiseReviewResult
from dichotomise.stages.sift import SiftResult
from dichotomise.utils.archive import Archive
from dichotomise.utils.text import natural_sort_key


def _relative(path: Path, base: Path) -> str:
    return str(path.relative_to(base))


def pseudonymous_source_names(files: Sequence[DicomMetadata]) -> dict[Path, Path]:
    """Map each source file to a neutral `folder-NN/NNNN` name for a sanitised run's reports.

    Source filenames and folder names can carry identifiers: a participant
    code, the scan date and time, or the original SOP Instance UID. A
    sanitised run's reports refer to files by position instead; the way back
    to the real names is the source map written next to the source archive.
    """
    by_folder: dict[Path, list[Path]] = {}
    for metadata in files:
        by_folder.setdefault(metadata.path.parent, []).append(metadata.path)
    folder_width = max(2, len(str(len(by_folder))))
    names: dict[Path, Path] = {}
    folders = sorted(by_folder, key=lambda folder: (natural_sort_key(folder.name), folder))
    for folder_number, folder in enumerate(folders, 1):
        paths = sorted(by_folder[folder], key=lambda path: (natural_sort_key(path.name), path))
        file_width = max(4, len(str(len(paths))))
        for file_number, path in enumerate(paths, 1):
            names[path] = Path(f"folder-{folder_number:0{folder_width}d}") / (
                f"{file_number:0{file_width}d}"
            )
    return names


def pseudonymised_audit(
    audit_result: AuditResult,
    names: Mapping[Path, Path],
    *,
    hide_series_descriptions: bool = False,
) -> AuditResult:
    """Return `audit_result` with every source path replaced by its pseudonymous name.

    With `hide_series_descriptions`, each distinct series description becomes
    `description-N` (in order of first appearance), so findings that compare
    descriptions still work without printing them.
    """
    labels: dict[str, str] = {}
    groups: dict[Path, dict[Path, int]] = {}
    for audited_file in audit_result.files:
        group = audited_file.series_folder
        if group is not None:
            groups.setdefault(group.parent, {})[group] = audited_file.metadata.series_number
    # Numbered by the series number, never by the label, which starts with the description.
    series_number = {
        group: number
        for siblings in groups.values()
        for number, group in enumerate(
            sorted(siblings, key=lambda item: (siblings[item], natural_sort_key(item.name))), 1
        )
    }

    shown_names: dict[tuple[Path, Path], Path] = {}

    def shown_group(audited_file: AuditedFile) -> Path | None:
        group = audited_file.series_folder
        if group is None:
            return None
        parent = names[audited_file.metadata.path].parent
        return shown_names.setdefault(
            (parent, group), parent / f"series-{series_number[group]:03d}"
        )

    def shown(description: str) -> str:
        if not hide_series_descriptions:
            return description
        return labels.setdefault(description, f"description-{len(labels) + 1}")

    files = [
        replace(
            audited_file,
            metadata=replace(
                audited_file.metadata,
                path=names[audited_file.metadata.path],
                series_description=shown(audited_file.metadata.series_description),
            ),
            duplicate_of=names[audited_file.duplicate_of] if audited_file.duplicate_of else None,
            series_folder=shown_group(audited_file),
        )
        for audited_file in audit_result.files
    ]
    return replace(audit_result, files=files)


def _write(reports_dir: Path, filename: str, data: dict[str, object]) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / filename
    report_path.write_text(json.dumps(data, indent=2) + "\n")
    return report_path


def _audit_series_rows(audit_result: AuditResult) -> list[dict[str, object]]:
    """Summarise structural and metadata findings for each physical DICOM folder."""
    files_by_folder: dict[Path, list[AuditedFile]] = {}
    for audited_file in audit_result.files:
        files_by_folder.setdefault(audited_file.folder, []).append(audited_file)

    rows: list[dict[str, object]] = []
    for folder, files in sorted(
        files_by_folder.items(), key=lambda item: (natural_sort_key(item[0].name), item[0])
    ):
        inconsistencies = metadata_inconsistencies(files)
        duplicate_count = sum(file.is_duplicate for file in files)
        misfiled_count = sum(file.is_misfiled for file in files)
        conflict_count = sum(file.is_sop_conflict for file in files)
        low_priority = all(f.metadata.modality in {"PR", "SR"} for f in files)
        rows.append(
            {
                "series_folder": folder.name,
                "dicom_count": len(files),
                "duplicate_count": duplicate_count,
                "misfiled_count": misfiled_count,
                "sop_conflict_count": conflict_count,
                "metadata_inconsistencies": "; ".join(inconsistencies),
                "status": "flagged"
                if duplicate_count or misfiled_count or conflict_count or inconsistencies
                else "pass",
                "notes": describe_findings(files, compact_duplicates=low_priority),
            }
        )
    return rows


def _write_audit_csv(reports_dir: Path, rows: list[dict[str, object]]) -> Path:
    """Write the spreadsheet-friendly audit inventory alongside its JSON report."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / "stage-01-audit.csv"
    fieldnames = [
        "series_folder",
        "dicom_count",
        "duplicate_count",
        "misfiled_count",
        "sop_conflict_count",
        "metadata_inconsistencies",
        "status",
        "notes",
    ]
    with report_path.open("w", newline="") as report_file:
        writer = csv.DictWriter(report_file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "notes": " | ".join(row["notes"])})  # type: ignore[arg-type]
    return report_path


def write_audit_report(audit_result: AuditResult, reports_dir: Path, *, subject_label: str) -> Path:
    """Write stage-01-report.json: what the structural checks found.

    `subject_label` is the run's public label (the real PatientID, or the
    replacement label if --sanitise was used). The report never reads
    `audit_result.subject.subject_id` directly, since that is always the raw
    PatientID and this report can sit inside a sanitised run's reports/
    folder.
    """
    series_rows = _audit_series_rows(audit_result)
    duplicates = sorted(
        {file.folder.name for file in audit_result.files if file.is_duplicate},
        key=natural_sort_key,
    )
    misfiled = sorted(
        {file.folder.name for file in audit_result.files if file.is_misfiled},
        key=natural_sort_key,
    )
    data = {
        "stage": "audit",
        "subject_label": subject_label,
        "total_files": len(audit_result.files),
        "duplicate_count": sum(file.is_duplicate for file in audit_result.files),
        "misfiled_count": sum(file.is_misfiled for file in audit_result.files),
        "sop_conflict_count": sum(file.is_sop_conflict for file in audit_result.files),
        "duplicates": duplicates,
        "misfiled": misfiled,
        "metadata_inconsistency_count": sum(
            bool(row["metadata_inconsistencies"]) for row in series_rows
        ),
        "series": series_rows,
    }
    report_path = _write(reports_dir, "stage-01-report.json", data)
    _write_audit_csv(reports_dir, series_rows)
    return report_path


def write_sift_report(
    sift_result: SiftResult,
    reports_dir: Path,
    *,
    rectify_result: RectifyResult | _RectifyPlan | None = None,
    rectify_review_result: RectifyReviewResult | _RectifyReviewPlan | None = None,
    review_archive: Archive | None = None,
    review_sanitised: bool = False,
    names: Mapping[Path, Path] | None = None,
) -> Path:
    """Write stage-02-report.json: what was retained versus sent for review."""
    review = [
        {
            "file": (
                names[item.metadata.path].as_posix()
                if names
                else _relative(item.metadata.path, sift_result.subject.source_root)
            ),
            "reason": item.reason,
        }
        for item in sift_result.review
    ]
    data = {
        "stage": "sift",
        "retained_count": len(sift_result.retained),
        "retained_identical_files_not_rewritten": (
            rectify_result.merged_identical_count if rectify_result is not None else 0
        ),
        "review_count": len(sift_result.review),
        "review": review,
        "review_rename_collisions": (
            rectify_review_result.collision_count if rectify_review_result is not None else 0
        ),
        "review_identical_files_not_rewritten": (
            rectify_review_result.merged_identical_count if rectify_review_result is not None else 0
        ),
        "review_archive": review_archive.path.name if review_archive is not None else None,
        "review_archive_checksum": (
            review_archive.checksum_path.read_text().split()[0]
            if review_archive is not None
            else None
        ),
        "review_archive_sanitised": review_sanitised,
    }
    return _write(reports_dir, "stage-02-report.json", data)


def _write_manifest_csv(
    files: list[Path],
    root: Path,
    manifest_path: Path,
    names: Mapping[Path, Path] | None = None,
) -> Path:
    """Write a CSV of file/size/modified/created, paths relative to `root`.

    With `names`, each file is listed under its pseudonymous name instead.

    "date_created" uses st_birthtime where the OS provides it (macOS/BSD),
    falling back to st_ctime (metadata-change time, not true creation time)
    on Linux, since Linux exposes no creation time via Python's os.stat.
    """
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["file", "size_bytes", "date_modified", "date_created"]
        )
        writer.writeheader()
        for path in sorted(files, key=lambda item: names[item] if names else item):
            stat = path.stat()
            created = getattr(stat, "st_birthtime", stat.st_ctime)
            writer.writerow(
                {
                    "file": names[path].as_posix() if names else _relative(path, root),
                    "size_bytes": stat.st_size,
                    "date_modified": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                    "date_created": datetime.fromtimestamp(created, tz=UTC).isoformat(),
                }
            )
    return manifest_path


def write_source_manifest(
    subject: CapturedSubject, reports_dir: Path, *, names: Mapping[Path, Path] | None = None
) -> Path:
    """Write source-manifest.csv: every captured source file, size and timestamps."""
    files = sorted(metadata.path for metadata in subject.files)
    return _write_manifest_csv(
        files, subject.source_root, reports_dir / "source-manifest.csv", names
    )


def write_retained_manifest(
    sift_result: SiftResult, reports_dir: Path, *, names: Mapping[Path, Path] | None = None
) -> Path:
    """Write retained-manifest.csv: every file that passed sift, size and timestamps."""
    files = [item.path for item in sift_result.retained]
    return _write_manifest_csv(
        files, sift_result.subject.source_root, reports_dir / "retained-manifest.csv", names
    )


def write_review_manifest(
    sift_result: SiftResult, reports_dir: Path, *, names: Mapping[Path, Path] | None = None
) -> Path | None:
    """Write review-manifest.csv, or None if nothing was sent to review."""
    if not sift_result.review:
        return None
    files = [item.metadata.path for item in sift_result.review]
    return _write_manifest_csv(
        files, sift_result.subject.source_root, reports_dir / "review-manifest.csv", names
    )


def _series_inventory(
    audit_result: AuditResult,
    rectify_result: RectifyResult | _RectifyPlan,
    sanitise_result: SanitiseResult | None,
    names: Mapping[Path, Path] | None = None,
    describe_series: bool = True,
) -> list[dict[str, object]]:
    """Return one filename mapping for the first retained DICOM in each series.

    With `names` (a sanitised run), the source file is listed under its
    pseudonymous name and the series under its reissued SeriesInstanceUID.
    """
    source_by_series: dict[str, list[AuditedFile]] = {}
    for audited_file in audit_result.files:
        source_by_series.setdefault(audited_file.metadata.series_instance_uid, []).append(
            audited_file
        )

    rectified_by_series: dict[str, list[DicomMetadata]] = {}
    for metadata in rectify_result.files:
        rectified_by_series.setdefault(metadata.series_instance_uid, []).append(metadata)

    final_path_by_rectified = (
        dict(zip((m.path for m in rectify_result.files), sanitise_result.files, strict=True))
        if sanitise_result is not None
        else {}
    )

    inventory: list[tuple[tuple[int, str, str], dict[str, object]]] = []
    for series_uid, source_files in source_by_series.items():
        first_source = min(
            source_files,
            key=lambda item: (item.metadata.instance_number, item.metadata.path.name),
        )
        rectified_files = rectified_by_series.get(series_uid, [])
        first_rectified = (
            min(rectified_files, key=lambda item: (item.instance_number, item.path.name))
            if rectified_files
            else None
        )
        after_name = None
        if first_rectified is not None:
            after_name = final_path_by_rectified.get(
                first_rectified.path, first_rectified.path
            ).name
        metadata = first_source.metadata
        acquisition = summarise_acquisition(rectified_files)
        shown_series_uid: str | None = series_uid
        if names is not None:
            shown_series_uid = None
            if first_rectified is not None:
                final_path = final_path_by_rectified.get(first_rectified.path)
                if final_path is not None:
                    header = pydicom.dcmread(final_path, stop_before_pixels=True)
                    shown_series_uid = str(header.get("SeriesInstanceUID", "")) or None
        entry: dict[str, object] = {
            "series_number": metadata.series_number,
            "series_description": metadata.series_description if describe_series else None,
            "series_instance_uid": shown_series_uid,
            "source_file_count": len(source_files),
            "archived_file_count": len(rectified_files),
            "review_file_count": sum(
                item.is_duplicate or item.is_misfiled or item.is_sop_conflict
                for item in source_files
            ),
            "first_dicom_before": (names[metadata.path] if names else metadata.path).name,
            "first_dicom_after": after_name,
        }
        if acquisition.is_multi_echo_or_ti:
            ti_index = infer_ti_index(rectified_files)
            entry["acquisition"] = acquisition.describe()
            entry["files"] = [
                {
                    "file": final_path_by_rectified.get(item.path, item.path).name,
                    "echo_number": item.echo_number,
                    "echo_time_ms": item.echo_time,
                    "asl_context": item.asl_context,
                    "ti_index_inferred": ti_index.get(item.path),
                }
                for item in sorted(rectified_files, key=lambda f: (f.instance_number, f.path.name))
            ]
        inventory.append(((metadata.series_number, metadata.series_description, series_uid), entry))
    return [entry for _, entry in sorted(inventory)]


def write_finalise_report(
    finalise_result: FinaliseResult,
    reports_dir: Path,
    *,
    file_count: int,
    sanitised: bool,
    sanitise_level: str | None,
    audit_result: AuditResult | None = None,
    rectify_result: RectifyResult | _RectifyPlan | None = None,
    sanitise_result: SanitiseResult | None = None,
    names: Mapping[Path, Path] | None = None,
    describe_series: bool = True,
) -> Path:
    """Write stage-03-report.json: archive details and per-series filename mappings."""
    data = {
        "stage": "finalise",
        "subject_label": finalise_result.subject_label,
        "archive": finalise_result.archive.path.name,
        "checksum": finalise_result.archive.checksum_path.read_text().split()[0],
        "file_count": file_count,
        "sanitised": sanitised,
        "sanitise_level": sanitise_level,
    }
    if audit_result is not None and rectify_result is not None:
        data["series_inventory"] = _series_inventory(
            audit_result, rectify_result, sanitise_result, names, describe_series
        )
    return _write(reports_dir, "stage-03-report.json", data)


def source_map_final_files(
    sift_result: SiftResult,
    rectify_result: RectifyResult | _RectifyPlan,
    rectify_review_result: RectifyReviewResult | _RectifyReviewPlan,
    sanitise_result: SanitiseResult | None,
    sanitise_review_result: SanitiseReviewResult | None,
) -> dict[Path, str]:
    """Map each source file to its final path inside the dichotomised or review archive."""
    final_tree = sanitise_result.sanitised_dir if sanitise_result else rectify_result.rectified_dir
    final_path_by_rectified = (
        dict(zip((m.path for m in rectify_result.files), sanitise_result.files, strict=True))
        if sanitise_result is not None
        else {}
    )
    review_tree = (
        sanitise_review_result.sanitised_review_dir
        if sanitise_review_result
        else rectify_review_result.review_rectified_dir
    )
    final_review_by_rectified = (
        dict(
            zip(
                (m.path for m in rectify_review_result.files),
                sanitise_review_result.files,
                strict=True,
            )
        )
        if sanitise_review_result is not None
        else {}
    )
    final_files: dict[Path, str] = {}
    for item in sift_result.retained:
        target = rectify_result.target_by_source[item.path]
        final = final_path_by_rectified.get(target, target)
        final_files[item.path] = final.relative_to(final_tree).as_posix()
    for review_item in sift_result.review:
        target = rectify_review_result.target_by_source[review_item.metadata.path]
        final = final_review_by_rectified.get(target, target)
        final_files[review_item.metadata.path] = final.relative_to(review_tree).as_posix()
    return final_files


def write_source_map(
    map_path: Path,
    sift_result: SiftResult,
    names: Mapping[Path, Path],
    final_files: Mapping[Path, str] | None = None,
) -> Path:
    """Write the CSV that links a sanitised run's pseudonymous report names to the real ones.

    It holds real source paths, so it sits in archives/ beside the source
    archive, which already holds the real identity, and never in reports/.
    It is written once up front, so a run that fails partway still leaves the
    way back to the real names, and again at the end with `final_files`
    filled in (it is blank until then).
    """
    rows = [(item.path, "retained") for item in sift_result.retained] + [
        (item.metadata.path, f"review:{item.reason}") for item in sift_result.review
    ]
    map_path.parent.mkdir(parents=True, exist_ok=True)
    with map_path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["report_name", "source_file", "size_bytes", "status", "final_file"]
        )
        writer.writeheader()
        for source, status in sorted(rows, key=lambda row: names[row[0]]):
            writer.writerow(
                {
                    "report_name": names[source].as_posix(),
                    "source_file": _relative(source, sift_result.subject.source_root),
                    "size_bytes": source.stat().st_size,
                    "status": status,
                    "final_file": final_files[source] if final_files else "",
                }
            )
    return map_path
