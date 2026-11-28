# The commands UpgradeLens runs, and why

This page lists every external command (git, Maven, Java tools, HTTP calls) that each stage runs, in order,
with what it does and why the stage needs it. Read it to understand a log, to reproduce one step by hand, or to
check that the tool never changes your repository.

See [how-it-works.md](how-it-works.md) for the stages themselves and how to run them, and
[step-by-step.md](step-by-step.md) to run a check phase by phase and check each phase's output.

## Before you start: the names used on this page

| Name | Meaning | Example |
|---|---|---|
| `R` | the run folder, `runs\<run id>\` | `runs\20261127-101433\` |
| `<repoUrl>` | `repoUrl` in `config\upgrade-lens.json` | |
| base branch | `baseBranch`, the branch before the upgrade | `main` |
| head branch | `headBranch`, the branch with the upgrade | `change_branch` |
| `<base>` | the commit of the base branch | `67f9627` |
| `<head>` | the commit of the head branch | `e99246b` |
| `<mergebase>` | the commit where the head branch left the base branch | `67f9627` in this example |
| `<worktree>` | `R\worktrees\main` or `R\worktrees\change_branch` | |

**Why the merge base matters.** Every diff compares `<mergebase>` with `<head>` (a "three-dot" diff,
`main...change_branch`). So a commit someone pushed to `main` after the branch was made is **not** reported as a
change of `change_branch`. In the example the head branch starts at the tip of `main`, so `<mergebase>` and
`<base>` are the same commit (`67f9627`).

**Safe by design.**
- Your own clone is never touched. Stage 0 makes a fresh copy inside the run folder.
- Nothing is ever committed or pushed. The only writes into a worktree are the `target\` folders (builds) and
  test files that stage 5 copies in and removes again.
- Every command and its full output go to `R\logs\` (or `R\standalone-stages\logs\` with `--standalone`), for
  example `phase3.log` and `deps-tree-head.log`. If a step fails, the exact command is in the log.

## Overview

| Stage | Script | Tools it uses | In one line |
|---|---|---|---|
| 0 setup | `ul.py setup` | git, java, mvn | copy the repository and check out both branches |
| 1 build | `phase1-build.py` | mvn | build and test both branches, at the same time |
| 2 changes | `phase2-changes.py` | git | what files, lines and methods changed |
| 3 dependencies and API | `phase3-deps.py` | mvn, javap, jdeps | what libraries and public APIs changed |
| 4 impact | `phase4-impact.py` | git | what the changes can break, and what tests cover them |
| 5 behavior | `phase5-behavior.py` | java, HTTP, jcmd, mvn, git | run both apps and compare their answers |
| 6 flows | `phase6-flows.py` | none | decide the test scope of each flow |
| 7 manual items | `phase7-manual.py` | none | list what a person must check |
| 8 report | `phase8-output.py` | none | write the report |
| 9 review | `phase9-review.py` | none | record the review decisions |
| 10 verdict | `phase10-verdict.py` | none | safe, safe with listed tests, or blocked |

---

## Stage 0: setup (`ul.py setup`)

Goal: a private copy of the repository with both branches checked out side by side, and a record of the tools.

| # | Command | What it does | Why |
|---|---|---|---|
| 1 | `git ls-remote --heads <repoUrl>` | lists the branches on the server, without downloading anything | checks that the URL works and that both branches exist, before any time is spent |
| 2 | `git init --bare R\repo.git` | creates an empty repository with no files checked out | one shared store of commits for both branches |
| 3 | `git -C R\repo.git remote add origin <repoUrl>` | points that repository at your server | so it can fetch from it |
| 4 | `git -C R\repo.git fetch --no-tags origin main change_branch` | downloads only the two branches, without tags | faster than a full clone |
| 5 | `git -C R\repo.git worktree add --detach R\worktrees\main <base>` | checks out the base commit into its own folder | the base side is read only, so it is a fixed commit, not a branch |
| 6 | `git -C R\repo.git branch --track change_branch origin/change_branch` | creates a local head branch that follows the server's | so a developer can later commit the new tests on it and push |
| 7 | `git -C R\repo.git worktree add R\worktrees\change_branch change_branch` | checks out the head branch into its own folder | both branches exist side by side, so they can build at the same time |
| 8 | `git -C R\repo.git merge-base <base> <head>` | finds the commit where the head branch started | every later diff starts from it (see above) |
| 9 | `git -C R\repo.git rev-list --count <mergebase>..<base>` | counts the commits on `main` since that point | tells you if `main` moved on; those commits are not part of the check |
| 10 | `java -version`, `mvn -v`, `git --version` | prints the tool versions | recorded in `artifacts\environment.json`, so a result can be explained later |

Result: `R\repo.git`, `R\worktrees\main\`, `R\worktrees\change_branch\`, `status.json`,
`artifacts\environment.json`.

---

## Stage 1: build (`phase1-build.py`)

Goal: compiled classes, jars, test results and coverage for both branches.

Both builds run **in parallel**, each with **its own JDK** (`java.base` and `java.head` in the config), because
an upgrade often changes the Java version.

```shell
mvn -B -ntp -Dmaven.gitcommitid.nativegit=true -s <settings> -f R\worktrees\main\pom.xml          -Dmaven.test.failure.ignore=true clean verify
mvn -B -ntp -Dmaven.gitcommitid.nativegit=true -s <settings> -f R\worktrees\change_branch\pom.xml -Dmaven.test.failure.ignore=true clean verify
```

| Part | Meaning |
|---|---|
| `-B` | batch mode: no prompts, no colors, clean log |
| `-ntp` | no transfer progress: the log is not filled with download lines |
| `-Dmaven.gitcommitid.nativegit=true` | git-commit-id-plugin reads Git with the git executable; its built-in JGit reader cannot resolve worktrees of a bare repository. Ignored when the project does not use the plugin |
| `-s <settings>` | your Maven settings file (mirrors, credentials), only if configured |
| `-f <pom>` | which branch's project to build |
| `-Dmaven.test.failure.ignore=true` | a failing test does not stop the build. Test failures are a result to compare, not an error |
| `clean verify` | compile, run the tests, build the jar, write the JaCoCo coverage report |

Then the script compares every test case between the branches: same, new, removed, now failing, now passing.

Result: `artifacts\build-results.json`, `artifacts\test-results.json`, `logs\build-<branch>.log`, and in each
worktree `target\classes`, `target\<app>.jar`, `target\site\jacoco\jacoco.xml`.

A **compile** failure stops the run (exit 4). A **test** failure does not.

---

## Stage 2: changes (`phase2-changes.py`)

Goal: exactly what changed, down to the method. Git only, no build needed.

| Command | What it gives |
|---|---|
| `git -C R\repo.git diff --name-status -M <mergebase> <head>` | each changed file: added, modified, deleted or renamed (`-M` finds renames) |
| `git -C R\repo.git diff -U0 -M <mergebase> <head>` | the changed lines, with no context lines (`-U0`), so each line number is exact |
| `git -C R\repo.git diff --numstat -M -w --ignore-blank-lines <mergebase> <head>` | the same diff, ignoring spaces and blank lines. A file that changes here by zero lines is **formatting only** |
| `git -C R\repo.git log --no-merges --name-only <mergebase>..<head>` | the commits of the head branch, with the files each one touched |
| `git -C R\repo.git show <commit>:<file>` | the full file at one commit, to find the method each changed line belongs to |

Then, in Python: each file gets a category (main code, test, build, config, formatting only), each changed line
is mapped to its method, and the test files are checked for weakened assertions and removed or disabled tests.

Result: `artifacts\change-inventory.json`, `reports\change-inventory.md`.

---

## Stage 3: dependencies and API (`phase3-deps.py`)

Goal: which libraries changed, and which public classes and methods changed, both in your code and in the
libraries your code calls.

### Maven, on each branch

| Command | What it gives |
|---|---|
| `mvn -f <worktree>\pom.xml dependency:tree -Dverbose -DoutputFile=...` | the full dependency tree, with the versions Maven left out (`-Dverbose`) |
| `mvn -f <worktree>\pom.xml dependency:analyze` | declared but unused libraries, and used but undeclared ones |
| `mvn -f <worktree>\pom.xml dependency:build-classpath -Dmdep.outputFile=...` | the exact list of jars, needed to find the library classes below |

The two trees are compared: added, removed, version changes, conflicts. Each change is marked required,
replacement, unnecessary, or needs review.

### JDK tools

All four run with the **head branch's JDK** (JDK 25 in the example) on both branches, so both sides are read the
same way and the newer class files can be read.

| Command | Reads | What it gives |
|---|---|---|
| `javap -public -cp target\classes <class>` | your compiled classes | the public API of your code, to compare between branches |
| `jdeps -verbose:class target\classes` | your compiled classes | every library class your code refers to |
| `javap -c -p -l <classes>` | your compiled classes | every library method and field your code calls, with the source line |
| `javap -public -s <library classes>` | the library jars | the public API of each library class you use, with exact signatures, so overloads compare correctly |

Result: `artifacts\dependency-diff.json`, `api-diff.json`, `library-api-diff.json`, `deps-*.txt`,
`classpath-*.txt`, and `logs\deps-tree-*.log`, `deps-analyze-*.log`, `deps-classpath-*.log`.

---

## Stage 4: impact (`phase4-impact.py`)

Goal: what the changes can affect, and whether a test covers it.

| Command | Why |
|---|---|
| `git -C R\repo.git diff -U0 -M <mergebase> <head>` | the changed lines again, as evidence for each finding |
| `git -C R\repo.git show <mergebase>:<file>` | the old version of a changed file, to compare with the new one |

It also reads, without running anything:
- `target\site\jacoco\jacoco.xml` on both branches: which lines the tests run (coverage).
- the rules in `config\rules\`: known upgrade pitfalls to look for.

Everything else runs in Python: apply the rules, find the flows (endpoints, scheduled jobs, listeners) and the
code each one reaches, mark reached code that no test runs as **untraced**, and list each changed method with
its reason and coverage.

Result: `artifacts\impact-findings.json`, `reach.json`, `changed-methods.json`.

---

## Stage 5: behavior (`phase5-behavior.py`)

Goal: run both versions of the app for real, and compare how they behave.

### Run the app, on each branch

| Step | What it does | Why |
|---|---|---|
| `java -jar target\<app>.jar --server.port=<free port>` | starts the app on a free port | the two apps never clash, and nothing already running is disturbed |
| `GET /actuator/health` | waits until the app is up | |
| `GET /actuator/beans` | lists every Spring bean | a bean that appears or disappears after an upgrade is a classic silent change |
| `GET /actuator/env` | lists every property and its value | catches renamed or changed defaults |
| each request in `config\probes.json` | sends your sample requests | the two branches' answers (status, body) are compared |
| `jcmd <pid> GC.heap_info` | reads the heap use | records each branch's memory use, so the two can be compared |

The app is stopped afterwards.

### Run the proposed tests, on each branch

| Command | Why |
|---|---|
| `mvn -f <worktree>\pom.xml test -Dtest=<classes>` | runs only the new tests from the test writer, on both branches, to check they pass and that they catch a planted regression |
| `git -C <worktree> status --porcelain` and `git checkout -- <file>` | cleanup: puts the worktree back exactly as it was |

Result: `artifacts\behavior-results.json`, `gaps.json`, `logs\app-*.log`, `logs\tests-*.log`.

---

## Stages 6, 7, 9 and 10: no external commands

These stages only read and write JSON files in the run folder.

| Stage | What it decides | Writes |
|---|---|---|
| 6 flows | the test scope of each flow: full, targeted or smoke | `artifacts\flows.json` |
| 7 manual items | what tests and probes cannot decide, each with a question and an owner | `artifacts\manual-items.json` |
| 9 review | applies and checks the reviewer's and the people's decisions | `review\review-record.json` |
| 10 verdict | **safe**, **safe with listed tests**, or **blocked** (exit code 1, with reasons) | `verdict.json` |

## Stage 8: report

Builds the report from the artifacts of the earlier stages. It runs no git and no Maven.

Result: `reports\findings.json`, `report.md`, `report.html`, `summary.md`, `summary.html`. Start with
`summary.md`.

---

## Reproducing one step by hand

1. Open the stage's log, for example `R\logs\phase3.log`, and copy the exact command line.
2. Run it from the same folder. For the JDK tools, use the JDK the log names.
3. Compare with the output file the log points to.

To rerun a whole stage without touching the run's report, use standalone mode:

```shell
python .github/upgradelens/scripts/phaseN-<name>.py --run-id <id> --standalone
```

Its output goes to `R\standalone-stages\`.
