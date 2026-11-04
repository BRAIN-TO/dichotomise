from __future__ import annotations

import pytest
from pydicom.dataset import Dataset

from dichotomise.errors import PolicyNotFoundError, RelabelError
from dichotomise.pydcm.relabel import (
    Policy,
    apply_policy,
    generate_default_label,
    load_policy,
    normalise_numeric_label,
    validate_label,
)


def test_generate_default_label_uses_date_and_initials_from_the_patient_name() -> None:
    # Initials are last-name-first, then first-name, matching LAST^FIRST order.
    assert generate_default_label("Doe^Jane^19900101") == "19900101_DJ"


def test_generate_default_label_rejects_a_name_without_the_expected_shape() -> None:
    with pytest.raises(RelabelError):
        generate_default_label("Doe^Jane")


def test_generate_default_label_rejects_an_invalid_embedded_date() -> None:
    with pytest.raises(RelabelError):
        generate_default_label("Doe^Jane^19901301")  # month 13


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("5", "sub-0005"),
        ("sub-42", "sub-0042"),
        ("0007", "sub-0007"),
    ],
)
def test_normalise_numeric_label_pads_to_four_digits(value: str, expected: str) -> None:
    assert normalise_numeric_label(value) == expected


@pytest.mark.parametrize("value", ["0", "10000", "abc", "sub-"])
def test_normalise_numeric_label_rejects_values_out_of_range(value: str) -> None:
    with pytest.raises(RelabelError):
        normalise_numeric_label(value)


def test_validate_label_accepts_letters_digits_underscore_and_hyphen() -> None:
    assert validate_label("sub-0005_A") == "sub-0005_A"


@pytest.mark.parametrize("label", ["has space", "semi;colon", "a" * 65])
def test_validate_label_rejects_unsafe_or_overlong_labels(label: str) -> None:
    with pytest.raises(RelabelError):
        validate_label(label)


def _dataset(**fields: object) -> Dataset:
    dataset = Dataset()
    for key, value in fields.items():
        setattr(dataset, key, value)
    return dataset


def test_load_policy_default_has_no_actions() -> None:
    policy = load_policy("default")

    assert policy.name == "default"
    assert policy.actions == {}


def test_load_policy_full_includes_standards_actions_as_well_as_its_own() -> None:
    policy = load_policy("full")

    assert "PatientName" in policy.actions  # inherited from standard
    assert policy.actions["PatientName"]["action"] == "replace"
    assert policy.actions["ProtocolName"]["action"] == "replace"  # full's own


def test_load_policy_custom_overrides_its_base_and_adds_new_fields() -> None:
    policy = load_policy("custom")

    assert policy.actions["PatientName"]["action"] == "replace"  # inherited via full -> standard
    assert policy.actions["ProtocolName"]["action"] == "keep"  # custom's override of full's replace
    assert policy.actions["AccessionNumber"]["action"] == "remove"  # custom's own addition


def test_load_policy_accepts_a_json_filename() -> None:
    policy = load_policy("standard.json")

    assert policy.name == "standard"


def test_load_policy_raises_a_plain_error_for_an_unknown_policy_name() -> None:
    with pytest.raises(PolicyNotFoundError):
        load_policy("does-not-exist")


def test_apply_policy_default_leaves_a_dataset_unchanged() -> None:
    dataset = _dataset(PatientName="Doe^Jane^19900101", PatientID="12345")

    apply_policy(
        dataset,
        load_policy("default"),
        subject_label="sub-0005",
        scan_date="20260914",
        replacement_cache={},
    )

    assert dataset.PatientName == "Doe^Jane^19900101"
    assert dataset.PatientID == "12345"


def test_apply_policy_standard_replaces_identity_and_removes_institution() -> None:
    dataset = _dataset(
        PatientName="Doe^Jane^19900101",
        PatientID="12345",
        PatientBirthDate="19900101",
        PatientSex="F",
        InstitutionName="Sunnybrook Health Sciences Center",
        StudyInstanceUID="1.1",
    )

    apply_policy(
        dataset,
        load_policy("standard"),
        subject_label="sub-0005",
        scan_date="20260914",
        replacement_cache={},
    )

    assert dataset.PatientName == "sub-0005"
    assert dataset.PatientID == "sub-0005"
    assert dataset.PatientBirthDate != "19900101"  # scrambled, not the real DOB
    assert dataset.PatientSex == "F"  # demographic fields are kept
    assert dataset.PatientIdentityRemoved == "YES"
    assert not hasattr(dataset, "InstitutionName")
    assert dataset.StudyInstanceUID != "1.1"


