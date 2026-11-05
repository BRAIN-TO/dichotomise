"""Replacement-label generation and the sanitisation-policy engine."""

from __future__ import annotations

import calendar
import json
import random
import re
from collections import defaultdict
from collections.abc import Callable, Iterator, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pydicom
from pydicom.datadict import dictionary_VR, keyword_for_tag, tag_for_keyword
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence
from pydicom.uid import generate_uid

from dichotomise.errors import PolicyNotFoundError, PolicyValidationError, RelabelError
from dichotomise.pydcm.names import generate_name
from dichotomise.pydcm.read import _pixel_fingerprint

_POLICIES_DIR = Path(__file__).parent / "policies"

_LABEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_MAX_LABEL_LENGTH = 64


def normalise_numeric_label(value: str) -> str:
    """Turn a plain number (with or without a "sub-" prefix) into "sub-NNNN"."""
    suffix = value.removeprefix("sub-")
    if not re.fullmatch(r"\d+", suffix):
        raise RelabelError("A numerical replacement ID must be a number from 1 to 9999")
    number = int(suffix)
    if not 1 <= number <= 9999:
        raise RelabelError("A numerical replacement ID must be a number from 1 to 9999")
    return f"sub-{number:04d}"


def validate_label(label: str) -> str:
    """Return `label` unchanged if it is safe to use in a filename and DICOM field."""
    if _LABEL_PATTERN.fullmatch(label) is None:
        raise RelabelError(
            "A replacement ID may only contain letters, digits, underscores, and hyphens"
        )
    if len(label) > _MAX_LABEL_LENGTH:
        raise RelabelError(f"A replacement ID must be {_MAX_LABEL_LENGTH} characters or fewer")
    return label


@dataclass(frozen=True)
class Policy:
    """A named sanitisation policy: what happens to each DICOM field."""

    name: str
    actions: dict[str, dict[str, Any]]
    description: str = ""

    def keeps(self, field: str) -> bool:
        """Whether the policy leaves `field` as it is (a field with no action is kept)."""
        return bool(self.actions.get(field, {}).get("action", "keep") == "keep")


_ACTIONS_REQUIRING_VALUE = {"replace", "add", "add_code_sequence"}
_VALID_ACTIONS = {
    "keep",
    "remove",
    "replace",
    "add",
    "add_code_sequence",
    "regenerate",
    "scramble_date",
    "random_name",
}


def _validate_actions(policy_name: str, actions: object) -> dict[str, dict[str, Any]]:
    """Validate policy actions before they are applied to a DICOM dataset."""
    if not isinstance(actions, dict):
        raise PolicyValidationError(f"Policy {policy_name!r} needs an actions object.")
    validated: dict[str, dict[str, Any]] = {}
    for field, instruction in actions.items():
        if not isinstance(field, str) or not isinstance(instruction, dict):
            raise PolicyValidationError(
                f"Policy {policy_name!r} has an invalid action instruction."
            )
        action = instruction.get("action")
        if not isinstance(action, str) or action not in _VALID_ACTIONS:
            raise PolicyValidationError(
                f"Policy {policy_name!r} has an unsupported action for {field}: {action!r}."
            )
        if action in _ACTIONS_REQUIRING_VALUE and "value" not in instruction:
            raise PolicyValidationError(
                f"Policy {policy_name!r} needs a value for {field}'s {action} action."
            )
        if action == "add_code_sequence" and not isinstance(instruction["value"], list):
            raise PolicyValidationError(
                f"Policy {policy_name!r} needs a list of codes for {field}."
            )
        validated[field] = instruction
    return validated


def load_policy(name: str) -> Policy:
    """Load one of the bundled policies ("retain", "standard", or "full").

    "full" is written as "standard" plus a few extra fields (its JSON file's
    "extends" key), so its actions here already include everything
    "standard" does, with "full"'s own entries taking priority where the two
    disagree.
    """
    name = name.removesuffix(".json")
    if name == "default":
        return Policy(name="default", actions={})
    return _load_policy(name, ancestors=frozenset())


