# check-pypi-metadata

A command-line tool that queries PyPI to collect metadata about the latest
release of each package listed in one or more requirements files.

For each package it reports:

| Column | Description |
|---|---|
| `version` | Latest version on PyPI |
| `trusted_publishing` | Uploaded via Trusted Publishing (OIDC)? |
| `has_provenance` | Has a PEP 740 provenance attestation? |
| `has_sdist` | Source distribution available? |
| `has_wheels` | Binary wheels available? |
| `pure_python` | Wheels are pure Python (`py3-none-any`)? |
| `has_freethreaded` | Free-threaded CPython wheels available? |
| `latest_python_wheel` | Newest CPython version with a wheel (e.g. `3.13`); `3.10+` for a stable-ABI (`abi3`) wheel, forward-compatible with later releases too; `n/a` for pure-Python packages |
| `notes` | Warnings or error messages |

Output is a tab-separated table written to stdout (one row per package, sorted
alphabetically).  Progress and warnings go to stderr.

## Installation

```
pip install git+https://github.com/nsoranzo/check-pypi-metadata.git
```

HTML scraping (for packages that lack PEP 740 attestations) also requires a
system Chrome/Chromium.  Pass `--no-browser` to skip this step.

## Usage

```
check-pypi-metadata [OPTIONS] [REQUIREMENTS_FILE ...]
```

**Arguments**

- `REQUIREMENTS_FILE …` — one or more requirements files, or a single
  directory to search for `*requirements.txt` files (default: all
  `*requirements.txt` files in the current working directory).

**Options**

| Option | Default | Description |
|---|---|---|
| `--workers N` | 10 | Concurrent HTTP workers |
| `--no-browser` | — | Skip HTML scraping; show `n/a` for packages without PEP 740 attestations |
| `--browser-path PATH` | auto | Path to Chrome/Chromium executable |

## Examples

Audit the current directory's requirements files and save to a TSV:

```
check-pypi-metadata > metadata.tsv
```

Packages missing free-threaded wheels:

```
awk -F'\t' '$7 == "no" && $8 == "no" {print $1}' metadata.tsv
```

Packages not using Trusted Publishing:

```
awk -F'\t' '$3 == "no" {print $1}' metadata.tsv
```

Packages whose latest wheel doesn't yet target Python 3.15 (comparing
major/minor as integers, since e.g. `"3.9" < "3.15"` is false when compared
as plain numbers; `+`-suffixed values are skipped since a stable-ABI wheel
already covers later releases, including 3.15):

```
awk -F'\t' '$9 != "n/a" && $9 !~ /\+$/ { split($9, v, "."); if (v[1] < 3 || (v[1] == 3 && v[2] < 15)) print $1, $9 }' metadata.tsv
```

## How Trusted Publishing is determined

1. **PEP 740 provenance** — the PyPI Integrity API is queried for the sdist
   (or first wheel for wheel-only packages).  A provenance object implies
   Trusted Publishing was used → `yes`.
2. **HTML scraping** (fallback) — if no attestation is found, Playwright drives
   a headless Chrome instance to read the *"Uploaded using Trusted Publishing?"*
   field on the pypi.org release page.  Pass `--no-browser` to skip this step.

## Requirements

- Python ≥ 3.10
- [`playwright`](https://playwright.dev/python/) (installed automatically)
- A system Chrome/Chromium for HTML scraping (pass `--no-browser` to skip)