def test_apply_policy_regenerates_the_same_uid_consistently_across_files() -> None:
    replacement_cache: dict[str, str] = {}
    first = _dataset(StudyInstanceUID="1.1")
    second = _dataset(StudyInstanceUID="1.1")

    apply_policy(
        first,
        load_policy("standard"),
        subject_label="sub-0005",
        scan_date="20260914",
        replacement_cache=replacement_cache,
    )
    apply_policy(
        second,
        load_policy("standard"),
        subject_label="sub-0005",
        scan_date="20260914",
        replacement_cache=replacement_cache,
    )

    assert first.StudyInstanceUID == second.StudyInstanceUID
    assert first.StudyInstanceUID != "1.1"


def test_sop_reference_is_regenerated_consistently_before_its_target() -> None:
    reference = _dataset()
    nested = Dataset()
    nested.ReferencedSOPInstanceUID = "1.2.3.4"
    reference.ReferencedImageSequence = [nested]
    target = _dataset(SOPInstanceUID="1.2.3.4")
    cache: dict[str, str] = {}
    for dataset in (reference, target):
        apply_policy(
            dataset,
            load_policy("standard"),
            subject_label="sub-0001",
            scan_date="20260101",
            replacement_cache=cache,
        )
    assert nested.ReferencedSOPInstanceUID == target.SOPInstanceUID
    assert target.SOPInstanceUID != "1.2.3.4"


def test_policy_applies_to_nested_datasets_and_removes_sequences() -> None:
    dataset = _dataset()
    nested = Dataset()
    nested.PatientName = "Original^Name"
    dataset.ReferencedImageSequence = [nested]
    removed = Dataset()
    removed.PatientName = "Other^Name"
    dataset.AcquisitionContextSequence = [removed]
    policy = Policy(
        name="nested",
        actions={
            "PatientName": {"action": "replace", "value": "Anonymous"},
            "AcquisitionContextSequence": {"action": "remove"},
        },
    )
    apply_policy(
        dataset, policy, subject_label="sub-0001", scan_date="20260101", replacement_cache={}
    )
    assert str(nested.PatientName) == "Anonymous"
    assert "AcquisitionContextSequence" not in dataset


def test_regeneration_preserves_class_uids_and_remaps_known_references() -> None:
    dataset = _dataset(SOPInstanceUID="1.2.3.4")
    nested = Dataset()
    nested.ReferencedSOPInstanceUID = ["1.2.3.4", "1.2.3.5"]
    nested.ReferencedSOPClassUID = "1.2.3.4"
    dataset.ReferencedImageSequence = [nested]
    apply_policy(
        dataset,
        load_policy("standard"),
        subject_label="sub-0001",
        scan_date="20260101",
        replacement_cache={},
    )
    assert nested.ReferencedSOPInstanceUID[0] == dataset.SOPInstanceUID
    assert nested.ReferencedSOPInstanceUID[1] != "1.2.3.5"
    assert nested.ReferencedSOPClassUID == "1.2.3.4"


def test_apply_policy_full_also_removes_descriptive_fields() -> None:
    dataset = _dataset(
        PatientName="Doe^Jane^19900101",
        PatientID="12345",
        ProtocolName="bto_CS4.8_MP2RAGE_0.8mm_iso",
        SeriesDescription="bto_CS4.8_MP2RAGE_0.8mm_iso_INV2",
    )

    apply_policy(
        dataset,
        load_policy("full"),
        subject_label="sub-0005",
        scan_date="20260914",
        replacement_cache={},
    )

    assert dataset.ProtocolName == "DeIdentified"
    assert not hasattr(dataset, "SeriesDescription")


def test_apply_policy_scramble_date_shifts_year_by_exactly_one() -> None:
    policy = Policy(name="test", actions={"PatientBirthDate": {"action": "scramble_date"}})
    dataset = _dataset(PatientBirthDate="19900615")

    apply_policy(
        dataset, policy, subject_label="sub-0005", scan_date="20260914", replacement_cache={}
    )

    scrambled = dataset.PatientBirthDate
    assert scrambled != "19900615"
    assert len(scrambled) == 8
    assert int(scrambled[:4]) in (1989, 1991)  # +/- 1 year, never the real year


