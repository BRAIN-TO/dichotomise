"""The audit stage: duplicates, SOP conflicts, misfiled files and plain-language notes."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from dichotomise.pydcm.read import iter_dicom_metadata
from dichotomise.stages.audit import audit, audit_directory, describe_findings, folder_notes
from dichotomise.stages.capture import CapturedSubject


def _subject(directory: Path) -> CapturedSubject:
    """A subject whose files are every DICOM found under `directory`."""
    return CapturedSubject(
        subject_id="sub-01",
        patient_name="Doe^Jane^19900101",
        study_instance_uid="study-a",
        scan_date="20260101",
        scan_time="120000",
        files=list(iter_dicom_metadata(directory)),
        source_root=directory,
        working_dir=directory,
    )


def test_audit_flags_duplicate_and_misfiled_files(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    capture_dir = tmp_path / "capture"

    # 021-DWI: file 2 is a byte-for-byte copy of file 1; file 3 is distinct.
    make_dicom_file(
        capture_dir / "021-DWI" / "1.dcm",
        StudyInstanceUID="study-a",
        SeriesInstanceUID="series-dwi",
        SeriesNumber=21,
        ProtocolName="DWI",
        InstanceNumber=1,
    )
    shutil.copyfile(capture_dir / "021-DWI" / "1.dcm", capture_dir / "021-DWI" / "2.dcm")
    make_dicom_file(
        capture_dir / "021-DWI" / "3.dcm",
        StudyInstanceUID="study-a",
        SeriesInstanceUID="series-dwi",
        SeriesNumber=21,
        ProtocolName="DWI",
        InstanceNumber=2,
    )

    # 022-fMRI: files 1 and 2 agree on the folder's series; file 3 disagrees (misfiled).
    make_dicom_file(
        capture_dir / "022-fMRI" / "1.dcm",
        SeriesInstanceUID="series-fmri",
        SeriesNumber=22,
        ProtocolName="fMRI",
        InstanceNumber=1,
    )
    make_dicom_file(
        capture_dir / "022-fMRI" / "2.dcm",
        SeriesInstanceUID="series-fmri",
        SeriesNumber=22,
        ProtocolName="fMRI",
        InstanceNumber=2,
    )
    make_dicom_file(
        capture_dir / "022-fMRI" / "3.dcm",
        SeriesInstanceUID="series-other",
        SeriesNumber=99,
        ProtocolName="other",
        InstanceNumber=1,
    )

    result = audit(_subject(capture_dir))

    assert len(result.files) == 6
    flagged_duplicate = {f.metadata.path.name for f in result.files if f.is_duplicate}
    ok_files = [f for f in result.files if not f.is_duplicate and not f.is_misfiled]

    assert flagged_duplicate == {"2.dcm"}
    assert len(ok_files) == 4
    misfiled_paths = {f.metadata.path for f in result.files if f.is_misfiled}
    assert misfiled_paths == {capture_dir / "022-fMRI" / "3.dcm"}


def test_audit_directory_checks_source_files_in_place(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source_dir = tmp_path / "source"
    dicom_file = make_dicom_file(source_dir / "series" / "1.dcm", PatientID="scanner-01")

    (result,) = audit_directory(source_dir)

    assert result.subject.source_root == source_dir
    assert [file.metadata.path for file in result.files] == [dicom_file]
    assert list(source_dir.rglob("*")) == [source_dir / "series", dicom_file]


def test_audit_directory_audits_each_subject_on_its_own(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source_dir = tmp_path / "scanner_export"
    for patient, folder in (("sub-02", "second"), ("sub-01", "first")):
        make_dicom_file(
            source_dir / folder / "1.dcm", PatientID=patient, StudyInstanceUID=f"study-{patient}"
        )

    results = audit_directory(source_dir)

    assert [result.subject.subject_id for result in results] == ["sub-01", "sub-02"]
    assert [len(result.files) for result in results] == [1, 1]


def test_audit_explains_a_folder_written_twice_with_one_misfiled_file(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    # Mirrors an XA60 export: the series is written twice into one folder
    # (files 5-8 repeat 1-4) and, in the first half, instance 4 of the series
    # is replaced by a file that belongs to another series (file 4).
    folder = tmp_path / "ASL_9_MR"
    series = {
        "StudyInstanceUID": "1.2.1",
        "SeriesInstanceUID": "1.2.9",
        "SeriesNumber": 9,
        "SeriesDescription": "asl",
    }
    for instance in (1, 2, 3):
        make_dicom_file(
            folder / f"{instance}.dcm",
            SOPInstanceUID=f"1.2.9.{instance}",
            InstanceNumber=instance,
            ProtocolName="asl",
            **series,
        )
    make_dicom_file(
        folder / "4.dcm",
        SOPInstanceUID="1.2.10.2",
        InstanceNumber=2,
        ProtocolName="asl",
        StudyInstanceUID="1.2.1",
        SeriesInstanceUID="1.2.10",
        SeriesNumber=10,
        SeriesDescription="MoCoSeries",
    )
    for number in (5, 6, 7):
        shutil.copyfile(folder / f"{number - 4}.dcm", folder / f"{number}.dcm")
    make_dicom_file(
        folder / "8.dcm",
        SOPInstanceUID="1.2.9.4",
        InstanceNumber=4,
        ProtocolName="asl",
        **series,
    )

    result = audit(_subject(tmp_path))

    assert {f.metadata.path.name for f in result.files if f.is_duplicate} == {
        "5.dcm",
        "6.dcm",
        "7.dcm",
    }
    assert {f.metadata.path.name for f in result.files if f.is_misfiled} == {"4.dcm"}
    notes = describe_findings([f for f in result.files if f.metadata.path.parent == folder])
    assert notes == [
        '4.dcm belongs to series 10 "MoCoSeries" (instance 2), not series 9',
        "5-7.dcm are byte-identical copies of 1-3.dcm",
        "8.dcm (instance 4) is kept: its counterpart 4.dcm is misfiled",
        "SeriesInstanceUID, SeriesNumber, SeriesDescription differ only because of the "
        "1 misfiled file",
    ]


def test_audit_sends_files_sharing_a_uid_with_different_bytes_to_review_as_conflicts(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    folder = tmp_path / "series"
    shared = {
        "StudyInstanceUID": "1.2.1",
        "SeriesInstanceUID": "1.2.2",
        "SOPInstanceUID": "1.2.3.9",
    }
    make_dicom_file(folder / "1.dcm", InstanceNumber=1, **shared)
    make_dicom_file(folder / "2.dcm", InstanceNumber=2, **shared)

    result = audit(_subject(tmp_path))

    assert all(f.is_sop_conflict and not f.is_duplicate for f in result.files)
    (note,) = folder_notes(result)
    assert note.texts == [
        "1-2.dcm share one SOP Instance UID but their contents differ; all are sent to review"
    ]


def test_audit_names_extensionless_duplicates_by_their_real_filenames(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    folder = tmp_path / "export"
    series = {"SeriesInstanceUID": "1.2.2", "SeriesNumber": 9, "StudyInstanceUID": "1.2.1"}
    make_dicom_file(folder / "1", SOPInstanceUID="1.2.3.1", InstanceNumber=1, **series)
    shutil.copyfile(folder / "1", folder / "2")
    shutil.copyfile(folder / "1", folder / "MRe.1.2.3.9")

    result = audit(_subject(folder))

    assert {f.metadata.path.name for f in result.files if f.is_duplicate} == {"2", "MRe.1.2.3.9"}
    notes = describe_findings(result.files)
    assert any("2 is a byte-identical copy of 1" in note for note in notes)
    assert not any(".dcm" in note for note in notes)


def _write_series(
    make_dicom_file: Callable[..., Path],
    folder: Path,
    specs: list[tuple[int, int]],
    *,
    name: Callable[[int, int, str], str],
) -> None:
    """Write `count` files for each (series number, count) spec into one `folder`."""
    for number, count in specs:
        for instance in range(1, count + 1):
            sop = f"1.2.{number}.{instance}"
            make_dicom_file(
                folder / name(number, instance, sop),
                StudyInstanceUID="1.2.1",
                SeriesInstanceUID=f"1.2.{number}",
                SeriesNumber=number,
                SeriesDescription=f"desc{number}",
                ProtocolName=f"proto{number}",
                InstanceNumber=instance,
                SOPInstanceUID=sop,
            )


def _uid_named(number: int, instance: int, sop: str) -> str:
    """File name in the scanner style: `MRe.<SOP UID>`, no extension."""
    return f"MRe.{sop}"


def _plain_named(number: int, instance: int, sop: str) -> str:
    """File name made of the series and instance numbers, with a `.dcm` extension."""
    return f"{number}-{instance}.dcm"


def test_audit_does_not_call_files_misfiled_when_one_folder_holds_two_series(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    folder = tmp_path / "flat"
    _write_series(make_dicom_file, folder, [(9, 8), (12, 3)], name=_uid_named)

    result = audit(_subject(folder))

    assert not any(f.is_misfiled for f in result.files)
    assert len({f.folder for f in result.files}) == 2


def test_audit_does_not_call_files_misfiled_when_one_series_dominates_a_flat_folder(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    folder = tmp_path / "flat"
    _write_series(
        make_dicom_file, folder, [(9, 12), (20, 1), (21, 1), (22, 1), (23, 1)], name=_uid_named
    )

    result = audit(_subject(folder))

    assert not any(f.is_misfiled for f in result.files)
    assert len({f.folder for f in result.files}) == 5


def test_audit_splits_a_folder_of_three_series_even_when_files_have_plain_names(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    folder = tmp_path / "flat"
    _write_series(make_dicom_file, folder, [(9, 3), (12, 3), (13, 3)], name=_plain_named)

    result = audit(_subject(folder))

    assert not any(f.is_misfiled for f in result.files)
    assert sorted(f.folder.name for f in {f.folder: f for f in result.files}.values()) == [
        "desc12_12",
        "desc13_13",
        "desc9_9",
    ]


def test_audit_still_flags_a_stray_file_in_a_one_series_folder(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    folder = tmp_path / "021-DWI"
    _write_series(make_dicom_file, folder, [(21, 6)], name=_plain_named)
    _write_series(make_dicom_file, folder, [(22, 1)], name=_plain_named)

    result = audit(_subject(folder))

    assert {f.metadata.path.name for f in result.files if f.is_misfiled} == {"22-1.dcm"}
    assert {f.folder for f in result.files} == {folder}


def _extension_named(number: int, instance: int, sop: str) -> str:
    """File name made of the SOP UID plus a `.dcm` extension."""
    return f"{sop}.dcm"


def test_audit_treats_files_named_by_their_uid_with_an_extension_as_a_single_folder_export(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    folder = tmp_path / "flat"
    _write_series(make_dicom_file, folder, [(9, 8), (12, 3)], name=_extension_named)

    result = audit(_subject(folder))

    assert not any(f.is_misfiled for f in result.files)
    assert len({f.folder for f in result.files}) == 2


def test_audit_does_not_mistake_a_shorter_uid_inside_a_longer_one_for_the_file_name(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    folder = tmp_path / "021-DWI"
    for instance in range(1, 7):
        make_dicom_file(
            folder / f"{instance}.dcm",
            SeriesInstanceUID="1.2.9",
            SeriesNumber=9,
            InstanceNumber=instance,
            SOPInstanceUID=f"1.2.9.{instance}",
        )
    make_dicom_file(
        folder / "stray-1.2.9.1.dcm",
        SeriesInstanceUID="1.2.7",
        SeriesNumber=7,
        InstanceNumber=1,
        SOPInstanceUID="1.2.9.1",
    )

    result = audit(_subject(folder))

    assert {f.metadata.path.name for f in result.files if f.is_misfiled} == {"stray-1.2.9.1.dcm"}