def _load_policy(name: str, *, ancestors: frozenset[str]) -> Policy:
    """Load and validate one policy file, resolving `extends` and refusing circular inheritance."""
    if name in ancestors:
        chain = " -> ".join((*sorted(ancestors), name))
        raise PolicyValidationError(f"Policy inheritance is circular: {chain}.")
    policy_file = _POLICIES_DIR / f"{name}.json"
    if not policy_file.is_file():
        available = ", ".join(sorted(p.stem for p in _POLICIES_DIR.glob("*.json")))
        raise PolicyNotFoundError(
            f"No sanitisation policy named '{name}'. Available policies: {available}"
        )
    try:
        raw = json.loads(policy_file.read_text())
    except json.JSONDecodeError as error:
        raise PolicyValidationError(f"Policy {name!r} is not valid JSON.") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
        raise PolicyValidationError(f"Policy {name!r} needs a string name.")
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise PolicyValidationError(f"Policy {name!r} needs a string description.")
    actions: dict[str, dict[str, Any]] = {}
    base_name = raw.get("extends")
    if base_name is not None:
        if not isinstance(base_name, str):
            raise PolicyValidationError(f"Policy {name!r} has an invalid extends value.")
        actions.update(_load_policy(base_name, ancestors=ancestors | {name}).actions)
    actions.update(_validate_actions(name, raw.get("actions", {})))
    return Policy(name=raw["name"], actions=actions, description=description)


def _remove_field(dataset: Dataset, field: str) -> None:
    """Delete a field if the dataset has it."""
    if hasattr(dataset, field):
        delattr(dataset, field)


def _iter_nested_datasets(dataset: Dataset) -> Iterator[Dataset]:
    """Yield a dataset and every dataset contained in its sequences."""
    yield dataset
    for tag in list(dataset.keys()):
        # Read the VR from the raw element where there is one, so elements that are not
        # sequences are never converted.
        raw = dataset.get_item(tag)
        if (raw.VR or dataset[tag].VR) != "SQ":
            continue
        value = dataset[tag].value
        if not isinstance(value, Sequence):
            continue
        for item in value:
            yield from _iter_nested_datasets(item)


def _iter_policy_datasets(dataset: Dataset) -> Iterator[Dataset]:
    """Yield the main dataset, nested datasets, and DICOM file metadata."""
    yield from _iter_nested_datasets(dataset)
    file_meta = getattr(dataset, "file_meta", None)
    if file_meta is not None:
        yield from _iter_nested_datasets(file_meta)


def _index_by_tag(dataset: Dataset) -> dict[int, list[Dataset]]:
    """Map each tag to the datasets that hold it, in the order `_iter_policy_datasets` visits."""
    index: dict[int, list[Dataset]] = defaultdict(list)
    for current in _iter_policy_datasets(dataset):
        # .keys(), not iteration: iterating a Dataset converts every element.
        for tag in current.keys():  # noqa: SIM118
            index[tag].append(current)
    return index


def _is_sequence_tag(tag: int | None) -> bool:
    """True when the DICOM dictionary says this tag is a sequence (VR `SQ`)."""
    if tag is None:
        return False
    try:
        return bool(dictionary_VR(tag) == "SQ")
    except KeyError:
        return False


def _replace_field(
    dataset: Dataset, field: str, instruction: dict[str, Any], placeholders: dict[str, str]
) -> None:
    """Set a field to the policy's value, substituting placeholders such as `<subject-label>`."""
    if "value" not in instruction:
        return  # nothing to set yet; see docs/sanitise-policies.md
    value = instruction["value"]
    if isinstance(value, str) and value in placeholders:
        value = placeholders[value]
    setattr(dataset, field, value)


def _add_code_sequence(dataset: Dataset, field: str, instruction: dict[str, Any]) -> None:
    """Add a DICOM coded sequence from the policy's serialisable code records."""
    records = instruction.get("value")
    if not isinstance(records, list):
        raise RelabelError(f"{field} needs a list of coded values")
    items = []
    for record in records:
        if not isinstance(record, dict):
            raise RelabelError(f"{field} contains a code that is not an object")
        item = Dataset()
        for attribute, value in record.items():
            setattr(item, attribute, value)
        items.append(item)
    setattr(dataset, field, Sequence(items))