def test_apply_policy_scramble_date_is_consistent_across_files() -> None:
    cache: dict[str, str] = {}
    first = _dataset(PatientBirthDate="19900615")
    second = _dataset(PatientBirthDate="19900615")
    policy = Policy(name="test", actions={"PatientBirthDate": {"action": "scramble_date"}})

    apply_policy(
        first, policy, subject_label="sub-0005", scan_date="20260914", replacement_cache=cache
    )
    apply_policy(
        second, policy, subject_label="sub-0005", scan_date="20260914", replacement_cache=cache
    )

    assert first.PatientBirthDate == second.PatientBirthDate


def test_apply_policy_random_name_replaces_with_a_generated_name() -> None:
    policy = Policy(name="test", actions={"PatientName": {"action": "random_name"}})
    dataset = _dataset(PatientName="Doe^Jane^19900101")

    apply_policy(
        dataset, policy, subject_label="sub-0005", scan_date="20260914", replacement_cache={}
    )

    assert dataset.PatientName != "Doe^Jane^19900101"
    assert dataset.PatientName  # non-empty


def test_apply_policy_random_name_is_formatted_last_caret_first_and_title_cased() -> None:
    policy = Policy(name="test", actions={"PatientName": {"action": "random_name"}})
    dataset = _dataset(PatientName="Doe^Jane^19900101")

    apply_policy(
        dataset, policy, subject_label="sub-0005", scan_date="20260914", replacement_cache={}
    )

    last, caret, first = str(dataset.PatientName).partition("^")
    assert caret == "^"
    assert last and last[0].isupper()
    assert first and first[0].isupper() and first[1:].islower()


def test_random_name_title_cases_mc_surnames_correctly() -> None:
    from dichotomise.pydcm.relabel import _format_name_part

    assert _format_name_part("mclean") == "McLean"
    assert _format_name_part("mccarthy") == "McCarthy"
    assert _format_name_part("gracious") == "Gracious"


def test_apply_policy_random_name_is_consistent_across_files() -> None:
    cache: dict[str, str] = {}
    first = _dataset(PatientName="Doe^Jane^19900101")
    second = _dataset(PatientName="Doe^Jane^19900101")
    policy = Policy(name="test", actions={"PatientName": {"action": "random_name"}})

    apply_policy(
        first, policy, subject_label="sub-0005", scan_date="20260914", replacement_cache=cache
    )
    apply_policy(
        second, policy, subject_label="sub-0005", scan_date="20260914", replacement_cache=cache
    )

    assert first.PatientName == second.PatientName


def test_load_policy_minimal_keeps_standards_removals_and_uses_the_subject_pseudonym() -> None:
    policy = load_policy("minimal")

    assert policy.actions["InstitutionName"]["action"] == "remove"  # inherited from standard
    assert policy.actions["PatientName"]["action"] == "replace"
    assert policy.actions["PatientName"]["value"] == "<subject-name>"
    assert policy.actions["PatientBirthDate"]["action"] == "replace"
    assert policy.actions["PatientBirthDate"]["value"] == "<scan-date>"


def test_apply_policy_minimal_gives_a_random_name_and_scandate_birthdate() -> None:
    dataset = _dataset(
        PatientName="Doe^Jane^19900101",
        PatientID="12345",
        PatientBirthDate="19900101",
    )

    apply_policy(
        dataset,
        load_policy("minimal"),
        subject_label="sub-0005",
        scan_date="20260914",
        replacement_cache={},
    )

    assert dataset.PatientName not in ("Doe^Jane^19900101", "sub-0005")
    assert dataset.PatientID == "sub-0005"
    assert dataset.PatientBirthDate == "20260914"


def _item(**fields: object) -> Dataset:
    item = Dataset()
    for key, value in fields.items():
        setattr(item, key, value)
    return item


def test_apply_policy_reaches_fields_at_every_depth_of_nested_sequences() -> None:
    dataset = Dataset()
    dataset.PatientID = "real-id"
    dataset.OtherPatientIDsSequence = [_item(PatientID="real-id-too")]
    dataset.ReferencedStudySequence = [
        _item(
            ReferencedSOPInstanceUID="1.2.3",
            ReferencedSeriesSequence=[_item(ReferencedSOPInstanceUID="1.2.3")],
        )
    ]
    policy = Policy(
        name="test",
        actions={
            "PatientID": {"action": "replace", "value": "sub-0001"},
            "ReferencedSOPInstanceUID": {"action": "regenerate"},
        },
    )

    apply_policy(dataset, policy, subject_label="sub-0001", scan_date="", replacement_cache={})

    outer = dataset.ReferencedStudySequence[0]
    inner = outer.ReferencedSeriesSequence[0]
    assert dataset.PatientID == "sub-0001"
    assert dataset.OtherPatientIDsSequence[0].PatientID == "sub-0001"
    assert outer.ReferencedSOPInstanceUID != "1.2.3"
    assert inner.ReferencedSOPInstanceUID == outer.ReferencedSOPInstanceUID


