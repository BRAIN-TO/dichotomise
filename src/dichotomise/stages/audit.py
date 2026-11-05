"""Stage 3: structural QA over the subject's DICOM files (duplicates, misfiled series)."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from dichotomise.errors import NoDicomFilesFoundError
from dichotomise.pydcm.acquisition import summarise_acquisition
from dichotomise.pydcm.identify import find_duplicate_pairs, find_misfiled_files, find_sop_conflicts
from dichotomise.pydcm.read import DicomMetadata, iter_dicom_metadata
from dichotomise.stages.capture import CapturedSubject, group_by_subject, subject_from_files
from dichotomise.utils.text import natural_sort_key, safe_filename_text

_CONSISTENCY_FIELDS: tuple[tuple[str, str], ...] = (
    ("PatientID", "patient_id"),
    ("PatientName", "patient_name"),
    ("StudyInstanceUID", "study_instance_uid"),
    ("StudyDate", "study_date"),
    ("StudyTime", "study_time"),
    ("SeriesInstanceUID", "series_instance_uid"),
    ("SeriesNumber", "series_number"),
    ("SeriesDescription", "series_description"),
    ("ProtocolName", "protocol_name"),
)


@dataclass(frozen=True)
class AuditedFile:
    """One file's scan metadata, plus the structural checks run against it.

    `is_duplicate` means a byte-for-byte copy of `duplicate_of`.
    `is_sop_conflict` means the SOP Instance UID is shared with a file whose
    bytes differ: a metadata error, never counted as a duplicate.
    """

    metadata: DicomMetadata
    is_duplicate: bool
    is_misfiled: bool
    is_sop_conflict: bool = False
    duplicate_of: Path | None = None
    series_folder: Path | None = None

    @property
    def folder(self) -> Path:
        """The series-level group this file is audited in.

        Normally its physical folder. For a folder that holds several series
        (see `_is_multi_series_folder`) it is a label for the file's own
        series, so the series checks never depend on how the export was laid out.
        """
        return self.series_folder or self.metadata.path.parent


@dataclass(frozen=True)
class AuditResult:
    """The structural findings for every file captured for one subject."""

    subject: CapturedSubject
    files: list[AuditedFile]


def metadata_inconsistencies(files: Sequence[AuditedFile]) -> list[str]:
    """Return identity and series fields whose values differ within one folder."""
    return [
        label
        for label, attribute in _CONSISTENCY_FIELDS
        if len({getattr(file.metadata, attribute) for file in files}) > 1
    ]


def audit(subject: CapturedSubject) -> AuditResult:
    """Check a captured subject's files for duplicates, UID conflicts and misfiled files.

    Runs against `subject.files` as capture() found them in place. No
    directory is re-scanned here.

    Misfiled and duplicate checks are computed per physical folder: a
    folder's own files decide, by majority, what series that folder is
    supposed to contain (see pydcm.identify.find_misfiled_files), and
    duplicates (byte-for-byte copies) are only looked for between files that
    already sit in the same folder. SOP Instance UID conflicts are checked
    across the whole subject.
    """
    return _audit_files(subject.files, subject)


def audit_directory(directory: Path) -> list[AuditResult]:
    """Audit DICOMs in place, one result per subject, without creating a run or copying files."""
    files_by_subject = group_by_subject(iter_dicom_metadata(directory))
    if not files_by_subject:
        raise NoDicomFilesFoundError(f"No DICOM files were found under {directory}")
    results = []
    for output_number, subject_files in enumerate(files_by_subject.values(), start=1):
        subject = subject_from_files(
            subject_files,
            source_root=directory,
            working_dir=directory,  # unused: QC mode never writes, so there's nothing to anchor
            output_number=output_number,
        )
        results.append(_audit_files(subject_files, subject))
    return results


_DICOM_SUFFIXES = (".dcm", ".ima", ".dicom")


def _is_named_after_own_uid(item: DicomMetadata) -> bool:
    """Whether the file's name ends with its own SOP Instance UID, ignoring a DICOM extension."""
    name = item.path.name
    for suffix in _DICOM_SUFFIXES:
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    return bool(item.sop_instance_uid) and name.endswith(item.sop_instance_uid)


def _is_multi_series_folder(items: Sequence[DicomMetadata]) -> bool:
    """Whether one physical folder evidently holds several series rather than one.

    That is the case for 3 or more distinct SeriesInstanceUIDs, or for 2 or
    more when most files are named after their own SOP Instance UID (the
    scanner's single-folder export). A folder of one series with strays from
    one other series is not: the strays are what the misfiled check finds.
    Known limit: strays from two different other series look like a
    container and are not flagged.
    """
    series = {item.series_instance_uid for item in items}
    if len(series) < 2:
        return False
    if len(series) >= 3:
        return True
    return sum(_is_named_after_own_uid(item) for item in items) * 2 > len(items)


