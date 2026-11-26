"""Step 1 of UpgradeLens: checks the config and prerequisites. Same as phase0-setup.py --check-only.

Example:
  python scripts/check-prereq.py
"""

import argparse
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.core import run_main, run_script  # noqa: E402


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Check the UpgradeLens config and prerequisites.")
    p.add_argument("--config")
    a = p.parse_args(argv)
    args = ["--check-only"] + (["--config", a.config] if a.config else [])
    return run_script("phase0-setup.py", args)


if __name__ == "__main__":
    run_main(main)
