import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sales_scoring import ActivityInput, ActivityType, ScoringEngine  # noqa: E402


@pytest.fixture
def make_engine(tmp_path):
    engines = []

    def factory(tz="Asia/Shanghai", name="test.db"):
        eng = ScoringEngine(str(tmp_path / name), tz)
        engines.append(eng)
        return eng

    yield factory
    for eng in engines:
        eng.close()


def act(activity_id, sp, type_, occurred_at, amount=0, note=None):
    return ActivityInput(activity_id, sp, type_, occurred_at, amount, note)
