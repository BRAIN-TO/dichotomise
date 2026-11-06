# Installation

**TL;DR:** Python 3.11 or later is required. Choose the environment that suits your workflow.

`dichotomise` requires Python 3.11+. It is a standard, `pyproject.toml`-based
Python package with no tool-specific runtime dependency, so install it with
whichever of these you already use.

## venv

**TL;DR:** Use a Python virtual environment and install from PyPI.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install dichotomise
dichotomise --help
```

## conda / mamba

**TL;DR:** Create a conda environment, then install with pip.

`dichotomise` itself comes from PyPI, not conda-forge, so `pip install` still
does the actual install, inside a conda/mamba-managed environment:

```bash
conda create -n dichotomise python=3.11
conda activate dichotomise
pip install dichotomise
dichotomise --help
```

(`mamba` is a drop-in replacement for `conda` in both commands above.)

## From a source checkout

**TL;DR:** Clone the repository and install the package locally.

```bash
git clone https://github.com/srikash/dichotomise.git
cd dichotomise
pip install .            # or: pip install -e . for an editable install
```

## uv

**TL;DR:** Install as an isolated tool or set up the source checkout.

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

## Container images

**TL;DR:** Use the published Docker or Apptainer image, optionally through the wrapper script.

Every tagged release also publishes a Docker image and an Apptainer/Singularity
image to GHCR, which is useful for HPC clusters and containerised pipelines where
installing Python packages directly isn't an option.

```bash
# Docker
docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:/data" ghcr.io/srikash/dichotomise:2.9.0 --help

# Apptainer / Singularity
apptainer run oras://ghcr.io/srikash/dichotomise:2.9.0-sif --help
```

Mount your source and output directories under `/data` (the image's working
directory) so `--source-dir`/`--out-dir` paths resolve inside the container.
`:latest` tracks the most recent release; pin a version tag for reproducible
runs. `-u` makes the output yours rather than root's on Linux.

[`scripts/dichotomise-container`](../scripts/dichotomise-container) wraps this so
you don't have to think about image references or mounts: it takes the exact
same options as native `dichotomise` (its `--help` output is byte-identical,
since it delegates straight into the containerised CLI), auto-detects
`docker` or `apptainer`, and rewrites `--source-dir`/`--out-dir`/
`--mapping-file` into the right bind mounts for you.

```bash
./scripts/dichotomise-container --source-dir ./study/sub-001 --out-dir ./dichotomise-runs
```

Set `DICHOTOMISE_ENGINE=docker|apptainer` to force an engine, and
`DICHOTOMISE_VERSION=2.9.0` (default `latest`) to pin a release.

[Back to the README](../README.md)
