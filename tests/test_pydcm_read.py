from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pydicom
import pytest

from dichotomise.errors import NotDicomError
from dichotomise.pydcm import read as read_module
from dichotomise.pydcm.read import (
    _optional_number,
    _pixel_fingerprint,
    iter_dicom_metadata,
    read_metadata,
)


@pytest.mark.parametrize("field", ["PixelData", "FloatPixelData", "DoubleFloatPixelData"])
def test_pixel_fingerprint_detects_same_length_changes(field: str) -> None:
    dataset = pydicom.Dataset()
    setattr(dataset, field, b"\x01\x02\x03\x04")
    original = _pixel_fingerprint(dataset)
    setattr(dataset, field, b"\x01\x02\x03\x05")
    assert _pixel_fingerprint(dataset) != original


def test_pixel_fingerprint_tracks_interpretation_fields() -> None:
    dataset = pydicom.Dataset()
    dataset.Rows = 2
    original = _pixel_fingerprint(dataset)
    dataset.Rows = 3
    assert _pixel_fingerprint(dataset) != original


def test_read_metadata_extracts_expected_fields(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    dicom_path = make_dicom_file(
        tmp_path / "1.dcm",
        PatientID="sub-01",
        SeriesNumber=3,
        SeriesDescription="DWI",
        ProtocolName="DWI_64dir",
        InstanceNumber=7,
        EchoNumbers=2,
    )

    metadata = read_metadata(dicom_path)

    assert metadata.path == dicom_path
    assert metadata.patient_id == "sub-01"
    assert metadata.series_number == 3
    assert metadata.series_description == "DWI"
    assert metadata.protocol_name == "DWI_64dir"
    assert metadata.instance_number == 7
    assert metadata.echo_number == 2
    assert metadata.study_date == "20260101"
    assert metadata.study_time == "120000"


def test_read_metadata_rejects_non_dicom_file(tmp_path: Path) -> None:
    not_dicom = tmp_path / "notes.txt"
    not_dicom.write_text("this is not a DICOM file")

    with pytest.raises(NotDicomError):
        read_metadata(not_dicom)


def test_iter_dicom_metadata_finds_only_dicom_files_recursively(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    dicom_a = make_dicom_file(tmp_path / "series-a" / "1.dcm")
    dicom_b = make_dicom_file(tmp_path / "series-b" / "nested" / "2.dcm")
    (tmp_path / "series-a" / "readme.txt").write_text("not a dicom file")

    found = {metadata.path for metadata in iter_dicom_metadata(tmp_path)}

    assert found == {dicom_a, dicom_b}


def test_read_metadata_takes_echo_and_asl_context_from_per_frame_groups(
    tmp_path: Path, make_multiframe_dicom_file: Callable[..., Path]
) -> None:
    path = make_multiframe_dicom_file(
        tmp_path / "1.dcm", echo=2, echo_time=24.6, asl_context="CONTROL", AcquisitionNumber=3
    )

    metadata = read_metadata(path)

    assert metadata.echo_number == 2
    assert metadata.echo_time == 24.6
    assert metadata.asl_context == "CONTROL"
    assert metadata.acquisition_number == 3
    assert metadata.number_of_frames == 2
    assert metadata.mixed_echoes is False


def test_read_metadata_flags_a_file_holding_more_than_one_echo(
    tmp_path: Path, make_multiframe_dicom_file: Callable[..., Path]
) -> None:
    path = make_multiframe_dicom_file(tmp_path / "1.dcm", echo=1)
    dataset = pydicom.dcmread(path)
    dataset.PerFrameFunctionalGroupsSequence[1].MREchoSequence[0].EchoNumbers = 2
    dataset.save_as(path)

    metadata = read_metadata(path)

    assert metadata.mixed_echoes is True
    assert metadata.echo_number is None


def test_optional_number_returns_none_instead_of_raising_on_malformed_values() -> None:
    assert _optional_number("n/a", float) is None
    assert _optional_number(["1", "2"], int) is None
    assert _optional_number("", int) is None
    assert _optional_number("24.6", float) == 24.6


def test_read_metadata_reads_a_single_inversion_time_from_the_shared_groups(
    tmp_path: Path, make_multiframe_dicom_file: Callable[..., Path]
) -> None:
    path = make_multiframe_dicom_file(tmp_path / "1.dcm")
    dataset = pydicom.dcmread(path)
    modifier = pydicom.Dataset()
    modifier.InversionTimes = [800.0]
    shared = pydicom.Dataset()
    shared.MRModifierSequence = [modifier]
    dataset.SharedFunctionalGroupsSequence = [shared]
    dataset.save_as(path)

    assert read_metadata(path).inversion_time == 800.0


def test_iter_dicom_metadata_gives_the_same_list_when_read_in_parallel(
    tmp_path: Path,
    make_dicom_file: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "export"
    for number in range(6):
        make_dicom_file(folder / f"file-{number}", InstanceNumber=number + 1)
    (folder / "notes.txt").write_text("not a DICOM file")
    serial = list(iter_dicom_metadata(folder))

    monkeypatch.setattr(read_module, "_PARALLEL_MIN_FILES", 1)
    parallel = list(iter_dicom_metadata(folder))

    assert len(serial) == 6
    assert parallel == serial


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("max 100000\n", None),
        ("100000 100000\n", 1),
        ("250000 100000\n", 3),
        ("", None),
        ("x y", None),
    ],
)
def test_cpu_limit_from_a_cgroup_quota(text: str, expected: int | None) -> None:
    assert read_module._cpu_limit_from_cgroup(text) == expected
