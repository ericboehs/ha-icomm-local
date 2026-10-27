"""The release helper the workflows rely on."""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release_check.py"


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


def test_changelog_is_mirrored_for_the_addon_dialog():
    assert run("mirror").returncode == 0


def test_current_version_has_a_changelog_section():
    import json
    version = json.loads((SCRIPT.parent.parent / "custom_components/icomm_local/manifest.json")
                         .read_text())["version"]
    out = run("notes", version)
    assert out.returncode == 0 and out.stdout.strip()


def test_mismatched_tag_is_refused():
    out = run("versions", "v99.0.0")
    assert out.returncode == 1 and "99.0.0" in out.stdout


def test_unknown_version_has_no_notes():
    assert run("notes", "99.0.0").returncode == 1


def test_addon_and_integration_versions_agree():
    assert run("same-version").returncode == 0


def _script():
    import importlib.util
    spec = importlib.util.spec_from_file_location("release_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_current_version_passes_the_tag_check():
    import json
    version = json.loads((SCRIPT.parent.parent / "custom_components/icomm_local/manifest.json")
                         .read_text())["version"]
    out = run("versions", f"v{version}")
    assert out.returncode == 0 and f"versions ok: {version}" in out.stdout


def test_a_section_needs_a_bullet():
    check = _script()
    changelog = ("## [1.2.3] - 2026-01-01\n\n### Fixed\n\nSome prose.\n\n"
                 "## [1.2.2] - 2025-12-31\n\n### Added\n- **Thing:** it works.\n\n"
                 "## [1.2.1] - 2025-12-30\n\n### Fixed\n  * nested star bullet\n")
    assert check.section("1.2.3", changelog)                     # has content...
    assert not check.has_entry(check.section("1.2.3", changelog))  # ...but no entry
    assert check.has_entry(check.section("1.2.2", changelog))
    assert check.has_entry(check.section("1.2.1", changelog))
    assert not check.has_entry(check.section("9.9.9", changelog))
    assert not check.has_entry("-not a bullet\n---")
