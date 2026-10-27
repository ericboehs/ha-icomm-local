"""The shared fixtures' own rules."""

import pytest
from conftest import require_openssl


def test_missing_openssl_skips_on_a_dev_machine(monkeypatch):
    monkeypatch.setattr("conftest.shutil.which", lambda name: None)
    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(pytest.skip.Exception):
        require_openssl()


def test_missing_openssl_fails_in_ci(monkeypatch):
    monkeypatch.setattr("conftest.shutil.which", lambda name: None)
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception, match="openssl"):
        require_openssl()


def test_present_openssl_runs(monkeypatch):
    monkeypatch.setattr("conftest.shutil.which", lambda name: "/usr/bin/openssl")
    monkeypatch.setenv("CI", "true")
    require_openssl()
