"""Grouping files into acquisitions and spotting multi-echo and multi-TI series."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from dichotomise.pydcm.acquisition import infer_ti_index, summarise_acquisition
from dichotomise.pydcm.read import DicomMetadata, read_metadata


def _write_series(
    folder: Path,
    make_multiframe_dicom_file: Callable[..., Path],
    *,
    acquisitions: int,
    ti_levels: int,
    echoes: tuple[int, ...] = (1, 2),
    asl: bool = True,
    **overrides: object,
) -> list[DicomMetadata]:
    """Write files in Siemens order: per acquisition, per TI, per echo."""
    files = []
    instance = 0
    for acquisition in range(1, acquisitions + 1):
        for ti in range(ti_levels):
            for echo in echoes:
                instance += 1
                path = make_multiframe_dicom_file(
                    folder / f"{instance}.dcm",
                    echo=echo,
                    echo_time=10.0 if echo == 1 else 24.6,
                    asl_context=("LABEL" if acquisition % 2 else "CONTROL") if asl else None,
                    InstanceNumber=instance,
                    AcquisitionNumber=acquisition,
                    AcquisitionDateTime=f"20260101120{acquisition:02d}.{ti * 90 + echo:03d}",
                    SeriesInstanceUID="1.2.3",
                    **overrides,
                )
                files.append(read_metadata(path))
    return files


def test_infer_ti_index_ranks_files_by_time_within_an_acquisition_and_echo(
    tmp_path: Path, make_multiframe_dicom_file: Callable[..., Path]
) -> None:
    files = _write_series(tmp_path, make_multiframe_dicom_file, acquisitions=3, ti_levels=2)

    index = infer_ti_index(files)

    assert [index[item.path] for item in files[:4]] == [1, 1, 2, 2]
    assert max(index.values()) == 2


def test_infer_ti_index_gives_nothing_for_a_single_ti_series(
    tmp_path: Path, make_multiframe_dicom_file: Callable[..., Path]
) -> None:
    files = _write_series(tmp_path, make_multiframe_dicom_file, acquisitions=3, ti_levels=1)

    assert infer_ti_index(files) == {}


def test_infer_ti_index_ignores_multi_file_acquisitions_that_are_not_asl(
    tmp_path: Path, make_multiframe_dicom_file: Callable[..., Path]
) -> None:
    # e.g. a TOF acquisition written as several slab files is not a TI series.
    files = _write_series(
        tmp_path, make_multiframe_dicom_file, acquisitions=2, ti_levels=3, asl=False
    )

    assert infer_ti_index(files) == {}


def test_infer_ti_index_trusts_a_stated_inversion_time_instead(
    tmp_path: Path, make_multiframe_dicom_file: Callable[..., Path]
) -> None:
    files = _write_series(
        tmp_path, make_multiframe_dicom_file, acquisitions=2, ti_levels=2, InversionTime=800
    )

    assert infer_ti_index(files) == {}


def test_infer_ti_index_needs_multi_frame_files(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    files = [
        read_metadata(
            make_dicom_file(
                tmp_path / f"{n}.dcm",
                AcquisitionNumber=1,
                AcquisitionDateTime=f"20260101120000.{n:03d}",
                InstanceNumber=n,
            )
        )
        for n in (1, 2)
    ]

    assert infer_ti_index(files) == {}


def test_summarise_acquisition_describes_dual_echo_dual_ti_label_control(
    tmp_path: Path, make_multiframe_dicom_file: Callable[..., Path]
) -> None:
    files = _write_series(tmp_path, make_multiframe_dicom_file, acquisitions=4, ti_levels=2)

    summary = summarise_acquisition(files)

    assert summary.describe() == (
        "dual-echo (TE 10/24.6 ms), "
        "dual-TI (inferred from acquisition order, no TI in the headers), "
        "LABEL/CONTROL alternating, 4 acquisitions"
    )
