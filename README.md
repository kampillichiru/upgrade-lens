# UpgradeLens

Checks a Spring Boot or Java upgrade before release by comparing a base branch (`master`) with a head branch
(`change_branch`): it builds and tests both, finds what changed and what it can break, writes and validates new
tests for what no test covers, and gives a verdict with a report.

Everything is in `.github`. It runs from a terminal, or from GitHub Copilot Chat in **VS Code or IntelliJ**: one
Copilot agent per phase. Python does all the mechanical work and decides the order; the agents run their phase, do
the judgment jobs (explain changes, check release notes, write and review tests) and explain the result.

Windows, Python 3.9 or later (standard library only), git, Maven, and a JDK per branch (with `java`, `javap`,
`jdeps`). No Docker. No local checkout of the application: each run clones the two branches from `repoUrl`.

Read [how it works](.github/upgradelens/docs/how-it-works.md) for the design, the guardrails, the logs and
troubleshooting, and [step by step](.github/upgradelens/docs/step-by-step.md) to run a check.

## The 7 phases

| Phase | Command | Copilot agent | Jobs (each one script or one agent) |
|---|---|---|---|
| 00-setup | `ul.py setup` | `ul-00-setup` | setup: config check, clone, one worktree per branch, merge base |
| 01-build | `ul.py build` | `ul-01-build` | build: Maven build and tests on both branches, JaCoCo coverage |
| 02-impact | `ul.py impact` | `ul-02-impact` | changes, deps, rules |
| 03-analysis | `ul.py analysis` | `ul-03-analysis` | **release-notes** (agent), **analyst** (agent), merge |
| 04-tests | `ul.py tests` | `ul-04-tests` | probes, gaps, **write** (agent), validate, **repair** (agent), revalidate |
| 05-review | `ul.py review` | `ul-05-review` | **review** (agent) |
| 06-report | `ul.py report` | `ul-06-report` | flows, manual, report, record, verdict |

`ul.py` is `python .github/upgradelens/ul.py`, run from the workspace root. Each phase runs on its own whenever its
inputs are there, on the current run or any run (`--run <id>`). `ul.py needs <phase>` lists its inputs and which
phase writes each one.

## Run

**End to end, from Copilot Chat:** pick the agent `ul-run` and say "run" (or "new run", "continue run <id>"), or
type `/upgradelens`. It loops `ul.py continue`, which always runs the next phase that is not done, and hands each
agent job to its phase agent (`ul-03-analysis`, `ul-04-tests`, `ul-05-review`) as a subagent. It ends with the
verdict. Python decides the order; `ul-run` only reacts to exit codes.

**One phase at a time:** pick that phase's agent (`ul-00-setup` ... `ul-06-report`) and say "run". Each one ends
with the next one to run.

From a terminal:

```powershell
python .github/upgradelens/ul.py doctor            # config and prerequisites, creates nothing
python .github/upgradelens/ul.py setup             # a new run; it becomes the current run
python .github/upgradelens/ul.py build
python .github/upgradelens/ul.py impact
python .github/upgradelens/ul.py analysis          # exit 20: an agent job waits (brief path printed)
python .github/upgradelens/ul.py tests
python .github/upgradelens/ul.py review
python .github/upgradelens/ul.py report            # exit 0 safe / safe with listed tests, 1 blocked
python .github/upgradelens/ul.py continue          # or: the next phase that is not done, whatever it is
python .github/upgradelens/ul.py all --no-agents   # every phase, scripts only (CI, quick facts)
```

Exit codes: 0 done; 1 verdict blocked (a result); 2 inputs, config or usage; 3 tool failure; 4 build failed;
5 agent wrote outside its folder; 6 agent answer failed its checks; 10 still running in the background (`ul.py wait`);
20 an agent job waits (read the brief, write the answer, run the same command again).

## When something is wrong

| Question | Command |
|---|---|
| Is my setup OK? | `ul.py doctor` |
| Where is the run, what is next? | `ul.py status` |
| Why did a phase fail, wait, go stale or score amber/red? | `ul.py why <phase>` |
| What does a phase need, and who writes it? | `ul.py needs <phase>` |
| Where are results lost (weak quality)? | `ul.py funnel` |

