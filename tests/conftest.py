import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture() -> Any:
    def _load(name: str) -> Any:
        path = FIXTURES / name
        if name.endswith(".json"):
            return json.loads(path.read_text())
        return path.read_text()

    return _load
