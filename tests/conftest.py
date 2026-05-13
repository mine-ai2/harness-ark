import pytest


@pytest.fixture
def ark_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ARK_HOME", str(tmp_path))
    return tmp_path
