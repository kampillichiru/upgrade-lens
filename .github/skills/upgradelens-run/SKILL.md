---
name: upgradelens-run
description: Run the full UpgradeLens upgrade check (master vs change_branch, configured in config\upgrade-lens.json) end to end - every stage script and every agent step (release notes, analyst, test writer, reviewer) - by following scripts\next-step.py one action at a time, with the guards that stop the run on any out-of-order step, write outside an agent's scope, or failed check. Also runs one agent on its own (scripts\run-agent.py) on a run that has its data. Use when the user asks to run, continue or resume the UpgradeLens check, or to run one UpgradeLens agent. Used by the upgradelens-orchestrator agent.
---

# UpgradeLens: run the check

You drive the run. You do not decide the order, judge results, or edit files. `scripts\next-step.py` decides
every step; the stage scripts produce the facts; the agents write proposals; the guards accept or reject them;
people decide what is accepted.

## Before the first step

1. Read `config\upgrade-lens.json`: note `repoUrl`, `baseBranch`, `headBranch`, `java.base`, `java.head`.
2. Run `python scripts/check-prereq.py`. If it prints a FAIL line, stop and report the line and its fix. Do not
   edit the config yourself.

## The loop

Repeat until the action is `done` or `stop`:

1. Run `python scripts/next-step.py --run-id <run id>` (without `--run-id` only for the very first step of a new run).
   It prints one JSON object. Do exactly its `action`, using the commands it prints, unchanged.

2. By action:

   | action | What you do |
   |---|---|
   | `run-stage` | Run `command`. Exit 0: go to 1. Exit 10: the stage is still running in the background: run `waitCommand` until it returns something other than 10. Any other exit: go to 1 (next-step reports the stop). For stage 0 (`run-stage.py --new`), read `runId` from its output and use it from now on. |
   | `wait` | Run `command` until it returns something other than 10. |
   | `run-agent` | (a) Run `beginCommand`. It prints JSON with `prompt` and `endCommand`. Exit 2: go to 1. (b) Start the subagent named in `subagent` with `prompt` as its whole task, word for word: do not add, shorten or explain. If your tool has no agent with that name (Claude Code), start a general-purpose subagent with the same `prompt`: it names the `.github/agents/` file the subagent follows. (c) When it returns, run `endCommand`, even if the subagent says it failed. (d) Go to 1. |
   | `end-agent` | Run `endCommand`, then go to 1. |
   | `stop` | Stop. Run `python scripts/diagnose.py --run-id <run id>` and report what it prints: the failed step, its exit code in words, missing inputs and the stage that writes them, the end of its logs, and the next command. Also give `summary` and `report`. Do not work around it. |
   | `done` | Report (see below). |

3. Keep a short to-do list of the steps done, so the user can follow.

## Report at the end

Start from `runs\<id>\reports\summary.md`: it answers, in order, what changed, whether the existing tests ran it,
which new tests were written and their result on both branches, every difference found, and what was not checked.
Give the user its section 1, the differences of section 5, and the path to it. Then:

- The verdict and each blocking reason (from the `done` output or `runs\<id>\verdict.json`).
- The report: `runs\<id>\reports\report.md` (and `.html`). Point to section 8a (impact in simple words, with
  examples and production risk), section 5 (why each changed method changed), section 9 (flows to test) and
  section 10 (new tests).
- The new tests: placed uncommitted in `runs\<id>\worktrees\<head branch>` (list in `artifacts\new-tests.json`),
  for a developer to review, commit and push.
- The manual items waiting for a person: `runs\<id>\artifacts\manual-items.json`; decisions go in
  `runs\<id>\review\manual-decisions.json`, written by that person.
- Steps that were skipped, and why: the `skipped` entries of `runs\<id>\orchestration\ledger.json`.

## One agent on its own

When the user asks for one agent only (for example "run the analyst on run <id>"), do not start the loop above.
Each agent can run on any run where the data it needs is there, outside the run's plan:

1. Run `python scripts/run-agent.py --run-id <run id>`. It prints, for each agent (`release-notes`, `analyst`,
   `tests`, `reviewer`), `ready`, `missing` (with the artifacts and the stage that writes them) or
   `nothing-to-do`. If the agent asked for is not `ready`, stop and report what is missing.
2. Run its `beginCommand` (add `--repair` when the user asks the test writer to repair the run's tests). It
   prints JSON with `subagent`, `prompt`, `outputFolder` and `endCommand`.
3. Start the subagent exactly as for `run-agent` above: the named subagent (or a general-purpose one in Claude
   Code) with `prompt` as its whole task, word for word.
4. When it returns, run `endCommand`, even if the subagent says it failed. Exit 0: accepted. Exit 6: report the
   check errors it printed. Exit 5: the agent wrote outside its folder; report the files.
5. Report the answer file in `outputFolder`. For a scripts-only run, `endCommand` prints an `apply` command that
   copies the answer into the run, and the run-pipeline command to rerun the stages that use it: run them only
   when the user asks. For an orchestrated run the answer stays in its session folder.

The agent writes only into `runs\<id>\standalone\<session>\out\`. It costs no `agentBudget` and is not recorded in
the ledger.

## When the user asks why a run failed or is stuck

Run `python scripts/diagnose.py --run-id <run id>` (add `--stage <n>` or `--key <plan key>` for one step, `--lines 80`
for more log). It changes nothing. Explain its output in plain words and name the fix (for example the config key,
JDK path or build plugin). Do not edit the config or the run yourself.

## Guardrails (the scripts enforce them; do not test them)

- Order: `run-stage.py` and `agent-step.py --action begin` refuse any step that next-step did not print
  (exit 2, "Out of order"). Stage scripts of an orchestrated run refuse to run directly.
- Inputs: `run-stage.py` refuses a stage whose inputs are missing (exit 2, "cannot start", with the stage that
  writes each one); next-step then prints `stop`.
- One stage at a time: a lock per run. Long stages run detached and ignore Ctrl+C; exit 10 only means "still
  running", and stopping a `waitCommand` does not stop the stage.
- A stage that was interrupted (killed, or its runner died) is retried once: next-step prints it again as
  `run-stage` with `attempt` 2. Stage 5 first restores anything the interrupted pass left in the worktrees. A
  stage that failed for a real reason (build, tool, usage) is not retried: next-step prints `stop`.
- In a test repair round, stage 5 reruns only the tests the repair changed; the others keep their results.
- Agent write scope: `end` compares a fingerprint of the run folder, both worktrees' git state, and the tool's
  scripts, config, agents and skills. Any change outside the agent's own files stops the run.
- Agent output checks: stale input hash, a gap with no test and no reason, a changed method with no reason, a
  high or medium impact with no plain explanation, a release note without a source, a review missing a test:
  the step fails and is retried once with the errors in the prompt (`orchestration.maxAgentAttempts`).
- Budget: each `begin` counts one call against `agentBudget`. When it is spent, the run stops.

## Never

- Never run `phase*.py`, `run-pipeline.py`, `mvn`, or a git command that changes state yourself.
- Never edit, create or delete a file: no source, no test, no config, no artifact, no `manual-decisions.json`.
- Never skip `endCommand`, never call `begin` twice in a row for the same key, never invent a run id.
- Never pass your own summary to a subagent instead of `prompt`.
- Never run `run-agent.py --action apply` unless the user asked for it. Never copy an agent's files yourself.
- Never commit, stage, push or create a branch. Never use Docker.
- Never call a verdict safe that the scripts did not give. A listed item is not a pass.
