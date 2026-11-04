from __future__ import annotations

import hashlib
import shutil
import tarfile
from pathlib import Path

import pytest

import dichotomise.utils.archive as archive_module
from dichotomise.errors import ArchiveCreationError
from dichotomise.utils.archive import make_tarball, make_tarball_from_files, verify_archive


def _make_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / "series-a").mkdir(parents=True)
    (source / "series-a" / "1.dcm").write_bytes(b"scan content")
    return source


def test_make_tarball_reports_progress_once_per_file_up_to_the_total(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "series-a").mkdir(parents=True)
    (source / "series-a" / "1.dcm").write_bytes(b"12345")
    (source / "series-a" / "2.dcm").write_bytes(b"1234567890")
    archive_path = tmp_path / "out" / "study_archive.tar.gz"
    updates: list[tuple[int, int]] = []

    def record(done: int, total: int) -> None:
        updates.append((done, total))

    make_tarball(source, archive_path, on_progress=record)

    assert updates == [(5, 15), (15, 15)]


def test_make_tarball_from_files_reports_progress(tmp_path: Path) -> None:
    source = tmp_path / "export"
    file_a = source / "a.dcm"
    file_a.parent.mkdir(parents=True)
    file_a.write_bytes(b"1234567890")
    archive_path = tmp_path / "study_archive.tar.gz"
    updates: list[tuple[int, int]] = []

    def record(done: int, total: int) -> None:
        updates.append((done, total))

    make_tarball_from_files([file_a], source, archive_path, on_progress=record)

    assert updates == [(10, 10)]


def test_source_digests_match_archived_file_contents(tmp_path: Path) -> None:
    source = _make_source(tmp_path) / "series-a" / "1.dcm"
    digests: dict[Path, str] = {}
    archive = archive_module._write_tarball(
        [(source, Path("1.dcm"))], tmp_path / "archive.tar.gz", None, digests
    )
    with tarfile.open(archive.path) as tar:
        member = tar.extractfile("1.dcm")
        assert member is not None
        assert digests[source] == hashlib.sha256(member.read()).hexdigest()


def test_make_tarball_creates_an_archive_containing_the_original_files(tmp_path: Path) -> None:
    source = _make_source(tmp_path)
    archive_path = tmp_path / "out" / "study_archive.tar.gz"

    archive = make_tarball(source, archive_path)

    assert archive.path == archive_path
    assert archive.path.exists()
    with tarfile.open(archive.path, "r:gz") as tar:
        names = tar.getnames()
    assert "series-a/1.dcm" in names


def test_make_tarball_does_not_duplicate_files_nested_inside_folders(tmp_path: Path) -> None:
    source = _make_source(tmp_path)
    archive_path = tmp_path / "study_archive.tar.gz"

    archive = make_tarball(source, archive_path)

    with tarfile.open(archive.path, "r:gz") as tar:
        file_names = [member.name for member in tar.getmembers() if member.isfile()]

    assert file_names.count("series-a/1.dcm") == 1


def test_make_tarball_writes_a_checksum_sidecar_next_to_the_archive(tmp_path: Path) -> None:
    source = _make_source(tmp_path)
    archive_path = tmp_path / "study_archive.tar.gz"

    archive = make_tarball(source, archive_path)

    assert archive.checksum_path == tmp_path / "study_archive.sha256"
    assert archive.checksum_path.exists()
    assert len(archive.checksum_path.read_text().strip()) == 64  # a sha256 hex digest


def test_verify_archive_accepts_an_untampered_archive(tmp_path: Path) -> None:
    source = _make_source(tmp_path)
    archive = make_tarball(source, tmp_path / "study_archive.tar.gz")

    assert verify_archive(archive.path, archive.checksum_path) is True


def test_verify_archive_rejects_a_tampered_archive(tmp_path: Path) -> None:
    source = _make_source(tmp_path)
    archive = make_tarball(source, tmp_path / "study_archive.tar.gz")

    with archive.path.open("ab") as handle:
        handle.write(b"unexpected extra bytes")

    assert verify_archive(archive.path, archive.checksum_path) is False


def test_make_tarball_falls_back_to_the_stdlib_codec_without_pigz(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(archive_module, "pigz_path", lambda: None)
    source = _make_source(tmp_path)

    archive = make_tarball(source, tmp_path / "study_archive.tar.gz")

    with tarfile.open(archive.path, "r:gz") as tar:
        assert "series-a/1.dcm" in tar.getnames()
    assert verify_archive(archive.path, archive.checksum_path) is True


@pytest.mark.skipif(shutil.which("pigz") is None, reason="pigz is not installed")
def test_make_tarball_uses_pigz_when_available(tmp_path: Path) -> None:
    source = _make_source(tmp_path)

    archive = make_tarball(source, tmp_path / "study_archive.tar.gz")

    with tarfile.open(archive.path, "r:gz") as tar:
        assert "series-a/1.dcm" in tar.getnames()
    assert verify_archive(archive.path, archive.checksum_path) is True


def test_make_tarball_raises_when_pigz_exits_with_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fake "pigz" that reads its input (so tarfile never sees a broken
    # pipe) and then fails, to check the failure is surfaced rather than
    # silently producing a truncated or empty archive.
    fake_pigz = tmp_path / "fake-pigz"
    fake_pigz.write_text("#!/bin/sh\ncat >/dev/null\nexit 7\n")
    fake_pigz.chmod(0o755)
    monkeypatch.setattr(archive_module, "pigz_path", lambda: str(fake_pigz))
    source = _make_source(tmp_path)

    with pytest.raises(ArchiveCreationError, match="status 7"):
        make_tarball(source, tmp_path / "study_archive.tar.gz")
