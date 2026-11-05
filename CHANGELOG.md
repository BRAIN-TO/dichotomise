# Changelog

## Unreleased

Integrity and provenance checks, plus speed-ups. Sanitised and archived output is unchanged for inputs that
pass the checks; inputs that fail one now stop the run instead of being processed.

- Detect source files that change during capture or processing, and carry source provenance through to finalisation.
- Verify sanitised files after writing them: pixel data and interpretation fields, `PatientID`, and the SOP Instance UID in the header and file meta.
- Require a private digest manifest when finalising standalone retained or review files, and verify standalone sanitised output.
- Reuse the digests gathered while archiving the source to detect duplicates and check copies.
- Write sanitised output directly from the source files unless `--keep-working-files` asks for the intermediate copies; move verified unzipped output into place instead of copying it.
- Apply a policy-supplied series description (`replace`/`add`) to folder names.
- Validate every study label and policy before archiving, and stop the compressor promptly on interruption.
- Preserve policy-added code sequences. Share the image-verification, sanitisation and audit-folder summary code between stages.
- Split the README into `docs/` guides.

## v2.8.0

**Privacy fix: re-run any data sanitised with v2.7.4 or earlier before you share it.** With `standard`,
`minimal` and `full`, original UIDs survived in `ReferencedSOPInstanceUID`, `DimensionOrganizationUID` and
other UID attributes (Siemens UIDs embed the scan date and time), and those references no longer pointed
at any file in the archive. Only the study, series, SOP instance and frame-of-reference UIDs were reissued.
A policy that regenerates `SOPInstanceUID` (all the shipped ones except `retain`) now remaps every instance UID by value, so a file's SOP Instance UID and the
references to it stay in step. Class, transfer-syntax and coding-scheme UIDs and registry UIDs
(`1.2.840.10008.*`) are kept, as is any field the policy names itself. `retain` is unchanged. Private
(vendor) elements are still kept by every shipped policy, and some hold identifiers; check them before
sharing.

- Fix: an export with every DICOM in one folder (no `.dcm` extension needed) had every file reported
  as "misfiled", so a real run sent nearly all of it to review. The misfiled check assumed one series
  per folder. A folder that holds three or more series, or two or more when most files are named after their own
  SOP Instance UID, is now audited series by series, by the headers, so a sorted export and a
  single-folder export give the same result. A folder of exactly two series with other file names is
  still read as one series plus strays.
- `--qc` now audits and prints each subject separately, as a normal run does, instead of one merged
  table. SOP-UID conflicts are therefore checked within a subject in `--qc` too. A subject with no
  PatientID is labelled `(no PatientID)`.
- A file named `<SOPInstanceUID>.dcm` counts as named after its own UID when deciding whether a folder
  holds a whole study. Under a policy that hides series descriptions, the `series-NNN` labels in the
  reports are numbered by series number, so their order no longer follows the hidden descriptions.
- Faster. Sanitising walks each file's datasets once instead of once per policy field (an Enhanced MR
  file with the `minimal` policy: 54 ms to 13 ms), and exports of 200 files or more have their headers
  read by several processes (`--qc` on 8294 files: 28 s to 3 s). A four-subject sanitised run went from
  A four-subject sanitised run (8294 files) took 10 min 10 s with v2.7.4's code and takes 4 min 32 s now, UID remapping included. The speedups do not change output. If you call `iter_dicom_metadata` from your own
  script, put the call under `if __name__ == "__main__":`.

## v2.7.4

This release also carries the changes prepared as v2.7.2 and v2.7.3, which were never published.

Fix: a byte-identical copy of a file in another folder (for example two copies of one
export under one `--source-dir`) made the run fail at finalise with "Expected 2 file(s) in the
finished output, but found 1". `rectify` now writes such a copy once, as the README and v2.7.1
already said, and `stage-02-report.json` reports how many were skipped as
`retained_identical_files_not_rewritten`.

Change to `--sanitise` reports: the files in `reports/` no longer carry source names. Source
filenames, folder names and the original SeriesInstanceUID can hold a participant code, the scan
date and time, or the original SOP Instance UID (Siemens exports often name files after it). A
sanitised run now lists files as `folder-NN/NNNN` in the manifests, `stage-01` (report and CSV, NOTES
included) and `stage-02`, and `stage-03` shows the reissued SeriesInstanceUID (under `retain`, which reissues no UIDs,
`stage-03` still shows the original ones, as the archive does). The way back to the real
names is `<patient-id>_<hex>_source-map_<timestamp>.csv` in `archives/`, beside the source archive,
which already holds the real identity. Unsanitised runs, `--qc` and the printed NOTES are unchanged.

The source map is written as soon as the audit has sorted the files, so a run that fails partway still
leaves the way back to the real names (its `final_file` column is blank until the run finishes).

Fix: a policy that does not keep the series description (such as `full`) left it in the archive's
folder names, in `stage-03` (`series_description`) and in the NOTES that quote a misfiled file's
series. Folders are now named by series number alone, `stage-03` shows `null`, and the NOTES use
`description-N`.

Also: the audit's duplicate note used the filename `.dcm` even for files without that extension.

## v2.7.1

- Docs only, plus one help string. README: `--qc` prints the audit table and NOTES (the `--qc` help
  text said "tables only"); SOP-UID conflicts are checked across the whole study (the whole source
  directory under `--qc`); the numerically first duplicate is kept; byte-identical copies in
  different folders are not flagged and collapse to one output file; the filename carries the
  original UID only under a policy that does not reissue UIDs (`retain`); the install table of
  contents points at real headings.

