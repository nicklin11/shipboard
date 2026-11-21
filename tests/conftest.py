"""The test_*.py files here are standalone scripts that run their checks (and
patch modules) at import time, so pytest must never import them in-process.
They are executed one per subprocess by tests/test_suite.py instead."""
from pathlib import Path

_HERE = Path(__file__).parent
collect_ignore = [
    p.name for p in _HERE.glob("test_*.py") if p.name != "test_suite.py"
]
