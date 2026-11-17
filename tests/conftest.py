import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from galpanel2.core.store import create_library


@pytest.fixture
def lib(tmp_path):
    with create_library(str(tmp_path / "test.galdb")) as lib:
        yield lib
