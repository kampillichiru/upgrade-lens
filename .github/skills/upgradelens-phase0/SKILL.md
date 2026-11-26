---
name: upgradelens-phase0
description: Run Phase 0 (setup) of the UpgradeLens upgrade check - validate config and prerequisites, create the run folder, worktrees for master and change_branch, and the environment record. Use when the user asks to run, start, or check the setup for an UpgradeLens run, or to check prerequisites before a run.
---

# UpgradeLens Phase 0: setup

Phase 0 is deterministic. The Python script does all the work. Do not reimplement its checks.

## Steps

1. Read `config\upgrade-lens.json`. Note `repoUrl`, `baseBranch`, and `headBranch`.

2. Run the check only, to see what is missing, without creating anything:

   ```shell
   python scripts/check-prereq.py
   ```

3. If the check passes, run the full setup. The script does these in order:
   - Snapshot the config, the rules and the probes into `runs\<run id>\config\`.
   - Clone the two branches from `repoUrl` into `runs\<run id>\repo.git` (bare, no tags). No local checkout
     of the application is used.
   - Add a worktree for each branch under `runs\<run id>\worktrees\`, named after the branch (two names that
     collide get `-base` and `-head`): the base branch detached, the head branch on its local branch tracking
     origin (so new tests placed there later can be committed and pushed by a developer).
   - Record the merge base of the two branches: every diff of the run is three-dot (`base...head`).
   - Write `artifacts\environment.json`.

   To use another config file, pass `--config <path>` to both scripts.

   ```shell
   python scripts/run-stage.py --new
   ```

   It runs `phase0-setup.py` and creates the run's orchestration ledger, so the orchestrator can continue the
   run (skill `upgradelens-run`). It prints the run id and the run folder. Remember the run id. Do not call
   `phase0-setup.py` directly: a run without a ledger cannot be continued by the orchestrator.

4. Read `runs\<run id>\status.json`. Phase 0 is done only when `state` is `"done"`.

5. Report to the user:
   - the run id and run folder
   - the commit for each branch and the merge base, from `runs\<run id>\artifacts\environment.json`
   - the Java and Maven version for each side
   - any FAIL line from the prerequisite check, with the fix

## Exit codes

- `0`: Phase 0 done.
- `2`: config or prerequisite failure (including an unreachable `repoUrl` or a missing branch). Nothing was changed. Fix the FAIL lines in `config\upgrade-lens.json` and run again. The run folder is only created after the config is valid.
- `3`: tool failure (git worktree or Maven). The run folder exists with `state` set to `failed-tool`. Show the step reason from `status.json`.

## Rules

- Write nothing outside the run folder, and never commit or push. The run's clone and worktrees live in the
  run folder. `python scripts/cleanup-run.py --run-id <id>` removes them after the review.
- Docker is not used. Do not check for it or ask for it.
- Do not print secrets. The Maven settings file can contain credentials; only its path is recorded.
