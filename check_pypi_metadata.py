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
  - has_sdist
  - has_wheels
  - pure_python: whether the wheels are pure-Python (platform tag "any")
  - latest_python_wheel: the newest CPython version for which a (regular, non-free-
    threaded) wheel was built (e.g. "3.13"); "3.10+" for a stable-ABI (abi3) wheel,
    which is forward-compatible with later releases too; "n/a" for pure-Python packages
  - latest_freethreaded_wheel: same as latest_python_wheel, but for free-threaded
    CPython wheels (Python or ABI tag matches cp<N>t, e.g. cp314t, or the PEP 803
    "abi3t" stable ABI, which is likewise forward-compatible); "missing" when
    free-threading applies but no such wheel has been published yet; "n/a" for
    pure-Python packages

Output: tab-separated table to stdout (header + one row per package, sorted).
Progress and warnings go to stderr.
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
    TYPE_CHECKING,
    TypedDict,
)

if TYPE_CHECKING:
    from playwright.sync_api import Page

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

# Matches free-threaded CPython tags such as cp313t, cp314t, and the "abi3t" stable
# ABI for free-threaded builds introduced in CPython 3.15 (PEP 803).
_FREETHREADED_RE = re.compile(r"(?:cp\d+|abi3)t$")
# Matches CPython tags such as cp39, cp313, cp313t; group 1 is the major digit,
# group 2 is the (possibly multi-digit) minor version.
_CP_TAG_RE = re.compile(r"^cp(\d)(\d+)t?$")
# PEP 503 name normalisation: collapse runs of [-_.] to a single dash
_NORMALISE_NAME_RE = re.compile(r"[-_.]+")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class Result(TypedDict):
    package: str
    notes: str
    version: str | None
    trusted_publishing: bool | None
    has_provenance: bool | None
    has_sdist: bool
    tp_check_fn: str | None
    has_wheels: bool
    pure_python: bool | None
    latest_python_wheel: str | None
    latest_freethreaded_wheel: str | None


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


def _request(url: str, accept: str | None = None, timeout: int = 15) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def get_pypi_info(package: str) -> dict[str, Any] | None:
    """Return the parsed JSON from the PyPI JSON API, or None if not found."""
    url = PYPI_JSON_URL.format(package=package)
    try:
        return json.loads(_request(url))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _wheel_tags(filename: str) -> tuple[list[str], str, str] | None:
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


def _cp_version(tag: str) -> tuple[int, int] | None:
    """Parse a CPython tag such as 'cp313' or 'cp313t' into (major, minor); else None."""
    m = _CP_TAG_RE.match(tag)
    if m is None:
        # Expected for non-CPython interpreter tags (e.g. PyPy "pp310", GraalPy
        # "gp313"); not an error, so not surfaced in notes.
        return None
    return int(m.group(1)), int(m.group(2))


def _format_latest_cp_wheel(versions: list[tuple[tuple[int, int], bool]]) -> str | None:
    """
    Summarize a set of (version, is_stable_abi) entries for one line (regular or
    free-threaded) as a single "latest wheel" string, or None if *versions* is empty.

    A stable-ABI (abi3/abi3t) wheel is forward-compatible with every later
    release, so if one or more exist, the LOWEST stable-ABI minimum already
    covers everything any higher-minimum stable-ABI wheel — or any exact
    wheel — would add. E.g. ast-serialize 0.8.0 ships both cp39-abi3 and (via
    a compound cp315-abi3.abi3t wheel, see PEP 803) a redundant, later-
    anchored abi3 entry; the correct summary is "3.9+", not "3.15+".
    Without any stable-ABI entry, report the highest exact version instead,
    since exact wheels don't extend forward and the highest one is the
    current ceiling of support.
    """
    if not versions:
        return None
    stable_versions = [v for v, is_stable_abi in versions if is_stable_abi]
    if stable_versions:
        major, minor = min(stable_versions)
        return f"{major}.{minor}+"
    major, minor = max(v for v, _ in versions)
    return f"{major}.{minor}"


