# Job: release-notes (phase 03-analysis)

You check the library changes of the run against the libraries' own documentation. You record what the
documentation says. You do not decide whether the upgrade is safe.

Your brief (`runs\<run id>\03-analysis\release-notes\brief.md`) names the run folder, the inputs, the one file you
write, and the `inputHash` value. Paths below are relative to the run folder.

## Inputs (read only)

- `artifacts\library-api-diff.json`: library classes the code uses whose public API changed between the two
  classpaths. Each item has an `id` (LIB-001...), the `class`, its `state` (changed, removed, moved, added),
  `movedTo`, the artifacts on each side (`artifact.base`, `artifact.head`), the removed and changed members,
  `removedUsed`, and `usage` (the code lines on each side).
- `artifacts\dependency-diff.json`: `changed` (version changes, with `major` and `direct`) and `assessment`.
- The source of both worktrees (paths in the brief), when you need to see how the code uses a class.

## Procedure

1. The items to check are listed in your brief's task: every item of `library-api-diff.json` with `severity` high
   or medium (`ref` = its `id`), and every `changed` entry of `dependency-diff.json` with `direct` and `major`
   true (`ref` = its `ga`).
2. For each, find the official source for the version step (from `artifact.base` to `artifact.head`): a migration
   guide for the major version, the release notes of the exact versions, or the changelog, on the project's own
   site or repository. Not blog posts or Q&A sites.
3. Decide:
   - `confirmed`: the source states the change, and the head code follows the documented replacement
   - `contradicted`: the source says something different from the diff, or the head code does not use the
     documented replacement
   - `not-found`: the source does not mention the change, or no source could be fetched
   Quote the sentence that decides it (at most 30 words).
4. Add the behavior notes the source gives for the same step that can affect this code (changed defaults, removed
   configuration, changed serialization) as `impact`, tied to the code lines in `usage` (file:line).
5. Write the answer file named in the brief (`release-notes.json`):

```json
{
  "schemaVersion": 1,
  "agent": "upgradelens-release-notes",
  "inputHash": "<the value in your brief>",
  "items": [
    {
      "ref": "LIB-001",
      "artifact": "com.fasterxml.jackson.core:jackson-databind:2.15.4 -> tools.jackson.core:jackson-databind:3.1.8",
      "claim": "JsonNode moved from com.fasterxml.jackson.databind to tools.jackson.databind.",
      "result": "confirmed | contradicted | not-found",
      "source": { "title": "Jackson 3.0 release notes", "url": "https://..." },
      "quote": "The short sentence from the source that decides the result.",
      "impact": "What the source says can change for the code that uses it, with file:line from usage."
    }
  ]
}
```

## Rules for a reliable answer

- Every `confirmed` or `contradicted` needs an https URL and a quote from that page. Without both, the result is
  `not-found`. The checks reject a confirmed item without a source, and a quote over 40 words.
- Never invent a version, a URL or a quote. If a page cannot be fetched, say so in `impact` and use `not-found`.
- Do not send repository content, names or paths to a web search: search for the library, its versions and the
  class or member names only.
- Write only the answer file in your out folder. Do not edit the worktrees, the clone, `config\` or `.github`.
- When done, run the command at the end of your brief, and report the number of items per result and any item
  without a source.
