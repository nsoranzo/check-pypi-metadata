#!/usr/bin/env python3
"""
For each package listed in one or more requirements files, query PyPI to collect
metadata about the latest release:

  - version
  - trusted_publishing: whether the sdist was uploaded using Trusted Publishing
    (OIDC authentication).  Determined as follows:
      * If a PEP 740 provenance attestation is present (checked via the PyPI
        Integrity API), this implies Trusted Publishing was used → "yes".
      * Otherwise, the pypi.org release HTML page is scraped via a headless
        browser (Playwright + system Chrome) to read the "Uploaded using
        Trusted Publishing?" field directly.  Requires --browser-path or a
        discoverable system Chrome/Chromium.  Pass --no-browser to skip this
        step (those packages will show "n/a").
  - has_provenance: whether the sdist has a PEP 740 provenance attestation
    (a strictly stronger signal than Trusted Publishing alone; implies it).
  - has sdist
  - has wheels
  - pure-Python wheels   (platform tag "any")
  - free-threaded wheels (Python tag matches cp<N>t, e.g. cp314t)

Output: tab-separated table to stdout (header + one row per package, sorted).
Progress and warnings go to stderr.

To filter the output to packages with missing free-threaded wheels, use:

$ awk -F'\t' '$7 == "no" && $8 == "no" {print $1}' deps_pypi_metadata.tsv

To filter the output to packages without Trusted Publishing, use:

$ awk -F'\t' '$3 == "no" {print $1}' deps_pypi_metadata.tsv
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import (
    as_completed,
    ThreadPoolExecutor,
)
from pathlib import Path
from typing import (
    Any,
    Optional,
    TypedDict,
)

__version__ = "1.0.0"

PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"
PYPI_INTEGRITY_URL = "https://pypi.org/integrity/{package}/{version}/{filename}/provenance"
PYPI_RELEASE_URL = "https://pypi.org/project/{package}/{version}/"
USER_AGENT = f"check_pypi_metadata/{__version__} (https://github.com/nsoranzo/check-pypi-metadata)"

# Default browser executable candidates for Playwright-based scraping
_DEFAULT_BROWSER_CANDIDATES = [
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/usr/bin/google-chrome-stable",
]

# Matches free-threaded CPython tags such as cp313t, cp314t
_FREETHREADED_RE = re.compile(r"cp\d+t$")
# PEP 503 name normalisation: collapse runs of [-_.] to a single dash
_NORMALISE_NAME_RE = re.compile(r"[-_.]+")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class Result(TypedDict):
    package: str
    notes: str
    version: Optional[str]
    trusted_publishing: Optional[bool]
    has_provenance: Optional[bool]
    has_sdist: bool
    tp_check_fn: Optional[str]
    has_wheels: bool
    pure_python: Optional[bool]
    has_freethreaded: Optional[bool]


def parse_requirements(files: list[Path]) -> list[str]:
    """Extract sorted unique package names from one or more requirements files."""
    packages = set()
    for req_file in files:
        if not req_file.exists():
            print(f"Warning: {req_file} not found, skipping.", file=sys.stderr)
            continue
        with open(req_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Extract the package name: strip version specifiers, extras,
                # environment markers, and URL requirements (@).
                name = re.split(r"[><=!~\[;@\s]", line)[0]
                if name:
                    packages.add(_NORMALISE_NAME_RE.sub("-", name.lower()))
    return sorted(packages)


def _request(url: str, accept: Optional[str] = None, timeout: int = 15) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def get_pypi_info(package: str) -> Optional[dict[str, Any]]:
    """Return the parsed JSON from the PyPI JSON API, or None if not found."""
    url = PYPI_JSON_URL.format(package=package)
    try:
        return json.loads(_request(url))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _wheel_tags(filename: str) -> Optional[tuple[list[str], str, str]]:
    """
    Parse a wheel filename and return (python_tags, abi_tag, platform_tag).
    python_tags is a list (the tag may be composite, e.g. "cp39.cp310").
    Returns None if the filename cannot be parsed.
    """
    # Wheel filename: {name}-{version}(-{build})?-{python}-{abi}-{platform}.whl
    stem = filename[:-4]  # strip ".whl"
    parts = stem.split("-")
    if len(parts) < 5:
        return None
    platform_tag = parts[-1]
    abi_tag = parts[-2]
    python_tag = parts[-3]
    return python_tag.split("."), abi_tag, platform_tag


def analyse_wheels(urls: list[dict[str, Any]]) -> tuple[bool, bool, Optional[bool], Optional[bool], str]:
    """
    Given the list of file dicts from the PyPI JSON "urls" field, return:
      has_sdist        bool
      has_wheels       bool
      pure_python      bool | None  (None when no wheels)
      has_freethreaded bool | None  (None when wheels are pure Python or absent)
      notes            str          (non-empty when wheel filenames could not be parsed)
    """
    has_sdist = False
    wheel_files = []
    for f in urls:
        if f["packagetype"] == "sdist":
            has_sdist = True
        elif f["packagetype"] == "bdist_wheel":
            wheel_files.append(f["filename"])

    has_wheels = bool(wheel_files)
    if not has_wheels:
        return has_sdist, False, None, None, ""

    # A wheel is pure Python when its platform tag is "any"
    platform_tags = set()
    python_tags_flat = set()
    abi_tags_flat = set()
    for fn in wheel_files:
        parsed = _wheel_tags(fn)
        if parsed is None:
            continue
        py_tags, abi_tag, plat = parsed
        platform_tags.add(plat)
        python_tags_flat.update(py_tags)
        abi_tags_flat.add(abi_tag)

    if not platform_tags:
        # All wheel filenames failed to parse; cannot determine characteristics.
        return has_sdist, True, None, None, "could not parse any wheel filename"

    pure_python = platform_tags == {"any"}

    if pure_python:
        has_freethreaded = None
    elif abi_tags_flat == {"none"}:
        # All wheels use ABI "none" (binary bundles not compiled against Python
        # headers, e.g. nodejs-wheel-binaries, playwright, pylibmagic, typos).
        # Free-threading is not applicable to these packages.
        has_freethreaded = None
    else:
        # Check both python tags (e.g. cp313t-cp313t) and ABI tags
        # (e.g. cp314-cp314t) — the convention differs across releases.
        has_freethreaded = any(_FREETHREADED_RE.match(t) for t in python_tags_flat | abi_tags_flat)

    return has_sdist, True, pure_python, has_freethreaded, ""


def check_provenance(package: str, version: str, sdist_filename: str) -> tuple[bool, Optional[str]]:
    """
    Return (bool, error_str|None).
    True  = PEP 740 provenance object found (implies Trusted Publishing was used).
    False = no provenance attestation (HTTP 404).
    Note: a package can be uploaded via Trusted Publishing (OIDC) without
    producing a PEP 740 attestation; that case is indistinguishable from a
    plain API-token upload through the public PyPI APIs.
    """
    url = PYPI_INTEGRITY_URL.format(package=package, version=version, filename=sdist_filename)
    try:
        _request(url, accept="application/vnd.pypi.integrity.v1+json")
        return True, None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False, None
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# HTML scraping via Playwright (fallback when has_provenance is False)
# ---------------------------------------------------------------------------

# Matches "Uploaded using Trusted Publishing? Yes" or "No" (with optional whitespace)
_TP_RE = re.compile(r"Uploaded using Trusted Publishing\?\s*(Yes|No)", re.IGNORECASE)


def _find_browser_executable(explicit_path: Optional[str]) -> Optional[str]:
    if explicit_path:
        return explicit_path
    for candidate in _DEFAULT_BROWSER_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def scrape_trusted_publishing(page: Any, package: str, version: str, tp_filename: str) -> Optional[bool]:
    """
    Using an already-open Playwright *page*, navigate to the PyPI release page,
    locate the per-file section for *tp_filename* (an sdist or wheel filename),
    and return True/False for "Uploaded using Trusted Publishing?", or None if
    it cannot be determined.
    """
    url = PYPI_RELEASE_URL.format(package=package, version=version)
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    html = page.content()

    # Each file has a <section id="{filename}"> (or a div with that id).
    # Find the block that starts at id="{tp_filename}" and extract the TP flag.
    escaped = re.escape(tp_filename)
    if not re.search(rf'id="{escaped}"', html):
        # Section not in initial DOM — pypi.org may serve a Fastly bot-detection
        # challenge page first; waiting for network-idle (capped at 8 s) lets the
        # challenge JS solve, the browser redirect to the real page, and that page
        # load, all before analytics traffic restarts.
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
            html = page.content()
        except Exception:
            pass  # timeout is expected once analytics start; use whatever we have
    section_start = re.search(rf'id="{escaped}"', html)
    if not section_start:
        return None
    # Take a generous slice after the section start (next ~2 KB is enough)
    snippet = html[section_start.start() : section_start.start() + 2048]
    m = _TP_RE.search(snippet)
    if m:
        return m.group(1).lower() == "yes"
    return None


def check_package(package: str) -> Result:
    """
    Query PyPI for *package* and return a Result dict.
    On failure, *version* is None and *notes* contains the error message.
    """
    try:
        data = get_pypi_info(package)
        if data is None:
            return {
                "package": package,
                "notes": "not found on PyPI",
                "version": None,
                "trusted_publishing": None,
                "has_provenance": None,
                "has_sdist": False,
                "tp_check_fn": None,
                "has_wheels": False,
                "pure_python": None,
                "has_freethreaded": None,
            }

        version = data["info"]["version"]
        has_sdist, has_wheels, pure_python, has_freethreaded, wheel_notes = analyse_wheels(data["urls"])

        # Check for PEP 740 provenance attestation via the Integrity API.
        # Use the sdist if present; fall back to the first wheel for packages
        # that ship wheels only (e.g. greenlet, playwright, pywin32).
        has_provenance = None
        provenance_error = None
        sdist_fn = next((f["filename"] for f in data["urls"] if f["packagetype"] == "sdist"), None)
        tp_check_fn = sdist_fn or next((f["filename"] for f in data["urls"] if f["packagetype"] == "bdist_wheel"), None)
        if tp_check_fn:
            has_provenance, provenance_error = check_provenance(package, version, tp_check_fn)
        notes_parts = [p for p in (wheel_notes, provenance_error) if p]
        notes = "; ".join(notes_parts)

        return {
            "package": package,
            "notes": notes,
            "version": version,
            "trusted_publishing": has_provenance or None,  # filled in later by scraper
            "has_provenance": has_provenance,
            "has_sdist": has_sdist,
            "tp_check_fn": tp_check_fn,
            "has_wheels": has_wheels,
            "pure_python": pure_python,
            "has_freethreaded": has_freethreaded,
        }
    except Exception as exc:
        return {
            "package": package,
            "notes": str(exc),
            "version": None,
            "trusted_publishing": None,
            "has_provenance": None,
            "has_sdist": False,
            "tp_check_fn": None,
            "has_wheels": False,
            "pure_python": None,
            "has_freethreaded": None,
        }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _tri(value: Optional[bool]) -> str:
    """Format a bool|None as yes/no/n/a."""
    if value is None:
        return "n/a"
    return "yes" if value else "no"


COLUMNS = [
    "package",
    "version",
    "trusted_publishing",
    "has_provenance",
    "has_sdist",
    "has_wheels",
    "pure_python",
    "has_freethreaded",
    "notes",
]


def result_to_row(r: Result) -> list[str]:
    if r["version"] is None:
        return [r["package"], "", "", "", "", "", "", "", r["notes"]]
    return [
        r["package"],
        r["version"],
        _tri(r["trusted_publishing"]),
        _tri(r["has_provenance"]),
        _tri(r["has_sdist"]),
        _tri(r["has_wheels"]),
        _tri(r["pure_python"]),
        _tri(r["has_freethreaded"]),
        r["notes"],
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Query PyPI for packages listed in requirements files and report metadata "
            "(Trusted Publishing, PEP 740 provenance, wheel availability, pure-Python, free-threaded)."
        )
    )
    parser.add_argument(
        "requirements_files",
        nargs="*",
        type=Path,
        default=None,
        metavar="REQUIREMENTS_FILE",
        help=(
            "Requirements files to read, or a single directory to search for "
            "*requirements.txt files (default: all *requirements.txt files in "
            "the current working directory)"
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=10,
        help="Number of concurrent HTTP workers (default: 10)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help=(
            "Skip HTML scraping for packages without PEP 740 attestations. "
            "Those packages will show 'n/a' for trusted_publishing."
        ),
    )
    parser.add_argument(
        "--browser-path",
        metavar="PATH",
        help=(
            "Path to a Chrome/Chromium executable for HTML scraping "
            "(default: auto-detect from common system locations)"
        ),
    )
    args = parser.parse_args(argv)

    if not args.requirements_files:
        args.requirements_files = sorted(Path.cwd().glob("*requirements.txt"))
    elif len(args.requirements_files) == 1 and args.requirements_files[0].is_dir():
        args.requirements_files = sorted(args.requirements_files[0].glob("*requirements.txt"))

    print("Requirements files:", file=sys.stderr)
    for f in args.requirements_files:
        print(f"  {f}", file=sys.stderr)

    packages = parse_requirements(args.requirements_files)
    total = len(packages)
    print(f"Checking {total} unique packages...", file=sys.stderr)

    results: dict[str, Result] = {}

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(check_package, pkg): pkg for pkg in packages}
        done = 0
        for future in as_completed(futures):
            done += 1
            print(f"\r  {done}/{total}", end="", file=sys.stderr, flush=True)
            r = future.result()
            results[r["package"]] = r

    print(file=sys.stderr)

    # --- Phase 2: HTML scraping for packages with has_provenance=False ---
    needs_scraping: list[Result] = [
        r for r in results.values() if r["version"] is not None and r["has_provenance"] is False and r["tp_check_fn"]
    ]

    if needs_scraping and not args.no_browser:
        browser_path = _find_browser_executable(args.browser_path)
        if browser_path is None:
            print(
                "Warning: no Chrome/Chromium found; skipping HTML scraping "
                "(use --browser-path or --no-browser to suppress this warning).",
                file=sys.stderr,
            )
        else:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                print(
                    "Warning: playwright not installed; skipping HTML scraping "
                    "(pip install playwright to enable it).",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Scraping {len(needs_scraping)} release pages for Trusted Publishing info...",
                    file=sys.stderr,
                )
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True, executable_path=browser_path)
                    page = browser.new_page()
                    # Block resources we don't need so pages load faster.
                    # Use fulfill (empty 200) rather than abort — aborting
                    # sub-resources can propagate net::ERR_ABORTED to the main
                    # navigation on some pages.
                    page.route(
                        "**/*",
                        lambda route: (
                            route.fulfill(status=200, content_type="text/plain", body="")
                            if route.request.resource_type in {"image", "stylesheet", "font", "media"}
                            else route.continue_()
                        ),
                    )
                    for i, r in enumerate(needs_scraping, 1):
                        print(
                            f"\r\033[K  {i}/{len(needs_scraping)}: {r['package']}",
                            end="",
                            file=sys.stderr,
                            flush=True,
                        )
                        try:
                            tp_fn = r["tp_check_fn"]
                            assert tp_fn is not None  # guaranteed by needs_scraping filter
                            version = r["version"]
                            assert version is not None  # guaranteed by needs_scraping filter
                            tp = scrape_trusted_publishing(page, r["package"], version, tp_fn)
                            r["trusted_publishing"] = tp
                        except Exception as exc:
                            print(
                                f"\nWarning: scraping {r['package']} failed: {exc}",
                                file=sys.stderr,
                            )
                    browser.close()
                print(file=sys.stderr)

    # TSV output, sorted by package name
    print("\t".join(COLUMNS))
    for pkg in sorted(results):
        print("\t".join(result_to_row(results[pkg])))


if __name__ == "__main__":
    main()