## v2.7.0

**Breaking: rectified filenames change.** Files are now named
`<series>_<instance>_<SOPInstanceUID>_e<echo>.dcm` (previously
`<series>_<SeriesInstanceUID>_<instance>_e<echo>.dcm`). The SOP Instance UID is unique per file, so
names no longer rely on instance numbers. With `--sanitise`, files are renamed after their UID is
regenerated, so the filename matches the header and no longer carries the original UID.

- A duplicate is now strictly a byte-for-byte copy. Previously a file was a duplicate if its parsed
  content matched another file's, ignoring its SOP Instance UID and creation time. A re-saved copy
  with a new UID is therefore no longer reported as a duplicate.
- Files that share a SOP Instance UID but differ in bytes are no longer treated as duplicates. They
  go to review with reason `sop_conflict`, and the run continues.
- The duplicate you keep is the numerically first file (`2.dcm` before `10.dcm`), not the first in
  string order.
- A NOTES section follows the audit table and is written to `stage-01-report.json` (`notes`) and
  `stage-01-audit.csv` (`notes`, `sop_conflict_count`). It names the file that belongs to another
  series, which files repeat which, and why series metadata varies.
- Enhanced (multi-frame) MR: the echo number and time are read from the per-frame groups, so
  multi-echo files are no longer all named `_e01`. Each series now reports echoes, ASL
  label/control and an inferred TI index (stage-03 report `series_inventory`). The TI is inferred
  from acquisition order only, and only when the headers state no inversion time.
- Byte-identical files that land on one name in the review tree are written once instead of
  getting `-dupNN` names (`review_identical_files_not_rewritten` in `stage-02-report.json`).
- `stage-01-audit.csv` gains `sop_conflict_count` and `notes` columns.

## v2.6.5

- Archive creation (source, dichotomised, and review archives) now prefers `pigz` for gzip compression when it's on PATH -- parallel across CPU cores, versus the single-threaded stdlib `gzip`/`zlib` codec used otherwise. The CLI reports "pigz found, using it." up front when it's available.

## v2.6.4

- README: document plain `venv` and conda/mamba as the primary install paths; `uv` is now its own section, kept for continuity with this project's dev/CI setup rather than presented as the default.
- No other code changes; this is the release tagged with an actual GitHub release, to trigger Zenodo DOI archiving via its release webhook (v2.6.3 only got a git tag + PyPI/container publish).

## v2.6.3

- No code changes; cuts a release/tag to trigger Zenodo DOI archiving via the GitHub release webhook.

## v2.6.2

- More informative CLI output: an opening banner (version, source path, and either the output directory or "QC Mode"), a subject/file count right after capture, a live "Time elapsed" ticker while a run is in progress (interactive terminals only), and a total-time summary at the end.
- Archive creation (source, dichotomised, and review archives) now reports byte progress (`KB done/KB total`) on an interactive terminal.
- The banner's "Output:" line now shows the actual created, timestamped run folder rather than the raw `--out-dir` argument.
- `--keep-working-files` now warns that it keeps a full second copy of every subject's files on disk, on top of the archive.
- Long paths in CLI output (banner, success/error messages) no longer wrap mid-path.

## v2.6.0

- QC/audit tables (CLI and `stage-01-report.json`/`stage-01-audit.csv`) now sort series folders in numeric series order (e.g. `..._6_MR` before `..._29_MR`) instead of alphabetically.
- Cut redundant DICOM copying: `capture()` no longer copies files into a working tree, and `sift()` is now a logical split — rectify() is the pipeline's one real copy of each file's bytes, reading straight from the original source.
- `source_archive()` now archives each subject's files straight from the original source directory, before anything else touches them, rather than from a working copy.
- `archives/` now holds the source archive alongside the dichotomised and review archives (previously a separate `source/` folder).
- New `--keep-unzipped` flag: in addition to the always-created archive, also writes each study's final (optionally sanitised) files unarchived, into `final/` — e.g. for a later BIDS conversion.

## v2.5.0

- Published Docker and Apptainer/Singularity images to GHCR on every release, alongside PyPI.
- Added `scripts/dichotomise-container`, a wrapper with the exact same CLI as native `dichotomise` (its `--help` is byte-identical) that runs everything inside the published container — auto-detects `docker`/`apptainer` and handles the bind mounts for you.
- CI now auto-tags `main` whenever `pyproject.toml`'s version changes, so a version bump alone triggers the PyPI release and container builds with no manual tagging step.

## v2.4.0

- Review files (duplicates, misfiled) no longer disappear with the working directory by default. They're renamed like retained files, then archived separately as a checksummed `review-archive.tar.gz` in `archives/`.
- `--sanitise` now covers review files too — same policy, same replacement IDs as the main archive, so excluded files can't leak real PHI in a sanitised run.
- A rename collision in review (misfiled/duplicate metadata causes these) no longer aborts the run; colliding files get a `-dupNN` suffix.
- New per-subject manifests: `source-manifest.csv`, `retained-manifest.csv`, `review-manifest.csv` — file, size, timestamps.
- `stage-02-report.json` gained `review_rename_collisions`, `review_archive`, `review_archive_checksum`, `review_archive_sanitised`.
- CLI summary table: new "Review archive" column.
