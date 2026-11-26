# How UpgradeLens works, and what to run when

This page is for the people who run UpgradeLens and fix it when something goes wrong. For the design
reasons behind it, read `approach.md`.

## 1. What it does

UpgradeLens checks an upgrade (for example Spring Boot 3 to 4, Java 17 to 25) before it goes to production.
It compares two branches of the same repository:

- **base** (`baseBranch`, for example `master`): what runs in production today
- **head** (`headBranch`, for example `change_branch`): the upgrade

It builds and tests both, finds what changed (code, dependencies, library APIs), predicts what the change can
break, proves or disproves it by running both applications and new tests side by side, and ends with a
verdict: **safe**, **safe with listed tests**, or **blocked**, with a report a person can read.

## 2. Two kinds of work: scripts and agents

| | Python scripts (stages 0 to 10) | Agents (4 of them) |
|---|---|---|
| Do | mechanical work: git, Maven, javap, JaCoCo, HTTP probes, comparing results, the report and the verdict | judgment: read release notes, explain a change, write tests, review tests |
| Same input, same output | yes | no (an AI model) |
| Cost | free | counts against `agentBudget` (12 calls per run by default) |
| Decide the verdict | yes | never. An agent only **proposes**; a script checks the proposal and a stage decides |
| Where | `scripts\phase0-setup.py` ... `scripts\phase10-verdict.py` | `.github\agents\*.agent.md` |

An agent's answer is used only when it passes its checks and its **input hash** still matches the files it read.
If those files change, the answer is stale and is ignored.

## 3. The pieces

```
you
 └─ /upgradelens-orchestrator   (Copilot or Claude Code; instructions in .github)
     └─ loop: python scripts/next-step.py      → prints ONE next action as JSON
         ├─ run-stage  → scripts/run-stage.py  → scripts/phaseN-*.py      (a stage)
         └─ run-agent  → scripts/agent-step.py --action begin             (guard: fingerprint, prompt)
                         → subagent (.github/agents/<agent>.agent.md)      (writes its answer)
                         → scripts/agent-step.py --action end             (guard: write scope, checks)
```

- **The orchestrator agent** only coordinates. It never decides the order, edits a file or judges a result.
  It runs what `next-step.py` prints.
- **`next-step.py`** knows the fixed plan (`scripts\lib\plan.py`) and the run's ledger
  (`runs\<id>\orchestration\ledger.json`), and prints the one next step.
- **`run-stage.py`** runs one stage. It refuses a step that is out of order, a second stage while one is running,
  and a stage whose inputs are missing. Long stages run in the background (exit 10 = still running).