def _cached_value(
    dataset: Dataset,
    field: str,
    replacement_cache: MutableMapping[str, str],
    generate: Callable[[str], str],
) -> str | None:
    """Return a new value for `field`, generated once and reused for every later call.

    `replacement_cache` is shared across every file in a subject's run, keyed
    by field name plus the field's original value, so the same original
    value (the same person's real UID, birth date, or name) always maps to
    the same replacement, however many files it appears in.
    """
    original = str(getattr(dataset, field, "")).strip()
    if not original:
        return None
    key = f"{field}:{original}"
    if key not in replacement_cache:
        replacement_cache[key] = generate(original)
    return replacement_cache[key]


# UIDs that name a class, a transfer syntax or a coding scheme rather than one instance. They
# are shared by every site, so they stay as they are (DICOM PS3.15 Annex E remaps instance
# and collection UIDs, not these). Registry UIDs (1.2.840.10008.*) stay too.
_KEPT_UID_KEYWORDS = frozenset(
    {
        "SOPClassUID",
        "ReferencedSOPClassUID",
        "ReferencedSOPClassUIDInFile",
        "MediaStorageSOPClassUID",
        "RelatedGeneralSOPClassUID",
        "OriginalSpecializedSOPClassUID",
        "TransferSyntaxUID",
        "ReferencedTransferSyntaxUIDInFile",
        "ImplementationClassUID",
        "CodingSchemeUID",
        "MappingResourceUID",
        "ContextGroupExtensionCreatorUID",
        "TemplateExtensionCreatorUID",
        "TemplateExtensionOrganizationUID",
    }
)
_REGISTRY_UID_PREFIX = "1.2.840.10008."
_NEW_UID_MARK = "uid-new:"


def _new_uid_for(original: str, replacement_cache: MutableMapping[str, str]) -> str:
    """The replacement for one UID value, the same wherever and under whatever keyword it appears.

    Keyed by the value, not the field, so an image's SOPInstanceUID and the
    ReferencedSOPInstanceUID that points at it come out equal.
    """
    key = f"uid:{original}"
    if key not in replacement_cache:
        replacement_cache[key] = str(generate_uid())
        replacement_cache[_NEW_UID_MARK + replacement_cache[key]] = original
    return replacement_cache[key]


def _regenerate_field(
    dataset: Dataset, field: str, replacement_cache: MutableMapping[str, str]
) -> None:
    """Replace a UID with its stable new value, so repeats and references stay in step."""
    original = str(getattr(dataset, field, "")).strip()
    if original:
        setattr(dataset, field, _new_uid_for(original, replacement_cache))


def _remap_remaining_uids(
    index: dict[int, list[Dataset]],
    explicit_fields: set[str],
    replacement_cache: MutableMapping[str, str],
    *,
    follow_only: bool = False,
) -> None:
    """Replace every UID value the policy's own actions did not handle.

    Skipped: class and scheme UIDs, registry UIDs, fields the policy names
    itself (it chose keep, remove or replace for those), and values that are
    already replacements. With `follow_only`, a value is replaced only if this
    run has already regenerated it, so a partial policy never reissues a UID
    its author chose to keep.
    """
    for tag, holders in index.items():
        keyword = keyword_for_tag(tag)
        if keyword in _KEPT_UID_KEYWORDS or keyword in explicit_fields:
            continue
        try:
            vr = dictionary_VR(tag)
        except KeyError:  # a private element: use the VR the file states
            vr = holders[0].get_item(tag).VR
        if vr != "UI":
            continue
        for holder in holders:
            if tag not in holder:
                continue
            element = holder[tag]
            many = element.VM > 1
            values = (
                [str(value).strip() for value in element.value]
                if many
                else [str(element.value).strip()]
            )
            remapped = [
                value
                if not value
                or value.startswith(_REGISTRY_UID_PREFIX)
                or (_NEW_UID_MARK + value) in replacement_cache
                else replacement_cache.get(f"uid:{value}", value)
                if follow_only
                else _new_uid_for(value, replacement_cache)
                for value in values
            ]
            if remapped != values:
                element.value = remapped if many else remapped[0]


