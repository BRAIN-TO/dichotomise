"""The structural checks dichotomise was built for: duplicate, conflicting and misfiled files."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

from dichotomise.pydcm.read import DicomMetadata
from dichotomise.utils import fs
from dichotomise.utils.text import natural_sort_key


def file_digest(path: Path, cache: MutableMapping[Path, str] | None = None) -> str:
    """SHA-256 of the file's raw bytes: equal only for byte-for-byte copies."""
    return fs.file_digest(path, cache)


def find_duplicate_pairs(
    paths: Sequence[Path], cache: MutableMapping[Path, str] | None = None
) -> dict[Path, Path]:
    """Map each byte-for-byte copy to the original it repeats.

    Only files with identical bytes count: a file that shares its SOP
    Instance UID or scan content but differs in any byte is not a duplicate.
    The first file in natural filename order (`2.dcm` before `10.dcm`) is the
    original; every later identical file is mapped to it.
    """
    by_size: dict[int, list[Path]] = {}
    for path in paths:
        by_size.setdefault(path.stat().st_size, []).append(path)
    pairs: dict[Path, Path] = {}
    for same_size in by_size.values():
        if len(same_size) < 2:
            continue
        originals: dict[str, Path] = {}
        for path in sorted(same_size, key=lambda item: natural_sort_key(item.name)):
            digest = file_digest(path, cache)
            if digest in originals:
                pairs[path] = originals[digest]
            else:
                originals[digest] = path
    return pairs


def find_duplicate_files(paths: Sequence[Path]) -> set[Path]:
    """Given files known to sit in the same folder, return the byte-for-byte copies."""
    return set(find_duplicate_pairs(paths))


def find_sop_conflicts(
    files: Sequence[DicomMetadata], cache: MutableMapping[Path, str] | None = None
) -> set[Path]:
    """Return files that share a SOP Instance UID with a file whose bytes differ.

    A SOP Instance UID names exactly one file. Two files that claim the same
    one but differ are a metadata error, not a duplicate, so every file in
    such a group is returned.
    """
    by_uid: dict[str, list[Path]] = {}
    for item in files:
        if item.sop_instance_uid:
            by_uid.setdefault(item.sop_instance_uid, []).append(item.path)
    conflicts: set[Path] = set()
    for group in by_uid.values():
        if len(group) > 1 and len({file_digest(path, cache) for path in group}) > 1:
            conflicts.update(group)
    return conflicts


_T = TypeVar("_T")


def _majority(values: Iterable[_T]) -> _T | None:
    """Return the only most common value, or None when there is no majority."""
    counts = Counter(values)
    if not counts:
        return None
    top_count = counts.most_common(1)[0][1]
    tied = [value for value, count in counts.items() if count == top_count]
    return tied[0] if len(tied) == 1 else None


def find_misfiled_files(
    files_by_folder: Mapping[Any, Sequence[DicomMetadata]],
) -> set[Path]:
    """Return files whose own series details disagree with their folder's majority.

    For each folder, the series identifier, series number, and scan protocol
    name that most files there agree on is treated as what the folder is
    supposed to contain. A file whose own details disagree with that
    majority is flagged. A scan that is legitimately split across more than
    one folder is not penalised by this check.
    """
    misfiled: set[Path] = set()
    for items in files_by_folder.values():
        if not items:
            continue
        dominant_uid = _majority(item.series_instance_uid for item in items)
        dominant_number = _majority(item.series_number for item in items)
        dominant_protocol = _majority(item.protocol_name for item in items)
        for item in items:
            if (
                item.series_instance_uid != dominant_uid
                or item.series_number != dominant_number
                or item.protocol_name != dominant_protocol
            ):
                misfiled.add(item.path)
    return misfiled
