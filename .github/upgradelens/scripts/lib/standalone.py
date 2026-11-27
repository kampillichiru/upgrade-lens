"""Standalone mode of the stage scripts (phase1 to phase10, --standalone --run-id <id>).

A stage in standalone mode runs on any run that has the inputs it needs, in any state, also a finished one, and
changes nothing that the run's own phases, report or verdict read. (ul.py phases rerun in place, with history and
staleness; standalone mode is for trying a stage script change without touching the run at all.)

  - writes   everything (artifacts, reports, review, logs, status.json, verdict.json) goes to the mirror folder
             runs\\<id>\\standalone-stages\\, laid out like the run folder itself
  - reads    each input from the mirror when an earlier standalone stage wrote it there, else from the run, so
             standalone stages can be chained (for example 2, then 4 on the new inventory)
  - refuses  while a phase of the run is running (its lock is held), because stages 1, 3 and 5
             use the run's worktrees

The worktrees are the one thing a standalone stage shares with the run: stage 1 rebuilds their target folders
(from the same commits), stage 5 copies tests in and out of them and puts back the tests stage 8 placed, and
stage 8 does not place tests in standalone mode.
"""

import os

from lib.core import read_json_safe, resolve_run_id
from lib.lock import run_lock

MIRROR = "standalone-stages"


class StagePaths:
    """Where a stage reads and writes. Without standalone, both are the run folder."""

    def __init__(self, run_dir: str, standalone: bool):
        self.run_dir = run_dir
        self.standalone = standalone
        self.root = os.path.join(run_dir, MIRROR) if standalone else run_dir

    def out(self, *parts) -> str:
        """A path the stage writes: in the mirror in standalone mode."""
        return os.path.join(self.root, *parts)

    def inp(self, *parts) -> str:
        """A path the stage reads: the mirror's copy when a standalone stage wrote one, else the run's."""
        if self.standalone:
            p = os.path.join(self.root, *parts)
            if os.path.exists(p):
                return p
        return os.path.join(self.run_dir, *parts)

    def read(self, *parts):
        return read_json_safe(self.inp(*parts))


def select_run(runs_dir: str, run_id: str | None, standalone: bool, states=(), any_state: bool = False) -> str | None:
    """The run a stage works on. Without standalone: resolve_run_id (the state-order guard). In
    standalone mode: the given run, in any state, when its folder has status.json and no stage is running."""
    if not standalone:
        return resolve_run_id(runs_dir, run_id, states, any_state)
    if not run_id:
        print("--standalone needs --run-id.")
        return None
    run_dir = os.path.join(runs_dir, run_id)
    if not os.path.isfile(os.path.join(run_dir, "status.json")):
        print(f"Run not found: {run_dir} (no status.json)")
        return None
    lk = run_lock(run_dir)
    if lk["active"]:
        print(f"Run {run_id} has a stage running ({lk['key']}, pid {lk['pid']}). Wait for it, then run the "
              "standalone stage.")
        return None
    return run_id


def add_standalone_arg(parser) -> None:
    parser.add_argument("--standalone", action="store_true",
                        help="any run in any state; writes only to runs\\<id>\\standalone-stages\\ (needs --run-id)")