- **`agent-step.py`** wraps each agent: before it, a fingerprint of everything the agent must not touch;
  after it, the write-scope check (any change outside the agent's own file stops the run) and the content
  checks (one retry with the errors).

What each stage and agent needs, writes and is checked on is defined once:
`scripts\lib\stages.py` (stages) and `scripts\lib\agents.py` (agents).

## 4. The plan of a full run

| Step | What happens | Main output (`runs\<id>\artifacts\`) |
|---|---|---|
| Stage 0 | check config and tools, clone both branches, one worktree each, merge base | `environment.json` |
| Stage 1 | Maven build and tests on both branches, in parallel, each with its own JDK | `build-results.json`, `test-results.json` |
| Stage 2 | changed files and methods (diff from the merge base) | `change-inventory.json` |
| Stage 3 | dependency tree diff, application API diff, library API diff | `dependency-diff.json`, `api-diff.json`, `library-api-diff.json` |
| **Agent: release-notes** | checks each library change against the library's release notes | `agent\release-notes.json` |
| Stage 4 | rules, flows (endpoints, jobs, listeners), reach, coverage, changed methods | `impact-findings.json`, `reach.json`, `changed-methods.json` |
| **Agent: analyst** | hypotheses, plain explanations, reason for each changed method | `agent\hypotheses.json` |
| Stage 4 again | merges the analyst's hypotheses into the findings | (same files) |
| Stage 5 (probes) | starts both apps, sends the same requests, compares the answers | `behavior-results.json` |
| **Agent: test writer** | a characterization test for every gap (`test-gaps.json`) | `agent\tests-manifest.json`, `agent\tests\` |
| Stage 5 (tests) | runs the new tests on both branches, with injected regressions | `behavior-results.json` |
| Repair round | the test writer fixes tests that did not validate; stage 5 reruns only those | |
| Stage 6 | test scope per flow: full, targeted, smoke | `flows.json` |
| Stage 7 | questions for a person, with the expected master result | `manual-items.json` |
| Stage 8 | places validated tests uncommitted in the head worktree; writes the report | `reports\report.md`, `reports\summary.md` |
| **Agent: reviewer** | independent review of the validated tests | `agent\review-answer.json` |
| Stage 9 | records the reviews and the manual decisions | `review\review-record.json` |
| Stage 10 | verdict, then rebuilds the report with it | `verdict.json` |

A step that is not needed (for example no library change for the release-notes agent) is **skipped with a
recorded reason**, never silently.

## 5. Four ways to run it

| Mode | Command | Agents | Use it for |
|---|---|---|---|
| **A. Full run** | `/upgradelens-orchestrator` (in Copilot or Claude Code) | all four | the real check before a release |
| **B. Scripts only** | `python scripts/run-pipeline.py --scripts-only` | none | CI, quick facts, testing the scripts, no AI cost |
| **C. One agent on its own** | `python scripts/run-agent.py --run-id <id> ...` or `/upgradelens-orchestrator agent <step> on run <id>` | one | trying, redoing or adding one agent's answer |
| **D. One stage again** | `python scripts/run-pipeline.py --scripts-only --run-id <id> --from N [--through M]` | none | redoing stages of a scripts-only run after a fix |

### A. Full run

```
/upgradelens-orchestrator                         start a new run
/upgradelens-orchestrator continue run <id>       continue a run that was interrupted
```

The orchestrator loops until `done` (verdict and report) or `stop` (it then runs `diagnose.py` and tells you why).

### B. Scripts only

```shell
python scripts/run-pipeline.py --scripts-only                        # stages 0 to 10, new run
python scripts/run-pipeline.py --scripts-only --through 4            # only up to stage 4
python scripts/run-pipeline.py --scripts-only --run-id <id> --from 5 # continue or redo from stage 5
```

There are no release-note checks, no hypotheses or change reasons, no new tests and no reviews. The verdict and
the report say what the agents would have covered and did not.

### C. One agent on its own

Each agent runs on **any run where the data it needs is there**:

```shell
python scripts/run-agent.py --run-id <id>                                # which agents can run now, and what is missing
python scripts/run-agent.py --run-id <id> --step analyst --action begin  # prints the prompt and the endCommand
#   ... give the prompt, word for word, to the agent ...
python scripts/run-agent.py --run-id <id> --action end --session <s> --guard-sha256 <h>
python scripts/run-agent.py --run-id <id> --action apply --session <s>   # scripts-only runs: hand the answer to the stages
```

The easier way is to ask the orchestrator (`agent analyst on run <id>`): it runs all of the above for you.

| Agent (`--step`) | Needs | Ready after | Its answer is used from |
|---|---|---|---|
| `release-notes` | dependency diff, library API diff | stage 3 | stage 7 |
| `analyst` | change inventory, dependency diff, API diffs, environment | stage 3 | stage 4 |
| `tests` | impact findings, reach, changed methods, test gaps | stage 5 | stage 5 |
| `tests --repair` | the above, plus behavior results and the run's tests | stage 5 with tests | stage 5 |
| `reviewer` | behavior results, the test manifest | stage 5 and the test writer | stage 9 |

What protects the run:

- The agent writes only into `runs\<id>\standalone\<session>\out\`. Any change elsewhere (artifacts, earlier
  answers, the ledger, the worktrees, the tool) rejects the call (exit 5).
- Its answer gets the same checks as in a full run (exit 6 when it fails them).
- It is not counted in `agentBudget` and not written to the ledger.
- `apply` works only on a **scripts-only** run, and only while the agent's inputs are unchanged. A full run's
  agent answers stay as its plan left them.

### D. One stage again

Stage scripts of a **full run refuse to run directly**: a direct call would bypass the order, the lock and the
agent checks. For a full run, the only way forward is the orchestrator.

For a **scripts-only** run, rerun from the stage you fixed, and every later stage that depends on it:

```shell
python scripts/run-pipeline.py --scripts-only --run-id <id> --from 4 --through 4   # only stage 4
python scripts/run-pipeline.py --scripts-only --run-id <id> --from 4               # stage 4 and everything after
```

A single `phaseN-*.py` can also run directly on a scripts-only run
(`python scripts/phase4-impact.py --run-id <id>`). Later stages are not updated then, so prefer `--from N`.

## 6. When to run what

| Situation | Run |
|---|---|
| Check an upgrade before a release | **A** full run |
| A full run was interrupted (laptop closed, Ctrl+C) | **A** `continue run <id>`: an interrupted stage is retried |
| A full run stopped | `python scripts/diagnose.py --run-id <id>`, fix the cause, then **A** a new run |
| CI: facts on every push, no AI cost | **B** scripts only |
| First try on a new project: do builds, coverage and probes work? | **B** `--through 5`, then read `diagnose.py` |
| You changed a rule in `config\rules\` | **B** `--run-id <id> --from 4` on a scripts-only run |
| You changed `config\probes.json` | **B** `--run-id <id> --from 5` |
| You changed an agent's instructions and want to see the effect | **C** that agent on an existing run, compare its answer with the run's |
| One agent's answer looks weak | **C** run it again on the same run. The answer stays in its session; for a full run, start a new run to use it |
| You want the AI part on a scripts-only run | **C** each agent in plan order (release-notes, analyst, then `--from 4`; tests, then `--from 5`; reviewer, then `--from 9`), with `apply` after each |
| The test writer's tests did not validate | **C** `tests --repair` (needs the run's tests and behavior results) |
| A stage is slow or failing and you want to see only it | **D** `--from N --through N` on a scripts-only run |

Rules of thumb:

- **Scripts first, agents second.** An agent only adds value when the stages before it produced good data. When
  a stage output looks wrong, fix and rerun the stage; do not run the agent again.
- **Agent on its own = experiment. Full run = decision.** A verdict for a release comes only from a full run.
- **Never edit a file in `runs\<id>\` by hand**, except `review\manual-decisions.json`, which a person writes.

## 7. When something goes wrong

```shell
python scripts/diagnose.py --run-id <id>              # finds the failed or stuck step
python scripts/diagnose.py --run-id <id> --stage 5    # one stage, whatever its state
python scripts/diagnose.py --run-id <id> --key agent-tests --lines 80
```

It changes nothing. It shows every step with its attempts, and for the problem step: each attempt's exit code
in words, the inputs (present or missing, and which stage writes a missing one), a hint, the end of the right
logs, an agent's check errors, and the next command. In chat: "why did run <id> fail?".

Exit codes of the stages:

| Exit | Meaning |
|---|---|
| 0 | finished (stage 10: safe, or safe with listed tests) |
| 1 | stage 10: verdict blocked. This is a result, not a failure: `verdict.json` lists each reason |
| 2 | usage, config, order problem, or missing inputs. Nothing was changed |
| 3 | tool failure (git, Maven, Java), or an error in the stage |
| 4 | stage 1: a build or an existing test failed on one branch |
| 10 | still running in the background: run the printed `waitCommand` |
| 130, 3221225786 | interrupted (Ctrl+C, closed console window). A full run retries it |

Where things are in a run folder:

```
runs\<id>\
  status.json                 the state of the stages
  artifacts\                  what each stage wrote (section 4)
  artifacts\agent\            the agents' accepted answers (full run) or applied ones (scripts-only)
  standalone\<session>\       one agent on its own: guard.json, out\, result.json
  orchestration\ledger.json   every step of a full run, with attempts and reasons
  orchestration\<key>.*.log   the runner's output of each stage
  logs\phaseN.log             each stage's own log; build-*.log, app-*.log, tests-*.log, deps-*.log
  reports\summary.md          start here: one page
  reports\report.md / .html   the full report
  verdict.json                the verdict and its blocking reasons
  worktrees\<head>\           the validated new tests, uncommitted, for a developer to review and commit
```

## 8. Command cheat sheet

| Goal | Command |
|---|---|
| Check config and tools | `python scripts/check-prereq.py` |
| Full run | `/upgradelens-orchestrator` |
| Continue a full run | `/upgradelens-orchestrator continue run <id>` |
| One agent | `/upgradelens-orchestrator agent <step> on run <id>` |
| Which agents can run | `python scripts/run-agent.py --run-id <id>` |
| Scripts only | `python scripts/run-pipeline.py --scripts-only` |
| Redo stages of a scripts-only run | `python scripts/run-pipeline.py --scripts-only --run-id <id> --from N` |
| Rebuild a scripts-only run's report | `python scripts/phase8-output.py --run-id <id> --report-only` |
| Why did it fail | `python scripts/diagnose.py --run-id <id>` |
| Tests of the tool itself | `python -m pytest tests` |
