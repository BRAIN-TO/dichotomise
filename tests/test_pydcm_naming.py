"""Rectified file and folder names."""

from __future__ import annotations

from pathlib import Path

from dichotomise.pydcm.naming import rectified_path, with_new_sop_instance_uid
from dichotomise.pydcm.read import DicomMetadata


def _metadata(**overrides: object) -> DicomMetadata:
    """A `DicomMetadata` with fixed defaults; keyword arguments override them."""
    defaults: dict[str, object] = {
        "path": Path("/source/1.dcm"),
        "patient_id": "sub-01",
        "patient_name": "Doe^Jane^19900101",
        "study_instance_uid": "1.2.3",
        "series_instance_uid": "1.2.840.10008.5.1.4.1.1.4.99",
        "sop_instance_uid": "1.2.3.4",
        "series_number": 21,
        "series_description": "diffusion",
        "protocol_name": "DWI 64 dir",
        "instance_number": 5,
        "echo_number": 1,
        "study_date": "20260101",
        "study_time": "120000",
    }
    defaults.update(overrides)
    return DicomMetadata(**defaults)  # type: ignore[arg-type]


def test_rectified_path_uses_the_series_description_for_the_folder_name() -> None:
    base_dir = Path("/rectified")

    result = rectified_path(base_dir, _metadata())

    assert result == (base_dir / "021-diffusion" / "021_0005_1.2.3.4_e01.dcm")


def test_rectified_path_pads_series_instance_and_echo_numbers() -> None:
    result = rectified_path(
        Path("/rectified"),
        _metadata(series_number=3, instance_number=42, echo_number=9),
    )

    assert result.parent.name.startswith("003-")
    assert result.name.startswith("003_0042_")
    assert result.name.endswith("_e09.dcm")


def test_rectified_path_falls_back_to_na_for_blank_series_description() -> None:
    result = rectified_path(Path("/rectified"), _metadata(series_description=""))

    assert result.parent.name == "021-NA"


def test_rectified_path_defaults_missing_echo_number_to_one() -> None:
    result = rectified_path(Path("/rectified"), _metadata(echo_number=None))

    assert "_e01.dcm" in result.name


def test_rectified_path_is_unique_per_sop_instance_uid_even_with_repeated_instance_numbers() -> (
    None
):
    first = rectified_path(Path("/r"), _metadata(sop_instance_uid="1.2.3.4", instance_number=1))
    second = rectified_path(Path("/r"), _metadata(sop_instance_uid="1.2.3.5", instance_number=1))

    assert first != second


def test_with_new_sop_instance_uid_swaps_the_uid_in_the_filename() -> None:
    path = rectified_path(Path("/r"), _metadata(sop_instance_uid="1.2.3.4"))

    renamed = with_new_sop_instance_uid(path, "1.2.3.4", "9.8.7")

    assert renamed.name == "021_0005_9.8.7_e01.dcm"
    assert renamed.parent == path.parent


def test_rectified_path_uses_only_the_series_number_when_the_description_is_withheld() -> None:
    result = rectified_path(Path("/rectified"), _metadata(), describe_series=False)

    assert result.parent == Path("/rectified/021")


def test_with_new_sop_instance_uid_leaves_the_series_and_instance_prefix_alone() -> None:
    path = rectified_path(Path("/r"), _metadata(sop_instance_uid="1", series_number=1))

    renamed = with_new_sop_instance_uid(path, "1", "9")

    assert renamed.name == "001_0005_9_e01.dcm"
