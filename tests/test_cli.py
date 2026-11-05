"""Command-line behaviour: options, messages and the files a full run writes."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from click import unstyle
from click.testing import CliRunner

import dichotomise.cli as cli_module
from dichotomise.cli import cli


def test_interactive_interrupt_stops_work(tmp_path: Path) -> None:
    marker = tmp_path / "finished"
    script = """
import importlib, os, signal, sys, threading, time
from pathlib import Path
from rich.console import Console
cli = importlib.import_module('dichotomise.cli')
cli.console = Console(force_terminal=True)
def work():
    threading.Timer(0.1, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
    time.sleep(1)
    Path(sys.argv[1]).write_text('finished')
try:
    cli._run_with_elapsed_ticker(time.monotonic(), work)
except KeyboardInterrupt:
    pass
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(marker)], capture_output=True, timeout=5, check=False
    )
    assert result.returncode == 0, result.stderr.decode()
    assert not marker.exists()


def test_cli_runs_end_to_end_and_reports_the_archives(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm", PatientID="sub-01", StudyInstanceUID="study-a")
    out_dir = tmp_path / "out"

    result = CliRunner().invoke(cli, ["--source-dir", str(source), "--out-dir", str(out_dir)])

    assert result.exit_code == 0, result.output
    # archives/ holds both the source archive and the dichotomised archive.
    archives = list(out_dir.glob("*_dichotomise_outputs/archives/*.tar.gz"))
    assert len(archives) == 2
    dichotomised_archives = [a for a in archives if "_dichotomised-archive_" in a.name]
    source_archives = [a for a in archives if "_source-archive_" in a.name]
    assert len(dichotomised_archives) == 1
    assert len(source_archives) == 1
    assert dichotomised_archives[0].name in result.output
    assert "DICOM inventory" in result.output
    assert "Series folder" in result.output
    assert "Pass" in result.output
    assert "Started: Study 1: archiving source DICOMs" in result.output
    assert "Completed: Study 1: creating verified archive" in result.output
    assert "Audit and archive summary" in result.output
    assert "dichotomise v" in result.output
    assert f"Source: {source}" in result.output
    # Shows the actual created, timestamped run folder, not just out_dir.
    assert f"Output: {dichotomised_archives[0].parent.parent}" in result.output
    assert "Found 1 subject, 1 DICOM file" in result.output
    assert "total time" in result.output


def test_cli_reports_when_pigz_is_found(
    tmp_path: Path, make_dicom_file: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "pigz_path", lambda: "/usr/bin/pigz")
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm", PatientID="sub-01", StudyInstanceUID="study-a")

    result = CliRunner().invoke(
        cli, ["--source-dir", str(source), "--out-dir", str(tmp_path / "out")]
    )

    assert result.exit_code == 0, result.output
    assert "pigz found, using it." in result.output


def test_cli_says_nothing_about_pigz_when_it_is_not_found(
    tmp_path: Path, make_dicom_file: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "pigz_path", lambda: None)
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm", PatientID="sub-01", StudyInstanceUID="study-a")

    result = CliRunner().invoke(
        cli, ["--source-dir", str(source), "--out-dir", str(tmp_path / "out")]
    )

    assert result.exit_code == 0, result.output
    assert "pigz" not in result.output


def test_cli_help_groups_required_and_optional_flags() -> None:
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    assert "Input (required)" in result.output
    assert "Output (standard runs)" in result.output
    assert "QC mode" in result.output
    assert "Optional flags" in result.output
    assert "Usage" in result.output
    assert "Single-Subject mode" in result.output
    assert "Multi-Subject mode" in result.output
    assert "[required]" not in result.output
    assert "--sanitise-policy" in result.output
    assert "--sanitise-level" not in result.output
    assert "--subj-id" in result.output
    assert "--subject-id" not in result.output


def test_cli_qc_prints_a_separate_table_for_each_subject(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "scanner_export"
    for patient in ("sub-01", "sub-02"):
        folder = source / patient
        for number in (3, 4, 5):
            make_dicom_file(
                folder / f"s{number}.dcm",
                PatientID=patient,
                StudyInstanceUID=f"study-{patient}",
                SeriesInstanceUID=f"1.2.{number}",
                SeriesNumber=number,
                SeriesDescription=f"scan{number}",
                ProtocolName=f"scan{number}",
            )

    result = CliRunner().invoke(cli, ["--source-dir", str(source), "--qc"])

    assert result.exit_code == 0, result.output
    assert result.output.count("DICOM inventory") == 2
    assert "sub-01" in result.output
    assert "sub-02" in result.output
    assert "misfiled" not in result.output
    for number in (3, 4, 5):
        assert result.output.count(f"scan{number}_{number}") == 2


def test_cli_qc_audits_in_place_without_creating_output(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm", PatientID="sub-01", StudyInstanceUID="study-a")
    out_dir = tmp_path / "out"

    result = CliRunner().invoke(cli, ["--qc", "--source-dir", str(source)])

    assert result.exit_code == 0, result.output
    assert "dichotomise v" in result.output
    assert f"Source: {source}" in result.output
    assert "QC Mode" in result.output
    assert "Output:" not in result.output
    assert "DICOM inventory" in result.output
    assert "Series folder" in result.output
    assert "QC complete in" in result.output
    assert "no files or folders were created." in result.output
    assert not out_dir.exists()


def test_cli_qc_rejects_an_output_directory(tmp_path: Path) -> None:
    source = tmp_path / "export"
    source.mkdir()

    result = CliRunner().invoke(
        cli,
        ["--qc", "--source-dir", str(source), "--out-dir", str(tmp_path / "out")],
        env={"FORCE_COLOR": "1"},
    )

    assert result.exit_code != 0
    assert "--qc only accepts --source-dir" in unstyle(result.output)


def test_cli_qc_rejects_keep_unzipped(tmp_path: Path) -> None:
    source = tmp_path / "export"
    source.mkdir()

    result = CliRunner().invoke(
        cli, ["--qc", "--source-dir", str(source), "--keep-unzipped"], env={"FORCE_COLOR": "1"}
    )

    assert result.exit_code != 0
    assert "--qc only accepts --source-dir" in unstyle(result.output)


def test_cli_keep_unzipped_writes_the_final_tree_alongside_the_archive(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm", PatientID="sub-01", StudyInstanceUID="study-a")
    out_dir = tmp_path / "out"

    result = CliRunner().invoke(
        cli, ["--source-dir", str(source), "--out-dir", str(out_dir), "--keep-unzipped"]
    )

    assert result.exit_code == 0, result.output
    run_dir = next(out_dir.glob("*_dichotomise_outputs"))
    unzipped = list(run_dir.glob("final/*/**/*.dcm"))
    assert len(unzipped) == 1
    assert not run_dir.joinpath("working").exists()


def test_cli_requires_output_directory_outside_qc(tmp_path: Path) -> None:
    source = tmp_path / "export"
    source.mkdir()

    result = CliRunner().invoke(cli, ["--source-dir", str(source)], env={"FORCE_COLOR": "1"})

    assert result.exit_code != 0
    assert "--out-dir is required unless --qc is used." in unstyle(result.output)


def test_cli_audit_table_reports_metadata_and_structural_findings(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    series_folder = source / "nested-export" / "study-20260914" / "BTO_hires_fieldmap"
    first_file = make_dicom_file(
        series_folder / "1.dcm", PatientID="sub-01", StudyInstanceUID="study-a"
    )
    shutil.copy2(first_file, series_folder / "2.dcm")
    make_dicom_file(
        series_folder / "3.dcm",
        PatientID="sub-01",
        StudyInstanceUID="study-a",
        SeriesNumber=2,
        SeriesDescription="other_series",
        ProtocolName="other_series",
    )

    result = CliRunner().invoke(
        cli, ["--source-dir", str(source), "--out-dir", str(tmp_path / "out")]
    )

    assert result.exit_code == 0, result.output
    assert "Failed" in result.output
    assert "BTO_hires_fieldmap" in result.output
    assert "nested-export/study-20260914" not in result.output


def test_cli_sanitise_infers_numerical_mode_from_subject_id(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm", PatientID="sub-01", StudyInstanceUID="study-a")
    out_dir = tmp_path / "out"

    result = CliRunner().invoke(
        cli,
        [
            "--source-dir",
            str(source),
            "--out-dir",
            str(out_dir),
            "--sanitise",
            "--subj-id",
            "5",
        ],
    )

    assert result.exit_code == 0, result.output
    archives = list(out_dir.glob("*_dichotomise_outputs/archives/*_dichotomised-archive_*.tar.gz"))
    assert "sub-0005" in archives[0].name


def test_cli_new_id_adds_the_sub_prefix(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm", PatientID="scanner-42", StudyInstanceUID="study-a")
    out_dir = tmp_path / "out"

    result = CliRunner().invoke(
        cli,
        [
            "--source-dir",
            str(source),
            "--out-dir",
            str(out_dir),
            "--sanitise",
            "--new-id",
            "ADNC0751",
        ],
    )

    assert result.exit_code == 0, result.output
    archives = list(out_dir.glob("*_dichotomise_outputs/archives/*_dichotomised-archive_*.tar.gz"))
    assert archives[0].name.startswith("sub-ADNC0751_")


def test_cli_automatically_enumerates_a_numerical_multi_study_run(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "one" / "1.dcm", PatientID="source-01", StudyInstanceUID="study-a")
    make_dicom_file(source / "two" / "1.dcm", PatientID="source-02", StudyInstanceUID="study-b")

    result = CliRunner().invoke(
        cli,
        [
            "--source-dir",
            str(source),
            "--out-dir",
            str(tmp_path / "out"),
            "--sanitise",
            "--subj-id",
            "6",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Warning: Multiple studies found with --subj-id" in result.output
    archives = list((tmp_path / "out").glob("*_dichotomise_outputs/archives/*.tar.gz"))
    assert any(archive.name.startswith("sub-0006_") for archive in archives)
    assert any(archive.name.startswith("sub-0007_") for archive in archives)


def test_cli_loads_a_json_mapping_file_when_its_extension_is_omitted(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm", PatientID="scanner-42", StudyInstanceUID="study-a")
    mapping_path = tmp_path / "labels.json"
    mapping_path.write_text(
        '{"instructions": ["This entry is ignored."], "scanner-42": "sub-0042"}'
    )
    out_dir = tmp_path / "out"

    result = CliRunner().invoke(
        cli,
        [
            "--source-dir",
            str(source),
            "--out-dir",
            str(out_dir),
            "--sanitise",
            "--mapping-file",
            str(mapping_path.with_suffix("")),
        ],
    )

    assert result.exit_code == 0, result.output
    archives = list(out_dir.glob("*_dichotomise_outputs/archives/*_dichotomised-archive_*.tar.gz"))
    assert archives[0].name.startswith("sub-0042_")


def test_cli_accepts_a_custom_sanitise_policy(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(
        source / "series" / "1.dcm",
        PatientID="sub-01",
        StudyInstanceUID="study-a",
        ProtocolName="DWI",
    )
    out_dir = tmp_path / "out"

    result = CliRunner().invoke(
        cli,
        [
            "--source-dir",
            str(source),
            "--out-dir",
            str(out_dir),
            "--sanitise",
            "--subj-id",
            "1",
            "--sanitise-policy",
            "custom.json",
        ],
    )

    assert result.exit_code == 0, result.output


def test_cli_reports_a_plain_error_for_an_unknown_sanitise_policy(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm", PatientID="sub-01", StudyInstanceUID="study-a")
    out_dir = tmp_path / "out"

    result = CliRunner().invoke(
        cli,
        [
            "--source-dir",
            str(source),
            "--out-dir",
            str(out_dir),
            "--sanitise",
            "--subj-id",
            "1",
            "--sanitise-policy",
            "does-not-exist",
        ],
    )

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "does-not-exist" in result.output


def test_cli_rejects_sanitise_options_without_the_sanitise_flag(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm", PatientID="sub-01", StudyInstanceUID="study-a")
    out_dir = tmp_path / "out"

    result = CliRunner().invoke(
        cli,
        ["--source-dir", str(source), "--out-dir", str(out_dir), "--subj-id", "5"],
    )

    assert result.exit_code != 0
    assert "--sanitise" in result.output


def test_cli_reports_a_plain_error_when_no_dicom_files_are_found(tmp_path: Path) -> None:
    source = tmp_path / "export"
    source.mkdir()
    (source / "readme.txt").write_text("nothing here")
    out_dir = tmp_path / "out"

    result = CliRunner().invoke(cli, ["--source-dir", str(source), "--out-dir", str(out_dir)])

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "No DICOM files" in result.output


def test_cli_random_name_implies_sanitise_with_the_minimal_policy(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(
        source / "series" / "1.dcm",
        PatientID="sub-01",
        PatientName="Doe^Jane^19900101",
        StudyInstanceUID="study-a",
    )
    out_dir = tmp_path / "out"

    result = CliRunner().invoke(
        cli, ["--source-dir", str(source), "--out-dir", str(out_dir), "--random-name"]
    )

    assert result.exit_code == 0, result.output
    archives = list(out_dir.glob("*_dichotomise_outputs/archives/*_dichotomised-archive_*.tar.gz"))
    assert len(archives) == 1
    # --sanitise wasn't passed explicitly, but --random-name implies it.
    assert "sub-01" not in archives[0].name


def test_cli_random_name_conflicting_with_sanitise_policy_is_an_error(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm", PatientID="sub-01", StudyInstanceUID="study-a")
    out_dir = tmp_path / "out"

    result = CliRunner().invoke(
        cli,
        [
            "--source-dir",
            str(source),
            "--out-dir",
            str(out_dir),
            "--random-name",
            "--sanitise-policy",
            "full",
        ],
    )

    assert result.exit_code != 0
    assert "--random-name" in result.output


def test_cli_qc_labels_a_subject_without_a_patient_id(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm", PatientID="", StudyInstanceUID="study-a")

    result = CliRunner().invoke(cli, ["--source-dir", str(source), "--qc"])

    assert result.exit_code == 0, result.output
    assert "no PatientID" in result.output
