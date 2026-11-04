# dichotomise [![Version](https://img.shields.io/badge/version-2.8.0-purple.svg)](https://github.com/srikash/dichotomise/releases/tag/v2.8.0) [![License](https://img.shields.io/badge/license-MIT-orange.svg)](LICENSE) [![CI](https://github.com/srikash/dichotomise/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/srikash/dichotomise/actions/workflows/ci.yml) [![DOI](https://zenodo.org/badge/1397808016.svg)](https://doi.org/10.5281/zenodo.23166271)

<b><ins>DIC</ins></b>h<b><ins>O</ins></b>to<b><ins>M</ins></b>ise is a command-line tool for checking, sorting, renaming,
de-identifying, and archiving DICOM exports from Siemens XA60+ systems.

## Table of contents

- [The default export](#the-default-export)
- [Why repeated filenames are positively dreadful](#why-repeated-filenames-are-positively-dreadful)
  - [The new export format is the antithesis of good data-handling practice](#the-new-export-format-is-the-antithesis-of-good-data-handling-practice)
  - [Two example incidents (from amongst several)](#two-example-incidents-from-amongst-several)
- [What it does](#what-it-does)
- [Installation](#installation)
  - [venv](#venv)
  - [conda / mamba](#conda--mamba)
  - [From a source checkout](#from-a-source-checkout)
  - [uv](#uv)
  - [Container images](#container-images)
- [Usage](#usage)
- [Output structure](#output-structure)
  - [Reports](#reports)
- [Sanitisation](#sanitisation)
- [Layout](#layout)
- [Development](#development)
- [Citing dichotomise](#citing-dichotomise)
- [The dichotomise workflow](#the-dichotomise-workflow)

-------
### The default export

For a given study, the XA60+ export places DICOMs from each series into sub-folders named:
```text
<ProtocolName>_<SeriesNumber>_MR/
```

The DICOMs inside are named `1.dcm`, `2.dcm`, and so on. The numbering starts again in 
every series folder and recurs across subject exports. 

```text
subject-export/
  DWI_21_MR/
    1.dcm
    2.dcm
  fMRI_22_MR/
    1.dcm
    2.dcm
```
`1.dcm` in one series may be entirely unrelated to `1.dcm` in another. The
files can contain different acquisitions and have different file sizes.

## Why repeated filenames are positively dreadful

#### TL;DR

Naming files `1.dcm`, `2.dcm`, `3.dcm` is the worst possible choice. It is a
regression from something that previously worked well.

| Export | File naming |
|---|---|
| Older exports | Distinct `.IMA` filenames |
| XA30 exports | Distinct `.dcm` filenames |
| XA60+ exports | `1.dcm`, `2.dcm`, … repeated in every series folder |

Every DICOM carries a globally unique SOP Instance UID. The XA60 export retains 
that identifier in the header but does not use it to distinguish the exported
filename. The file's name identifies it only while its surrounding folder
structure remains intact. That is a data-integrity problem at the point of
export, before a researcher runs a pipeline or moves a file.

FAIR principle **F1** calls for globally unique, persistent identifiers for
data. The DICOM header supplies one; the exported filename hides it from
ordinary file operations. BIDS takes the opposite approach to filenames: its
applicable entities identify the data within the filename itself.

### The new export format is the antithesis of good data-handling practice

1. **Give each file a usable identity.** XA60+ assigns the same name to
   unrelated DICOMs. A file separated from its folder cannot be identified by
   name, although its SOP Instance UID remains in its header. This undermines
   findability at the file-system level and makes safe handling depend on
   reading DICOM metadata every time.

2. **Keep the file and its context in agreement.** The export uses a series
   folder to provide context that the filename lacks. If that folder
   contradicts the DICOM header, the name offers no independent clue.
   
   [Example Incident 1](#incident-1-dicom-placed-in-the-wrong-series) shows that this
   disagreement occurred within a single export.

3. **Make collisions visible before they cost data.** When exports are
   combined, flattened or restored into one directory, repeated names
   collide. Depending on the operation and its settings, a collision can
   overwrite a file or stop the transfer. Either way, the filename cannot
   distinguish the DICOMs involved. Safe reuse demands an explicit identity
   check, not trust in the exported names.

4. **Preserve the evidence needed to audit and reuse data.** Matching
   filenames do not establish that two DICOMs contain the same instance or
   the same scan content. Counters alone cannot verify completeness, detect a
   misplaced instance or establish where a file came from. FAIR **R1.2**
   calls for detailed provenance; losing a file's folder context makes that
   provenance harder to recover from the export, even though metadata remains
   in the DICOM.

   [Example Incident 2](#incident-2-duplicated-dwi-series) shows
   why a plausible filename sequence cannot serve as an audit.

### Two example incidents (from amongst several)

We noticed inconsistencies in scanner exports and inspected the DICOM metadata
manually. Both incidents below were found in the export as received, from the
DICOM headers, before any sorting or processing by dichotomise. They come from
the same Siemens MAGNETOM Terra.X (`syngo MR XA60`) and are examples of the
intermittent errors we have encountered, not an exhaustive list. These occur in product sequences
and C2Ps alike.

#### Incident 1: DICOM placed in the wrong series

`300.dcm` appeared in the folder for Series 21. Its header gives SeriesNumber 22,
a SeriesInstanceUID different from the other 300 files in the folder, and
InstanceNumber 5. It is the only file with SeriesNumber 22 in that export, so
Series 22 has no folder of its own. The export's folder contradicted the file's
metadata. The same fault was seen again elsewhere: a SeriesNumber 24 file in the
Series 23 folder, with the genuine Series 23 instance absent from the export.

#### Incident 2: Duplicated DWI series

We expected 81 DICOMs in each DWI series (the protocol has 81 diffusion
directions), but each export folder contained 162. Files `1.dcm`-`81.dcm` held
instances 1-81 and files `82.dcm`-`162.dcm` held instances 1-81 again. Each
duplicate pair was byte-identical and shared its SOPInstanceUID. The file
times show the two blocks were written about half an hour apart: the second
pass carried on numbering from the next free integer instead of recognising
instances already on disk. The same happened to a
300-measurement fMRI series (600 files for 300 expected). The filenames `1`-`162`
look like one valid sequence; only the file count or a header audit reveals the
problem.

A separate study had two copies containing 631 and 616 files. The larger
copy contained 15 byte-identical duplicates across seven series; each pair
shared a SOP Instance UID. It also contained a file whose header identified
Series 24 inside the Series 23 folder, where the genuine Series 23 instance
was missing. The files alone do not establish why the two copies differed.
Together, these findings show why a count check and a folder-name check are
not enough: a useful audit needs both content hashes and header metadata.

# dichotomise to the rescue

`dichotomise` addresses this failure at ingestion. It reads identity from
DICOM metadata rather than trusting exported filenames or series folders. It
preserves the original files, audits for duplicate content and inconsistent
placement or metadata, separates files needing review, and writes clearly
named, verified output.

The tool is intended for any DICOM export that needs structured auditing,
sorting and validation. Its immediate motivation is the XA60+ export: a
globally unique identifier is already inside each DICOM, yet the scanner
hands researchers filenames that cannot distinguish one file from another
outside a fragile folder hierarchy.

## What it does

One command runs the full pipeline, in order:

1. **capture**: groups the source directory's DICOM files by
   `(PatientID, StudyInstanceUID)` from their own headers, never from folder
   names. Nothing is copied yet; every file stays exactly where the export
   left it.
2. **source_archive**: archives each subject's untouched DICOM files,
   straight from the source directory, with a checksum, before anything
   else is read from or written to them. A later copy can then never silently
   diverge from what this archive preserves.
3. **audit**: flags duplicates (a file is a duplicate only if it is a
   byte-for-byte copy of another file in the same folder; the numerically
   first copy, `2.dcm` before `10.dcm`, is kept), files that share a
   SOP Instance UID with a file whose bytes differ (a metadata error, sent to
   review as `sop_conflict`, never called a duplicate; checked across the
   whole study), files sitting in the wrong series folder, and inconsistent patient,
   study, or series metadata within a series. Series come from the headers, not from the
   layout: a folder of one series is checked for strays, and a folder that holds several
   series (a whole study exported into one folder, with or without a `.dcm` extension) is
   audited series by series (three or more series, or two when most files are named after
   their own SOP Instance UID; two series under other names still read as one series plus
   strays). The CLI prints a Rich inventory table
   followed by a NOTES section that explains each finding in plain language
   (for example which file belongs to which series, and which files repeat
   which), and the reports include the same notes and a spreadsheet-friendly
   CSV.
   Byte-identical copies in *different* folders are not flagged: they are
   not duplicates by this definition, and they share one output filename, so
   they end up as a single file in the sorted tree.
4. **sift**: splits files into `retained` and `review`, logically, before
   the final archive is ever built, so a flagged file is never silently
   included in the archive you share. This is a classification step only
   (no files move yet). You do not need to delete and re-export a whole
   series to exclude one bad file: `sift` already excludes it, and the
   untouched source archive (in `archives/`) remains the reference if you
   ever need the original file back.
5. **rectify**: the pipeline's one copy of each file's bytes: it reads
   retained files straight from the source directory and writes them into a
   sorted, clearly named tree:
   `<series number>-<series description>/<series>_<instance>_<SOP instance UID>_e<echo>.dcm`.
   The SOP Instance UID is the identifier DICOM guarantees to be unique per
   file, so a name never depends on instance numbers being unique. The echo
   number comes from the `EchoNumbers` header or, for Enhanced MR files, from
   the per-frame groups. In a
   `--sanitise` run the file is renamed after the UID is regenerated, so the
   filename matches the header and, with any policy that reissues UIDs (all
   shipped ones except `retain`), never carries the original UID.
   Review files are renamed the same way (surfacing what a misfiled or
   duplicate file's own metadata claims, even when that metadata is wrong),
   and are then always archived with a checksum into `archives/` as their own
   `review-archive`, independent of `--keep-working-files`.
6. **sanitise** *(optional, `--sanitise`)*: replaces patient identity
   according to a chosen policy and, for every policy that regenerates `SOPInstanceUID`, remaps every
   remaining instance UID by value, so an image's SOP Instance UID and the references
   that point at it stay in step; see [Sanitisation](#sanitisation) below. When
   used, the `review-archive` is de-identified with the same policy and the
   same replacement identifiers as the main archive, so a sanitised run never
   leaks real PHI through its review files either.
7. **finalise**: independently re-reads the finished output tree (not a
   cached record from earlier stages) to catch any corruption introduced by
   copying or renaming, then archives it with a checksum.

This is a from-scratch, simplified rewrite of the original `dichotomise`,
aimed at being easy to read, debug, and extend, including for someone new to
Python. It implements a single end-to-end pipeline; there is no separate
expert/stage-by-stage command.

## Installation

`dichotomise` requires Python 3.11+. It is a standard, `pyproject.toml`-based
Python package with no tool-specific runtime dependency, so install it with
whichever of these you already use.

### venv

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install dichotomise
dichotomise --help
```

### conda / mamba

`dichotomise` itself comes from PyPI, not conda-forge, so `pip install` still
does the actual install, inside a conda/mamba-managed environment:

```bash
conda create -n dichotomise python=3.11
conda activate dichotomise
pip install dichotomise
dichotomise --help
```

(`mamba` is a drop-in replacement for `conda` in both commands above.)

### From a source checkout

```bash
git clone https://github.com/srikash/dichotomise.git
cd dichotomise
pip install .            # or: pip install -e . for an editable install
```

### uv

Included for continuity with this project's own development and CI setup,
which uses [uv](https://docs.astral.sh/uv/); not required for everyday use.

```bash
# from PyPI, into an isolated tool environment
uv tool install --python 3.11 dichotomise
dichotomise --help

# from a source checkout
cd dichotomise
uv venv --python 3.11
uv sync --locked
uv run dichotomise --help
```

### Container images

Every tagged release also publishes a Docker image and an Apptainer/Singularity
image to GHCR, which is useful for HPC clusters and containerized pipelines where
installing Python packages directly isn't an option.

```bash
# Docker
docker run --rm -v "$PWD:/data" ghcr.io/srikash/dichotomise:2.8.0 --help

# Apptainer / Singularity
apptainer run oras://ghcr.io/srikash/dichotomise:2.8.0-sif --help
```

Mount your source and output directories under `/data` (the image's working
directory) so `--source-dir`/`--out-dir` paths resolve inside the container.
`:latest` tracks the most recent release; pin a version tag for reproducible
runs.

[`scripts/dichotomise-container`](scripts/dichotomise-container) wraps this so
you don't have to think about image references or mounts: it takes the exact
same options as native `dichotomise` (its `--help` output is byte-identical,
since it delegates straight into the containerized CLI), auto-detects
`docker` or `apptainer`, and rewrites `--source-dir`/`--out-dir`/
`--mapping-file` into the right bind mounts for you.

```bash
./scripts/dichotomise-container --source-dir ./study/sub-001 --out-dir ./dichotomise-runs
```

Set `DICHOTOMISE_ENGINE=docker|apptainer` to force an engine, and
`DICHOTOMISE_VERSION=2.8.0` (default `latest`) to pin a release.

## Usage

```bash
dichotomise --source-dir ./study/sub-001 --out-dir ./dichotomise-runs
```

To inspect an export without creating an output directory or saving a report:

```bash
dichotomise --qc --source-dir ./study/sub-001
```

`--qc` reads the DICOMs in place and prints, for each subject found, the Rich audit
table and NOTES section in the terminal. It never copies, changes, archives, or writes files.

`--source-dir` accepts either one subject/session export or a scanner export
containing multiple subjects. They are discovered from DICOM metadata, so
folder names do not need to be sensible.

| Flag | Purpose |
|---|---|
| `--source-dir` (required) | Raw DICOM directory to process. |
| `--out-dir` | Parent directory for the timestamped output folder. Required unless `--qc` is used. |
| `--qc` | Audit the source DICOMs in place and print the audit table and NOTES only. Cannot be combined with processing flags. |
| `--sanitise` | Replace patient identity before final archiving. This uses `minimal` unless a policy is chosen. |
| `--sanitise-policy` | JSON policy name: `minimal` (the default), `standard`, `full`, `retain`, `custom`, or a policy you add yourself. Both `custom` and `custom.json` are accepted. Implies `--sanitise`. |
| `--subj-id` | First numerical replacement ID. For one study, `6` produces `sub-0006`. With several studies, IDs are enumerated automatically as `sub-0006`, `sub-0007`, `sub-0008`, and so on; the CLI logs a warning. |
| `--new-id` | Replacement ID for one study. `ADNC0751` becomes `sub-ADNC0751`; an existing `sub-` prefix is retained. |
| `--random-name` | Generate a random replacement name using the `minimal` sanitisation policy. |
| `--mapping` | One or more `PatientID:replacement_id` pairs. Repeat the flag or separate pairs with commas. The PatientID must exactly match the DICOM `PatientID`. |
| `--mapping-file` | JSON object mapping DICOM PatientID to replacement ID, for example `{"source-01": "sub-0001"}`. A full path may be given with or without the `.json` suffix. |
| `--keep-working-files` | Keep the processed DICOM files written during rectify/sanitise (`working/`) instead of deleting them once the archives are verified. |
| `--keep-unzipped` | Also keep each study's final (optionally sanitised) files unarchived, in `final/`, independent of `--keep-working-files`, for a later BIDS conversion or other tooling that wants plain files rather than a tarball. |

For one study, use `--subj-id`, `--random-name`, or `--new-id`. For several
studies, use `--subj-id` (automatic enumeration), `--random-name`, `--mapping`,
or `--mapping-file`; `--new-id` remains intentionally limited to one study.

[`docs/example-mapping.json`](docs/example-mapping.json) is a ready-to-copy
mapping-file example. Its keys must match the source DICOM `PatientID` values;
its values are the exact replacement labels to write. Its `instructions` field
is ignored by the CLI.

## Output structure

Every run creates one timestamped, UTC output folder beneath `--out-dir`:

```text
<run-timestamp>_dichotomise_outputs/
  archives/
    <patient-id>_<6char-hex>_source-archive_<run-timestamp>.tar.gz
    <patient-id>_<6char-hex>_source-archive_<run-timestamp>.sha256
    <patient-id>_<6char-hex>_source-map_<run-timestamp>.csv           # only with --sanitise
    <subject-label>_<6char-hex>_dichotomised-archive_<run-timestamp>.tar.gz
    <subject-label>_<6char-hex>_dichotomised-archive_<run-timestamp>.sha256
    <subject-label>_<6char-hex>_review-archive_<run-timestamp>.tar.gz        # only if review files exist
    <subject-label>_<6char-hex>_review-archive_<run-timestamp>.sha256       # only if review files exist
  working/                   # temporary, removed unless --keep-working-files
  final/                      # only with --keep-unzipped
    <subject-label>_<6char-hex>/   # the same tree that archives/*_dichotomised-archive.tar.gz holds, unarchived
  reports/
    <subject-label>_<6char-hex>/
      stage-01-report.json
      stage-01-audit.csv
      stage-02-report.json
      stage-03-report.json
      source-manifest.csv
      retained-manifest.csv
      review-manifest.csv     # only if review files exist
  run-status.json             # in_progress, complete, or failed
```

`run-status.json` lets you distinguish a complete result from one left by a
failed or interrupted run. It contains no patient details.

A multi-subject run produces one source archive and one dichotomised archive
per study, both under `archives/`. Each source archive name uses the original
patient ID plus a random six-character hexadecimal suffix. The source archive
contains untouched DICOM files and their original headers, including patient
identity; use the sanitised dichotomised archive for sharing.

Processed archive and report names use the study's `<subject-label>` plus a
random six-character hexadecimal suffix. `<subject-label>` is the real
`PatientID`, unless `--sanitise` was used, in which case it is the replacement
label. A sanitised archive's filename and DICOM headers therefore do not
expose the original identifier.

If `audit`/`sift` found any duplicate, misfiled, or `sop_conflict` files for a subject, they
are renamed the same way as the main archive's files and archived separately
into `archives/` as a `review-archive` tarball with its own checksum. This
archive always survives, independent of `--keep-working-files`, and is
de-identified whenever the run itself is sanitised. When a subject has no
review files, no `review-archive` is created.

### Reports

Each study has a directory under `reports/` containing:

- `stage-01-report.json`: audit totals, duplicate and misfiled folders, and
  one summary per physical DICOM folder, each with its plain-language `notes`.
- `stage-01-audit.csv`: the same per-folder audit summary for spreadsheets,
  including duplicate/misfiled/SOP-conflict counts, differing metadata fields
  and `notes`.
- `stage-02-report.json`: files retained versus copied to `review/`, how
  many byte-identical retained files were not written a second time
  (`retained_identical_files_not_rewritten`), how
  many review files needed a disambiguating `-dupNN` suffix when renamed
  (`review_rename_collisions`; only files that share a name but differ in
  bytes), how many byte-identical review files were not written a second time
  (`review_identical_files_not_rewritten`), and the name/checksum/sanitised state of that
  subject's `review-archive` in `archives/` (`null`/`false` when there were no
  review files to archive).
- `stage-03-report.json`: archive checksum and a `series_inventory` with the
  first original and final DICOM filename for every series. For multi-echo or
  multi-TI series it also lists, per file, the echo number and time, the ASL
  context, and an inferred TI index. Siemens does not write the TI in these
  headers, so the index is inferred from acquisition order and labelled as
  such.
- `source-manifest.csv` / `retained-manifest.csv` / `review-manifest.csv`
  (the last only if review files exist): one row per file with `file`,
  `size_bytes`, `date_modified`, and `date_created`. On Linux, `date_created`
  is the filesystem's metadata-change time (`st_ctime`), not a true creation
  time, since Linux exposes no creation time via Python's `os.stat`.

The audit table and CSV flag differences in Patient ID/name, study UID/date/
time, and series UID/number/description/protocol within a DICOM folder.

In a `--sanitise` run the reports never carry source names. Source filenames, folder names and
the original SeriesInstanceUID can hold a participant code, the scan date and time, or the original
SOP Instance UID, so the manifests, `stage-01` and `stage-02` list files as `folder-NN/NNNN` and
`stage-03` shows the reissued SeriesInstanceUID. (The `retain` policy reissues no UIDs, so there
`stage-03` keeps the original UIDs, as the archive does.) `<patient-id>_<hex>_source-map_<run-timestamp>.csv`
in `archives/`, written as soon as the audit has sorted the files, maps each `folder-NN/NNNN` back to its source file, size, retained or review status
and final filename. It holds real names, so keep it with the source archive and out of anything you
share. A policy that does not keep the series description (such as `full`) also keeps it out of
the archive's folder names (they become just the series number) and out of the reports.

Compression is always `.tar.gz`; `pigz` is used when it is on `PATH`.

## Sanitisation

`--sanitise` applies a named policy, a small JSON file describing what
happens to each DICOM field: kept, removed, replaced with a fixed or
run-specific value, given a newly generated identifier (consistent across
every file for that subject), a birth date scrambled to an approximate but
different year, or a randomly generated placeholder name.

Five policies ship in `src/dichotomise/pydcm/policies/`:

| Policy | What happens |
|---|---|
| `retain` | Nothing changed. |
| `standard` | Identity, patient address, accession number, institution, and device-operator fields removed; every instance UID reissued (study, series, SOP instance, frame of reference, referenced instances, dimension organisation), consistently across files; birth date scrambled by ±1 year (day/month randomised too); demographic fields (e.g. sex) and all scan-descriptive text (protocol name, series/study description, etc.) kept. |
| `full` | Everything `standard` does, plus scan-descriptive text and the device serial number also removed. |
| `minimal` *(the default level)* | Everything `standard` does, but a generated pseudonym is used for both patient name (`Abrahall^Gracious`) and patient ID/output name (`abrahall_gracious`); the birth date is the scan date rather than scrambled. |
| `custom` | A worked, commented example for building your own. It is not used automatically. |

Full detail, including the real scanner-export comparison these were
built from, the policy file schema, and how to write your own, is in
[`docs/sanitise-policies.md`](docs/sanitise-policies.md). In short: copy
`custom.json` to `<your-policy-name>.json` in the same folder, edit it, and
run with `--sanitise-policy <your-policy-name>`.

A policy that regenerates `SOPInstanceUID` remaps every UID value except class, transfer-syntax and
coding-scheme UIDs and the standard's registry UIDs (`1.2.840.10008.*`), which are shared
by every site and stay as they are. A field the policy names itself follows its own action.
The shipped policies keep private (vendor) elements, and some of those hold identifiers,
UIDs included. Check them before you share sanitised data.

## Layout

```text
src/dichotomise/
  cli.py               # the `dichotomise` command (Click + Rich)
  pipeline.py           # runs every stage in order
  run.py                 # output paths for one run
  errors.py              # error types
  stages/                 # one file per pipeline stage
  pydcm/                   # DICOM-specific metadata and sanitisation logic
    policies/                # the sanitisation policy JSON files
    names.py                 # a self-contained adjective+surname placeholder-name generator
  utils/                    # generic filesystem/archive/console helpers
```

## Development

```bash
uv run pytest
uv run ruff format .
uv run ruff check .
uv run mypy src
```

`tests/data/` (real, non-synthetic scan exports used for some tests) is
gitignored and never committed. It may contain identifying information.

Exports of 200 files or more have their headers read by several processes. The `dichotomise`
command handles this itself. If you call `dichotomise.pydcm.read.iter_dicom_metadata` from your
own script, put the call under `if __name__ == "__main__":`.

## Citing dichotomise

Each release is archived on Zenodo with a DOI
([![DOI](https://zenodo.org/badge/1397808016.svg)](https://doi.org/10.5281/zenodo.23166271)).
Citation metadata (including the author and license) is in
[`CITATION.cff`](CITATION.cff). GitHub renders a "Cite this repository"
button from it on the repo's main page, or cite it directly:

> Kashyap, S. (2026). *dichotomise* [Computer software].
> https://doi.org/10.5281/zenodo.23166271

## The dichotomise workflow

```mermaid
flowchart TD
    SOURCE["Raw scanner export<br/>(one folder, or sorted into subfolders)"] --> CAPTURE["capture<br/>Groups DICOM by subject from the headers, in place"]
    CAPTURE --> ARCHIVE["source_archive<br/>Per-study verified tarball + checksum (archives/), straight from the source files"]
    ARCHIVE --> AUDIT["audit<br/>Series found from the headers; byte-for-byte duplicates, SOP-UID conflicts, misfiled files and metadata QA per subject"]
    AUDIT --> REPORT1["stage-01-report.json<br/>stage-01-audit.csv<br/>source-manifest.csv"]
    AUDIT --> SIFT["sift<br/>Splits retained vs review, logically"]
    SIFT --> REPORT2["stage-02-report.json<br/>retained-manifest.csv<br/>review-manifest.csv"]
    SIFT -. "--sanitise only" .-> SOURCE_MAP["source-map.csv (archives/)<br/>Written now, completed at the end; maps folder-NN/NNNN report names to real source files"]
    SIFT --> RECTIFY["rectify<br/>Copies retained files into a metadata-sorted, renamed DICOM tree (an identical repeat is written once)"]
    SIFT --> REVIEW_RECTIFY["rectify_review<br/>Copies and renames review files the same way"]
    RECTIFY --> SANITISE["sanitise (optional, --sanitise)<br/>Replaces patient identity per policy and remaps every instance UID by value; reports use folder-NN/NNNN names"]
    REVIEW_RECTIFY --> SANITISE_REVIEW["sanitise_review (optional, --sanitise)<br/>Same policy, shared replacement identifiers"]
    RECTIFY --> FINALISE["finalise<br/>Re-verifies output, archives it (archives/)"]
    SANITISE --> FINALISE
    REVIEW_RECTIFY --> REVIEW_ARCHIVE["finalise_review<br/>Checksummed review-archive.tar.gz (archives/), only if non-empty"]
    SANITISE_REVIEW --> REVIEW_ARCHIVE
    FINALISE --> REPORT3["stage-03-report.json"]
    FINALISE --> ARCHIVES["archives/dichotomised-archive.tar.gz"]
```

The project is licensed under the MIT License.