def test_apply_policy_removes_a_sequence_that_nests_itself_and_the_fields_inside_it() -> None:
    dataset = Dataset()
    dataset.ContentSequence = [
        _item(TextValue="secret", ContentSequence=[_item(TextValue="more secret")])
    ]
    dataset.ReferencedStudySequence = [_item(ContentSequence=[_item(TextValue="nested secret")])]
    policy = Policy(
        name="test",
        actions={
            "ContentSequence": {"action": "remove"},
            "TextValue": {"action": "remove"},
        },
    )

    apply_policy(dataset, policy, subject_label="sub-0001", scan_date="", replacement_cache={})

    assert "ContentSequence" not in dataset
    assert "ContentSequence" not in dataset.ReferencedStudySequence[0]
    assert "secret" not in repr(dataset)


def _referencing_pair() -> tuple[Dataset, Dataset]:
    """A presentation-state-like file that refers to an image by its SOP Instance UID."""
    image = Dataset()
    image.PatientID = "real-id"
    image.SOPInstanceUID = "1.2.3.2"
    image.SOPClassUID = "1.2.840.10008.5.1.4.1.1.4"
    state = Dataset()
    state.PatientID = "real-id"
    state.SOPInstanceUID = "1.2.3.1"
    state.SOPClassUID = "1.2.840.10008.5.1.4.1.1.11.1"
    state.DimensionOrganizationUID = "1.3.12.2.1107.5.2.9"
    state.ReferencedImageSequence = [
        _item(
            ReferencedSOPClassUID="1.2.840.10008.5.1.4.1.1.4",
            ReferencedSOPInstanceUID="1.2.3.2",
        )
    ]
    state.ContentSequence = [_item(CodingSchemeUID="1.3.12.2.1107.5.2")]
    return state, image


def _sanitise_both(policy_name: str) -> tuple[Dataset, Dataset]:
    state, image = _referencing_pair()
    policy = load_policy(policy_name)
    cache: dict[str, str] = {}
    for dataset in (state, image):
        apply_policy(
            dataset, policy, subject_label="sub-0001", scan_date="20260101", replacement_cache=cache
        )
    return state, image


@pytest.mark.parametrize("policy_name", ["standard", "minimal", "full"])
def test_apply_policy_remaps_referenced_uids_to_the_new_instance_uids(policy_name: str) -> None:
    state, image = _sanitise_both(policy_name)

    reference = state.ReferencedImageSequence[0].ReferencedSOPInstanceUID
    assert image.SOPInstanceUID != "1.2.3.2"
    assert reference == image.SOPInstanceUID
    assert state.DimensionOrganizationUID != "1.3.12.2.1107.5.2.9"


def test_apply_policy_leaves_class_scheme_and_registry_uids_alone() -> None:
    state, image = _sanitise_both("standard")

    assert state.SOPClassUID == "1.2.840.10008.5.1.4.1.1.11.1"
    assert image.SOPClassUID == "1.2.840.10008.5.1.4.1.1.4"
    assert state.ReferencedImageSequence[0].ReferencedSOPClassUID == "1.2.840.10008.5.1.4.1.1.4"
    assert state.ContentSequence[0].CodingSchemeUID == "1.3.12.2.1107.5.2"


def test_apply_policy_retain_keeps_every_original_uid() -> None:
    state, image = _sanitise_both("retain")

    assert state.ReferencedImageSequence[0].ReferencedSOPInstanceUID == "1.2.3.2"
    assert state.DimensionOrganizationUID == "1.3.12.2.1107.5.2.9"
    assert image.SOPInstanceUID == "1.2.3.2"


def test_apply_policy_respects_an_explicit_keep_for_a_uid_field() -> None:
    state, _ = _referencing_pair()
    policy = Policy(
        name="test",
        actions={
            "SOPInstanceUID": {"action": "regenerate"},
            "DimensionOrganizationUID": {"action": "keep"},
        },
    )

    apply_policy(state, policy, subject_label="s", scan_date="", replacement_cache={})

    assert state.SOPInstanceUID != "1.2.3.1"
    assert state.DimensionOrganizationUID == "1.3.12.2.1107.5.2.9"