Every phase writes `runs\<id>\<phase>\log.txt` (what happened and why), `quality.json` (green / amber / red with the
numbers), `tools\<job>.log` (raw tool output, secrets masked) and, for agent jobs, `<job>\brief.md`,
`<job>\out\` and `<job>\checks.json`. `runs\<id>\run.log` is the timeline of the whole run.

## Layout

```
.github/
  agents/ul-run.agent.md                                   the conductor: end to end, phase agents as subagents
  agents/ul-00-setup.agent.md ... ul-06-report.agent.md   one Copilot agent per phase
  prompts/upgradelens.prompt.md                            /upgradelens: starts ul-run
  copilot-instructions.md                                  what UpgradeLens is, for every Copilot chat
  upgradelens/
    ul.py                 the one command
    scripts/runner/       phases, jobs, guardrails, logging, quality, status and why
    scripts/phase*.py     one stage script per mechanical step (git, Maven, javap, probes, report, verdict)
    scripts/lib/          shared functions (unit tested), stage and agent definitions, agent checks, guards
    jobs/*.md             the instructions of each agent job (analyst, release-notes, test-writer, test-reviewer)
    config/               upgrade-lens.json (this computer's paths), rules\*.json, probes.json
    docs/                 how-it-works.md, step-by-step.md, commands.md, approach.md, architecture.svg
    tests/                pytest tests
runs/                     one folder per run (git-ignored), outside .github
```

## Configuration

Every tunable value is in `.github\upgradelens\config\upgrade-lens.json`. Pass another file with `--config <path>`;
the run remembers the file it was set up with.

| Key | Meaning | Default |
|---|---|---|
| `repoUrl` | repository under test: any URL or path git can clone | required |
| `baseBranch`, `headBranch` | the two branches | required |
| `java.base`, `java.head` | JDK folder per branch | required |
| `maven.command`, `maven.settingsFile` | Maven and the `settings.xml` both builds use | `mvn`, Maven default |
| `maven.goals`, `maven.extraArgs` | build goals (must reach `package`, and run JaCoCo) and extra arguments | `clean verify`, none |
| `git.command` | git | `git` |
| `runsFolder` | where run folders go, relative to the workspace root | `runs` |
| `rulesDir`, `probesFile` | rule set and probe manifest, relative to `.github\upgradelens` | `config\rules`, `config\probes.json` |
| `build.integrationTests` | `auto`: run `*IT` classes through surefire when the pom has no failsafe plugin; `failsafe-only`: never | `auto` |
| `timeouts.buildMinutes`, `timeouts.startupSeconds` | build and application startup limits | 60, 120 |
| `dependencies.ignoreUnused` | `group:artifact` patterns that are never "unnecessary" | none |
| `library.enabled`, `library.maxMembersPerClass` | library API diff on or off, members listed per class | true, 40 |
| `agentBudget` | agent calls per run (every attempt counts) | 12 |
| `orchestration.maxAgentAttempts` | attempts of one agent job (a rejected answer gets a new brief with the problems) | 2 |
| `orchestration.maxTestRepairRounds` | repair round for tests that did not validate (0 or 1) | 1 |
| `orchestration.maxTests` | new tests per run | 15 |
| `owners.reviewer`, `owners.riskAcceptor` | owners of the manual review items | unassigned |

## The report

Start with `runs\<id>\reports\summary.md` (and `.html`): what changed, whether the existing tests ran it on both
branches, which new tests were written and their result on both branches, every difference found, what was not
checked, and a run trace (each job, its result, and the file to open when it failed). `reports\report.md` (and
`.html`) is the evidence behind it.

## New tests

New tests are written in the run (`04-tests\write\out\tests`, published to `artifacts\agent\tests`), run on both
branches with injected regressions, and only when validated copied by 06-report into the head worktree,
uncommitted. The automation never stages, commits or pushes; a developer reviews them, then:

```shell
cd runs\<id>\worktrees\<head branch>
git add <the new test files>
git commit -m "Add characterization tests from UpgradeLens run <id>"
git push
```

When done: `python .github/upgradelens/scripts/cleanup-run.py --run-id <id>` removes the run's clone and
worktrees (it refuses while the head worktree has uncommitted or unpushed work, unless `--force`).

## Decisions by a person

After 06-report lists the manual items (`artifacts\manual-items.json`), their owner writes
`runs\<id>\review\manual-decisions.json`, then runs `ul.py report` again:

```json
{ "decisions": [ { "itemId": "MI-001", "decision": "accept", "reviewer": "name", "note": "why" } ] }
```

`decision` is `accept`, `reject`, `needs-test` or `defer`.

## Unit tests

```shell
python -m pip install pytest pyflakes   # once
python -m pytest .github/upgradelens/tests
```

## Not built, or built only as a gap

Listed in the verdict, never a silent pass: Testcontainers integration tests (no Docker), mutation testing (PIT),
per-test coverage.