def analyse_wheels(
    urls: list[dict[str, Any]],
) -> tuple[bool, bool, bool | None, str | None, str | None, str]:
    """
    Given the list of file dicts from the PyPI JSON "urls" field, return:
      has_sdist                  bool
      has_wheels                 bool
      pure_python                bool | None  (None when no wheels)
      latest_python_wheel        str | None   (e.g. "3.13"; "3.10+" for a stable-ABI
                                                (abi3) wheel, forward-compatible with
                                                later releases; reflects the regular
                                                (GIL) build only; None when wheels are
                                                pure Python, absent, or have no
                                                parseable regular CPython tag)
      latest_freethreaded_wheel  str | None   (same as latest_python_wheel, but for
                                                free-threaded wheels, where "+" denotes
                                                the PEP 803 "abi3t" stable ABI instead;
                                                "missing" instead of None when free-
                                                threading applies but no such wheel has
                                                been published yet)
      notes                      str          (non-empty when one or more wheel filenames
                                                could not be parsed, even if others were)
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
        return has_sdist, False, None, None, None, ""

    # A wheel is pure Python when its platform tag is "any"
    platform_tags = set()
    abi_tags_flat = set()
    # ((major, minor), is_stable_abi, is_freethreaded) for each parseable CPython
    # tag, one entry per wheel/tag.
    cp_versions: list[tuple[tuple[int, int], bool, bool]] = []
    unparsed_count = 0
    for fn in wheel_files:
        parsed = _wheel_tags(fn)
        if parsed is None:
            unparsed_count += 1
            continue
        py_tags, abi_tag, plat = parsed
        platform_tags.add(plat)
        # A wheel's ABI tag can be a "compressed" set, e.g. "abi3.abi3t" (PEP
        # 803): such a wheel is compatible with CPython under EITHER ABI, so
        # each sub-tag is treated as its own, independent entry below.
        abi_subtags = abi_tag.split(".")
        abi_tags_flat.update(abi_subtags)
        for abi_subtag in abi_subtags:
            # A wheel tagged e.g. "cp310-abi3" is built against the stable/
            # limited C API: it's forward-compatible with every later 3.x
            # release, not just the one named in the python tag. "abi3t" is
            # the free-threaded-build counterpart introduced in CPython 3.15
            # (PEP 803), with the same forward-compatibility guarantee along
            # the free-threaded line.
            is_stable_abi = abi_subtag in ("abi3", "abi3t")
            # Before CPython 3.15, free-threaded builds don't support the
            # stable ABI, so packages ship them as exact-version wheels
            # alongside a separate, wider-reaching abi3 line for the regular
            # (GIL) build — e.g. PyNaCl 1.6.2 ships both cp38-abi3 (regular,
            # 3.8+) and cp314-cp314t (free-threaded, 3.14t only).
            is_freethreaded = bool(_FREETHREADED_RE.match(abi_subtag)) or any(
                _FREETHREADED_RE.match(pt) for pt in py_tags
            )
            for pt in py_tags:
                v = _cp_version(pt)
                if v is not None:
                    cp_versions.append((v, is_stable_abi, is_freethreaded))

    if not platform_tags:
        # All wheel filenames failed to parse; cannot determine characteristics.
        return has_sdist, True, None, None, None, "could not parse any wheel filename"

    pure_python = platform_tags == {"any"}

    if pure_python:
        latest_python_wheel = None
        latest_freethreaded_wheel = None
    elif abi_tags_flat == {"none"}:
        # All wheels use ABI "none" (binary bundles not compiled against Python
        # headers, e.g. nodejs-wheel-binaries, playwright, pylibmagic, typos).
        # Free-threading and the CPython version are not applicable to these packages.
        latest_python_wheel = None
        latest_freethreaded_wheel = None
    else:
        # Report the regular (GIL) build's and the free-threaded build's
        # compatibility separately — they can differ (see the PyNaCl example
        # above).
        regular_versions = [(v, is_stable_abi) for v, is_stable_abi, is_ft in cp_versions if not is_ft]
        freethreaded_versions = [(v, is_stable_abi) for v, is_stable_abi, is_ft in cp_versions if is_ft]
        latest_python_wheel = _format_latest_cp_wheel(regular_versions)
        latest_freethreaded_wheel = _format_latest_cp_wheel(freethreaded_versions) or "missing"

    notes = f"could not parse {unparsed_count} of {len(wheel_files)} wheel filenames" if unparsed_count else ""

    return has_sdist, True, pure_python, latest_python_wheel, latest_freethreaded_wheel, notes


def check_provenance(package: str, version: str, sdist_filename: str) -> tuple[bool, str | None]:
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


def _find_browser_executable(explicit_path: str | None) -> str | None:
    if explicit_path:
        return explicit_path
    for candidate in _DEFAULT_BROWSER_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


class ScrapingError(Exception):
    """Raised when HTML scraping fails to retrieve the expected content."""


def scrape_trusted_publishing(page: "Page", package: str, version: str, tp_filename: str) -> bool:
    """
    Using an already-open Playwright *page*, navigate to the PyPI release page,
    locate the per-file section for *tp_filename* (an sdist or wheel filename),
    and return True/False for "Uploaded using Trusted Publishing?".

    Raises ScrapingError if the page content cannot be retrieved (e.g., due to
    CAPTCHA challenges or other bot-detection mechanisms).
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
        except Exception:  # noqa: S110
            pass  # timeout is expected once analytics start; use whatever we have
    section_start = re.search(rf'id="{escaped}"', html)
    if not section_start:
        # Check if we're stuck on a CAPTCHA/bot-detection page
        if "captcha" in html.lower() or "challenge" in html.lower():
            raise ScrapingError(f"PyPI served a CAPTCHA challenge page for {package}; " "automated scraping is blocked")
        raise ScrapingError(f"could not find file section for '{tp_filename}' in PyPI page for {package}/{version}")
    # Take a generous slice after the section start (next ~2 KB is enough)
    snippet = html[section_start.start() : section_start.start() + 2048]
    m = _TP_RE.search(snippet)
    if m:
        return m.group(1).lower() == "yes"
    raise ScrapingError(f"could not find 'Uploaded using Trusted Publishing?' text for {package}/{version}")


