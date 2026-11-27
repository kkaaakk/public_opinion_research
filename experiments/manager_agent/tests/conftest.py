import pytest
from offline import offline


@pytest.fixture(autouse=True)
def block_external_io():
    with offline():
        yield
