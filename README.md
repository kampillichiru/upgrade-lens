# UpgradeLens

Checks a Spring Boot or Java upgrade by comparing a baseline branch (`master`) with a candidate branch
(`change_branch`). Read `docs/approach.md` for the full design, and `docs/architecture.svg` for the picture.

Runs on Windows with Python 3.9 or later (standard library only), from a terminal, IntelliJ, or VS Code. No Docker. Needs git, Maven
and a JDK per branch (with `java`, `javap` and `jdeps`). No local checkout of the application is needed: each
run clones the two branches from `repoUrl` into its own folder.

## Layout

```
upgrade-lens/
  config/       upgrade-lens.json (this computer's paths), rules\*.json, probes.json
  scripts/      one Python script per stage, next-step.py, run-stage.py, agent-step.py, run-pipeline.py, cleanup-run.py
  scripts/lib/  core.py, analysis.py, library.py, orchestration.py: shared, pure functions (unit tested)
  tests/        pytest tests for scripts/lib
  .github/      agents/ and skills/: every agent and skill instruction (GitHub Copilot and Claude Code)
  .github/prompts/upgradelens-orchestrator.prompt.md   /upgradelens-orchestrator in GitHub Copilot
  .claude/skills/upgradelens-orchestrator/SKILL.md     /upgradelens-orchestrator in Claude Code (points to .github)
  CLAUDE.md     tells Claude Code where the .github instructions are
  docs/         approach.md, architecture.svg
  runs/         one folder per run (git-ignored)
```

## Configuration

Every tunable value is in `config\upgrade-lens.json`. Pass another file with `--config <path>` to any script;
`run-stage.py` and `run-pipeline.py` hand the same file to every stage (and set `UPGRADELENS_CONFIG` for the agent steps).

| Key | Meaning | Default |
|---|---|---|
| `repoUrl` | repository under test: any URL or path git can clone (`githubUrl` is accepted as the old name) | required |
| `baseBranch`, `headBranch` | the two branches | required |
| `java.base`, `java.head` | JDK folder per branch | required |
| `maven.command`, `maven.settingsFile` | Maven and the `settings.xml` both builds use | `mvn`, Maven default |
| `maven.goals`, `maven.extraArgs` | build goals (must reach `package`) and extra arguments | `clean verify`, none |
| `git.command` | git | `git` |
| `runsFolder` | where run folders go | `runs` |
| `rulesDir`, `probesFile` | rule set and probe manifest | `config\rules`, `config\probes.json` |
| `build.integrationTests` | `auto`: run `*IT` classes through surefire when the pom has no failsafe plugin; `failsafe-only`: never | `auto` |
| `timeouts.buildMinutes`, `timeouts.startupSeconds` | build and application startup limits | 60, 120 |
| `dependencies.ignoreUnused` | `group:artifact` patterns that are never "unnecessary" (runtime-only libraries) | none |
| `library.enabled`, `library.maxMembersPerClass` | library API diff on or off, members listed per class | true, 40 |
| `agentBudget` | agent calls per run | 12 |
| `orchestration.maxStageAttempts` | tries of a stage that was interrupted (killed, runner died); a real failure is never retried | 3 |
| `owners.reviewer`, `owners.riskAcceptor` | owners of the manual review items | unassigned |

Check it, without creating anything: `python scripts/check-prereq.py`.

## Stages

