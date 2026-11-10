"""Release/CI helpers: version consistency and the CLI version source."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_checker():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "check_version", root / "packaging" / "check_version.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_tree(root: Path, pyproject: str, init: str, iss: str) -> None:
    (root / "src" / "personal_download_manager").mkdir(parents=True, exist_ok=True)
    (root / "packaging").mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "x"\nversion = "{pyproject}"\n', encoding="utf-8"
    )
    (root / "src" / "personal_download_manager" / "__init__.py").write_text(
        f'__version__ = "{init}"\n', encoding="utf-8"
    )
    (root / "packaging" / "pdm.iss").write_text(
        f'#define AppVersion "{iss}"\n', encoding="utf-8"
    )


def test_version_checker_accepts_consistency(tmp_path):
    checker = _load_checker()
    _make_tree(tmp_path, "1.2.3", "1.2.3", "1.2.3")
    version, sources = checker.check(tmp_path)
    assert version == "1.2.3"
    assert sources == {
        "pyproject.toml": "1.2.3",
        "src/personal_download_manager/__init__.py": "1.2.3",
        "packaging/pdm.iss": "1.2.3",
    }


def test_version_checker_rejects_drift_and_expect(tmp_path):
    checker = _load_checker()
    _make_tree(tmp_path, "1.2.3", "1.2.4", "1.2.3")
    with pytest.raises(ValueError, match="mismatch"):
        checker.check(tmp_path)

    _make_tree(tmp_path, "1.2.3", "1.2.3", "1.2.3")
    with pytest.raises(ValueError, match="expected"):
        checker.check(tmp_path, "9.9.9")

    (tmp_path / "packaging" / "pdm.iss").unlink()
    with pytest.raises(ValueError, match="cannot read"):
        checker.check(tmp_path)


def test_repository_versions_are_consistent():
    checker = _load_checker()
    version, sources = checker.check()
    assert version
    assert set(sources.values()) == {version}


def test_cli_version_uses_package_version(capsys):
    from personal_download_manager import __version__
    from personal_download_manager.cli import main

    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert capsys.readouterr().out.strip() == f"pdm {__version__}"


def test_installer_ships_only_public_docs():
    """Never wildcard docs\\*: local-only notes must not enter the installer."""
    root = Path(__file__).resolve().parents[1]
    iss = (root / "packaging" / "pdm.iss").read_text(encoding="utf-8")
    assert 'Source: "..\\docs\\*"' not in iss
    for internal in (
        "HANDOFF.md",
        "DEVELOPMENT-STATUS",
        "RENAME-STATUS",
        "github-ssh",
        "reverse-engineering",
        "comparison-",
        "public-",
    ):
        assert internal not in iss
    for public in (
        "browser-integration.md",
        "gui.md",
        "gui.png",
        "testing.md",
        "updating.md",
        "signing.md",
    ):
        assert public in iss
