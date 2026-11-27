# Running a check, phase by phase

Each phase is one command and one Copilot agent. Run them in order, check each one's output, and only then go on.
A phase can be run again at any time; the phases after it then show as stale until you run them again.

`ul.py` below means `python .github/upgradelens/ul.py`, run from the workspace root. `R` is the run folder
`runs\<run id>\`. For the design and the guardrails, see [how-it-works.md](how-it-works.md).

## End to end in one go

Pick the agent `ul-run` in Copilot Chat (agent mode) and say "run" ("new run", "continue run <id>", "scripts
only"), or type `/upgradelens` in VS Code. It repeats `ul.py continue` (the next phase that is not done), waits for
long phases, and starts `ul-03-analysis`, `ul-04-tests` and `ul-05-review` as subagents when their agent jobs wait.
It stops at the verdict, or at the first failure with `ul.py why`. Use the phase-by-phase way below when you want to
check each phase before the next.

## From Copilot Chat, phase by phase (VS Code or IntelliJ)

1. Open Copilot Chat in agent mode and pick the agent `ul-00-setup`. Say "run".
2. When it finishes, it names the next agent. Pick it and say "run". Repeat up to `ul-06-report`.
3. For a run other than the current one, say "run on run <run id>".
4. `python .github/upgradelens/ul.py status` shows where the current run is and which agent is next.

The agents of 03-analysis, 04-tests and 05-review do the judgment jobs in the same chat: the runner hands them a
brief, they write the answer, and the runner checks it. You can read every brief and answer in the run folder.

## From a terminal

Same commands, without an agent. Agent jobs stop the phase with exit 20 and print the brief path: give the brief
to any agent (or do the job yourself), then run the same command again. Use `--no-agents` to skip them.

## Before the first run

```powershell
ul.py doctor        # every line must be OK; it names the config key or tool to fix
```

The config is `.github\upgradelens\config\upgrade-lens.json` (branches, JDKs, Maven settings).

## 00-setup

```powershell
ul.py setup
```

Check: the run id it prints (it is now the current run), `R\artifacts\environment.json` (`branches.base.name`,
`head.name`, commits, JDKs), and `R\worktrees\` with one folder per branch. Quality amber "the base branch moved on"
means commits after the merge base are not part of the check.

## 01-build

```powershell
ul.py build         # long: exit 10 means still running; then ul.py wait
```

Check: quality green. `R\artifacts\test-results.json` `summary`: `tests` above 0, `nowFailing` and `removed` 0 or
known. Red "no JaCoCo report" means the project's build goals must run the `jacoco-maven-plugin`. On a failed build:
`ul.py why build` shows the Maven error and the log of each branch.

## 02-impact

```powershell
ul.py impact
```

Check: the high and medium findings make sense for this upgrade (`R\artifacts\impact-findings.json`); a rule
flagged "fired on N of M changed files" is likely too broad (`config\rules\`). `changed-methods.json` `untested` are
the changed methods no test runs: they become gaps for the test writer. `reach.json` `flows` above 0.

## 03-analysis (agent jobs)

```powershell
ul.py analysis      # exit 20: the release-notes or analyst job waits for the agent
```

Check `R\artifacts\agent\hypotheses.json`: each changed method has a reason that reads right, and each high or
medium finding has a plain explanation. The quality score flags a high share of "not determined". Release notes:
each confirmed item has a source link; open a couple.

## 04-tests (agent jobs)

```powershell
ul.py tests         # probes, gaps, then exit 20 for the test writer; validation runs Maven (exit 10, ul.py wait)
```

Check: `R\artifacts\behavior-results.json` `startupOk` true and every `different` probe explained; the gap list
`R\artifacts\test-gaps.json`; every gap covered by a test or listed as `uncovered` with a reason; most tests
`validated`. A test that passes on base and fails on head proves a behavior change: it is evidence, not a test bug.
`ul.py funnel` shows where gaps were lost.

## 05-review (agent job)

```powershell
ul.py review
```

Check `R\artifacts\agent\review.json`: one verdict per validated test, each fail with its reason.

## 06-report

Before it, the owners of the manual items (from a previous report run) may write `R\review\manual-decisions.json`.

```powershell
ul.py report        # exit 0 safe or safe with listed tests; exit 1 blocked (a result)
```

Read `R\reports\summary.md` first, then `report.md`. The validated tests are in the head worktree, uncommitted:
`git -C R\worktrees\<head branch> status` lists exactly the files in `R\artifacts\new-tests.json`.

## If a phase looks wrong

1. `ul.py why <phase>`: the cause, the raw output, the stage's hint, the agent's rejected checks, what is stale.
2. Fix the cause (the config, a rule, a job's instructions), then run that phase again. Its previous attempt is
   kept in `R\<phase>\history\<n>\` to compare.
3. Run the later phases again: `ul.py status` shows them as stale.

| Symptom | Where to fix it |
|---|---|
| wrong branch, JDK, Maven settings | `config\upgrade-lens.json` (`baseBranch`, `headBranch`, `java.*`, `maven.*`) |
| build fails on one branch | the project itself, or `maven.goals` |
| no JaCoCo report | the project's pom must run `jacoco-maven-plugin` in the build goals |
| a rule fires wrongly or not at all | `config\rules\` |
| a probe is wrong or missing | `config\probes.json` |
| an agent's answer is weak | its job file in `.github\upgradelens\jobs\`; rerun the phase with `--job <job> --force` |

## Cheat sheet

| Goal | Command |
|---|---|
| Where am I, what is next | `ul.py status` |
| Run a phase | `ul.py <setup\|build\|impact\|analysis\|tests\|review\|report>` |
| One job of a phase | `ul.py <phase> --job <job>` (`--force` to ask the agent again) |
| Another run | add `--run <id>`; `ul.py runs`; `ul.py use <id>` |
| Wait for a background phase | `ul.py wait --follow` |
| Why did it fail / look weak | `ul.py why <phase>`, `ul.py funnel` |
| What does a phase need | `ul.py needs <phase>` |
| Scripts only, every phase | `ul.py all --no-agents` |
