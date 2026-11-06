"""Sort mode: put DICOMs into the pipeline's subject and series folders, in place or as a copy.

This is the standalone `--sort` tool. `plan_sort` decides every target and writes nothing, so a
dry run is just a printed plan; `apply_sort` then moves or copies. Neither imports click or rich.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dichotomise.errors import DestinationExistsError, NamingCollisionError, SortError
from dichotomise.stages.capture import Fingerprint, capture, check_sources
from dichotomise.stages.rectify import copy_planned, plan_rectify
from dichotomise.stages.sift import SiftResult


@dataclass(frozen=True)
class SortPlan:
    """What a sort would do. Nothing has been touched when this exists."""

    root: Path  # the folder the sorted tree lives in: the source (in place) or the out dir
    copy: bool
    moves: list[tuple[Path, Path]]  # (source, target) to act on
    in_place_already: list[Path]  # files already at their target
    repeats: list[Path]  # byte-identical repeats: left alone in place, written once as a copy
    fingerprints: dict[Path, Fingerprint]


@dataclass(frozen=True)
class SortResult:
    """What a sort did."""

    moved: int
    copied: int


def plan_sort(source_dir: Path, out_dir: Path | None = None) -> SortPlan:
    """Decide where every DICOM under `source_dir` goes; refuse anything unsafe, touch nothing.

    With no `out_dir` the files are moved inside `source_dir`, which must hold them all directly
    (or already at their target, so an interrupted sort can be re-run). With an `out_dir` they
    are copied there, whatever the source layout.
    """
    copy = out_dir is not None
    root = out_dir or source_dir
    if out_dir is not None and source_dir.resolve() in {
        out_dir.resolve(),
        *out_dir.resolve().parents,
    }:
        raise SortError(
            "--out-dir must be outside --source-dir, or its files would be sorted again."
        )
    subjects, fingerprints = capture(source_dir, root)
    moves: list[tuple[Path, Path]] = []
    already: list[Path] = []
    repeats: list[Path] = []
    for subject in subjects:
        try:
            plan = plan_rectify(
                SiftResult(subject, subject.files, []), {}, rectified_dir=subject.working_dir
            )
        except NamingCollisionError as error:
            raise NamingCollisionError(
                f"{error}. Sorting stops on a clash; the full pipeline sends such files to review."
            ) from error
        for target, source in plan.sources.items():
            if source == target:
                already.append(source)
                continue
            if not copy and source.parent != source_dir:
                raise SortError(
                    f"{source_dir} already has DICOMs in subfolders; give --out-dir to sort a copy."
                )
            if target.exists():
                raise DestinationExistsError(f"Sorting would overwrite an existing file: {target}")
            for folder in (target.parent, target.parent.parent):  # series and subject folders
                if folder.exists() and not folder.is_dir():
                    raise DestinationExistsError(
                        f"Sorting needs a folder at {folder}, but a file is there."
                    )
            moves.append((source, target))
        repeats += [p for p, t in plan.target_by_source.items() if plan.sources[t] != p]
    return SortPlan(root, copy, moves, already, repeats, fingerprints)


def apply_sort(plan: SortPlan) -> SortResult:
    """Carry out a plan: move the files (or copy them, checking each copy), then confirm."""
    check_sources({source: plan.fingerprints[source] for source, _ in plan.moves})
    done = 0
    try:
        for source, target in plan.moves:
            if target.exists():  # rename would silently replace it
                raise DestinationExistsError(f"Sorting would overwrite an existing file: {target}")
            if plan.copy:
                copy_planned({target: source}, {})
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                source.rename(target)
                after = os.stat(target)
                # A rename changes the inode's ctime, so compare the other four fields only.
                seen = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                if seen != plan.fingerprints[source][:4] or source.exists():
                    raise SortError(f"{target} does not match the file that was moved there")
            done += 1
    except OSError as error:
        finish = (
            "Remove the partial --out-dir before trying again."
            if plan.copy
            else "Run the same command again to finish."
        )
        raise SortError(
            f"Stopped after {done} of {len(plan.moves)} files ({error}). {finish}"
        ) from error
    return SortResult(moved=0 if plan.copy else done, copied=done if plan.copy else 0)
