# Sanitisation policies

**TL;DR:** Policies control which DICOM fields are kept, removed or replaced.
The processed output is only as private as the selected policy makes it.

This records the comparison behind `src/dichotomise/pydcm/policies/*.json` and
the policy file schema. See [usage and output](usage.md#output-structure) for
the output-folder structure these policies feed into.

## Where this came from

**TL;DR:** The policies were informed by comparing three scanner exports of
the same study; that comparison does not establish confidentiality compliance.

`tests/data/` contains the same study exported from the scanner three ways:
`default_export` (nothing removed), `service_export` (the scanner's
minimal-anonymisation export, the preferred method), and `reduced_export`
(the scanner's more aggressive export, not the preferred method).

Comparing the same image across all three (35 of 114 tags differ) shows a
consistent, graduated pattern:

- **`service`** removes *who* the subject is and *who/where* scanned them —
  patient name, patient ID, birth date, institution name/address/department,
  station name, operator, performing physician, and reissues every instance UID
  (study, series, SOP instance, frame of reference, referenced instances and
  the like) so nothing traces back to the source. It keeps every field describing *what* was scanned: protocol name,
  series/study description, image comments, procedure IDs, device serial
  number.
- **`reduced`** does everything `service` does, **plus** removes the
  descriptive fields too: protocol name becomes the literal string
  `"DeIdentified"`, series/study description, image comments, and procedure
  IDs are all removed, and the device serial number goes too. It also
  stamps a formal DICOM PS3.15 confidentiality-profile declaration
  (`DeidentificationMethod`, `DeidentificationMethodCodeSequence`) rather
  than a plain note.

This is why `reduced` is the non-preferred method: the result is untraceable,
but a clinician can no longer tell which folder holds which scan without
opening the images. `service`'s narrower removal is preferred because it
achieves the same untraceability while keeping the archive usable.

One quirk worth recording, not modelled in the policy files: `service`'s
`DerivationDescription` field reads `"Forced 'Reduced' Anonymity - Service"`
— an internal scanner label that doesn't match the export's own name. This
looks like scanner firmware wording, not a meaningful field value, and is
deliberately not reproduced.

Also worth recording: `reduced_export`'s folder names do not line up with
the `SeriesNumber` inside the files (an off-by-one, most likely because the
scanner's reduced-export folder numbering counts exported series in order
rather than reading the series number back out of each file). Matching a
series across the three exports safely requires comparing header content
inside the files, never folder names alone.

## Policy file schema

**TL;DR:** Each policy maps DICOM field names to actions and can inherit from
another policy.

Each policy is a small JSON file: a `name`, a `description`, a
`default_action` (what happens to every field not explicitly listed, always
`"keep"` for now), and `actions`: a map from DICOM field name to what
happens to it.

An action is one of:

- `"keep"` — left exactly as scanned (the default; fields don't need to be
  listed for this).
- `"remove"` — deleted from the file entirely.
- `"replace"` — set to the fixed `value` given, or a placeholder such as
  `<subject-label>` (the run's replacement label) or `<scan-date>` (the
  study's own scan date, not a real birth date).
- `"regenerate"`: replaced with a newly generated identifier, applied
  consistently everywhere that identifier appears, including inside nested
  reference sequences. A policy that regenerates `SOPInstanceUID` also remaps
  every other instance UID in the file by value, except class, transfer-syntax
  and coding-scheme UIDs, registry UIDs (`1.2.840.10008.*`) and any field the
  policy lists with a different action.
- `"add"` — a field that isn't present by default gets added.
- `"scramble_date"` — replaced with a random *but valid* date, shifted by
  exactly one year (chosen randomly as +1 or -1, never 0) from the real
  date, with the day and month also randomised. Used for `PatientBirthDate`
  in `standard`/`full`: an approximate age survives (useful for research),
  the exact birth date does not.
- `"random_name"` — replaced with a randomly generated, readable placeholder
  name, formatted as standard DICOM PN (`Surname^Firstname`, e.g.
  `Abrahall^Gracious`), via `pydcm/names.py`'s own `generate_name()`
  (adjective + surname, unrelated to the real name). See "The name
  generator" below.

`"regenerate"`, `"scramble_date"`, and `"random_name"` are all **consistent
within one subject's run**: the same original value (the same person's real
UID, birth date, or name) always produces the same replacement, however many
files it appears in — a person only has one birth date and one name, and two
files disagreeing on either would look like data corruption. This is done
with a cache the caller keeps for the whole run (`replacement_cache` in
`apply_policy()`), not by anything in the policy file itself.

The replacement cache is shared by retained and review files in one study.
Known nested references to reissued identifiers use the same replacements;
references between separately processed studies are not remapped together.
Series folder names follow the effective `SeriesDescription` action, so a
removed description is represented by `NA`. Source archives and provenance
reports still contain source information and must be handled accordingly.

`full.json` uses `"extends": "standard"` rather than repeating every
`standard` entry: a full-policy run applies every `standard` action first,
then applies `full`'s own list on top. `extends` can chain further —
`custom.json` extends `full`, so it inherits `full`'s actions, which
already include everything from `standard`.

JSON has no comment syntax, so a policy file may carry an `instructions`
field: a plain array of strings, ignored by the loader (`load_policy()`
only reads `name`, `description`, `extends`, and `actions`), there purely
for a person opening the file in a text editor. `custom.json` uses this to
explain, in place, how to build a new policy.

## The name generator

**TL;DR:** The built-in generator combines an adjective and a surname to make
a readable placeholder.

`pydcm/names.py`'s `generate_name()` produces the `random_name` action's
placeholder: one adjective plus one surname, joined by `_` (e.g.
`gracious_abrahall`), which `pydcm/relabel.py` then reformats into DICOM PN
form (`Abrahall^Gracious`). This is entirely our own code and our own word
lists — not a third-party package.

**100 adjectives × 1000 surnames = 100,000 possible unique names.**

The surnames are a hand-gathered sample of real British surnames from the
Guild of One-Name Studies' public index,
<https://one-name.org/surnames_A-Z/> — one page per letter of the alphabet
(the Guild's own "A" page alone lists over 250; only a sample was taken per
letter, for a manageable, still-large list). Multi-word and hyphenated
entries (e.g. "Ab Adam", "Audley-Charles") were excluded, to match the
plain single-word style the rest of the scheme uses. Some letters (Q, U, X,
Z) end up with far fewer surnames than others — that reflects real registry
sparsity, not a sampling mistake, and was kept rather than padded out with
invented names.

## Five policies

**TL;DR:** `minimal` is the default; the other policies retain, remove or
replace different fields.

| Policy | Matches | What happens |
|---|---|---|
| `default.json` | scanner's `default_export` | nothing changed |
| `standard.json` | scanner's `service_export` | identity + institution/device-operator removed, instance UIDs reissued, birth date scrambled by ±1 year (day/month randomised too), demographic fields (e.g. sex) and descriptive text kept |
| `minimal.json` | (worked example, not a scanner export) | extends `standard`, but the replacement name is a random placeholder in `Surname^Firstname` form (e.g. `McLean^Gracious`) instead of the plain subject label, and the birth date is simply the scan date rather than scrambled — a lighter-weight alternative when a distinctive placeholder name matters more than keeping an approximate age |
| `full.json` | scanner's `reduced_export` | everything `standard` does, plus descriptive text and device serial number removed |
| `custom.json` | (worked example, not a scanner export) | a template for a non-expert to copy: extends `full` but keeps protocol/series descriptions, and additionally removes accession number and referring/requesting physician — fields none of the other four touch |

To make your own: copy `custom.json` to `<your-policy-name>.json` in
`src/dichotomise/pydcm/policies/`, change its `name` field to match, edit
`actions`, and run with `--sanitise-policy <your-policy-name>`. Any file
saved there under that naming pattern is loadable this way — `cli.py` does
not hardcode a list of allowed levels, and an unrecognised name produces a
plain error naming what's available rather than a Python traceback.

## Status

**TL;DR:** The pipeline loads and applies these policy files.

`pydcm/relabel.py`'s `load_policy()`/`apply_policy()` and
`stages/sanitise.py` read these files; they are not just documentation.
`reports/`-style auditing of *which* policy was used on a given run is not
yet built.
