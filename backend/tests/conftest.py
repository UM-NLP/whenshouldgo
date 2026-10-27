from __future__ import annotations

import pytest

from fs_backend import clear_emulator_projects


@pytest.fixture(autouse=True)
def _emulator_cleanup():
    yield
    clear_emulator_projects()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "hot_contention: many threads on one document; skipped on the emulator "
        "(far slower than the fake, see docs/backlog.md); a small-scale race runs in "
        "test_emulator_behaviour.py")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    skip = pytest.mark.skip(
        reason="hot-document contention is covered at small scale in test_emulator_behaviour.py")
    for item in items:
        if item.get_closest_marker("hot_contention") and "emulator" in item.name:
            item.add_marker(skip)
