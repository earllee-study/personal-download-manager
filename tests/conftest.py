from __future__ import annotations

import pytest

from server import TestServer, make_content


@pytest.fixture
def server():
    srv = TestServer().start()
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture
def content():
    return make_content(8 * 1024 * 1024, seed=7)
