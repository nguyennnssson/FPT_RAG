"""Run all backend tests.

Each test file pins its own offline env vars at import, so they run as separate
processes to stay isolated.

    python tests/run_all.py
"""
import subprocess
import sys
from pathlib import Path

TESTS = ["test_unit.py", "test_smoke.py", "test_api.py", "test_history.py"]
here = Path(__file__).resolve().parent

failed = []
for name in TESTS:
    print(f"\n=== {name} ===")
    proc = subprocess.run([sys.executable, str(here / name)])
    if proc.returncode != 0:
        failed.append(name)

print("\n" + ("FAILED: " + ", ".join(failed) if failed else "ALL TEST SUITES PASSED"))
sys.exit(1 if failed else 0)