def check_package(package: str) -> Result:
    """
    Query PyPI for *package* and return a Result dict.
    On failure, *version* is None and *notes* contains the error message.
    """
    # Failure shape: only "notes" is overridden below if data is missing or an
    # exception is raised. Left untouched otherwise, so it's only ever mutated
    # in a single, all-or-nothing step once every field has been computed.
    result: Result = {
        "package": package,
        "notes": "",
        "version": None,
        "trusted_publishing": None,
        "has_provenance": None,
        "has_sdist": False,
        "tp_check_fn": None,
        "has_wheels": False,
        "pure_python": None,
        "latest_python_wheel": None,
        "latest_freethreaded_wheel": None,
    }
    try:
        data = get_pypi_info(package)
        if data is None:
            result["notes"] = "not found on PyPI"
            return result

        version = data["info"]["version"]
        has_sdist, has_wheels, pure_python, latest_python_wheel, latest_freethreaded_wheel, wheel_notes = (
            analyse_wheels(data["urls"])
        )

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

        result.update(
            {
                "notes": notes,
                "version": version,
                "trusted_publishing": has_provenance or None,  # filled in later by scraper
                "has_provenance": has_provenance,
                "has_sdist": has_sdist,
                "tp_check_fn": tp_check_fn,
                "has_wheels": has_wheels,
                "pure_python": pure_python,
                "latest_python_wheel": latest_python_wheel,
                "latest_freethreaded_wheel": latest_freethreaded_wheel,
            }
        )
        return result
    except Exception as exc:
        result["notes"] = str(exc)
        return result


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _tri(value: bool | None) -> str:
    """Format a bool|None as yes/no/n/a."""
    if value is None:
        return "n/a"
    return "yes" if value else "no"


def _opt(value: str | None) -> str:
    """Format a str|None as itself, or n/a when None."""
    return value if value is not None else "n/a"


COLUMNS = (
    "package",
    "version",
    "trusted_publishing",
    "has_provenance",
    "has_sdist",
    "has_wheels",
    "pure_python",
    "latest_python_wheel",
    "latest_freethreaded_wheel",
    "notes",
)

# Columns whose value is a bool|None (or bool) formatted via `_tri()` in `result_to_row()`.
_TRI_COLUMNS = {
    "trusted_publishing",
    "has_provenance",
    "has_sdist",
    "has_wheels",
    "pure_python",
}

# Columns whose value is a str|None formatted via `_opt()` in `result_to_row()`.
_OPT_COLUMNS = {
    "latest_python_wheel",
    "latest_freethreaded_wheel",
}


def result_to_row(r: Result) -> list[str]:
    row = []
    for col in COLUMNS:
        if col in ("package", "notes"):
            # These columns are always relevant and should be printed even if the package was not found on PyPI.
            row.append(r[col])  # type: ignore[literal-required]
        elif r["version"] is None:
            # If the package was not found on PyPI, leave all other columns blank.
            row.append("")
        elif col in _TRI_COLUMNS:
            row.append(_tri(r[col]))  # type: ignore[literal-required]
        elif col in _OPT_COLUMNS:
            row.append(_opt(r[col]))  # type: ignore[literal-required]
        else:
            row.append(r[col])  # type: ignore[literal-required]
    return row


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Query PyPI for packages listed in requirements files and report metadata "
            "(Trusted Publishing, PEP 740 provenance, wheel availability, pure-Python, "
            "free-threaded, latest Python wheel)."
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
        for done, future in enumerate(as_completed(futures), 1):
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
                    # Use stealth settings to avoid bot detection / CAPTCHA challenges
                    browser = pw.chromium.launch(
                        headless=True,
                        executable_path=browser_path,
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                    context = browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                        ),
                        viewport={"width": 1920, "height": 1080},
                        locale="en-US",
                    )
                    page = context.new_page()
                    # Remove navigator.webdriver to avoid detection
                    page.add_init_script('Object.defineProperty(navigator, "webdriver", {get: () => undefined});')
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
                            scrape_error = str(exc)
                            if r["notes"]:
                                r["notes"] += f"; {scrape_error}"
                            else:
                                r["notes"] = scrape_error
                    browser.close()
                print(file=sys.stderr)

    # TSV output, sorted by package name
    print("\t".join(COLUMNS))
    for pkg in sorted(results):
        print("\t".join(result_to_row(results[pkg])))


if __name__ == "__main__":
    main()
