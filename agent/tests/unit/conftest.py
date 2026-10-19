"""Isolate unit tests from the developer's shell.

Settings priority is: init args > environment variables > .env file > defaults.
`Settings(_env_file=None)` only disables the .env layer, so a stray AUDIT_* variable in the
shell (e.g. AUDIT_CACHE_MODE=replay) would silently change what "defaults" means.
Integration tests live outside this directory on purpose: they must see the real environment.
"""
import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_audit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("AUDIT_"):
            monkeypatch.delenv(name)