def _series_folders(files_by_folder: Mapping[Path, Sequence[DicomMetadata]]) -> dict[Path, Path]:
    """Give each file of a multi-series folder a label for its own series.

    The label mirrors the scanner's `<description>_<number>_<modality>` folder
    names, with a counter if two series would share one.
    """
    series_folder: dict[Path, Path] = {}
    for folder, items in files_by_folder.items():
        if not _is_multi_series_folder(items):
            continue
        first_of_series: dict[str, DicomMetadata] = {}
        for item in items:
            first_of_series.setdefault(item.series_instance_uid, item)
        labels: dict[str, Path] = {}
        used: Counter[str] = Counter()
        for uid, item in sorted(
            first_of_series.items(), key=lambda entry: (entry[1].series_number, entry[0])
        ):
            parts = [item.series_description, str(item.series_number), item.modality]
            label = safe_filename_text("_".join(part for part in parts if part))
            used[label] += 1
            labels[uid] = folder / (label if used[label] == 1 else f"{label}-{used[label]}")
        for item in items:
            series_folder[item.path] = labels[item.series_instance_uid]
    return series_folder


def _audit_files(
    files: Sequence[DicomMetadata], subject: CapturedSubject, digests: dict[Path, str] | None = None
) -> AuditResult:
    """Compute audit findings for a subject's DICOM files, wherever they sit on disk."""
    files_by_folder: dict[Path, list[Path]] = defaultdict(list)
    metadata_by_folder: dict[Path, list[DicomMetadata]] = defaultdict(list)
    metadata_by_path: dict[Path, DicomMetadata] = {}

    for metadata in files:
        path = metadata.path
        metadata_by_path[path] = metadata
        files_by_folder[path.parent].append(path)
        metadata_by_folder[path.parent].append(metadata)

    if not metadata_by_path:
        raise NoDicomFilesFoundError(f"No DICOM files were found for subject {subject.subject_id}")

    digests = {} if digests is None else digests
    sop_conflicts = find_sop_conflicts(files, digests)

    duplicate_of: dict[Path, Path] = {}
    for paths in files_by_folder.values():
        # A byte-identical copy always shares its SOP Instance UID, so only
        # files whose UID repeats in the folder can possibly be copies.
        uid_counts = Counter(metadata_by_path[path].sop_instance_uid for path in paths)
        candidates = [
            path
            for path in paths
            if path not in sop_conflicts and uid_counts[metadata_by_path[path].sop_instance_uid] > 1
        ]
        duplicate_of.update(find_duplicate_pairs(candidates, digests))

    series_folder = _series_folders(metadata_by_folder)
    metadata_by_group: dict[Path, list[DicomMetadata]] = defaultdict(list)
    for metadata in files:
        metadata_by_group[series_folder.get(metadata.path, metadata.path.parent)].append(metadata)
    misfiled = find_misfiled_files(metadata_by_group)

    audited_files = [
        AuditedFile(
            metadata=metadata,
            is_duplicate=path in duplicate_of,
            is_misfiled=path in misfiled,
            is_sop_conflict=path in sop_conflicts,
            duplicate_of=duplicate_of.get(path),
            series_folder=series_folder.get(path),
        )
        for path, metadata in sorted(metadata_by_path.items())
    ]
    return AuditResult(subject=subject, files=audited_files)


_LOW_PRIORITY_MODALITIES = frozenset({"PR", "SR"})
_MAX_LISTED = 3


@dataclass(frozen=True)
class FolderNote:
    """Plain-language findings for one physical folder.

    `low_priority` marks folders of presentation states / structured reports,
    which downstream steps do not depend on, so their notes are listed last.
    """

    folder: str
    texts: list[str]
    low_priority: bool


def _stem_number(path: Path) -> int | None:
    """The file name as a number (`12.dcm` gives 12), or None if the name is not a plain number."""
    return int(path.stem) if path.stem.isdecimal() else None


def _span(numbers: Sequence[int]) -> str:
    """Compress sorted integers into runs, e.g. [1, 2, 3, 7] -> "1-3, 7"."""
    runs: list[list[int]] = []
    for number in sorted(set(numbers)):
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    return ", ".join(str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in runs)


def _file_span(paths: Sequence[Path]) -> str:
    """Name files compactly: "400.dcm", "401-799.dcm", or listed names if not numbered."""
    numbers = [_stem_number(path) for path in paths]
    if all(number is not None for number in numbers):
        suffix = paths[0].suffix
        return f"{_span([n for n in numbers if n is not None])}{suffix}"
    names = sorted(path.name for path in paths)
    shown = ", ".join(names[:_MAX_LISTED])
    return shown + (f" and {len(names) - _MAX_LISTED} more" if len(names) > _MAX_LISTED else "")


