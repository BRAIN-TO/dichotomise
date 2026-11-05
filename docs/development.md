# Development and citation

**TL;DR:** Find the package layout, development checks and citation details here.

## Layout

**TL;DR:** Application code lives in `src/dichotomise/`; tests live in `tests/`.

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

**TL;DR:** Use uv to run the test suite, formatter, linter and type checker.

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

**TL;DR:** Use the citation metadata or the Zenodo concept DOI.

Each release is archived on Zenodo with a DOI
([![DOI](https://zenodo.org/badge/1397808016.svg)](https://doi.org/10.5281/zenodo.23166271)).
Citation metadata (including the author and licence) is in
[`CITATION.cff`](../CITATION.cff). GitHub renders a "Cite this repository"
button from it on the repo's main page, or cite it directly:

> Kashyap, S. (2026). *dichotomise* [Computer software].
> https://doi.org/10.5281/zenodo.23166271

[Back to the README](../README.md)
