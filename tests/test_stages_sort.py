"""Sort mode: put a folder of DICOMs into the pipeline's subject/series layout."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from dichotomise.errors import DestinationExistsError, NamingCollisionError, SortError
from dichotomise.stages.sort import apply_sort, plan_sort

_STUDY = "1.2.9.1"


def _flat_export(source: Path, make: Callable[..., Path], *, suffix: str = "") -> None:
    """Two series, every file directly in `source`, named like a scanner dump."""
    for number, (series, description) in enumerate([(21, "DWI"), (22, "T1")], start=1):
        make(
            source / f"IM{number:04d}{suffix}",
            StudyInstanceUID=_STUDY,
            SeriesInstanceUID=f"1.2.9.{series}",
            SeriesNumber=series,
            SeriesDescription=description,
            SOPInstanceUID=f"1.2.3.{number}",
            InstanceNumber=1,
        )


def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


@pytest.mark.parametrize("suffix", ["", ".dcm", ".IMA"])
def test_in_place_sort_moves_a_flat_folder_into_subject_and_series_folders(
    tmp_path: Path, make_dicom_file: Callable[..., Path], suffix: str
) -> None:
    source = tmp_path / "export"
    _flat_export(source, make_dicom_file, suffix=suffix)

    result = apply_sort(plan_sort(source))

    assert _tree(source) == [
        "sub-01_20260101/021-DWI/021_0001_1.2.3.1_e01.dcm",
        "sub-01_20260101/022-T1/022_0001_1.2.3.2_e01.dcm",
    ]
    assert result.moved == 2


def test_a_flat_folder_of_two_subjects_gets_one_folder_each(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    for name, patient, study in [("a", "sub-01", "1.2.9.1"), ("b", "sub-02", "1.2.9.2")]:
        make_dicom_file(
            source / name,
            PatientID=patient,
            StudyInstanceUID=study,
            SeriesNumber=3,
            SeriesDescription="T1",
            SOPInstanceUID=f"1.2.3.{ord(name)}",
        )

    apply_sort(plan_sort(source))

    assert _tree(source) == [
        "sub-01_20260101/003-T1/003_0001_1.2.3.97_e01.dcm",
        "sub-02_20260101/003-T1/003_0001_1.2.3.98_e01.dcm",
    ]


def test_a_name_clash_between_different_files_is_refused_and_nothing_moves(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    for name, protocol in [("a", "one"), ("b", "two")]:
        make_dicom_file(
            source / name,
            StudyInstanceUID=_STUDY,
            SOPInstanceUID="1.2.3.1",
            ProtocolName=protocol,
        )
    before = _tree(source)

    with pytest.raises(NamingCollisionError, match="full pipeline"):
        plan_sort(source)

    assert _tree(source) == before


def test_an_existing_target_is_refused(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    _flat_export(source, make_dicom_file)
    taken = source / "sub-01_20260101/021-DWI/021_0001_1.2.3.1_e01.dcm"
    taken.parent.mkdir(parents=True)
    taken.write_bytes(b"not a dicom, but in the way")

    with pytest.raises(DestinationExistsError):
        plan_sort(source)


def test_in_place_refuses_a_source_that_already_has_subfolders(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "old-folder" / "1.dcm", StudyInstanceUID=_STUDY)
    before = _tree(source)

    with pytest.raises(SortError, match="--out-dir"):
        plan_sort(source)

    assert _tree(source) == before


def test_planning_changes_nothing(tmp_path: Path, make_dicom_file: Callable[..., Path]) -> None:
    source = tmp_path / "export"
    _flat_export(source, make_dicom_file)
    before = _tree(source)

    plan = plan_sort(source)

    assert len(plan.moves) == 2
    assert _tree(source) == before


def test_a_sort_can_be_resumed_and_a_sorted_tree_has_nothing_to_move(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    _flat_export(source, make_dicom_file)
    first = plan_sort(source).moves[0]
    first[1].parent.mkdir(parents=True)
    first[0].rename(first[1])  # as if an earlier run stopped after one file

    resumed = plan_sort(source)
    assert len(resumed.moves) == 1
    assert len(resumed.in_place_already) == 1
    assert apply_sort(resumed).moved == 1

    assert plan_sort(source).moves == []


def test_an_identical_repeat_is_left_in_place_and_never_deleted(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    _flat_export(source, make_dicom_file)
    (source / "IM0003").write_bytes((source / "IM0001").read_bytes())

    plan = plan_sort(source)
    apply_sort(plan)

    assert len(plan.repeats) == 1
    assert len(_tree(source)) == 3


def test_copy_mode_leaves_the_source_alone_and_checks_each_copy(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source, out = tmp_path / "export", tmp_path / "sorted"
    make_dicom_file(
        source / "deep" / "er" / "1.dcm", StudyInstanceUID=_STUDY, SOPInstanceUID="1.2.3.1"
    )
    before = {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}

    result = apply_sort(plan_sort(source, out))

    assert result.copied == 1
    assert {p: p.read_bytes() for p in source.rglob("*") if p.is_file()} == before
    (copy,) = out.rglob("*.dcm")
    assert (
        copy.relative_to(out).as_posix()
        == "sub-01_20260101/001-test_series/001_0001_1.2.3.1_e01.dcm"
    )
    assert copy.read_bytes() == next(iter(before.values()))


def test_an_out_dir_inside_the_source_is_refused(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    source = tmp_path / "export"
    make_dicom_file(source / "1.dcm", StudyInstanceUID=_STUDY)

    with pytest.raises(SortError, match="outside"):
        plan_sort(source, source / "sorted")
