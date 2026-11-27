# Job: write / repair (phase 04-tests)

Goal: for each gap in the work list, a characterization test that pins the **base** branch's behavior. The runner
then runs each test on base and on head. Only a test that passes on base, passes unchanged on head, and fails for
every injected regression is validated. 06-report places each validated test, uncommitted, in the head worktree
for a developer to review and commit. Nothing is committed and no branch is created.

Your brief (`runs\<run id>\04-tests\write\brief.md`, or `...\repair\brief.md`) names the run folder, the work
list, your out folder, and the `inputHash` value. Paths below are relative to the run folder. You never run Maven
and never write into a worktree or the repository.

## Inputs (read only)

- `artifacts\test-gaps.json`: **your work list**, fixed before you start. Each gap has an `id` (G-01...), a
  `type`, a `ref` and a `detail`:
  - `untested-change`: a changed main-code method or type whose changed lines no test runs on head (`lines` lists
    them): the test must run those lines and assert their result
  - `weak-assertion`: the existing tests run a changed method, but a deliberate change of a changed line
    (`mutants`: line, original, mutated) made none of them fail. The test must fail for that change: use the
    mutant as an injection (`find` the original line text, `replace` with the mutated text)
  - `untraced-flow`: a flow that reaches code no test executes
  - `unverified-impact`: a high or medium finding (`ref` is its F-id in `impact-findings.json`) that no probe or
    test checked. The test shows whether the impact is real: it pins base's behavior where the finding points.
    Put the finding's `ruleId` in `linkedRules`.
- `artifacts\changed-methods.json`, `artifacts\reach.json`, `artifacts\impact-findings.json`: what each gap is about.
- `artifacts\behavior-results.json` (when present): probes already run and their outcomes.
- `config\probes.json`: a probe already covers a flow; a test adds what the probe does not check.
- The source of both worktrees (paths in the brief): read only.

## Procedure

1. **Read the work list.** Every gap must end in `gapIds` of a test, or in `uncovered` with the reason no test can
   show it (for example "runtime only: needs the real database"). The checks reject the answer otherwise.
2. **Learn the existing test pattern** from both worktrees' `src\test\java\...` before writing anything: the kind
   of test per layer (plain unit test with real collaborators, or `@SpringBootTest` with MockMvc), the package,
   the naming (`*Test`, `*IT`), the assertion library, the setup style. Prefer unit tests: they compile on both
   branches more easily, because framework test APIs move between major versions.
3. **Read base's behavior** for each gap (`<base worktree>\src\main\java\...`). Derive expected values from base's
   code, never from memory and never from head. If the code gives no clear expectation, assert only what the code
   clearly does, and say so in `purpose`.
4. **Write one test class per gap group**, in the same package as the code under test, named
   `<Subject>CharacterizationTest` (or `...CharacterizationIT` for the integration pattern), to
   `<out folder>\tests\<relative path>`.
   - It must compile on base and on head unchanged: check every import against both worktrees. Do not import a
     class whose package moved between the branches (`artifacts\library-api-diff.json` lists moved classes).
   - Name each test method after the behavior it pins (`cancelUnknownOrderThrowsNotFound`).
   - Assert the values the code sets; no timestamps, no order-dependent ids; create the data each test needs.
   - Every assertion must be able to fail.
5. **Name the regression each test must detect (injection)**, a find-and-replace on one head file:
   `file` (relative to the head worktree, `/`, under `src/main/`), `find` (occurs exactly once in that file on
   head: check it), `replace` (the broken version), `note` (the regression in words). A rename, a comment or a
   format change is not a regression.
6. **Write the manifest** `<out folder>\tests-manifest.json`:

```json
{
  "schemaVersion": 1,
  "writer": "upgradelens-test-writer",
  "inputHash": "<the value in your brief>",
  "tests": [
    {
      "id": "T-01",
      "className": "OrderCancelCharacterizationTest",
      "relativePath": "src/test/java/com/example/orders/OrderCancelCharacterizationTest.java",
      "gapIds": ["G-01"],
      "gap": "untraced-flow POST /api/orders/{id}/cancel",
      "purpose": "Why the test was added and what potential impact it proves or disproves, in plain words.",
      "pattern": "unit test with real collaborators, as OrderServiceTest",
      "methods": ["OrderController#cancel", "OrderService#cancel"],
      "flowIds": ["POST /api/orders/{id}/cancel"],
      "linkedRules": [],
      "cases": [
        { "method": "cancelExistingOrderSetsStatusCancelled", "gapIds": ["G-01"], "flowIds": ["POST /api/orders/{id}/cancel"] },
        { "method": "cancelUnknownOrderReturnsEmpty", "gapIds": ["G-01"] }
      ],
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

`cases` lists each test method with the gaps it really checks (`flowIds` optional, default the test's). Every gap
in `gapIds` must be covered by at least one case. A failing method then counts only for its own gaps: a difference
found by one method is not reported for every gap of the class. Do not list a gap that no method asserts.

`className` matches the file name; `relativePath` (forward slashes, under `src/test/`) matches the file's place
in `<out folder>\tests\`. Every file under `<out folder>\tests\` is in the manifest. At most
`orchestration.maxTests` tests (default 15): group gaps that one test can pin.

## Repair round (`repair` job)

Your out folder already holds the current tests and manifest. Read `artifacts\behavior-results.json` (`tests[]`):
fix only the tests that failed on base, did not compile, or missed an injected regression (`injections[].detected`
false). Keep the ids and the gap coverage. A test that passes on base and fails on head is **not broken**: it
proves a behavior change. Keep it unchanged and say so in `purpose`.

## Rules for a reliable answer

- Expected values come from base's code. Never make a test pass by copying what head does.
- Do not write a test for anything that is not in the work list. Do not invent a finding id or a rule id.
- Write only `<out folder>\tests-manifest.json` and `<out folder>\tests\**`. Never write `test-gaps.json`, a
  worktree, the clone, `config\` or `.github`: the runner rejects the answer.
- When done, run the command at the end of your brief, and report the test ids, the gaps each covers, and the
  uncovered gaps with their reasons.
