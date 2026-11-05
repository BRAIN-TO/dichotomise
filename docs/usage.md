# Usage and output

**TL;DR:** Process a source directory into archives and reports, or use `--qc` to audit it in place.

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

[`docs/example-mapping.json`](example-mapping.json) is a ready-to-copy
mapping-file example. Its keys must match the source DICOM `PatientID` values;
its values are the exact replacement labels to write. Its `instructions` field
is ignored by the CLI.

## Output structure

**TL;DR:** Each run has source and processed archives, reports, a status file, and optional unzipped output.

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
identity. Before sharing a processed archive, check that its sanitisation policy
removes the information your workflow requires.

Processed archive and report names use the study's `<subject-label>` plus a
random six-character hexadecimal suffix. `<subject-label>` is the real
`PatientID`, unless `--sanitise` was used, in which case it is the replacement
label. Processed archive names use the replacement label. Header contents depend on
the selected policy: `retain` keeps the original identifiers.

If `audit`/`sift` found any duplicate, misfiled, or `sop_conflict` files for a subject, they
are renamed the same way as the main archive's files and archived separately
into `archives/` as a `review-archive` tarball with its own checksum. This
archive always survives, independent of `--keep-working-files`, and, when sanitisation is requested, uses the same policy as the main archive. When a subject has no
review files, no `review-archive` is created.

### Reports

**TL;DR:** JSON, CSV and manifests record the audit, review decisions and archive details.

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

In a `--sanitise` run the reports never carry source names. Source filenames, folder names and
the original SeriesInstanceUID can hold a participant code, the scan date and time, or the original
SOP Instance UID, so the manifests, `stage-01` and `stage-02` list files as `folder-NN/NNNN` and
`stage-03` shows the reissued SeriesInstanceUID. (The `retain` policy reissues no UIDs, so there
`stage-03` keeps the original UIDs, as the archive does.)
`<patient-id>_<hex>_source-map_<run-timestamp>.csv` in `archives/`, written as soon as the audit has
sorted the files, maps each `folder-NN/NNNN` back to its source file, size, retained or review status
and final filename. It holds real names, so keep it with the source archive and out of anything you
share. A policy that does not keep the series description (such as `full`) also keeps it out of
the archive's folder names (they become just the series number) and out of the reports.

The audit table and CSV flag differences in Patient ID/name, study UID/date/
time, and series UID/number/description/protocol within a DICOM folder.

Compression is always `.tar.gz`; `pigz` is used when it is on `PATH`.

Reading headers, sanitising files and re-checking the finished output use several CPUs automatically
once a subject has 200 or more files (no flag; the container's CPU limit is respected). Subjects are
still processed one after another, and the output is the same as a single-process run.

Source files are checked for changes while their headers are captured and
throughout processing. These checks detect ordinary file changes; they do not
provide a filesystem snapshot.

## Sanitisation

**TL;DR:** Choose a policy for the processed files; the untouched source archive still contains original identifiers.

`--sanitise` applies a named policy, a small JSON file describing what
happens to each DICOM field: kept, removed, replaced with a fixed or
run-specific value, given a newly generated identifier (consistent across
every file for that subject), a birth date scrambled to an approximate but
different year, or a randomly generated placeholder name.

Five policies ship in `src/dichotomise/pydcm/policies/`:

| Policy | What happens |
|---|---|
| `retain` | Nothing changed. |
| `standard` | Identity, patient address, accession number, institution, and device-operator fields removed; every instance UID reissued (study, series, SOP instance, frame of reference, referenced instances, dimension organisation), consistently across files; birth date scrambled by ±1 year (day/month randomised too); demographic fields (e.g. sex) and scan-descriptive text kept. |
| `full` | Everything `standard` does, plus scan-descriptive text and the device serial number also removed. |
| `minimal` *(the default level)* | Everything `standard` does, but a generated pseudonym is used for both patient name (`Abrahall^Gracious`) and patient ID/output name (`abrahall_gracious`); the birth date is the scan date rather than scrambled. |
| `custom` | A worked, commented example for building your own. It is not used automatically. |

A policy that regenerates `SOPInstanceUID` remaps every UID value except class, transfer-syntax and
coding-scheme UIDs and the standard's registry UIDs (`1.2.840.10008.*`), which are shared
by every site and stay as they are. A field the policy names itself follows its own action.
A custom policy that regenerates some UIDs but not `SOPInstanceUID` reissues only those: references
and private elements holding a regenerated UID follow it, in any file of the study, and every other UID stays as it is.
The shipped policies keep private (vendor) elements, and some of those hold identifiers,
UIDs included. Check them before you share sanitised data.

Full detail, including the real scanner-export comparison these were
built from, the policy file schema, and how to write your own, is in
[`docs/sanitise-policies.md`](sanitise-policies.md). In short: copy
`custom.json` to `<your-policy-name>.json` in the same folder, edit it, and
run with `--sanitise-policy <your-policy-name>`.

[Back to the README](../README.md)
