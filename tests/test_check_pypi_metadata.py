from pathlib import Path

import pytest

from check_pypi_metadata import (
    _cp_version,
    _format_latest_cp_wheel,
    _opt,
    _tri,
    _wheel_tags,
    analyse_wheels,
    COLUMNS,
    parse_requirements,
    Result,
    result_to_row,
)


def wheel(filename: str) -> dict:
    return {"packagetype": "bdist_wheel", "filename": filename}


def sdist(filename: str = "pkg-1.0.tar.gz") -> dict:
    return {"packagetype": "sdist", "filename": filename}


class TestParseRequirements:
    def test_extracts_and_normalises_names(self, tmp_path: Path) -> None:
        req_file = tmp_path / "requirements.txt"
        req_file.write_text(
            "# a comment\n"
            "\n"
            "Flask==2.0.1\n"
            "requests>=2.0,<3.0\n"
            "Some_Pkg[extra]==1.0; python_version >= '3.8'\n"
            "foo_bar @ https://example.com/foo_bar-1.0-py3-none-any.whl\n"
        )
        assert parse_requirements([req_file]) == ["flask", "foo-bar", "requests", "some-pkg"]

    def test_deduplicates_across_files(self, tmp_path: Path) -> None:
        req1 = tmp_path / "a.txt"
        req2 = tmp_path / "b.txt"
        req1.write_text("Foo-Bar==1.0\n")
        req2.write_text("foo_bar==2.0\nbaz\n")
        assert parse_requirements([req1, req2]) == ["baz", "foo-bar"]

    def test_missing_file_exits_with_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.txt"
        with pytest.raises(SystemExit, match="not found"):
            parse_requirements([missing])


class TestWheelTags:
    def test_simple(self) -> None:
        assert _wheel_tags("pkg-1.0-py3-none-any.whl") == (["py3"], "none", "any")

    def test_compound_python_tag(self) -> None:
        assert _wheel_tags("pkg-1.0-cp39.cp310-abi3-manylinux2014_x86_64.whl") == (
            ["cp39", "cp310"],
            "abi3",
            "manylinux2014_x86_64",
        )

    def test_ignores_optional_build_tag(self) -> None:
        assert _wheel_tags("pkg-1.0-1-py3-none-any.whl") == (["py3"], "none", "any")

    def test_unparseable_returns_none(self) -> None:
        assert _wheel_tags("bad.whl") is None
        assert _wheel_tags("pkg-1.0.whl") is None


class TestCpVersion:
    @pytest.mark.parametrize(
        "tag,expected",
        [
            ("cp39", (3, 9)),
            ("cp313", (3, 13)),
            ("cp313t", None),  # "t" only ever appears in the ABI tag, never the interpreter tag
            ("pp310", None),  # PyPy
            ("gp313", None),  # GraalPy
            ("abi3", None),
            ("py3", None),
        ],
    )
    def test_parses_or_rejects(self, tag: str, expected: tuple[int, int] | None) -> None:
        assert _cp_version(tag) == expected


class TestFormatLatestCpWheel:
    def test_empty(self) -> None:
        assert _format_latest_cp_wheel([]) == "missing"

    def test_single_exact(self) -> None:
        assert _format_latest_cp_wheel([((3, 13), False)]) == "3.13"

    def test_single_stable(self) -> None:
        assert _format_latest_cp_wheel([((3, 10), True)]) == "3.10+"

    def test_highest_exact_wins_among_exact_only(self) -> None:
        assert _format_latest_cp_wheel([((3, 12), False), ((3, 13), False)]) == "3.13"

    def test_lowest_stable_wins_among_multiple_stable(self) -> None:
        # A stable-ABI wheel is forward-compatible forever, so the lowest
        # minimum already covers everything a higher one would add (e.g.
        # ast-serialize 0.8.0's cp39-abi3 vs. its cp315-abi3.abi3t wheel).
        assert _format_latest_cp_wheel([((3, 9), True), ((3, 15), True)]) == "3.9+"

    def test_stable_wins_over_higher_exact(self) -> None:
        assert _format_latest_cp_wheel([((3, 10), True), ((3, 13), False)]) == "3.10+"


