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
