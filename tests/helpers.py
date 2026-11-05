"""Builders shared by several test modules."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from dichotomise.stages.capture import CapturedSubject


def make_subject(working_dir: Path, source_root: Path | None = None) -> CapturedSubject:
    """A subject with no files, for stages that only need its folders and identifiers."""
    return CapturedSubject(
        subject_id="sub-01",
        patient_name="Doe^Jane^19900101",
        study_instance_uid="study-a",
        scan_date="20260101",
        scan_time="120000",
        files=[],
        source_root=source_root or working_dir,
        working_dir=working_dir,
    )


def force_pool(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Make every `_parallel_map` use a pool, even for two items, and record each pool run.

    Returns a list that gains one entry per pool map that ran (finished or raised, but did not
    break); a test asserts it is not empty so a run that silently fell back to serial cannot
    pass as a parallel one.
    """
    from concurrent.futures import ProcessPoolExecutor
    from concurrent.futures.process import BrokenProcessPool

    from dichotomise.pydcm import read

    started: list[int] = []

    class CountingPool(ProcessPoolExecutor):
        def map(self, *args: Any, **kwargs: Any) -> Any:
            try:
                return list(super().map(*args, **kwargs))
            except BrokenProcessPool:  # the work is about to be redone serially: not a pool run
                raise
            finally:
                if sys.exc_info()[0] is not BrokenProcessPool:
                    started.append(1)

    monkeypatch.setattr(read, "_PARALLEL_MIN_FILES", 1)
    monkeypatch.setattr(read, "_worker_count", lambda: 2)
    monkeypatch.setattr(read, "ProcessPoolExecutor", CountingPool)
    return started
