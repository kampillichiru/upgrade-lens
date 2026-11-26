---
name: upgradelens-missing-tests
description: Find changed or reached code that no test executes in an UpgradeLens run, and write characterization tests for it, following the project's existing test pattern. The tests are written against master (the base branch), kept in the run folder, run unchanged on both branches by stage 5, and placed uncommitted in the change_branch worktree by stage 8. Use when the user asks for missing tests in an UpgradeLens run, or when stage 4 reports untraced flows, untested changed methods or unverified findings. Used by the upgradelens-test-writer agent.
---

# UpgradeLens: write the missing tests

Goal: for each gap that stage 4 reports, write a test that pins master's behavior. Stage 5 runs each test on
master and on change_branch. Only a test that passes on master, passes unchanged on change_branch, and fails
for every injected regression is validated. Stage 8 places each validated test, uncommitted, in the
change_branch worktree of the run (and in `artifacts\new-tests.patch`). Nothing is committed, and no branch is
created; a person decides what to keep.

## Inputs

Run folder `runs\<run id>\`:
- `artifacts\changed-methods.json`: each changed method or type of the main code, with `tested` (false = no
  unit or integration test executes it on change_branch), its `reason`, its `flows`, and its coverage.
- `artifacts\reach.json`: each flow, the symbols it reaches, the coverage of each symbol, and `untraced`.
- `artifacts\impact-findings.json`: findings, with `affectedFlows` and `symbols`.
- `artifacts\behavior-results.json` (after stage 5, if it exists): probes already run, and their outcomes.
- `config\probes.json`: the probe manifest. A probe already covers a flow; a test should add what the probe
  does not check (status code paths, field values, error bodies it does not check).
- `worktrees\<base branch>\src\...` (master) and `worktrees\<head branch>\src\...` (change_branch): read only.

## Procedure

1. **Read the work list** `artifacts\test-gaps.json`. The orchestrator wrote it before you started; it is fixed
   for this step and you must not change it. Each gap has an `id` (G-01...), a `type`, a `ref` and a `detail`:
   - `untested-change`: a changed main-code method or type no test executes on change_branch
   - `untraced-flow`: a flow that reaches code no test executes
   - `unverified-impact`: a high or medium finding (`ref` is its F-id in `impact-findings.json`) that no probe
     or test checked. Write a test that shows whether the potential impact is real: it pins master's behavior
     at the point the finding names (for example the JSON a controller writes, the validation result, the
     default a library applies). Put the finding's `ruleId` in `linkedRules`, so stage 5 counts the test as
     the check for that finding.
   Every gap must end in `gapIds` of a test, or in `uncovered` with the reason no test can show it (for
   example "runtime only: needs the real database"). The end check rejects the manifest otherwise.

2. **Learn the existing test pattern.** Read the test folder of both worktrees (`src\test\java\...`) before
   writing anything. Note, and then follow:
   - the kind of test used for each layer: plain unit tests with real collaborators and no Spring context
     (for example `OrderServiceTest`), or `@SpringBootTest` with MockMvc for HTTP behavior (for example
     `OrderControllerIT`)
   - the package, the class naming (`*Test` for unit tests, `*IT` for integration tests), the test method
     naming, the assertion library (JUnit `Assertions`, Hamcrest, `jsonPath`), and the setup style
   Prefer the unit-test pattern when it can pin the behavior: it compiles on both branches more easily,
   because framework test APIs move between major versions.

3. **Read master's behavior.** For each gap, read the master source of the code involved
   (`worktrees\<base>\src\main\java\...`). Derive the expected values from the master code, not from memory and
   not from change_branch. If the code gives no clear expectation, assert only what the code clearly does, and
   say so in `purpose`.

4. **Write one test class per gap group.** Same package as the code under test. Name it
   `<Subject>CharacterizationTest` (or `<Subject>CharacterizationIT` when it follows the integration-test
   pattern), so that it cannot collide with an existing class. Write the file to
   `runs\<run id>\artifacts\agent\tests\<relative path>`, never into a worktree or the repository.

   Rules for the test code:
   - It must compile on master and on change_branch unchanged. Check every import against both worktrees'
     sources and test folders. Do not import a class whose package moved between the branches (stage 3's
     `artifacts\library-api-diff.json` lists moved classes, for example the Jackson 2 to 3 move).
   - Name each test after the behavior it pins, for example `cancelUnknownOrderThrowsNotFound`.
   - Assert the values the code sets. Do not assert on timestamps or on ids that depend on test order. Create
     the data each test needs.
   - Each assertion must be able to fail. An assertion that holds for any input is discarded.
   - Shared state (an in-memory repository in a Spring context) is shared between tests: use data no other test
     uses.

5. **Name the regression each test must detect (injection).** For each test, pick one change in the head
   source that breaks the behavior it pins. Write it as a find-and-replace on one head file:
   - `file`: a path under the head worktree, relative, with `/`.
   - `find`: text that occurs exactly once in that file on the head branch. Check it by reading or searching the file.
   - `replace`: the broken version.
   - `note`: the regression in words, for example "cancel reports the wrong status".
   The test must fail when this injection is applied. A test that passes with its injection is not a
   regression detector, and stage 5 will not validate it.

6. **Write the manifest** `runs\<run id>\artifacts\agent\tests-manifest.json`:

```json
{
  "schemaVersion": 1,
  "writer": "upgradelens-test-writer",
  "inputHash": "<value printed by the hash command>",
  "tests": [
    {
      "id": "T-01",
      "className": "OrderCancelCharacterizationTest",
      "relativePath": "src/test/java/com/example/orders/OrderCancelCharacterizationTest.java",
      "gapIds": ["G-01"],
      "gap": "untraced-flow POST /api/orders/{id}/cancel",
      "purpose": "Cancel was executed by no test on either branch. Pins master: cancel returns the order as CANCELLED, an unknown id throws OrderNotFoundException.",
      "pattern": "unit test with real collaborators, as OrderServiceTest",
      "methods": ["OrderController#cancel", "OrderService#cancel", "OrderRepository#updateStatus"],
      "flowIds": ["POST /api/orders/{id}/cancel"],
      "linkedRules": [],
      "injections": [
        { "file": "src/main/java/com/example/orders/OrderService.java",
          "find": "return repository.updateStatus(id, \"CANCELLED\");",
          "replace": "return repository.updateStatus(id, \"NEW\");",
          "note": "cancel reports the wrong status" }
      ]
    }
  ],
  "uncovered": [
    { "gapId": "G-04", "reason": "Runtime only: the Tomcat 11 connector default shows only on a real server; probe P-15 covers it." }
  ]
}
```

   `className` must match the file name, and `relativePath` (forward slashes, under `src/test/`) must match the
   file's place in `artifacts\agent\tests\`. `gapIds` lists the gaps the test covers (at least one). `purpose`
   says why the test was added and what potential impact it proves or disproves, in plain words; the report
   shows it next to the test. `linkedRules` lists the rule ids of the findings the test checks (from
   `impact-findings.json`). Each injection's `file` is under `src/main/`, and its `find` occurs exactly once.
   Every file under `artifacts\agent\tests\` must be in the manifest. At most `orchestration.maxTests` tests
   (default 15): group gaps that one test can pin.

7. **Input hash:** use the value in your prompt (or print it with
   `python scripts/agent-step.py --run-id <run id> --step tests --action hash`). Do not compute it yourself. Do not
   record your call: the orchestrator's begin step did.

8. **Hand over.** Report the manifest path, the test ids, the gaps each covers, and the gaps in `uncovered`
   with their reasons. Do not run Maven and do not edit the worktrees: stage 5 runs the tests on both branches.

## Repair round

When the prompt says it is a repair round, read `artifacts\behavior-results.json` (`tests[]`): for each test
that failed on master, failed to compile, or missed an injected regression, fix the test or the injection.
Keep the ids and the gap coverage. A test that passes on master and fails on change_branch is not broken: it
shows a real behavior change. Keep it unchanged and say so in `purpose`.

## Rules

- Write only under `runs\<run id>\artifacts\agent\`.
- Expected values come from master's code. Never make a test pass by copying what change_branch does.
- Do not write a test for anything that is not in `artifacts\test-gaps.json`. Keep the set small.
- Never write `artifacts\test-gaps.json`, `usage.json`, `review.json` or any file outside
  `artifacts\agent\tests-manifest.json` and `artifacts\agent\tests\`: the end check stops the run.
- Injections must be real regressions, not renames, comment edits or formatting.
- Do not invent a finding id or a rule id. Only use ids from `impact-findings.json`.
