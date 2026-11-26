---
description: Assess whether the branch-comparison automation solves the problem statement
argument-hint: [optional: path to the automation's config file]
---

You are assessing an existing automation against a written problem statement.

Read and run only. Do not modify files, do not run `git checkout` in the user's repo, do not commit, and do not push. Any clones, worktrees, or outputs go under `runs/`.

Config path (if given): $ARGUMENTS

## Problem statement

1. **Configuration file.** Branches, JDK path, Maven `settings.xml`, and every other tunable value are parameterized, not hardcoded.
2. **Workspace setup.** The run creates a new directory and checks out `master` and `change_branch` in it, with no collisions between the two checkouts. Worktrees or separate clones are both acceptable. A single checkout that switches branches is not.
3. **Maven builds.** Both branches are built with the config's JDK and `settings.xml`. The builds may run in parallel, but each needs its own directory and its own `target/`. A failure on either side is reported, not swallowed.
4. **Git diff.** Changed files between the two branches are found with a three-dot diff (`master...change_branch`).
5. **Dependencies in `pom.xml`.** Added and removed dependencies are identified, and the analysis covers:
    - potential impact
    - whether each addition is required
    - whether any addition is unnecessary
    - overlaps and version conflicts (e.g. `mvn dependency:tree -Dverbose`)
    - suggestions for deeper analysis (e.g. `dependency:analyze`)
6. **Class and method changes.** For each changed class:
    - changed methods are identified by mapping diff hunks to methods, not just files
    - the reason for the change is explained
    - coverage is checked with JUnit and integration tests
    - missing tests are written following the existing test pattern, and run on both branches
    - results are compared, and differences are reported
    - new tests are kept on the change side for review and are not committed
    - the report lists each new test and why it was added
7. **Library changes.** Method and signature changes in official library upgrades are identified, the reason is validated against release notes, and the impact on the code is assessed.
8. **Report.** A single report covers requirements 1–7, including the new tests and the reasons for them.

## Procedure

1. **Locate the automation.** Find the entry point, config files, pipeline modules, and existing tests. List the paths you found.
2. **Check each requirement statically.** For requirements 1–8, record:
    - **Implemented:** the file and line where it is handled, and how it works.
    - **Evidence:** what you read, or what you ran.
3. **Run what is safe to run.** Run the automation's own tests. If a dry run or a sample Java repo run is possible without changing the user's repo, do it. If a step cannot be run, mark it UNVERIFIED and say why.
4. **Assign a verdict** to each requirement:
    - **SOLVED:** implemented and verified by code or a run.
    - **PARTLY SOLVED:** implemented but with gaps, or verified only in part.
    - **MISSING:** not implemented, or implemented in a way that cannot satisfy the requirement.
    - **UNVERIFIED:** implemented as far as the code shows, but not run.
5. **Propose fixes.** For each requirement that is not SOLVED, give the smallest change that would close the gap.

## Output format

Respond in markdown with these sections, in this order:

1. **Summary table:** requirement, verdict, one-line reason.
2. **Details:** one subsection per requirement, with implementation evidence (file:line), verification result, gaps, and fix.
3. **Top 5 fixes,** ordered by impact on whether the automation solves the problem.
4. **Unverified list:** every item you could not run, and what is needed to run it.
5. **Assumptions:** anything in the problem statement you had to interpret, and how you interpreted it.

## Rules

- Cite file and line for every claim about the code.
- Do not mark anything SOLVED without code evidence or a run.
- Separate what you verified from what you inferred.
- If a requirement is ambiguous, state the interpretation you used.
- Flag any risk to the user's repo, such as a step that would change its working tree.
