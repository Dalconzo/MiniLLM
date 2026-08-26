from __future__ import annotations

import gc

import pytest


@pytest.fixture(autouse=True)
def collect_unclosed_fixture_resources():
    """Keep fixture resource leaks from making the suite depend on GC timing."""

    yield
    gc.collect()