def _scramble_date(original: str, field: str) -> str:
    """A random valid date shifted by exactly one year from `original` (never 0)."""
    try:
        year = int(original[:4])
    except (ValueError, IndexError) as error:
        raise RelabelError(f"{field} is not a valid date to scramble: {original!r}") from error
    new_year = year + random.choice((-1, 1))
    new_month = random.randint(1, 12)
    new_day = random.randint(1, calendar.monthrange(new_year, new_month)[1])
    return f"{new_year:04d}{new_month:02d}{new_day:02d}"


def _scramble_date_field(
    dataset: Dataset, field: str, replacement_cache: MutableMapping[str, str]
) -> None:
    """Replace a date with a scrambled one, the same scrambled date for the same original."""
    new_value = _cached_value(
        dataset, field, replacement_cache, lambda original: _scramble_date(original, field)
    )
    if new_value is not None:
        setattr(dataset, field, new_value)


def _format_name_part(word: str) -> str:
    """Title-case one name-generator word, e.g. "gracious" -> "Gracious".

    A "Mc"-prefixed surname (none currently in pydcm/names.py's list, but
    kept in case one is added later) needs that prefix capitalised specially
    rather than a plain str.capitalize(), e.g. "mclean" -> "McLean" rather
    than "Mclean".
    """
    if word.startswith("mc") and len(word) > 2:
        return "Mc" + word[2:].capitalize()
    return word.capitalize()


def format_subject_name(label: str) -> str:
    """Format a generated ``surname_given`` label as a DICOM person name."""
    surname, separator, given_name = label.partition("_")
    if not separator:
        return _format_name_part(surname)
    return f"{_format_name_part(surname)}^{_format_name_part(given_name)}"


def _generate_person_name() -> str:
    """A random "Surname^Firstname"-style placeholder, in DICOM PN form."""
    given, _, family = generate_name().rpartition("_")
    return f"{_format_name_part(family)}^{_format_name_part(given)}"


def _random_name_field(
    dataset: Dataset, field: str, replacement_cache: MutableMapping[str, str]
) -> None:
    """Replace a name with a generated one, the same name for the same original."""
    new_value = _cached_value(
        dataset, field, replacement_cache, lambda _original: _generate_person_name()
    )
    if new_value is not None:
        setattr(dataset, field, new_value)


def _apply_action(
    dataset: Dataset,
    field: str,
    instruction: dict[str, Any],
    placeholders: dict[str, str],
    replacement_cache: MutableMapping[str, str],
) -> None:
    """Carry out one policy action (keep, remove, replace, regenerate, ...) on a field."""
    action = instruction["action"]
    if action == "keep":
        return
    if action == "remove":
        _remove_field(dataset, field)
        return
    if action in ("replace", "add"):
        _replace_field(dataset, field, instruction, placeholders)
        return
    if action == "add_code_sequence":
        _add_code_sequence(dataset, field, instruction)
        return
    if action == "regenerate":
        _regenerate_field(dataset, field, replacement_cache)
        return
    if action == "scramble_date":
        _scramble_date_field(dataset, field, replacement_cache)
        return
    if action == "random_name":
        _random_name_field(dataset, field, replacement_cache)
        return
    raise RelabelError(f"Unknown sanitisation action for {field}: {action}")


def _sync_file_meta_sop_instance_uid(dataset: Dataset) -> None:
    """Keep the file-meta SOP instance UID aligned with the main dataset."""
    file_meta = getattr(dataset, "file_meta", None)
    if file_meta is None or not hasattr(dataset, "SOPInstanceUID"):
        return
    if hasattr(file_meta, "MediaStorageSOPInstanceUID"):
        file_meta.MediaStorageSOPInstanceUID = dataset.SOPInstanceUID


