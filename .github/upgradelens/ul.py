"""UpgradeLens: check a tech upgrade (base branch vs head branch) before release, one phase at a time.

    python .github/upgradelens/ul.py --help

Each phase also has a GitHub Copilot agent (.github/agents/ul-00-setup ... ul-06-report.agent.md) that runs it,
does its judgment jobs and explains the result. Python does all the mechanical work; the agents only propose.
"""

import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))

from lib.core import run_main  # noqa: E402
from runner.cli import main  # noqa: E402

if __name__ == "__main__":
    run_main(main)
