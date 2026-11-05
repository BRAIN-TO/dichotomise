"""The timestamped run folder and its standard subfolders."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dichotomise.errors import DestinationExistsError
from dichotomise.run import start_run


def test_start_run_creates_a_timestamped_output_folder(tmp_path: Path) -> None:
    fixed_now = datetime(2026, 9, 22, 14, 30, 12, tzinfo=UTC)

    run = start_run(tmp_path, now=fixed_now)

    assert run.timestamp == "20260922T143012Z"
    assert run.root == tmp_path / "20260922T143012Z_dichotomise_outputs"
    assert run.root.is_dir()


def test_run_exposes_the_agreed_output_subdirectories(tmp_path: Path) -> None:
    run = start_run(tmp_path, now=datetime(2026, 9, 22, 14, 30, 12, tzinfo=UTC))

    assert run.working_dir == run.root / "working"
    assert run.archives_dir == run.root / "archives"
    assert run.final_dir == run.root / "final"
    assert run.reports_dir == run.root / "reports"


def test_start_run_raises_a_plain_error_when_the_run_folder_already_exists(
    tmp_path: Path,
) -> None:
    fixed_now = datetime(2026, 9, 22, 14, 30, 12, tzinfo=UTC)
    start_run(tmp_path, now=fixed_now)

    with pytest.raises(DestinationExistsError):
        start_run(tmp_path, now=fixed_now)
