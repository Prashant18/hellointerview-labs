"""Trivial pure-Python smoke. Lab 1+ adds real concurrency tests."""

from app.main import API_VERSION


def test_version_string_present() -> None:
    assert API_VERSION
