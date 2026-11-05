"""End-to-end runs of `run_pipeline`: archives, reports, sanitising and the fail-closed checks."""

from __future__ import annotations

import csv
import json
import re
import shutil
import tarfile
from collections.abc import Callable
from pathlib import Path

import pydicom
import pytest

import dichotomise.stages.capture as capture_module
from dichotomise.errors import FinaliseVerificationError, RelabelError, UnsafeSourceError
from dichotomise.pipeline import run_pipeline


def test_run_pipeline_produces_one_archive_per_subject_and_cleans_up_working_files(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(
        source / "DWI_21_MR" / "1.dcm",
        PatientID="sub-01",
        StudyInstanceUID="study-a",
        SeriesInstanceUID="series-a",
        SeriesNumber=21,
        StudyDate="20260101",
        StudyTime="120000",
    )
    make_dicom_file(
        source / "other" / "1.dcm",
        PatientID="sub-02",
        StudyInstanceUID="study-b",
        SeriesInstanceUID="series-b",
        SeriesNumber=1,
        StudyDate="20260102",
        StudyTime="130000",
    )

    run = run_pipeline(source, tmp_path / "out")

    # Source and dichotomised archives now share one archives/ folder.
    source_archives = sorted(run.archives_dir.glob("*_source-archive_*.tar.gz"))
    final_archives = sorted(run.archives_dir.glob("*_dichotomised-archive_*.tar.gz"))
    assert len(source_archives) == 2
    assert any(path.name.startswith("sub-01_") for path in source_archives)
    assert any(path.name.startswith("sub-02_") for path in source_archives)
    assert len(final_archives) == 2
    assert any("sub-01" in p.name for p in final_archives)
    assert any("sub-02" in p.name for p in final_archives)
    assert not run.working_dir.exists()

    with tarfile.open(final_archives[0], "r:gz") as tar:
        names = tar.getnames()
    assert any(name.endswith(".dcm") for name in names)


def test_run_pipeline_reports_subject_and_file_counts_via_on_info(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "a" / "1.dcm", PatientID="sub-01", StudyInstanceUID="study-a")
    make_dicom_file(source / "b" / "1.dcm", PatientID="sub-02", StudyInstanceUID="study-b")
    messages: list[str] = []

    run_pipeline(source, tmp_path / "out", on_info=messages.append)

    assert "Found 2 subjects, 2 DICOM files" in messages


def test_run_pipeline_reports_archive_progress_up_to_the_total(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm", PatientID="sub-01", StudyInstanceUID="study-a")
    updates: list[tuple[str, int, int]] = []

    def record(description: str, done: int, total: int) -> None:
        updates.append((description, done, total))

    run_pipeline(source, tmp_path / "out", on_archive_progress=record)

    assert updates
    by_description: dict[str, list[tuple[int, int]]] = {}
    for description, done, total in updates:
        by_description.setdefault(description, []).append((done, total))
    for calls in by_description.values():
        last_done, last_total = calls[-1]
        assert last_done == last_total


def test_run_pipeline_does_not_cross_contaminate_subjects_sharing_one_source_folder(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    # Two subjects' files interleaved in the exact same literal folder: since
    # capture() no longer copies each subject into a folder of their own,
    # audit's per-folder majority vote must still only ever see one
    # subject's own files, never the other subject's sharing that folder.
    source = tmp_path / "export"
    make_dicom_file(
        source / "mixed" / "a1.dcm",
        PatientID="sub-01",
        StudyInstanceUID="study-a",
        SeriesInstanceUID="series-a",
        SeriesNumber=1,
        InstanceNumber=1,
    )
    make_dicom_file(
        source / "mixed" / "a2.dcm",
        PatientID="sub-01",
        StudyInstanceUID="study-a",
        SeriesInstanceUID="series-a",
        SeriesNumber=1,
        InstanceNumber=2,
    )
    make_dicom_file(
        source / "mixed" / "b1.dcm",
        PatientID="sub-02",
        StudyInstanceUID="study-b",
        SeriesInstanceUID="series-b",
        SeriesNumber=2,
        InstanceNumber=1,
    )
    make_dicom_file(
        source / "mixed" / "b2.dcm",
        PatientID="sub-02",
        StudyInstanceUID="study-b",
        SeriesInstanceUID="series-b",
        SeriesNumber=2,
        InstanceNumber=2,
    )

    run = run_pipeline(source, tmp_path / "out")

    assert len(list(run.archives_dir.glob("*_dichotomised-archive_*.tar.gz"))) == 2
    for report_dir in run.reports_dir.iterdir():
        audit_report = json.loads((report_dir / "stage-01-report.json").read_text())
        assert audit_report["misfiled_count"] == 0
        assert audit_report["duplicate_count"] == 0


def test_run_pipeline_keeps_the_final_tree_unzipped_when_requested(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(
        source / "021-DWI" / "1.dcm",
        PatientID="sub-01",
        StudyInstanceUID="study-a",
        SeriesInstanceUID="series-a",
        SeriesNumber=21,
    )

    run = run_pipeline(source, tmp_path / "out", keep_unzipped=True)

    final_dirs = list(run.final_dir.iterdir())
    assert len(final_dirs) == 1
    unzipped_files = [p for p in final_dirs[0].rglob("*") if p.is_file()]
    assert len(unzipped_files) == 1
    assert unzipped_files[0].suffix == ".dcm"
    # Independent of keep_working_files: the working tree is still cleaned up.
    assert not run.working_dir.exists()


def test_keep_unzipped_moves_tree_without_copying(
    tmp_path: Path, make_dicom_file: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm")

    def reject_copy(*args: object, **kwargs: object) -> None:
        raise AssertionError("The final tree should be moved")

    monkeypatch.setattr(shutil, "copytree", reject_copy)
    run = run_pipeline(source, tmp_path / "out", keep_unzipped=True)
    assert len(list(run.final_dir.rglob("*.dcm"))) == 1


def test_sanitise_without_working_files_skips_rectified_copies(
    tmp_path: Path, make_dicom_file: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm")

    def reject_copy(*args: object, **kwargs: object) -> None:
        raise AssertionError("Direct sanitisation should not copy source files")

    monkeypatch.setattr(shutil, "copy2", reject_copy)
    run = run_pipeline(source, tmp_path / "out", sanitise_requested=True)
    assert len(list(run.archives_dir.glob("*dichotomised-archive*.tar.gz"))) == 1


@pytest.mark.parametrize("sanitised", [False, True])
@pytest.mark.parametrize("keep_working", [False, True])
@pytest.mark.parametrize("keep_unzipped", [False, True])
def test_pipeline_output_options(
    tmp_path: Path,
    make_dicom_file: Callable[..., Path],
    sanitised: bool,
    keep_working: bool,
    keep_unzipped: bool,
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm")
    run = run_pipeline(
        source,
        tmp_path / "out",
        sanitise_requested=sanitised,
        keep_working_files=keep_working,
        keep_unzipped=keep_unzipped,
    )
    assert len(list(run.archives_dir.glob("*dichotomised-archive*.tar.gz"))) == 1
    assert run.working_dir.exists() is keep_working
    assert run.final_dir.exists() is keep_unzipped
    if keep_working and keep_unzipped:
        working = next(run.working_dir.rglob("*.dcm"))
        final = next(run.final_dir.rglob("*.dcm"))
        assert working.stat().st_ino != final.stat().st_ino


def test_run_pipeline_sanitise_default_label_mode_uses_patient_name(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(
        source / "series" / "1.dcm",
        PatientID="sub-01",
        PatientName="Doe^Jane^19900101",
        StudyInstanceUID="study-a",
    )

    run = run_pipeline(source, tmp_path / "out", sanitise_requested=True)

    # The source archive always keeps the real PatientID; only the
    # dichotomised archive is sanitised.
    archives = list(run.archives_dir.glob("*_dichotomised-archive_*.tar.gz"))
    assert len(archives) == 1
    assert "sub-01" not in archives[0].name
    source_archives = list(run.archives_dir.glob("*_source-archive_*.tar.gz"))
    assert len(source_archives) == 1
    assert "sub-01" in source_archives[0].name


def test_run_pipeline_sanitise_numerical_label_mode(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm", PatientID="sub-01", StudyInstanceUID="study-a")

    run = run_pipeline(
        source,
        tmp_path / "out",
        sanitise_requested=True,
        label_mode="numerical",
        subject_id="7",
    )

    archives = list(run.archives_dir.glob("*_dichotomised-archive_*.tar.gz"))
    assert "sub-0007" in archives[0].name


def test_numbering_overflow_fails_before_archiving(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "one" / "1.dcm", PatientID="one")
    make_dicom_file(source / "two" / "1.dcm", PatientID="two")
    out_dir = tmp_path / "out"
    with pytest.raises(RelabelError):
        run_pipeline(
            source, out_dir, sanitise_requested=True, label_mode="numerical", subject_id="9999"
        )
    assert not list(out_dir.rglob("*.tar.gz"))


def test_changed_source_is_rejected_after_archiving(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    scan = make_dicom_file(source / "series" / "1.dcm")

    def change_source(status: str, description: str, elapsed: float) -> None:
        if status == "completed" and description.endswith("archiving source DICOMs"):
            with scan.open("ab") as handle:
                handle.write(b"changed")

    with pytest.raises(UnsafeSourceError):
        run_pipeline(source, tmp_path / "out", on_stage=change_source)


def test_source_changed_after_capture_is_rejected_before_archiving(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    scan = make_dicom_file(source / "series" / "1.dcm")
    out_dir = tmp_path / "out"

    def change_source(status: str, description: str, elapsed: float) -> None:
        if status == "completed" and description == "Capturing DICOMs by study":
            with scan.open("ab") as handle:
                handle.write(b"changed")

    with pytest.raises(UnsafeSourceError):
        run_pipeline(source, out_dir, on_stage=change_source)
    assert not list(out_dir.rglob("*.tar.gz"))


def test_source_changed_during_capture_is_rejected(
    tmp_path: Path, make_dicom_file: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm")
    read = capture_module.dicom_read.read_metadata

    def changing_read(path: Path) -> object:
        metadata = read(path)
        with path.open("ab") as handle:
            handle.write(b"changed")
        return metadata

    monkeypatch.setattr(capture_module.dicom_read, "read_metadata", changing_read)
    with pytest.raises(UnsafeSourceError):
        run_pipeline(source, tmp_path / "out")


def test_sanitised_pixel_corruption_before_archiving_is_rejected(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm", PixelData=b"\x01\x02")
    out_dir = tmp_path / "out"

    def corrupt_output(status: str, description: str, elapsed: float) -> None:
        if status == "started" and description.endswith("creating verified archive"):
            output = next(out_dir.rglob("working/*/sanitise/**/*.dcm"))
            dataset = pydicom.dcmread(output)
            dataset.PixelData = b"\x03\x04"
            dataset.save_as(output)

    with pytest.raises(FinaliseVerificationError, match="Pixel data differs"):
        run_pipeline(source, out_dir, sanitise_requested=True, on_stage=corrupt_output)


def test_sanitised_identity_corruption_before_archiving_is_rejected(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm")
    out_dir = tmp_path / "out"

    def corrupt_output(status: str, description: str, elapsed: float) -> None:
        if status == "started" and description.endswith("creating verified archive"):
            output = next(out_dir.rglob("working/*/sanitise/**/*.dcm"))
            dataset = pydicom.dcmread(output)
            dataset.PatientID = "unexpected"
            dataset.save_as(output)

    with pytest.raises(FinaliseVerificationError, match="Sanitised identity differs"):
        run_pipeline(source, out_dir, sanitise_requested=True, on_stage=corrupt_output)


def test_changed_review_file_before_archiving_is_rejected(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    original = make_dicom_file(source / "series" / "1.dcm")
    shutil.copy2(original, original.with_name("2.dcm"))
    out_dir = tmp_path / "out"

    def corrupt_output(status: str, description: str, elapsed: float) -> None:
        if status == "started" and description.endswith("archiving review files"):
            output = next(out_dir.rglob("working/*/review-rectify/**/*.dcm"))
            with output.open("ab") as handle:
                handle.write(b"changed")

    with pytest.raises(FinaliseVerificationError, match="Review file differs"):
        run_pipeline(source, out_dir, on_stage=corrupt_output)


def test_run_pipeline_sanitise_numerical_mode_without_subject_id_raises(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm", PatientID="sub-01", StudyInstanceUID="study-a")

    with pytest.raises(RelabelError):
        run_pipeline(source, tmp_path / "out", sanitise_requested=True, label_mode="numerical")


def test_run_pipeline_writes_the_three_numbered_reports(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(
        source / "series" / "1.dcm",
        PatientID="sub-01",
        StudyInstanceUID="study-a",
        StudyDate="20260101",
        StudyTime="120000",
    )

    run = run_pipeline(source, tmp_path / "out")

    report_dirs = list(run.reports_dir.iterdir())
    assert len(report_dirs) == 1
    assert re.fullmatch(r"sub-01_[0-9a-f]{6}", report_dirs[0].name)

    reports = {p.name for p in report_dirs[0].iterdir()}
    assert reports == {
        "stage-01-audit.csv",
        "stage-01-report.json",
        "stage-02-report.json",
        "stage-03-report.json",
        "source-manifest.csv",
        "retained-manifest.csv",
    }

    finalise_report = json.loads((report_dirs[0] / "stage-03-report.json").read_text())
    assert finalise_report["subject_label"] == "sub-01"
    assert finalise_report["sanitised"] is False
    assert finalise_report["file_count"] == 1

    # reports/ survives the default working-file cleanup.
    assert not run.working_dir.exists()


def test_run_pipeline_archives_review_files_renamed_but_unsanitised(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(
        source / "021-DWI" / "1.dcm",
        PatientID="sub-01",
        StudyInstanceUID="study-a",
        SeriesInstanceUID="series-a",
        SeriesNumber=21,
        InstanceNumber=1,
    )
    shutil.copyfile(source / "021-DWI" / "1.dcm", source / "021-DWI" / "2.dcm")

    run = run_pipeline(source, tmp_path / "out")

    review_archives = list(run.archives_dir.glob("*_review-archive_*.tar.gz"))
    assert len(review_archives) == 1
    with tarfile.open(review_archives[0], "r:gz") as tar:
        names = [m.name for m in tar.getmembers() if m.isfile()]
    assert any(name.endswith(".dcm") for name in names)

    reports_dir = next(run.reports_dir.iterdir())
    sift_report = json.loads((reports_dir / "stage-02-report.json").read_text())
    assert sift_report["review_archive"] is not None
    assert sift_report["review_archive_sanitised"] is False
    assert not run.working_dir.exists()


def test_run_pipeline_sanitised_run_deidentifies_review_archive(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    real_patient_id = "VERY-REAL-PATIENT-ID"
    source = tmp_path / "export"
    make_dicom_file(
        source / "021-DWI" / "1.dcm",
        PatientID=real_patient_id,
        PatientName="Doe^Jane",
        StudyInstanceUID="study-a",
        SeriesInstanceUID="series-a",
        SeriesNumber=21,
        InstanceNumber=1,
    )
    shutil.copyfile(source / "021-DWI" / "1.dcm", source / "021-DWI" / "2.dcm")

    run = run_pipeline(
        source,
        tmp_path / "out",
        sanitise_requested=True,
        label_mode="numerical",
        subject_id="7",
    )

    review_archives = list(run.archives_dir.glob("*_review-archive_*.tar.gz"))
    assert len(review_archives) == 1
    with tarfile.open(review_archives[0], "r:gz") as tar:
        member = next(m for m in tar.getmembers() if m.isfile())
        extracted = tar.extractfile(member)
        assert extracted is not None
        content = extracted.read()
    assert real_patient_id.encode() not in content

    reports_dir = next(run.reports_dir.iterdir())
    sift_report = json.loads((reports_dir / "stage-02-report.json").read_text())
    assert sift_report["review_archive_sanitised"] is True


def test_run_pipeline_creates_no_review_archive_without_review_files(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "series" / "1.dcm", PatientID="sub-01", StudyInstanceUID="study-a")

    run = run_pipeline(source, tmp_path / "out")

    assert list(run.archives_dir.glob("*_review-archive_*")) == []


def test_run_pipeline_writes_manifests_for_retained_and_review_files(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(
        source / "021-DWI" / "1.dcm",
        PatientID="sub-01",
        StudyInstanceUID="study-a",
        SeriesInstanceUID="series-a",
        SeriesNumber=21,
        InstanceNumber=1,
    )
    shutil.copyfile(source / "021-DWI" / "1.dcm", source / "021-DWI" / "2.dcm")

    run = run_pipeline(source, tmp_path / "out")

    reports_dir = next(run.reports_dir.iterdir())
    assert (reports_dir / "source-manifest.csv").exists()
    assert (reports_dir / "retained-manifest.csv").exists()
    assert (reports_dir / "review-manifest.csv").exists()


def test_run_pipeline_reports_use_the_replacement_label_when_sanitised(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(
        source / "series" / "1.dcm",
        PatientID="sub-01",
        StudyInstanceUID="study-a",
        StudyDate="20260101",
        StudyTime="120000",
    )

    run = run_pipeline(
        source, tmp_path / "out", sanitise_requested=True, label_mode="numerical", subject_id="7"
    )

    report_dirs = list(run.reports_dir.iterdir())
    assert re.fullmatch(r"sub-0007_[0-9a-f]{6}", report_dirs[0].name)


def test_run_pipeline_sanitised_reports_never_contain_the_real_patient_id(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    real_patient_id = "2026.09.14-13:38:31-DST-1.3.12.2.1107.5.99.3-VERY-REAL-ID"
    source = tmp_path / "export"
    make_dicom_file(
        source / "series" / "1.dcm",
        PatientID=real_patient_id,
        StudyInstanceUID="study-a",
        StudyDate="20260101",
        StudyTime="120000",
    )

    run = run_pipeline(
        source, tmp_path / "out", sanitise_requested=True, label_mode="numerical", subject_id="1"
    )

    for report_file in run.reports_dir.rglob("*.json"):
        assert real_patient_id not in report_file.read_text()


def _export_with_identifying_names(source: Path, make_dicom_file: Callable[..., Path]) -> list[str]:
    """Write one retained file and one duplicate named after identifiers; return those names."""
    folder = source / "M0450__secret-folder"
    series = {
        "PatientID": "sub-01",
        "StudyInstanceUID": "9.8.7.6.0",
        "SeriesInstanceUID": "9.8.7.6.5",
        "SeriesNumber": 21,
        "InstanceNumber": 1,
        "SOPInstanceUID": "9.8.7.6.1",
    }
    original = make_dicom_file(folder / "MRe.9.8.7.6.1", **series)
    shutil.copy2(original, folder / "MRe.9.8.7.6.2")
    return ["M0450__secret-folder", "MRe.9.8.7.6", "9.8.7.6.1", "9.8.7.6.5", "9.8.7.6.0"]


def test_run_pipeline_sanitised_reports_never_contain_source_names_or_uids(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    identifying = _export_with_identifying_names(source, make_dicom_file)

    run = run_pipeline(source, tmp_path / "out", sanitise_requested=True)

    report_files = [path for path in run.reports_dir.rglob("*") if path.is_file()]
    assert report_files
    for report_file in report_files:
        text = report_file.read_text()
        for token in identifying:
            assert token not in text, f"{token} leaked into {report_file.name}"
    reports_dir = next(run.reports_dir.iterdir())
    sift_report = json.loads((reports_dir / "stage-02-report.json").read_text())
    assert sift_report["review"] == [{"file": "folder-01/0002", "reason": "duplicate"}]
    audit_report = json.loads((reports_dir / "stage-01-report.json").read_text())
    assert audit_report["duplicates"] == ["folder-01"]
    assert "folder-01/0001" in (reports_dir / "source-manifest.csv").read_text()


def test_run_pipeline_sanitised_run_writes_a_source_map_beside_the_source_archive(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    _export_with_identifying_names(source, make_dicom_file)

    run = run_pipeline(source, tmp_path / "out", sanitise_requested=True)

    (source_map,) = run.archives_dir.glob("*_source-map_*.csv")
    (source_archive,) = run.archives_dir.glob("*_source-archive_*.tar.gz")
    assert source_map.name.removesuffix(".csv").replace(
        "_source-map_", "_source-archive_"
    ) == source_archive.name.removesuffix(".tar.gz")
    rows = {row["report_name"]: row for row in csv.DictReader(source_map.read_text().splitlines())}
    assert rows["folder-01/0001"]["source_file"] == "M0450__secret-folder/MRe.9.8.7.6.1"
    assert rows["folder-01/0001"]["status"] == "retained"
    assert rows["folder-01/0001"]["final_file"].endswith("_e01.dcm")
    assert rows["folder-01/0002"]["source_file"] == "M0450__secret-folder/MRe.9.8.7.6.2"
    assert rows["folder-01/0002"]["status"] == "review:duplicate"
    assert rows["folder-01/0002"]["final_file"].endswith("_e01.dcm")


def _write_flat_export(source: Path, make_dicom_file: Callable[..., Path]) -> int:
    """Write three series into one folder, each file named after its own SOP Instance UID."""
    folder = source / "M0450__flat-secret"
    count = 0
    for number in (3, 4, 5):
        for instance in (1, 2, 3):
            sop = f"9.8.{number}.{instance}"
            make_dicom_file(
                folder / f"MRe.{sop}",
                PatientID="sub-01",
                StudyInstanceUID="9.8.0",
                SeriesInstanceUID=f"9.8.{number}",
                SeriesNumber=number,
                SeriesDescription=f"secret-protocol-{number}",
                ProtocolName=f"secret-protocol-{number}",
                InstanceNumber=instance,
                SOPInstanceUID=sop,
            )
            count += 1
    return count


def test_run_pipeline_keeps_every_file_of_a_single_folder_export(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    total = _write_flat_export(source, make_dicom_file)

    run = run_pipeline(source, tmp_path / "out")

    reports_dir = next(run.reports_dir.iterdir())
    sift_report = json.loads((reports_dir / "stage-02-report.json").read_text())
    assert sift_report["retained_count"] == total
    assert sift_report["review_count"] == 0
    audit_report = json.loads((reports_dir / "stage-01-report.json").read_text())
    assert audit_report["misfiled_count"] == 0
    assert len(audit_report["series"]) == 3
    (archive,) = run.archives_dir.glob("*_dichotomised-archive_*.tar.gz")
    with tarfile.open(archive, "r:gz") as tar:
        folders = {Path(m.name).parent.name for m in tar.getmembers() if m.isfile()}
    assert folders == {"003-secret-protocol-3", "004-secret-protocol-4", "005-secret-protocol-5"}


def test_run_pipeline_sanitised_reports_of_a_single_folder_export_name_no_source_folder(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    _write_flat_export(source, make_dicom_file)

    run = run_pipeline(source, tmp_path / "out", sanitise_requested=True)

    for report_file in run.reports_dir.rglob("*"):
        if report_file.is_file():
            text = report_file.read_text()
            for token in ("M0450__flat-secret", "MRe.9.8", "9.8.3.1"):
                assert token not in text, f"{token} leaked into {report_file.name}"
    reports_dir = next(run.reports_dir.iterdir())
    audit_report = json.loads((reports_dir / "stage-01-report.json").read_text())
    assert [row["series_folder"] for row in audit_report["series"]] == [
        "series-001",
        "series-002",
        "series-003",
    ]


def _write_export(source: Path, make_dicom_file: Callable[..., Path], *, flat: bool) -> None:
    """The same three series, either sorted into series folders or all in one folder."""
    for number in (3, 4, 5):
        for instance in (1, 2, 3):
            sop = f"9.8.{number}.{instance}"
            if flat:
                path = source / "everything" / f"MRe.{sop}"
            else:
                path = source / f"scan_{number}_MR" / f"{instance}.dcm"
            make_dicom_file(
                path,
                PatientID="sub-01",
                StudyInstanceUID="9.8.0",
                SeriesInstanceUID=f"9.8.{number}",
                SeriesNumber=number,
                SeriesDescription=f"scan{number}",
                ProtocolName=f"scan{number}",
                InstanceNumber=instance,
                SOPInstanceUID=sop,
            )


def test_run_pipeline_gives_the_same_result_for_sorted_and_single_folder_exports(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    outcomes = []
    for layout, flat in (("sorted", False), ("flat", True)):
        source = tmp_path / layout / "export"
        _write_export(source, make_dicom_file, flat=flat)
        run = run_pipeline(source, tmp_path / layout / "out")
        reports_dir = next(run.reports_dir.iterdir())
        audit_report = json.loads((reports_dir / "stage-01-report.json").read_text())
        sift_report = json.loads((reports_dir / "stage-02-report.json").read_text())
        (archive,) = run.archives_dir.glob("*_dichotomised-archive_*.tar.gz")
        with tarfile.open(archive, "r:gz") as tar:
            members = sorted(m.name for m in tar.getmembers() if m.isfile())
        outcomes.append(
            (
                audit_report["misfiled_count"],
                audit_report["duplicate_count"],
                [row["dicom_count"] for row in audit_report["series"]],
                sift_report["retained_count"],
                sift_report["review_count"],
                members,
            )
        )

    assert outcomes[0] == outcomes[1]
    assert outcomes[0][3:5] == (9, 0)


def test_run_pipeline_writes_the_source_map_before_sanitising_so_a_failed_run_keeps_it(
    tmp_path: Path, make_dicom_file: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "export"
    _export_with_identifying_names(source, make_dicom_file)

    def _fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("sanitise failed")

    monkeypatch.setattr("dichotomise.pipeline.sanitise", _fail)

    with pytest.raises(RuntimeError, match="sanitise failed"):
        run_pipeline(source, tmp_path / "out", sanitise_requested=True)

    (source_map,) = (tmp_path / "out").rglob("*_source-map_*.csv")
    rows = list(csv.DictReader(source_map.read_text().splitlines()))
    assert [row["report_name"] for row in rows] == ["folder-01/0001", "folder-01/0002"]
    assert rows[0]["source_file"] == "M0450__secret-folder/MRe.9.8.7.6.1"
    assert rows[0]["status"] == "retained"
    assert rows[0]["final_file"] == ""


def test_run_pipeline_full_policy_keeps_the_series_description_out_of_names_and_reports(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(
        source / "021" / "1.dcm",
        PatientID="sub-01",
        StudyInstanceUID="9.8.7.6.0",
        SeriesInstanceUID="9.8.7.6.5",
        SeriesNumber=21,
        SeriesDescription="secret-protocol",
    )

    run = run_pipeline(source, tmp_path / "out", sanitise_requested=True, sanitise_level="full")

    (archive,) = run.archives_dir.glob("*_dichotomised-archive_*.tar.gz")
    with tarfile.open(archive, "r:gz") as tar:
        members = [member.name for member in tar.getmembers()]
    assert not any("secret-protocol" in name for name in members)
    assert any(name.startswith("021/") for name in members)
    for report_file in run.reports_dir.rglob("*"):
        if report_file.is_file():
            assert "secret-protocol" not in report_file.read_text()


def test_run_pipeline_standard_policy_keeps_the_series_description_in_folder_names(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(
        source / "021" / "1.dcm",
        PatientID="sub-01",
        StudyInstanceUID="9.8.7.6.0",
        SeriesInstanceUID="9.8.7.6.5",
        SeriesNumber=21,
        SeriesDescription="kept-protocol",
    )

    run = run_pipeline(source, tmp_path / "out", sanitise_requested=True, sanitise_level="standard")

    (archive,) = run.archives_dir.glob("*_dichotomised-archive_*.tar.gz")
    with tarfile.open(archive, "r:gz") as tar:
        assert any("021-kept-protocol/" in member.name for member in tar.getmembers())


def test_run_pipeline_unsanitised_reports_keep_the_real_names_and_write_no_source_map(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    _export_with_identifying_names(source, make_dicom_file)

    run = run_pipeline(source, tmp_path / "out")

    reports_dir = next(run.reports_dir.iterdir())
    assert "M0450__secret-folder/MRe.9.8.7.6.1" in (reports_dir / "source-manifest.csv").read_text()
    assert not list(run.archives_dir.glob("*_source-map_*"))


def test_run_pipeline_sends_sop_uid_conflicts_to_review_without_failing(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    shared = {
        "PatientID": "sub-01",
        "StudyInstanceUID": "1.2.1",
        "SeriesInstanceUID": "1.2.2",
        "SeriesNumber": 21,
        "SOPInstanceUID": "1.2.3.9",
    }
    make_dicom_file(source / "021-DWI" / "1.dcm", InstanceNumber=1, **shared)
    make_dicom_file(source / "021-DWI" / "2.dcm", InstanceNumber=1, ProtocolName="other", **shared)
    make_dicom_file(
        source / "021-DWI" / "3.dcm",
        PatientID="sub-01",
        StudyInstanceUID="1.2.1",
        SeriesInstanceUID="1.2.2",
        SeriesNumber=21,
        InstanceNumber=3,
    )

    run = run_pipeline(source, tmp_path / "out")

    reports_dir = next(run.reports_dir.iterdir())
    sift_report = json.loads((reports_dir / "stage-02-report.json").read_text())
    assert sift_report["retained_count"] == 1
    assert {item["reason"] for item in sift_report["review"]} == {"sop_conflict"}
    audit_report = json.loads((reports_dir / "stage-01-report.json").read_text())
    assert audit_report["sop_conflict_count"] == 2
    assert audit_report["duplicate_count"] == 0
    (review_archive,) = run.archives_dir.glob("*_review-archive_*.tar.gz")
    with tarfile.open(review_archive, "r:gz") as tar:
        names = sorted(Path(m.name).name for m in tar.getmembers() if m.isfile())
    assert len(names) == 2
    assert any("-dup01" in name for name in names)


@pytest.mark.parametrize("sanitise", [False, True])
def test_run_pipeline_collapses_a_byte_identical_copy_in_another_folder(
    tmp_path: Path, make_dicom_file: Callable[..., Path], sanitise: bool
) -> None:
    source = tmp_path / "export"
    original = make_dicom_file(
        source / "exportA" / "021-DWI" / "1.dcm",
        PatientID="sub-01",
        StudyInstanceUID="1.2.1",
        SeriesInstanceUID="1.2.2",
        SeriesNumber=21,
        InstanceNumber=1,
    )
    (source / "exportB" / "021-DWI").mkdir(parents=True)
    shutil.copy2(original, source / "exportB" / "021-DWI" / "1.dcm")

    run = run_pipeline(source, tmp_path / "out", sanitise_requested=sanitise)

    (archive,) = run.archives_dir.glob("*_dichotomised-archive_*.tar.gz")
    with tarfile.open(archive, "r:gz") as tar:
        names = [Path(m.name).name for m in tar.getmembers() if m.isfile()]
    assert len(names) == 1
    audit_report = json.loads(
        (next(run.reports_dir.iterdir()) / "stage-01-report.json").read_text()
    )
    assert audit_report["duplicate_count"] == 0
    sift_report = json.loads((next(run.reports_dir.iterdir()) / "stage-02-report.json").read_text())
    assert sift_report["retained_count"] == 2
    assert sift_report["retained_identical_files_not_rewritten"] == 1


def test_run_pipeline_sanitised_archives_name_files_after_the_regenerated_uid(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    original_uid = "1.3.12.2.1107.5.2.61.237011.20261020110718"
    source = tmp_path / "export"
    make_dicom_file(
        source / "021-DWI" / "1.dcm",
        PatientID="sub-01",
        StudyInstanceUID="1.2.1",
        SeriesInstanceUID="1.2.2",
        SeriesNumber=21,
        SOPInstanceUID=original_uid,
    )
    copy = source / "021-DWI" / "2.dcm"
    shutil.copyfile(source / "021-DWI" / "1.dcm", copy)
    make_dicom_file(
        source / "021-DWI" / "3.dcm",
        PatientID="sub-01",
        StudyInstanceUID="1.2.1",
        SeriesInstanceUID="1.2.2",
        SeriesNumber=21,
        InstanceNumber=3,
    )

    run = run_pipeline(
        source,
        tmp_path / "out",
        sanitise_requested=True,
        label_mode="numerical",
        subject_id="7",
    )

    for pattern in ("*_dichotomised-archive_*.tar.gz", "*_review-archive_*.tar.gz"):
        (archive,) = run.archives_dir.glob(pattern)
        with tarfile.open(archive, "r:gz") as tar:
            names = [m.name for m in tar.getmembers() if m.isfile()]
        assert names
        assert all(original_uid not in name for name in names)


def test_run_pipeline_numbers_series_by_series_number_not_by_hidden_description(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    for number, description in ((5, "zzz-first-alphabetically-last"), (6, "aaa-second")):
        for instance in (1, 2):
            sop = f"9.8.{number}.{instance}"
            make_dicom_file(
                source / "all" / f"MRe.{sop}",
                PatientID="sub-01",
                StudyInstanceUID="9.8.0",
                SeriesInstanceUID=f"9.8.{number}",
                SeriesNumber=number,
                SeriesDescription=description,
                ProtocolName=description,
                InstanceNumber=instance,
                SOPInstanceUID=sop,
            )
    for number in (7, 8):
        make_dicom_file(
            source / "all" / f"MRe.9.8.{number}.1",
            PatientID="sub-01",
            StudyInstanceUID="9.8.0",
            SeriesInstanceUID=f"9.8.{number}",
            SeriesNumber=number,
            SeriesDescription="other",
            ProtocolName="other",
            SOPInstanceUID=f"9.8.{number}.1",
        )

    run = run_pipeline(source, tmp_path / "out", sanitise_requested=True, sanitise_level="full")

    reports_dir = next(run.reports_dir.iterdir())
    audit_report = json.loads((reports_dir / "stage-01-report.json").read_text())
    counts = {row["series_folder"]: row["dicom_count"] for row in audit_report["series"]}
    assert counts == {"series-001": 2, "series-002": 2, "series-003": 1, "series-004": 1}


def test_run_pipeline_pseudonymises_two_single_folder_subfolders_separately(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    for folder in ("first-secret", "second-secret"):
        for number in (3, 4, 5):
            sop = f"9.{folder[:1]}.{number}"
            make_dicom_file(
                source / folder / f"MRe.{sop}",
                PatientID="sub-01",
                StudyInstanceUID="9.8.0",
                SeriesInstanceUID=sop,
                SeriesNumber=number,
                SeriesDescription=f"scan{number}",
                ProtocolName=f"scan{number}",
                SOPInstanceUID=sop,
            )

    run = run_pipeline(source, tmp_path / "out", sanitise_requested=True)

    for report_file in run.reports_dir.rglob("*"):
        if report_file.is_file():
            text = report_file.read_text()
            assert "first-secret" not in text and "second-secret" not in text
    reports_dir = next(run.reports_dir.iterdir())
    audit_report = json.loads((reports_dir / "stage-01-report.json").read_text())
    assert [row["dicom_count"] for row in audit_report["series"]] == [1, 1, 1, 1, 1, 1]
    assert audit_report["misfiled_count"] == 0


def test_run_pipeline_finishes_when_every_file_of_a_subject_goes_to_review(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    shared = {"PatientID": "sub-01", "StudyInstanceUID": "1.2.1", "SOPInstanceUID": "1.2.3.9"}
    make_dicom_file(source / "s" / "1.dcm", InstanceNumber=1, **shared)
    make_dicom_file(source / "s" / "2.dcm", InstanceNumber=1, ProtocolName="other", **shared)

    run = run_pipeline(source, tmp_path / "out", sanitise_requested=True)

    reports_dir = next(run.reports_dir.iterdir())
    sift_report = json.loads((reports_dir / "stage-02-report.json").read_text())
    assert sift_report["retained_count"] == 0
    assert sift_report["review_count"] == 2
    assert len(list(run.archives_dir.glob("*_review-archive_*.tar.gz"))) == 1
