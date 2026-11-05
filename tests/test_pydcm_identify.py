from __future__ import annotations

import shutil
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

import pytest

from dichotomise.pydcm.identify import (
    file_digest,
    find_duplicate_pairs,
    find_misfiled_files,
    find_sop_conflicts,
)
from dichotomise.pydcm.read import DicomMetadata, iter_dicom_metadata, read_metadata

DATA_DIR = (
    Path(__file__).parent
    / "data"
    / "reduced_export"
    / "ANONPHA_ANONPHA_20260914"
    / "_20260914_133853.300000_6"
    / "_99_SR_1"
)


def _metadata(path: Path, **overrides: object) -> DicomMetadata:
    defaults: dict[str, object] = {
        "path": path,
        "patient_id": "sub-01",
        "patient_name": "Doe^Jane^19900101",
        "study_instance_uid": "1.2.3",
        "series_instance_uid": "1.2.840.10008.5.1.4.1.1.4.99",
        "sop_instance_uid": "1.2.3.4",
        "series_number": 21,
        "series_description": "diffusion",
        "protocol_name": "DWI_64dir",
        "instance_number": 1,
        "echo_number": 1,
        "study_date": "20260101",
        "study_time": "120000",
    }
    defaults.update(overrides)
    return DicomMetadata(**defaults)  # type: ignore[arg-type]


def test_file_digest_matches_only_for_byte_identical_files(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    original = make_dicom_file(tmp_path / "1.dcm")
    copy = tmp_path / "2.dcm"
    shutil.copyfile(original, copy)

    assert file_digest(original) == file_digest(copy)


def test_file_digest_differs_when_only_the_sop_instance_uid_differs(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    # Same scan content, but a new unique ID: not a byte-for-byte copy.
    shared = {"StudyInstanceUID": "1.2.3", "SeriesInstanceUID": "1.2.4", "InstanceNumber": 1}
    first = make_dicom_file(tmp_path / "1.dcm", **shared)
    second = make_dicom_file(tmp_path / "2.dcm", **shared)

    assert file_digest(first) != file_digest(second)


def test_find_duplicate_pairs_flags_only_byte_identical_later_copies(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    original = make_dicom_file(tmp_path / "1.dcm")
    repeat = tmp_path / "2.dcm"
    shutil.copyfile(original, repeat)
    resaved_with_new_uid = make_dicom_file(tmp_path / "3.dcm")
    different = make_dicom_file(tmp_path / "4.dcm", InstanceNumber=2)

    duplicates = set(find_duplicate_pairs([original, repeat, resaved_with_new_uid, different]))

    assert duplicates == {repeat}


def test_find_duplicate_pairs_keeps_the_numerically_first_file_as_the_original(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    ten = make_dicom_file(tmp_path / "10.dcm")
    two = tmp_path / "2.dcm"
    shutil.copyfile(ten, two)

    assert find_duplicate_pairs([ten, two]) == {ten: two}


def test_find_sop_conflicts_flags_every_file_sharing_a_uid_with_different_bytes(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    first = make_dicom_file(tmp_path / "1.dcm", SOPInstanceUID="1.2.3.9", InstanceNumber=1)
    second = make_dicom_file(tmp_path / "2.dcm", SOPInstanceUID="1.2.3.9", InstanceNumber=2)
    unrelated = make_dicom_file(tmp_path / "3.dcm")
    files = [read_metadata(path) for path in (first, second, unrelated)]

    assert find_sop_conflicts(files) == {first, second}


def test_find_sop_conflicts_ignores_a_byte_identical_repeat(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    original = make_dicom_file(tmp_path / "1.dcm")
    repeat = tmp_path / "2.dcm"
    shutil.copyfile(original, repeat)

    assert find_sop_conflicts([read_metadata(original), read_metadata(repeat)]) == set()


def test_find_misfiled_files_flags_a_file_that_disagrees_with_its_folder() -> None:
    belongs = _metadata(Path("021-DWI/1.dcm"), series_instance_uid="series-a")
    also_belongs = _metadata(Path("021-DWI/2.dcm"), series_instance_uid="series-a")
    misfiled = _metadata(Path("021-DWI/3.dcm"), series_instance_uid="series-b")

    result = find_misfiled_files({"021-DWI": [belongs, also_belongs, misfiled]})

    assert result == {misfiled.path}


def test_find_misfiled_files_flags_nothing_when_a_folder_agrees_with_itself() -> None:
    belongs = _metadata(Path("021-DWI/1.dcm"))
    also_belongs = _metadata(Path("021-DWI/2.dcm"))

    result = find_misfiled_files({"021-DWI": [belongs, also_belongs]})

    assert result == set()


def test_real_example_export_has_no_duplicates_or_misfiled_files() -> None:
    files_by_folder: dict[str, list[DicomMetadata]] = defaultdict(list)
    for metadata in iter_dicom_metadata(DATA_DIR):
        files_by_folder[str(metadata.path.parent)].append(metadata)

    if not files_by_folder:
        pytest.skip("optional real scanner data is unavailable under tests/data")

    for folder, items in files_by_folder.items():
        duplicates = set(find_duplicate_pairs([item.path for item in items]))
        assert duplicates == set(), f"unexpected duplicate(s) in {folder}"

    assert find_misfiled_files(files_by_folder) == set()
