"""Discover DICOM files and read the metadata the pipeline needs."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pydicom
from pydicom.errors import InvalidDicomError
from pydicom.multival import MultiValue

from dichotomise.errors import InvalidDicomMetadataError, NotDicomError


@dataclass(frozen=True)
class DicomMetadata:
    """The DICOM fields the pipeline needs, read from one file."""

    path: Path
    patient_id: str
    patient_name: str
    study_instance_uid: str
    series_instance_uid: str
    sop_instance_uid: str
    series_number: int
    series_description: str
    protocol_name: str
    instance_number: int
    echo_number: int | None
    study_date: str
    study_time: str
    modality: str = ""
    echo_time: float | None = None
    acquisition_number: int | None = None
    acquisition_datetime: str = ""
    number_of_frames: int = 1
    asl_context: str | None = None
    mixed_echoes: bool = False
    inversion_time: float | None = None
    is_asl: bool = False


def pixel_fingerprint(dataset: pydicom.Dataset) -> tuple[object, ...]:
    """Summarise a file's pixel data and the fields that say how to read it.

    Two datasets with equal fingerprints decode to the same image. Used to prove that
    sanitising and copying left the pixels, and their interpretation, untouched.
    """
    payloads = []
    for field in ("PixelData", "FloatPixelData", "DoubleFloatPixelData"):
        value = dataset.get(field)
        payloads.append(None if value is None else (len(value), hashlib.sha256(value).digest()))
    fields = (
        "Rows",
        "Columns",
        "NumberOfFrames",
        "SamplesPerPixel",
        "BitsAllocated",
        "BitsStored",
        "HighBit",
        "PixelRepresentation",
        "PlanarConfiguration",
        "PhotometricInterpretation",
    )
    file_meta = getattr(dataset, "file_meta", None)
    transfer_syntax = None if file_meta is None else file_meta.get("TransferSyntaxUID")
    return (*payloads, str(transfer_syntax), *(str(dataset.get(field)) for field in fields))


def _optional_number(value: object, convert: Callable[[Any], int | float]) -> Any:
    """Convert an optional, descriptive header value; None if absent or unusable.

    Used for fields that only feed notes and inference (echo time, acquisition
    number, ...). A malformed one must never stop a run that would otherwise
    have completed.
    """
    if value in (None, ""):
        return None
    try:
        return convert(value)
    except (TypeError, ValueError):
        return None


def _number_or_zero(value: object, field: str) -> int:
    """Return a DICOM number, using zero when an optional field is blank."""
    if value in (None, ""):
        return 0
    try:
        return int(str(value))
    except (TypeError, ValueError) as error:
        raise InvalidDicomMetadataError(
            f"{field} is not a usable whole number: {value!r}"
        ) from error


def _per_frame_values(dataset: pydicom.Dataset) -> tuple[set[int], set[float], set[str]]:
    """Collect the echo numbers, echo times and ASL contexts stated frame by frame.

    Enhanced (multi-frame) MR keeps these in the per-frame functional groups
    rather than at the top level of the file.
    """
    echo_numbers: set[int] = set()
    echo_times: set[float] = set()
    asl_contexts: set[str] = set()
    for frame in dataset.get("PerFrameFunctionalGroupsSequence", []):
        for echo in frame.get("MREchoSequence", [])[:1]:
            number = _optional_number(echo.get("EchoNumbers"), int)
            if number is not None:
                echo_numbers.add(number)
            echo_time = _optional_number(echo.get("EffectiveEchoTime"), float)
            if echo_time is not None:
                echo_times.add(echo_time)
        for asl in frame.get("MRArterialSpinLabelingSequence", [])[:1]:
            if asl.get("ASLContext"):
                asl_contexts.add(str(asl.ASLContext))
    return echo_numbers, echo_times, asl_contexts


def _inversion_time(dataset: pydicom.Dataset) -> float | None:
    """Return a stated inversion time (ms): top-level, or from the shared functional groups."""
    value = dataset.get("InversionTime")
    if value in (None, ""):
        for group in dataset.get("SharedFunctionalGroupsSequence", [])[:1]:
            for modifier in group.get("MRModifierSequence", [])[:1]:
                times = modifier.get("InversionTimes")
                # A single time is a bare number; several arrive as a list.
                value = times[0] if isinstance(times, MultiValue) and times else times
    inversion_time: float | None = _optional_number(value, float)
    return inversion_time


def read_metadata(path: Path) -> DicomMetadata:
    """Read the DICOM fields dichotomise needs from `path`.

    Raises NotDicomError if `path` cannot be read as a DICOM file.
    """
    try:
        dataset = pydicom.dcmread(path, stop_before_pixels=True)
    except (InvalidDicomError, OSError) as error:
        raise NotDicomError(f"{path} is not a readable DICOM file") from error

    return metadata_from_dataset(dataset, path)


def metadata_from_dataset(dataset: pydicom.Dataset, path: Path) -> DicomMetadata:
    """Build `DicomMetadata` from an open dataset; echo and ASL values may be per-frame."""
    echo_number = dataset.get("EchoNumbers")
    echo_time = dataset.get("EchoTime")
    frame_echo_numbers, frame_echo_times, frame_asl_contexts = _per_frame_values(dataset)
    mixed_echoes = len(frame_echo_numbers) > 1
    if echo_number is None and len(frame_echo_numbers) == 1:
        echo_number = next(iter(frame_echo_numbers))
    if echo_time is None and len(frame_echo_times) == 1:
        echo_time = next(iter(frame_echo_times))
    acquisition_number = dataset.get("AcquisitionNumber")
    return DicomMetadata(
        path=path,
        patient_id=str(dataset.get("PatientID", "")),
        patient_name=str(dataset.get("PatientName", "")),
        study_instance_uid=str(dataset.get("StudyInstanceUID", "")),
        series_instance_uid=str(dataset.get("SeriesInstanceUID", "")),
        sop_instance_uid=str(dataset.get("SOPInstanceUID", "")),
        series_number=_number_or_zero(dataset.get("SeriesNumber"), "SeriesNumber"),
        series_description=str(dataset.get("SeriesDescription", "")),
        protocol_name=str(dataset.get("ProtocolName", "")),
        instance_number=_number_or_zero(dataset.get("InstanceNumber"), "InstanceNumber"),
        echo_number=_number_or_zero(echo_number, "EchoNumbers")
        if echo_number is not None
        else None,
        study_date=str(dataset.get("StudyDate", "")),
        study_time=str(dataset.get("StudyTime", "")),
        modality=str(dataset.get("Modality", "")),
        echo_time=_optional_number(echo_time, float),
        acquisition_number=_optional_number(acquisition_number, int),
        acquisition_datetime=str(dataset.get("AcquisitionDateTime", "")),
        number_of_frames=_optional_number(dataset.get("NumberOfFrames"), int) or 1,
        asl_context="/".join(sorted(frame_asl_contexts)) or None,
        mixed_echoes=mixed_echoes,
        inversion_time=_inversion_time(dataset),
        is_asl="ASL" in [str(part) for part in dataset.get("ImageType", [])]
        or bool(frame_asl_contexts),
    )


# Reading headers is CPU-bound Python (pydicom parses a multi-frame file's per-frame
# groups on first access), so a large export is read by several processes. Below this
# many files the pool's start-up costs more than it saves.
_PARALLEL_MIN_FILES = 200
_MAX_WORKERS = 16
_CHUNK_SIZE = 16


def _read_or_none(path: Path) -> DicomMetadata | None:
    """Read one file's metadata, returning None when it is not DICOM (so a pool can skip it)."""
    try:
        return read_metadata(path)
    except NotDicomError:
        return None


