"""The sanitise stage: policy applied to a rectified tree, with consistent UIDs."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pydicom
from helpers import make_subject

from dichotomise.pydcm.read import read_metadata
from dichotomise.pydcm.relabel import Policy, load_policy
from dichotomise.stages.rectify import RectifyResult, RectifyReviewResult
from dichotomise.stages.sanitise import sanitise, sanitise_review


def test_sanitise_applies_the_policy_and_writes_a_new_tree(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    rectified_dir = tmp_path / "rectify"
    file_a = make_dicom_file(
        rectified_dir / "021-DWI" / "021_series-dwi_0001_e01.dcm",
        PatientID="12345",
        PatientName="Doe^Jane^19900101",
        StudyInstanceUID="study-a",
        InstitutionName="Sunnybrook",
    )

    subject = make_subject(tmp_path / "capture")
    from dichotomise.pydcm.read import read_metadata

    rectify_result = RectifyResult(
        subject=subject, rectified_dir=rectified_dir, files=[read_metadata(file_a)]
    )

    result = sanitise(rectify_result, policy=load_policy("standard"), subject_label="sub-0005")

    assert len(result.files) == 1
    sanitised_path = result.files[0]
    assert sanitised_path != file_a
    written = pydicom.dcmread(sanitised_path, force=True)
    assert written.PatientID == "sub-0005"
    assert written.PatientName == "sub-0005"
    assert not hasattr(written, "InstitutionName")
    # The original rectified file is untouched.
    original = pydicom.dcmread(file_a, force=True)
    assert original.PatientID == "12345"


def test_full_policy_does_not_keep_series_description_in_folder(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    rectified_dir = tmp_path / "rectify"
    source = make_dicom_file(
        rectified_dir / "001-private_name" / "1.dcm", SeriesDescription="private_name"
    )
    result = sanitise(
        RectifyResult(make_subject(tmp_path), rectified_dir, [read_metadata(source)]),
        policy=load_policy("full"),
        subject_label="sub-0001",
    )
    assert result.files[0].parent.name == "001-NA"
    assert "private_name" not in str(result.files[0])


def test_series_folder_uses_resolved_policy_placeholder(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    rectified_dir = tmp_path / "rectify"
    source = make_dicom_file(rectified_dir / "001-private" / "1.dcm")
    policy = Policy(
        name="custom",
        actions={"SeriesDescription": {"action": "replace", "value": "<subject-label>"}},
    )
    result = sanitise(
        RectifyResult(make_subject(tmp_path), rectified_dir, [read_metadata(source)]),
        policy=policy,
        subject_label="sub-0001",
    )
    assert result.files[0].parent.name == "001-sub-0001"


def test_sanitise_regenerates_uids_consistently_across_files(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    rectified_dir = tmp_path / "rectify"
    file_a = make_dicom_file(
        rectified_dir / "021-DWI" / "1.dcm", StudyInstanceUID="study-a", InstanceNumber=1
    )
    file_b = make_dicom_file(
        rectified_dir / "021-DWI" / "2.dcm", StudyInstanceUID="study-a", InstanceNumber=2
    )

    subject = make_subject(tmp_path / "capture")
    from dichotomise.pydcm.read import read_metadata

    rectify_result = RectifyResult(
        subject=subject,
        rectified_dir=rectified_dir,
        files=[read_metadata(file_a), read_metadata(file_b)],
    )

    result = sanitise(rectify_result, policy=load_policy("standard"), subject_label="sub-0005")

    written_a = pydicom.dcmread(result.files[0], force=True)
    written_b = pydicom.dcmread(result.files[1], force=True)
    assert written_a.StudyInstanceUID == written_b.StudyInstanceUID
    assert written_a.StudyInstanceUID != "study-a"


def test_sanitise_review_returns_none_when_there_are_no_review_files(tmp_path: Path) -> None:
    rectify_review_result = RectifyReviewResult(
        review_rectified_dir=tmp_path / "review-rectify", files=[], collision_count=0
    )

    result = sanitise_review(
        rectify_review_result,
        policy=load_policy("standard"),
        subject_label="sub-0005",
        scan_date="20260101",
        replacement_cache={},
    )

    assert result is None


def test_sanitise_review_deidentifies_content_and_preserves_rectified_names(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    review_rectified_dir = tmp_path / "review-rectify"
    file_a = make_dicom_file(
        review_rectified_dir / "021-diffusion" / "021_series-dwi_0001_e01.dcm",
        PatientID="12345",
        PatientName="Doe^Jane^19900101",
        StudyInstanceUID="study-a",
    )
    from dichotomise.pydcm.read import read_metadata

    rectify_review_result = RectifyReviewResult(
        review_rectified_dir=review_rectified_dir, files=[read_metadata(file_a)], collision_count=0
    )

    result = sanitise_review(
        rectify_review_result,
        policy=load_policy("standard"),
        subject_label="sub-0005",
        scan_date="20260101",
        replacement_cache={},
    )

    assert result is not None
    assert len(result.files) == 1
    sanitised_path = result.files[0]
    expected = result.sanitised_review_dir / "021-diffusion" / "021_series-dwi_0001_e01.dcm"
    assert sanitised_path == expected
    written = pydicom.dcmread(sanitised_path, force=True)
    assert written.PatientID == "sub-0005"
    original = pydicom.dcmread(file_a, force=True)
    assert original.PatientID == "12345"


def test_sanitise_review_shares_replacement_cache_with_retained_sanitise(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    from dichotomise.pydcm.read import read_metadata

    rectified_dir = tmp_path / "rectify"
    retained_file = make_dicom_file(
        rectified_dir / "021-DWI" / "021_series-a_0001_e01.dcm", StudyInstanceUID="shared-study"
    )
    subject = make_subject(tmp_path / "capture")
    rectify_result = RectifyResult(
        subject=subject, rectified_dir=rectified_dir, files=[read_metadata(retained_file)]
    )

    review_rectified_dir = tmp_path / "review-rectify"
    review_file = make_dicom_file(
        review_rectified_dir / "022-fMRI" / "022_series-b_0001_e01.dcm",
        StudyInstanceUID="shared-study",
    )
    rectify_review_result = RectifyReviewResult(
        review_rectified_dir=review_rectified_dir,
        files=[read_metadata(review_file)],
        collision_count=0,
    )

    replacement_cache: dict[str, str] = {}
    policy = load_policy("standard")
    retained_result = sanitise(
        rectify_result,
        policy=policy,
        subject_label="sub-0005",
        replacement_cache=replacement_cache,
    )
    review_result = sanitise_review(
        rectify_review_result,
        policy=policy,
        subject_label="sub-0005",
        scan_date="20260101",
        replacement_cache=replacement_cache,
    )

    assert review_result is not None
    retained_written = pydicom.dcmread(retained_result.files[0], force=True)
    review_written = pydicom.dcmread(review_result.files[0], force=True)
    assert retained_written.StudyInstanceUID == review_written.StudyInstanceUID
    assert retained_written.StudyInstanceUID != "shared-study"


def test_sanitise_renames_files_so_the_filename_matches_the_regenerated_sop_uid(
    tmp_path: Path, make_dicom_file: Callable[..., Path]
) -> None:
    original_uid = "1.3.12.2.1107.5.2.61.237011.20261020110718"
    rectified_dir = tmp_path / "rectify"
    source = make_dicom_file(
        rectified_dir / "021-DWI" / f"021_0001_{original_uid}_e01.dcm",
        SOPInstanceUID=original_uid,
        InstanceNumber=1,
    )
    metadata = read_metadata(source)
    rectify_result = RectifyResult(
        subject=make_subject(tmp_path), rectified_dir=rectified_dir, files=[metadata]
    )

    result = sanitise(
        rectify_result,
        policy=load_policy("standard"),
        subject_label="sub-0001",
        replacement_cache={},
    )

    (output,) = result.files
    new_uid = read_metadata(output).sop_instance_uid
    assert new_uid != original_uid
    assert output.name == f"021_0001_{new_uid}_e01.dcm"
    assert original_uid not in output.name
    assert [p.name for p in output.parent.iterdir()] == [output.name]