def apply_policy(
    dataset: Dataset,
    policy: Policy,
    *,
    subject_label: str,
    scan_date: str,
    replacement_cache: MutableMapping[str, str],
) -> None:
    """Apply `policy` to `dataset` in place.

    `replacement_cache` is kept and reused by the caller across every file in
    a run, so a regenerated UID, scrambled date, or random name that appears
    in more than one file comes out the same everywhere, rather than a
    different one per file.
    """
    placeholders = {
        "<subject-label>": subject_label,
        "<subject-name>": format_subject_name(subject_label),
        "<scan-date>": scan_date,
    }
    # Walk the dataset once and look each field up by tag, rather than walking every
    # nested dataset again for every field in the policy. The top-level dataset is
    # always visited; nested ones only if they hold the field. The index is rebuilt
    # when an action changes which datasets exist.
    index = _index_by_tag(dataset)
    for field, instruction in policy.actions.items():
        action = instruction["action"]
        tag = tag_for_keyword(field)
        targets = [dataset]
        if action not in ("add", "add_code_sequence") and tag is not None:
            targets += [
                current
                for current in index.get(tag, ())
                if current is not dataset and tag in current
            ]
        for current_dataset in targets:
            _apply_action(
                current_dataset,
                field,
                instruction,
                placeholders,
                replacement_cache,
            )
        if action in ("add", "add_code_sequence") or _is_sequence_tag(tag):
            index = _index_by_tag(dataset)
    # A policy that reissues SOPInstanceUID must also reissue every UID that refers to it,
    # or references would point at files that no longer exist. Fields the policy names
    # itself keep their own action. A policy that reissues only some UIDs (say the study
    # and frame of reference) just has its references follow those: a reference to a UID
    # it keeps stays as it is.
    # shortcut: a reference to a UID that a later file regenerates keeps its original value,
    # upgrade if partial policies with cross-series references appear
    if any(found["action"] == "regenerate" for found in policy.actions.values()):
        explicit = {
            field for field, found in policy.actions.items() if found["action"] != "regenerate"
        }
        reissues_sop = policy.actions.get("SOPInstanceUID", {}).get("action") == "regenerate"
        _remap_remaining_uids(index, explicit, replacement_cache, follow_only=not reissues_sop)
    _sync_file_meta_sop_instance_uid(dataset)


def sanitise_file(
    source_path: Path,
    target_path: Path,
    policy: Policy,
    *,
    subject_label: str,
    scan_date: str,
    replacement_cache: MutableMapping[str, str],
) -> str:
    """Read, sanitise, save, and re-open one DICOM file.

    The caller owns path selection and the shared replacement cache; this
    function owns all DICOM-specific file handling. Returns the saved file's
    SOP Instance UID, which the policy may have regenerated.
    """
    uid, _, _ = _sanitise_file(
        source_path,
        target_path,
        policy,
        subject_label=subject_label,
        scan_date=scan_date,
        replacement_cache=replacement_cache,
        verify=True,
    )
    return uid


def _sanitise_file(
    source_path: Path,
    target_path: Path,
    policy: Policy,
    *,
    subject_label: str,
    scan_date: str,
    replacement_cache: MutableMapping[str, str],
    verify: bool,
) -> tuple[str, tuple[object, ...], str]:
    """Sanitise one file and save it, optionally re-reading the saved file to check it.

    Returns the new SOP Instance UID, the pixel fingerprint taken before sanitising, and the
    PatientID written. With `verify`, the saved file must match: pixels, PatientID and SOP UID.
    """
    dataset = pydicom.dcmread(source_path)
    fingerprint = _pixel_fingerprint(dataset)
    apply_policy(
        dataset,
        policy,
        subject_label=subject_label,
        scan_date=scan_date,
        replacement_cache=replacement_cache,
    )
    dataset.save_as(target_path)
    if verify:
        saved = pydicom.dcmread(target_path)
        if _pixel_fingerprint(saved) != fingerprint:
            raise RelabelError(f"Pixel data differs after sanitisation: {target_path}")
        for field in ("PatientID", "SOPInstanceUID"):
            if str(saved.get(field, "")) != str(dataset.get(field, "")):
                raise RelabelError(f"{field} differs after sanitisation: {target_path}")
        saved_meta = getattr(saved, "file_meta", None)
        expected_meta = getattr(dataset, "file_meta", None)
        if (
            saved_meta is not None
            and expected_meta is not None
            and str(saved_meta.get("MediaStorageSOPInstanceUID", ""))
            != str(expected_meta.get("MediaStorageSOPInstanceUID", ""))
        ):
            raise RelabelError(f"File metadata differs after sanitisation: {target_path}")
    return str(dataset.get("SOPInstanceUID", "")), fingerprint, str(dataset.get("PatientID", ""))