def _cpu_limit_from_cgroup(text: str) -> int | None:
    """The CPU count a cgroup v2 `cpu.max` quota allows (rounded up), or None if unlimited."""
    parts = text.split()
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit() or int(parts[1]) == 0:
        return None
    return max(1, -(-int(parts[0]) // int(parts[1])))


def _worker_count() -> int:
    """How many header-reading processes to start: usable CPUs, capped by any container quota."""
    try:
        available = len(os.sched_getaffinity(0))
    except AttributeError:  # not available on every platform
        available = os.cpu_count() or 1
    try:  # a container started with --cpus limits CPU time, not the CPUs it can see
        quota = _cpu_limit_from_cgroup(Path("/sys/fs/cgroup/cpu.max").read_text())
    except OSError:
        quota = None
    return max(1, min(available, quota or available, _MAX_WORKERS))


def _available_memory() -> int | None:
    """Bytes of RAM this process may use: the cgroup limit if there is one, else the machine's."""
    try:
        limit = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if limit.isdigit():
            return int(limit)
    except OSError:
        pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return None  # shortcut: no cap where the platform cannot say, so memory is not bounded


def _workers_for(largest_file: int) -> int:
    """How many workers fit in half the memory, each holding about twice the largest file."""
    memory = _available_memory()
    if memory is None or largest_file <= 0:
        return _MAX_WORKERS
    return max(1, memory // 2 // (2 * largest_file))


def _read_all(paths: list[Path]) -> list[DicomMetadata | None]:
    """Read every path's metadata, in a pool of processes for a large export."""
    return _parallel_map(_read_or_none, paths, chunksize=_CHUNK_SIZE)


def _parallel_map(
    function: Callable[[Any], Any],
    items: list[Any],
    *,
    chunksize: int = 1,
    initializer: Callable[..., None] | None = None,
    initargs: tuple[Any, ...] = (),
    largest_file: int = 0,
) -> list[Any]:
    """Apply `function` to every item, in order, using a pool of processes when there are many.

    Below `_PARALLEL_MIN_FILES` items (read at call time, so a test can lower it), or when no pool
    can start here or one breaks (a sandbox without shared memory, no `forkserver` on Windows, a
    killed worker), the items are mapped one by one. That is safe to repeat because `function`
    must only read, or rewrite identical output. An error `function` itself raises is not retried:
    it is the first in input order, exactly as serially. `function` must be a module-level
    function (workers pickle it). `initializer(*initargs)` runs once per worker (and once here
    for the one-by-one path), so shared read-only context is sent once, not with every item.
    `largest_file` (bytes), when the items are whole files, limits the workers so that they fit in
    memory; a single worker means the items are mapped one by one.
    """
    workers = min(_worker_count(), _workers_for(largest_file))
    if len(items) >= _PARALLEL_MIN_FILES and workers >= 2:
        try:
            # forkserver, not fork: the CLI runs this beside a ticker thread.
            context = multiprocessing.get_context("forkserver")
            pool = ProcessPoolExecutor(
                max_workers=workers,
                mp_context=context,
                initializer=initializer,
                initargs=initargs,
            )
        except (OSError, NotImplementedError, ValueError):
            pool = None  # e.g. a sandbox without shared memory, or no forkserver on Windows
        if pool is not None:
            try:
                with pool:
                    return list(pool.map(function, items, chunksize=chunksize))
            except BrokenProcessPool:
                pass  # a worker died: do the work again here, where the real error will show
    if initializer is not None:
        initializer(*initargs)
    return [function(item) for item in items]


def iter_dicom_metadata(root: Path) -> Iterator[DicomMetadata]:
    """Recursively yield metadata for every readable DICOM file under `root`.

    Non-DICOM files (stray text files, checksums, thumbnails, ...) are
    silently skipped rather than raised on, since an export directory
    commonly contains a mix. Files come back in sorted path order whether
    they were read serially or in parallel.
    """
    paths = sorted(p for p in root.rglob("*") if p.is_file())
    for metadata in _read_all(paths):
        if metadata is not None:
            yield metadata