def _plural(count: int, singular: str, plural: str) -> str:
    """Choose the singular or plural word for a count."""
    return singular if count == 1 else plural


def _misfiled_notes(files: Sequence[AuditedFile]) -> list[str]:
    """Plain-language notes on files that belong to a different series than their folder."""
    misfiled = [file for file in files if file.is_misfiled]
    if not misfiled:
        return []
    belonging = [f.metadata.series_number for f in files if not f.is_misfiled]
    if not belonging:
        return [
            f"{_file_span([f.metadata.path for f in misfiled])} do not agree on one series, "
            "so none could be taken as the folder's series"
        ]
    dominant = max(set(belonging), key=belonging.count)
    groups: dict[tuple[int, str], list[AuditedFile]] = {}
    for file in misfiled:
        key = (file.metadata.series_number, file.metadata.series_description)
        groups.setdefault(key, []).append(file)
    notes: list[str] = []
    for (number, description), members in sorted(groups.items()):
        paths = [member.metadata.path for member in members]
        instances = {member.metadata.instance_number for member in members}
        verb = _plural(len(paths), "belongs", "belong")
        text = (
            f'{_file_span(paths)} {verb} to series {number} "{description}" '
            f"({_plural(len(instances), 'instance', 'instances')} {_span(sorted(instances))})"
        )
        if number != dominant:
            text += f", not series {dominant}"
        else:
            text += (
                ", but disagree with the rest of the folder in SeriesInstanceUID or ProtocolName"
            )
        notes.append(text)
    return notes


def _duplicate_runs(duplicates: dict[Path, Path]) -> tuple[list[str], int | None]:
    """Describe duplicate->original pairs as runs; also return the typical filename offset."""
    numbered = sorted(
        (dup_number, orig_number)
        for dup, orig in duplicates.items()
        if (dup_number := _stem_number(dup)) is not None
        and (orig_number := _stem_number(orig)) is not None
    )
    suffix = next((dup.suffix for dup in duplicates if _stem_number(dup) is not None), "")
    runs: list[list[tuple[int, int]]] = []
    for pair in numbered:
        if runs and pair[0] == runs[-1][-1][0] + 1 and pair[1] == runs[-1][-1][1] + 1:
            runs[-1].append(pair)
        else:
            runs.append([pair])
    texts = []
    for run in runs[:_MAX_LISTED]:
        (dup_lo, orig_lo), (dup_hi, orig_hi) = run[0], run[-1]
        if len(run) == 1:
            texts.append(f"{dup_lo}{suffix} is a byte-identical copy of {orig_lo}{suffix}")
        else:
            texts.append(
                f"{dup_lo}-{dup_hi}{suffix} are byte-identical copies of "
                f"{orig_lo}-{orig_hi}{suffix}"
            )
    if len(runs) > _MAX_LISTED:
        hidden = sum(len(run) for run in runs[_MAX_LISTED:])
        texts.append(f"and {hidden} more byte-identical {_plural(hidden, 'copy', 'copies')}")
    unnumbered = len(duplicates) - len(numbered)
    if unnumbered:
        texts.append(
            f"{unnumbered} other {_plural(unnumbered, 'file is a', 'files are')} byte-identical "
            f"{_plural(unnumbered, 'copy', 'copies')} of earlier files"
        )
    longest = max(runs, key=len)[0] if runs else None
    return texts, (longest[0] - longest[1]) if longest else None


def _duplicate_notes(files: Sequence[AuditedFile], *, compact: bool) -> list[str]:
    """Plain-language notes on byte-identical copies, in a compact form for PR/SR folders."""
    duplicates = {
        f.metadata.path: f.duplicate_of for f in files if f.is_duplicate and f.duplicate_of
    }
    if not duplicates:
        return []
    count = len(duplicates)
    if compact:
        written_twice = 2 * count == len(files)
        return [
            f"{count} byte-identical duplicate {_plural(count, 'file', 'files')}"
            + (" (the folder's contents are written twice)" if written_twice else "")
        ]
    texts, offset = _duplicate_runs(duplicates)
    notes = []
    prefix = "The folder's contents are written twice: " if 2 * count == len(files) else ""
    notes.append(prefix + "; ".join(texts))

    first_dup = min((n for n in map(_stem_number, duplicates) if n is not None), default=None)
    if first_dup is None or offset is None:
        return notes
    by_number = {_stem_number(f.metadata.path): f for f in files}
    kept: list[str] = []
    for file in files:
        number = _stem_number(file.metadata.path)
        if file.is_duplicate or file.is_misfiled or file.is_sop_conflict or number is None:
            continue
        if number <= first_dup:
            continue
        counterpart = by_number.get(number - offset)
        if counterpart is None:
            kept.append(
                f"{file.metadata.path.name} (instance {file.metadata.instance_number}) "
                "has no counterpart"
            )
        elif counterpart.is_misfiled:
            kept.append(
                f"{file.metadata.path.name} (instance {file.metadata.instance_number}) is kept: "
                f"its counterpart {counterpart.metadata.path.name} is misfiled"
            )
    if kept:
        notes.append(
            "; ".join(kept[:_MAX_LISTED])
            + (f"; and {len(kept) - _MAX_LISTED} more" if len(kept) > _MAX_LISTED else "")
        )
    return notes


