"""SCRIPTS ONLY (needs --scripts-only). Runs the UpgradeLens stages in order, without the agent steps.
Stops at the first failed stage, writes a partial report for it, and returns that stage's exit code.
The full check (with agents) runs from the upgradelens-orchestrator agent: next-step.py and run-stage.py.
A run started by the orchestrator cannot be continued with this script.

Stage 0   phase0-setup.py      validate config and prerequisites, run folder, worktrees, merge base, environment
Stage 1   phase1-build.py      Maven on both branches, in parallel, one JDK, worktree and log per side
Stage 2   phase2-changes.py    changed-files inventory, three-dot (base...head), methods per hunk, commits
Stage 3   phase3-deps.py       dependency trees and conflicts, dependency assessment, application and library API
Stage 4   phase4-impact.py     rules, flows, reach, coverage, untraced code, changed methods with reasons
Stage 5   phase5-behavior.py   startup, dumps, probes, proposed tests on both branches, gaps
Stage 6   phase6-flows.py      scope per flow: full, targeted, smoke
Stage 7   phase7-manual.py     manual review items, with expected master results and owners
Stage 8   phase8-output.py     validated tests placed uncommitted in the head worktree, findings.json, report
Stage 9   phase9-review.py     agent review and manual decisions, checked against their files
Stage 10  phase10-verdict.py   verdict and exit code

Every stage gets the same config: --config is passed to each, and UPGRADELENS_CONFIG is set.

On a failed stage the pipeline still writes reports\\report.md (stage 8 with --report-only) so the failure is
reported, not only logged. After a failed build (stage 1, exit 4) it first runs stage 2 too, so the report
shows what changed next to what failed.

Stage 10 exit codes: 0 safe or safe-with-listed-tests, 1 blocked. Other stages: 2 usage or config,
3 tool failure, 4 build or tests failed (stage 1).

Examples:
  python scripts/run-pipeline.py --scripts-only
  python scripts/run-pipeline.py --scripts-only --config D:\\configs\\orders-boot4.json
  python scripts/run-pipeline.py --scripts-only --run-id 20261126-131717 --from 9
"""

import argparse
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.core import read_json_safe, resolve_config_path, run_main, run_script, runs_root, sort_key  # noqa: E402
from lib.stages import STAGES, missing_message  # noqa: E402


def stage_number(value: str) -> int:
    n = int(value)
    if not 0 <= n <= 10:
        raise argparse.ArgumentTypeError("must be 0 to 10")
    return n


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Run the UpgradeLens stages in order, without the agent steps.")
    p.add_argument("--run-id", help="run to continue (with --from; the newest run is used when it is left out)")
    p.add_argument("--from", dest="from_stage", type=stage_number, default=0, help="first stage to run (0 to 10)")
    p.add_argument("--through", type=stage_number, default=10, help="last stage to run (default 10)")
    p.add_argument("--config")
    p.add_argument("--scripts-only", action="store_true")
    a = p.parse_args(argv)

    if not a.scripts_only:
        print("run-pipeline.py runs the scripts only: no analyst, release notes, test writer or reviewer, so no plain")
        print("explanations, no change reasons and no new tests. The full check runs from the upgradelens-orchestrator")
        print("agent (or the upgradelens-run skill), which drives scripts/next-step.py and scripts/run-stage.py.")
        print("For a scripts-only run (for example CI facts), pass --scripts-only.")
        return 2
    config = resolve_config_path(a.config)
    if not os.path.isfile(config):
        print(f"Config not found: {config}")
        return 2
    if a.through < a.from_stage:
        print(f"--through ({a.through}) is before --from ({a.from_stage}).")
        return 2
    os.environ["UPGRADELENS_CONFIG"] = config
    runs = runs_root(read_json_safe(config))
    run_id = a.run_id

    def run_folders():
        if not os.path.isdir(runs):
            return []
        return [n for n in os.listdir(runs) if os.path.isdir(os.path.join(runs, n))]

    if a.from_stage > 0 and not run_id:
        with_status = [n for n in run_folders() if os.path.isfile(os.path.join(runs, n, "status.json"))]
        if not with_status:
            print(f"No run to continue under {runs}.")
            return 2
        run_id = sorted(with_status, key=sort_key, reverse=True)[0]
        print(f"Continuing run {run_id} from stage {a.from_stage}.")
    if run_id and not os.path.isfile(os.path.join(runs, run_id, "status.json")):
        print(f"Run not found: {os.path.join(runs, run_id)}")
        return 2

    def failure_report(rid: str) -> None:
        # The failure goes into the report too, so it is never only in a log.
        code = run_script("phase8-output.py", ["--run-id", rid, "--report-only", "--config", config])
        report = os.path.join(runs, rid, "reports", "report.md")
        print(f"Partial report: {report}" if code == 0 else f"The partial report could not be written (exit {code}).")

    if a.from_stage <= 0 <= a.through:
        before = set(run_folders())
        code = run_script("phase0-setup.py", ["--config", config])
        new = sorted([n for n in run_folders() if n not in before], key=sort_key, reverse=True)
        run_id = new[0] if new else None
        if code != 0:
            where = f" Run folder: {os.path.join(runs, run_id)}" if run_id else ""
            print(f"Stage 0 failed (exit {code}). Pipeline stopped.{where}")
            return code
        if not run_id:
            print(f"Stage 0 reported success but no new run folder was found under {runs}. Pipeline stopped.")
            return 3

    run_dir = os.path.join(runs, run_id)
    for n, spec in STAGES.items():
        if n == 0 or n < a.from_stage or n > a.through:
            continue
        title = spec.title
        print(f"== Stage {n}: {title} (run {run_id}) ==", flush=True)
        missing = missing_message(run_dir, n)
        if missing:
            print(missing)
            print(f"Pipeline stopped. Diagnose: python scripts/diagnose.py --run-id {run_id}")
            return 2
        code = run_script(spec.script, ["--run-id", run_id, "--config", config])
        if n == 10:
            if code == 0:
                print(f"Verdict: safe or safe with listed tests. Report: {os.path.join(run_dir, 'reports', 'report.md')}")
                return 0
            if code == 1:
                print(f"Verdict: BLOCKED. See {os.path.join(run_dir, 'verdict.json')} and {os.path.join(run_dir, 'reports', 'report.md')}")
                return 1
            print(f"Stage 10 failed (exit {code}).")
            failure_report(run_id)
            return code
        if code != 0:
            print(f"Stage {n} ({title}) failed (exit {code}). Pipeline stopped. Run folder: {run_dir}")
            print(f"Diagnose: python scripts/diagnose.py --run-id {run_id} --stage {n}")
            if n == 1 and code == 4:
                print(f"== Stage 2: changed files, for the failure report (run {run_id}) ==", flush=True)
                run_script("phase2-changes.py", ["--run-id", run_id, "--config", config])
            failure_report(run_id)
            return code

    print(f"Stages {a.from_stage} to {a.through} done for run {run_id}. Run folder: {run_dir}")
    return 0


if __name__ == "__main__":
    run_main(main)
