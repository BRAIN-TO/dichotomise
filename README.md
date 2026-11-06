# dichotomise [![Version](https://img.shields.io/badge/version-2.8.0-purple.svg)](https://github.com/srikash/dichotomise/releases) [![Licence](https://img.shields.io/badge/licence-MIT-orange.svg)](LICENCE) [![CI](https://github.com/srikash/dichotomise/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/srikash/dichotomise/actions/workflows/ci.yml) [![DOI](https://zenodo.org/badge/1397808016.svg)](https://doi.org/10.5281/zenodo.23166271)

<b><ins>DIC</ins></b>h<b><ins>O</ins></b>to<b><ins>M</ins></b>ise checks, sorts, renames,
optionally sanitises and archives DICOM exports. It was prompted by problems
we found in Siemens XA60+ exports.

## Contents

- [Why dichotomise?](#why-dichotomise)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Documentation](#documentation)
- [Citation and licence](#citation-and-licence)

## Why dichotomise?

**TL;DR:** Scanner exports reuse filenames and can contain duplicated or
misfiled images. Dichotomise checks DICOM headers and file content to make
those problems visible.

XA60+ exports name files `1.dcm`, `2.dcm` and so on in every series folder.
Two unrelated images can therefore have the same filename. Once a file is
separated from its folder, the filename does not identify it, even though its
header carries a SOP Instance UID.

The export format makes good data-handling practice difficult. Dichotomise
addresses each problem as follows:

| Export problem | What dichotomise does |
|---|---|
| Repeated filenames hide file identity and can collide when files are moved. | Uses the SOP Instance UID from the header in each rectified filename. |
| A file's series folder can contradict its header. | Audits series metadata and sends files that disagree with their folder's majority to review. |
| A repeated export can add byte-identical files. | Detects byte-identical copies within each source folder and sends later copies to review. A copy in another folder is written once, not twice. |
| Two different files can claim one SOP Instance UID. | Sends every file in the group to review as a `sop_conflict`, never as a duplicate. |
| Processing can obscure the original export. | Archives the untouched source DICOMs and records manifests before processing. |

Two examples prompted this work. One Series 22 image was found in a Series 21
folder. In another export, an 81-image DWI series appeared twice as 162 files;
each duplicate pair was byte-identical. The files show what happened to the
export, but do not establish the underlying cause.

Read the [background and incident accounts](docs/background.md) for the
examples, evidence and limitations. The [workflow guide](docs/workflow.md)
explains the checks and how retained and review files are handled.

## Installation

**TL;DR:** Create a Python 3.11+ virtual environment and install from PyPI.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install dichotomise
dichotomise --help
```

For conda, uv, source checkouts and containers, see the
[installation guide](docs/installation.md).

## Quick start

**TL;DR:** Give dichotomise a source directory and an output directory. Use
`--qc` to audit the source in place without creating files.

```bash
dichotomise --source-dir ./study/sub-001 --out-dir ./dichotomise-runs
```

```bash
dichotomise --qc --source-dir ./study/sub-001
```

To only sort a folder of DICOMs into `<subject>/<series>/` subfolders, use `--sort`. It works in
place when every file sits directly in the source folder (no `--out-dir`); otherwise give `--out-dir`
to sort a copy. Add `--dry-run` first to see the plan; in place **moves your originals**.

```bash
dichotomise --sort --dry-run --source-dir ./study/sub-001
dichotomise --sort --source-dir ./study/sub-001
```

Use `--sanitise` to apply a policy to processed and review files. Choose the
policy carefully: descriptions can contain identifying text, and the
untouched **source archive retains the original identifiers**. See the
[sanitisation policies](docs/sanitise-policies.md) before sharing output.

See [usage and output](docs/usage.md) for all options, mapping files, archive
contents and reports.

## Documentation

**TL;DR:** Start with the guide for the task you need.

- [Background and incidents](docs/background.md): the export problems and evidence.
- [Installation](docs/installation.md): all supported installation methods.
- [Usage and output](docs/usage.md): options, archives, manifests and reports.
- [Workflow](docs/workflow.md): the processing stages and their checks.
- [Sanitisation policies](docs/sanitise-policies.md): policy actions and customisation.
- [Development and citation](docs/development.md): project layout and checks.
- [Changelog](CHANGELOG.md): what changed in each release.

## Citation and licence

**TL;DR:** Cite the Zenodo concept DOI; the project uses the MIT licence.

Citation details are in [CITATION.cff](CITATION.cff). The project is licensed
under the [MIT licence](LICENCE).
