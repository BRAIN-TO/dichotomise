"""Describe multi-echo / multi-TI ASL acquisitions from the headers of one series."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from dichotomise.pydcm.read import DicomMetadata

_MAX_TI_LEVELS = 8
_LEVEL_WORDS = {2: "dual", 3: "triple", 4: "quadruple"}


def infer_ti_index(files: Sequence[DicomMetadata]) -> dict[Path, int]:
    """Infer a 1-based TI index per file from acquisition timing, or {} if not clear.

    Siemens does not write the inversion time of these ASL files. What the
    headers do show is that each acquisition (AcquisitionNumber) is written
    as several multi-frame files per echo, one per TI, in time order. So
    within one acquisition and echo, the files ranked by AcquisitionDateTime
    give TI 1, 2, ... The result is only returned when it is unambiguous:
    every file is multi-frame ASL (ImageType ASL or an ASL context), states
    no inversion time of its own (if the headers give one, use that, do not
    infer), has an acquisition number and
    time, and every (acquisition, echo) group holds the same 2..8 files with
    distinct times.
    It is an inference from ordering, never a TI value.
    """
    if not files or any(
        not item.is_asl
        or item.number_of_frames < 2
        or item.inversion_time is not None
        or item.acquisition_number is None
        or not item.acquisition_datetime
        for item in files
    ):
        return {}
    groups: dict[tuple[int, int | None], list[DicomMetadata]] = {}
    for item in files:
        assert item.acquisition_number is not None
        groups.setdefault((item.acquisition_number, item.echo_number), []).append(item)
    sizes = {len(group) for group in groups.values()}
    if len(sizes) != 1:
        return {}
    (size,) = sizes
    if not 2 <= size <= _MAX_TI_LEVELS:
        return {}
    index: dict[Path, int] = {}
    for group in groups.values():
        times = [item.acquisition_datetime for item in group]
        if len(set(times)) != len(times):
            return {}
        ranked = sorted(group, key=lambda item: item.acquisition_datetime)
        for rank, item in enumerate(ranked, start=1):
            index[item.path] = rank
    return index


@dataclass(frozen=True)
class AcquisitionSummary:
    """What a series' headers say about echoes, TI levels and label/control."""

    echo_times: dict[int, float | None]
    ti_levels: int
    asl_contexts: tuple[str, ...]
    acquisitions: int

    @property
    def is_multi_echo_or_ti(self) -> bool:
        """True when the series holds more than one echo time or inversion-time level."""
        return len(self.echo_times) > 1 or self.ti_levels > 1

    def describe(self) -> str:
        """One line such as "dual-echo (TE 10.0/24.6 ms), dual-TI (inferred ...)"."""
        parts: list[str] = []
        echoes = len(self.echo_times)
        if echoes > 1:
            times = "/".join(f"{te:g}" for te in self.echo_times.values() if te is not None)
            word = _LEVEL_WORDS.get(echoes, str(echoes))
            parts.append(f"{word}-echo" + (f" (TE {times} ms)" if times else ""))
        if self.ti_levels > 1:
            word = _LEVEL_WORDS.get(self.ti_levels, str(self.ti_levels))
            parts.append(f"{word}-TI (inferred from acquisition order, no TI in the headers)")
        if self.asl_contexts:
            alternating = "/".join(self.asl_contexts)
            parts.append(
                f"{alternating} alternating"
                if len(self.asl_contexts) > 1
                else f"ASL context {alternating}"
            )
        if self.acquisitions > 1:
            parts.append(f"{self.acquisitions} acquisitions")
        return ", ".join(parts)


def summarise_acquisition(files: Sequence[DicomMetadata]) -> AcquisitionSummary:
    """Summarise echoes, inferred TI levels and ASL contexts across one series' files."""
    echo_times: dict[int, float | None] = {}
    for item in files:
        if item.echo_number is not None and echo_times.get(item.echo_number) is None:
            echo_times[item.echo_number] = item.echo_time
    contexts: list[str] = []
    for item in sorted(files, key=lambda f: (f.acquisition_number or 0, f.acquisition_datetime)):
        if item.asl_context and item.asl_context not in contexts:
            contexts.append(item.asl_context)
    ti_index = infer_ti_index(files)
    return AcquisitionSummary(
        echo_times=dict(sorted(echo_times.items())),
        ti_levels=max(ti_index.values(), default=0),
        asl_contexts=tuple(contexts),
        acquisitions=len({f.acquisition_number for f in files if f.acquisition_number is not None}),
    )