| Stage | Script | What it does |
|---|---|---|
| 0. Setup | `phase0-setup.py` | Config validation, run folder, clone of the two branches into `runs\<id>\repo.git`, one worktree per branch (base detached, head on its branch; no checkout switching, no collisions), merge base, environment record |
| 1. Build and test | `phase1-build.py` | Maven on both branches in parallel, each with its own JDK, worktree, `target\`, settings file and log; integration tests; test-by-test comparison of the two builds. A failure on either side fails the stage |
| 2. Changed files | `phase2-changes.py` | Three-dot diff (`base...head`, from the merge base): categories, changed methods per diff hunk, signature, annotation, default and removed-branch changes, commits per file |
| 3. Dependencies and API | `phase3-deps.py` | Dependency tree diff (`-Dverbose`: conflicts and who asked for which version), `dependency:analyze`, required / unnecessary / replacement assessment per added or removed dependency, suggested commands, application API diff, library API diff |
| 4. Impact | `phase4-impact.py` | Rules, flows (endpoints, jobs, listeners), reach, JaCoCo coverage, untraced code, changed methods with their reason and coverage on both branches |
| 5. Behavior | `phase5-behavior.py` | Startup smoke check, bean and property dumps, probes on both branches, proposed tests on both branches with injected regressions |
| 6. Flow selection | `phase6-flows.py` | Scope per flow: full, targeted, or smoke |
| 7. Manual review items | `phase7-manual.py` | Questions for a person, with the expected master result and an owner |
| 8. Output | `phase8-output.py` | Validated new tests copied, uncommitted, into the change worktree (plus `artifacts\new-tests.patch`) with the commands to commit them; `findings.json`, `report.md`, `report.html` |
| 9. Review | `phase9-review.py` | Agent review and manual decisions, checked by file hash and by author |
| 10. Verdict | `phase10-verdict.py` | Safe, safe with listed tests, or blocked, with exit code 0 or 1 |

### Library API diff (stage 3)

Uses only JDK tools. `jdeps` lists every library class the compiled main and test code refers to; `javap -c`
lists every library method and field it calls, with the source line. Each referenced class is found in each
branch's resolved classpath, and its public API (`javap -public -s`, with JVM descriptors) is compared. A class
that disappeared is matched by simple name to its new package (for example Jackson 2 to 3). Each change is tied
to the code lines that use it: `artifacts\library-api-diff.json`, report section 7, and rules
`R-LIB-API-REMOVED-USED` and `R-LIB-API-CHANGED-USED`.

The release-notes agent then checks each high or medium item, and each direct dependency that changed by a
major version, against the library's own release notes or migration guide.

### The report

Start with `runs\<id>\reports\summary.md` (and `.html`): one page that answers what changed, whether the existing
tests ran it on both branches, which new tests were written and their result on both branches, every difference found,
what was not checked, and a run trace (each step, its attempts, duration, result, and the log to open when it failed).
The detailed report below is the evidence behind it.

`runs\<id>\reports\report.md` (and `.html`) is one document: verdict; run, configuration and workspace; builds
and the test comparison; changed files; changed classes and methods with their reason and coverage;
dependencies (assessment, conflicts, suggestions); library API changes and release-notes results; impact
findings; flows; new tests and why each was added; manual decisions; agents; gaps; evidence. A failed stage
still produces the report, with the failure and the sections that could not be built.

## Run

Configure `config\upgrade-lens.json`, then start the **upgradelens-orchestrator** agent and ask it to run the
check:

- GitHub Copilot Chat (VS Code): type `/upgradelens-orchestrator`, or pick `upgradelens-orchestrator` in the agent dropdown
  (`.github\agents\upgradelens-orchestrator.agent.md`), then "run the UpgradeLens check".
- Claude Code: type `/upgradelens-orchestrator` (add `continue run <run id>` to resume). It points to the same `.github` orchestrator; each agent step runs as a general-purpose subagent with the printed prompt, which names its `.github\agents\` file.

The orchestrator does not choose the order. It loops on one script:

```shell
python scripts/next-step.py [--run-id <id>]     # prints one JSON action: run-stage, wait, run-agent, end-agent, stop, done
```

and runs exactly the commands it prints:

| Script | Role |
|---|---|
| `run-stage.py --new` / `--run-id <id> --key <key>` / `--wait` | the only entry to a stage: order check, one-stage lock, detached process (exit 10 = still running), failure report, ledger |
| `agent-step.py --key <key> --action begin` | order and budget check, fingerprint of everything the agent must not touch, the exact subagent prompt |
| `agent-step.py --key <key> --action end` | write-scope check (stops the run), output checks (one retry with the errors), ledger |

The fixed plan (`scripts\lib\orchestration.py`, `run_plan`):

```
stage 0, 1, 2, 3 -> release-notes agent -> stage 4 -> analyst agent -> stage 4 (merge)
-> stage 5 (probes) -> test writer (every gap in artifacts\test-gaps.json) -> stage 5 (tests)
-> [repair round: test writer, stage 5] -> stage 6, 7, 8 -> reviewer agent -> stage 9, 10
```

A step that is not needed is recorded as skipped with its reason. Every step is in
`runs\<id>\orchestration\ledger.json`.

Guardrails, enforced by the scripts (not by the agent's good behavior):

- A stage of an orchestrated run refuses to run directly (`phase*.py`), out of order, or while another stage
  runs. `run-pipeline.py` refuses without `--scripts-only` (scripts-only runs, no agents, for CI facts).
- An agent may write only its own files under `artifacts\agent`. Any change to the run folder, a worktree's
  git state, or the tool's scripts, config, agents or skills stops the run.
- Agent output is rejected (one retry) when: the input hash is stale; a changed main-code method or type has no
  reason and consequence; a rule behind a high or medium finding has no plain explanation, example and
  production risk; a gap has neither a test nor a reason; a test file, class name or injection does not match;
  a confirmed release note has no https source or quote; a validated test has no review.
- A test that passes on master and fails on change_branch is kept as proof of a behavior change (it blocks the
  verdict until a person accepts it); only tests that fail on master or miss an injection are repaired.
- Budget: each agent call counts against `agentBudget`. Limits: `orchestration.maxAgentAttempts` (2),
  `orchestration.maxTestRepairRounds` (1), `orchestration.maxTests` (15).

Stage exit codes: `0` safe or safe with listed tests, `1` blocked (see `runs\<id>\verdict.json`), `2` config or
usage, `3` tool failure, `4` build or tests failed (stage 1).

## Agents and skills

- `upgradelens-orchestrator` (skill `upgradelens-run`): the loop above. Never edits files.
- `upgradelens-release-notes`: validates library changes against the release notes and migration guides.
- `upgradelens-analyst`: hypotheses, a plain explanation with an example and the production risk for each
  impact rule, and the reason and consequence of each changed method (report sections 8a and 5).
- `upgradelens-test-writer` with the skill `upgradelens-missing-tests`: a characterization test, pinned to
  master with an injected regression, for every gap (untested change, untraced flow, unverified impact), or a
  reason why none is possible.
- `upgradelens-test-reviewer`: reviews the tests in a fresh context; writes only its verdict file.

Every instruction lives in `.github\agents\` and `.github\skills\`. The single file under `.claude\` is only the Claude Code slash-command entry point. GitHub Copilot loads these
files directly; Claude Code reaches them through `CLAUDE.md`. The scripts enforce the order, budget and write scope the
same way for both.

## Workspace and new tests

Each run is self-contained under `runs\<id>\`:

```
repo.git\                 bare clone of the two branches (from repoUrl)
worktrees\<base branch>\  baseline, detached, read only
worktrees\<head branch>\  change side, on the local branch <head branch> tracking origin
artifacts\agent\tests\    new tests as the test writer wrote them
```

New tests are written in the run folder (`artifacts\agent\tests`), run on both branches by stage 5, and only
when validated copied by stage 8 into the change worktree, uncommitted. Report section 10 lists each new test,
why it was added, and the commands to commit it. The automation never stages, commits or pushes; a developer
reviews the files in the change worktree, then:

```shell
cd runs\<id>\worktrees\<head branch>
git add <the new test files>
git commit -m "Add characterization tests from UpgradeLens run <id>"
git push
```

Or apply `runs\<id>\artifacts\new-tests.patch` to another checkout with `git apply`.

When done: `python scripts/cleanup-run.py --run-id <id>` (or `--all`) removes the run's clone and worktrees. It
refuses while the change worktree has uncommitted or unpushed work, unless `--force`.

## Decisions by a person

Create `runs\<run id>\review\manual-decisions.json` after stage 7 (the items are in `artifacts\manual-items.json`):

```json
{ "decisions": [ { "itemId": "MI-001", "decision": "accept", "reviewer": "name", "note": "why" } ] }
```

`decision` is `accept`, `reject`, `needs-test` or `defer`. Stage 9 rejects entries with an unknown item, an
unknown decision, or no reviewer. Stage 10 then unblocks an accepted confirmed change.

## Unit tests

```shell
python -m pip install pytest   # once
python -m pytest tests
```

They cover the parsers (dependency trees, verbose conflicts, javap and jdeps output, surefire reports), the
hunk-to-method mapping, the dependency assessment, the library API comparison, rule matching, flow reach,
coverage, the flow scope rule and the verdict rule. They run without Maven, a server, or git.

## Not built, or built only as a gap

Listed in the verdict, never a silent pass:

- Testcontainers integration tests: no Docker on this machine; the method is still to be decided.
- Mutation testing (PIT): only when main code changed; the step is not wired yet.
- Per-test coverage: coverage is per symbol, not per test.
- Resume with fingerprints (approach section 11): `--from` reruns from a stage, without fingerprints.