def _conflict_notes(files: Sequence[AuditedFile]) -> list[str]:
    """Notes on files that share a SOP Instance UID but differ in content."""
    groups: dict[str, list[Path]] = {}
    for file in files:
        if file.is_sop_conflict:
            groups.setdefault(file.metadata.sop_instance_uid, []).append(file.metadata.path)
    notes = [
        f"{_file_span(paths)} share one SOP Instance UID but their contents differ; "
        "all are sent to review"
        for paths in list(groups.values())[:_MAX_LISTED]
    ]
    if len(groups) > _MAX_LISTED:
        notes.append(f"and {len(groups) - _MAX_LISTED} more SOP Instance UID conflicts")
    return notes


def _inconsistency_notes(files: Sequence[AuditedFile]) -> list[str]:
    """Notes on header fields that differ in a folder, beyond those a misfiled file explains."""
    full = metadata_inconsistencies(files)
    if not full:
        return []
    rest = [file for file in files if not file.is_misfiled]
    remaining = metadata_inconsistencies(rest) if rest and len(rest) < len(files) else full
    explained = [label for label in full if label not in remaining]
    notes = []
    if explained:
        count = len(files) - len(rest)
        notes.append(
            f"{', '.join(explained)} differ only because of the {count} misfiled "
            f"{_plural(count, 'file', 'files')}"
        )
    if remaining:
        notes.append(f"{', '.join(remaining)} differ between files in this folder")
    return notes


def describe_findings(
    files: Sequence[AuditedFile], *, compact_duplicates: bool = False
) -> list[str]:
    """Explain, in short sentences, what the checks found in one folder."""
    notes = _misfiled_notes(files)
    notes += _duplicate_notes(files, compact=compact_duplicates)
    notes += _conflict_notes(files)
    notes += _inconsistency_notes(files)
    mixed = sum(file.metadata.mixed_echoes for file in files)
    if mixed:
        notes.append(
            f"{mixed} {_plural(mixed, 'file holds', 'files hold')} more than one echo; "
            "their names use e01"
        )
    kept = [f.metadata for f in files if not (f.is_duplicate or f.is_misfiled or f.is_sop_conflict)]
    if kept:
        acquisition = summarise_acquisition(kept)
        if acquisition.is_multi_echo_or_ti:
            notes.append(f"Acquisition: {acquisition.describe()}")
    return notes


@dataclass(frozen=True)
class _FolderSummary:
    """The audit findings for one physical folder, shared by the CLI table and the reports."""

    folder: Path
    files: list[AuditedFile]
    inconsistencies: list[str]
    duplicate_count: int
    misfiled_count: int
    conflict_count: int
    low_priority: bool
    notes: list[str]


def _folder_summaries(audit_result: AuditResult) -> list[_FolderSummary]:
    """Summarise each folder's findings, in natural folder order (`2` before `10`)."""
    files_by_folder: dict[Path, list[AuditedFile]] = defaultdict(list)
    for file in audit_result.files:
        files_by_folder[file.folder].append(file)
    result: list[_FolderSummary] = []
    for folder, files in sorted(
        files_by_folder.items(), key=lambda item: (natural_sort_key(item[0].name), item[0])
    ):
        low_priority = all(f.metadata.modality in _LOW_PRIORITY_MODALITIES for f in files)
        result.append(
            _FolderSummary(
                folder,
                files,
                metadata_inconsistencies(files),
                sum(file.is_duplicate for file in files),
                sum(file.is_misfiled for file in files),
                sum(file.is_sop_conflict for file in files),
                low_priority,
                describe_findings(files, compact_duplicates=low_priority),
            )
        )
    return result


def folder_notes(audit_result: AuditResult) -> list[FolderNote]:
    """Notes for every folder that has any, imaging series first, PR/SR folders last."""
    result = [
        FolderNote(summary.folder.name, summary.notes, summary.low_priority)
        for summary in _folder_summaries(audit_result)
        if summary.notes
    ]
    return sorted(result, key=lambda note: note.low_priority)