_ANALYSE_WHEELS_CASES: list[tuple[str, list[dict], tuple[bool, bool, bool | None, str | None, str | None, str]]] = [
    (
        "no wheels at all",
        [sdist()],
        (True, False, None, None, None, ""),
    ),
    (
        "pure-Python wheel",
        [wheel("pkg-1.0-py3-none-any.whl")],
        (False, True, True, None, None, ""),
    ),
    (
        "ABI-none binary bundle",
        [wheel("pkg-1.0-py3-none-manylinux_x86_64.whl")],
        (False, True, False, None, None, ""),
    ),
    (
        "all wheel filenames unparseable",
        [wheel("bad.whl")],
        (False, True, None, None, None, "could not parse any wheel filename"),
    ),
    (
        "one of two wheel filenames unparseable",
        [wheel("pkg-1.0-cp313-cp313-manylinux_x86_64.whl"), wheel("bad.whl")],
        (False, True, False, "3.13", "missing", "could not parse 1 of 2 wheel filenames"),
    ),
    (
        "plain exact wheel, no free-threaded build",
        [wheel("pkg-1.0-cp313-cp313-manylinux_x86_64.whl")],
        (False, True, False, "3.13", "missing", ""),
    ),
    (
        "protobuf-style: abi3 only",
        [wheel("protobuf-7.36.0-cp310-abi3-manylinux2014_x86_64.whl")],
        (False, True, False, "3.10+", "missing", ""),
    ),
    (
        "PyNaCl 1.6.2-style: abi3 regular + exact free-threaded",
        [
            wheel("PyNaCl-1.6.2-cp38-abi3-manylinux2014_x86_64.whl"),
            wheel("PyNaCl-1.6.2-cp314-cp314t-manylinux2014_x86_64.whl"),
        ],
        (False, True, False, "3.8+", "3.14", ""),
    ),
    (
        "PEP 803: separate abi3 and abi3t wheels",
        [
            wheel("pkg-1.0-cp38-abi3-manylinux2014_x86_64.whl"),
            wheel("pkg-1.0-cp315-abi3t-manylinux2014_x86_64.whl"),
        ],
        (False, True, False, "3.8+", "3.15+", ""),
    ),
    (
        "free-threaded-only package (no regular build at all)",
        [wheel("pkg-1.0-cp314-cp314t-manylinux2014_x86_64.whl")],
        (False, True, False, "missing", "3.14", ""),
    ),
    (
        "abi3 wheel plus a separate, newer exact wheel",
        [
            wheel("pkg-1.0-cp310-abi3-manylinux2014_x86_64.whl"),
            wheel("pkg-1.0-cp313-cp313-manylinux2014_x86_64.whl"),
        ],
        (False, True, False, "3.10+", "missing", ""),
    ),
    (
        "ast-serialize 0.8.0-style: compound abi3.abi3t tag (PEP 803)",
        [
            wheel("pkg-1.0-cp39-abi3-manylinux2014_x86_64.whl"),
            wheel("pkg-1.0-cp314-cp314t-manylinux2014_x86_64.whl"),
            wheel("pkg-1.0-cp315-abi3.abi3t-manylinux2014_x86_64.whl"),
        ],
        (False, True, False, "3.9+", "3.15+", ""),
    ),
]


class TestAnalyseWheels:
    @pytest.mark.parametrize(
        "description,urls,expected",
        _ANALYSE_WHEELS_CASES,
        ids=[case[0] for case in _ANALYSE_WHEELS_CASES],
    )
    def test_scenarios(
        self,
        description: str,
        urls: list[dict],
        expected: tuple[bool, bool, bool | None, str | None, str | None, str],
    ) -> None:
        assert analyse_wheels(urls) == expected, description


class TestTriAndOpt:
    @pytest.mark.parametrize("value,expected", [(None, "n/a"), (True, "yes"), (False, "no")])
    def test_tri(self, value: bool | None, expected: str) -> None:
        assert _tri(value) == expected

    @pytest.mark.parametrize("value,expected", [(None, "n/a"), ("3.13", "3.13"), ("missing", "missing")])
    def test_opt(self, value: str | None, expected: str) -> None:
        assert _opt(value) == expected


class TestResultToRow:
    def test_full_result(self) -> None:
        result: Result = {
            "package": "foo",
            "notes": "",
            "version": "1.0",
            "trusted_publishing": True,
            "has_provenance": True,
            "has_sdist": True,
            "tp_check_fn": "foo-1.0.tar.gz",
            "has_wheels": True,
            "pure_python": False,
            "latest_python_wheel": "3.9+",
            "latest_freethreaded_wheel": "missing",
        }
        assert dict(zip(COLUMNS, result_to_row(result))) == {
            "package": "foo",
            "version": "1.0",
            "trusted_publishing": "yes",
            "has_provenance": "yes",
            "has_sdist": "yes",
            "has_wheels": "yes",
            "pure_python": "no",
            "latest_python_wheel": "3.9+",
            "latest_freethreaded_wheel": "missing",
            "notes": "",
        }

    def test_package_not_found(self) -> None:
        result: Result = {
            "package": "missing-pkg",
            "notes": "not found on PyPI",
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
        row = dict(zip(COLUMNS, result_to_row(result)))
        assert row["package"] == "missing-pkg"
        assert row["notes"] == "not found on PyPI"
        for col in COLUMNS:
            if col not in ("package", "notes"):
                assert row[col] == "", col
