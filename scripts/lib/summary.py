"""The one-page summary of a run (reports\\summary.md and summary.html), written by stage 8 next to the detailed
report.

It answers the question the automation exists for, in this order:
  1. the answer: can change_branch be released, and why not
  2. what changed
  3. did the existing tests run the changed code, on both branches, with the same result
  4. where they did not: which new tests were written, and their result on master and on change_branch
  5. every difference found between the branches, with master's and change_branch's actual result
  6. what was not checked, and what a person still has to decide
  7. the run trace: every step, its status, attempts, duration, what it produced, and the log to open

Everything comes from files the run already wrote; no stage needs to run again. A missing file shows as
"not run" in the step that should have written it, so a partial run still gets a summary.
"""

from __future__ import annotations

import datetime
import html
import os
import re

from .core import as_list, get, items, read_json_safe, s


# ---------- small helpers ----------

def short_text(text, limit: int = 160) -> str:
    t = re.sub(r"\s+", " ", s(text)).strip()
    if len(t) <= limit:
        return t
    cut = t[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(".,;:") + " ..."


def first_sentence(text, limit: int = 160) -> str:
    t = re.sub(r"\s+", " ", s(text)).strip()
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    return short_text(m.group(1) if m else t, limit)


def cell(text) -> str:
    return s(text).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def table(headers, rows) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(cell(c) for c in r) + " |")
    if not rows:
        out.append("| None. |" + " |" * (len(headers) - 1))
    return out + [""]


def parse_time(value):
    try:
        return datetime.datetime.fromisoformat(s(value))
    except ValueError:
        return None


def duration(start, end) -> str:
    a, b = parse_time(start), parse_time(end)
    if not a or not b:
        return "-"
    secs = int((b - a).total_seconds())
    return f"{secs // 60} min {secs % 60:02d} s" if secs >= 60 else f"{secs} s"


def pass_fail(result) -> str:
    if not result:
        return "-"
    if result.get("ran") is False:
        return "did not run"
    return "pass" if result.get("passed") else "FAIL"


# ---------- the run trace ----------

STEP_TITLES = {
    "stage-0": "Setup: clone both branches", "stage-1": "Build and run existing tests", "stage-2": "Find changed files",
    "stage-3": "Dependencies and library APIs", "agent-release-notes": "Agent: check release notes",
    "stage-4": "Impact rules, flows, coverage", "agent-analyst": "Agent: impact hypotheses",
    "stage-4-merge": "Impact again, with hypotheses", "stage-5-probes": "Start both apps, compare probes",
    "agent-tests": "Agent: write missing tests", "stage-5-tests": "Run new tests on both branches",
    "stage-6": "Decide test scope per flow", "stage-7": "List items for a person", "stage-8": "Place tests, write report",
    "agent-reviewer": "Agent: review new tests", "stage-9": "Record reviews and decisions", "stage-10": "Verdict",
}


def step_title(key: str) -> str:
    if key in STEP_TITLES:
        return STEP_TITLES[key]
    m = re.match(r"^agent-tests-repair-(\d+)$", key)
    if m:
        return f"Agent: repair tests (round {m.group(1)})"
    m = re.match(r"^stage-5-repair-(\d+)$", key)
    if m:
        return f"Rerun repaired tests (round {m.group(1)})"
    return key


def step_output(key: str, a: dict) -> str:
    """One line: what the step produced, from the artifact it writes."""
    if key == "stage-0":
        env = a["environment"]
        if not env:
            return "no environment record"
        return (f"{get(env, 'branches', 'base', 'name')} at {s(get(env, 'branches', 'base', 'commit'))[:10]}, "
                f"{get(env, 'branches', 'head', 'name')} at {s(get(env, 'branches', 'head', 'commit'))[:10]}")
    if key == "stage-1":
        b = a["builds"]
        if not b:
            return "no build results"
        parts = []
        for side in ("base", "head"):
            sd = get(b, "sides", side) or {}
            parts.append(f"{sd.get('branch')}: {'built' if sd.get('exitCode') == 0 else 'FAILED'}, "
                         f"{get(sd, 'tests', 'passed')}/{get(sd, 'tests', 'total')} tests pass")
        return "; ".join(parts)
    if key == "stage-2":
        inv = a["inventory"]
        return f"{get(inv, 'summary', 'files')} changed file(s), {get(inv, 'summary', 'commits')} commit(s)" if inv else "no inventory"
    if key == "stage-3":
        d, lib = a["deps"], a["library"]
        if not d:
            return "no dependency diff"
        sm = d.get("summary") or {}
        text = f"{sm.get('changed')} dependency version(s) changed ({sm.get('majorChanged')} major)"
        if get(lib, "available"):
            text += f"; {get(lib, 'summary', 'high')} high and {get(lib, 'summary', 'medium')} medium library API change(s) the code uses"
        return text
    if key == "agent-release-notes":
        n = items(get(a["notes"], "items"))
        if not n:
            return "-"
        count = lambda r: len([x for x in n if x.get("result") == r])  # noqa: E731
        return f"{len(n)} item(s): {count('confirmed')} confirmed, {count('contradicted')} contradicted, {count('not-found')} not found"
    if key in ("stage-4", "stage-4-merge"):
        imp, reach = a["impact"], a["reach"]
        if not imp:
            return "no impact findings"
        return (f"{get(imp, 'summary', 'findings')} potential impact(s); {get(reach, 'summary', 'flows')} flow(s), "
                f"{get(reach, 'summary', 'flowsWithUntracedCode')} with code no test runs")
    if key == "agent-analyst":
        h = a["hypotheses"]
        return f"{len(items(get(h, 'hypotheses')))} hypothesis(es), {len(items(get(h, 'explanations')))} explanation(s)" if h else "-"
    if key.startswith("stage-5"):
        b = a["behavior"]
        if not b:
            return "no behavior results"
        ps = b.get("probeSummary") or {}
        tests = items(b.get("tests"))
        text = f"probes: {ps.get('same')} same, {ps.get('different')} different, {ps.get('error')} error"
        if tests and key != "stage-5-probes":
            text += f"; new tests: {len(tests)} run, {len([t for t in tests if t.get('validated')])} validated"
        return text
    if key.startswith("agent-tests"):
        m = a["manifest"]
        return f"{len(items(get(m, 'tests')))} test(s), {len(items(get(m, 'uncovered')))} gap(s) explained as not testable" if m else "-"
    if key == "stage-6":
        sm = get(a["flows"], "summary") or {}
        return f"{sm.get('full')} flow(s) need the full regression, {sm.get('targeted')} targeted, {sm.get('smoke')} smoke" if sm else "-"
    if key == "stage-7":
        return f"{get(a['manual'], 'summary', 'items')} item(s) for a person" if a["manual"] else "-"
    if key == "stage-8":
        return f"{len(items(get(a['newTests'], 'placed')))} validated test file(s) placed, uncommitted" if a["newTests"] else "-"
    if key == "agent-reviewer":
        r = items(get(a["reviewAnswer"], "reviews"))
        return f"{len([x for x in r if x.get('verdict') == 'pass'])} pass, {len([x for x in r if x.get('verdict') == 'fail'])} fail" if r else "-"
    if key == "stage-9":
        m = get(a["review"], "manual") or {}
        return f"manual decisions: {m.get('status')} ({len(items(m.get('decisions')))} recorded)" if m else "-"
    if key == "stage-10":
        v = a["verdict"]
        return f"{v.get('verdict')}: {len(items(v.get('blocking')))} blocking, {len(items(v.get('listed')))} listed" if v else "-"
    return "-"


def exit_meaning(code) -> str:
    """Plain words for a stage exit code."""
    c = s(code)
    meanings = {"0": "finished", "1": "verdict blocked", "2": "usage, config or order problem", "3": "tool failure (git, Maven, Java)",
                "4": "build or tests failed", "130": "interrupted (Ctrl+C)", "-2": "interrupted (Ctrl+C)",
                "3221225786": "killed: Ctrl+C or its console window was closed",
                "-1073741510": "killed: Ctrl+C or its console window was closed"}
    return f"exit {c} = {meanings[c]}" if c in meanings else f"exit {c}"


def step_log(run_dir: str, entry: dict) -> str:
    key = s(entry.get("key"))
    if entry.get("kind") == "agent":
        return s(entry.get("errorsFile")) or "-"
    log = os.path.join(run_dir, "orchestration", f"{key}.out.log")
    return log if os.path.isfile(log) else "-"


def run_trace(run_dir: str, a: dict) -> list[str]:
    led = read_json_safe(os.path.join(run_dir, "orchestration", "ledger.json"))
    rows = []
    if led:
        entries = [e for e in items(led.get("entries"))]
        for e in entries:
            state = s(e.get("state"))
            mark = {"done": "done", "skipped": "skipped", "failed": "FAILED", "running": "RUNNING"}.get(state, state)
            what = s(e.get("reason")) if state in ("skipped", "failed") else step_output(s(e.get("key")), a)
            if state == "failed" and e.get("exitCode") not in (None, ""):
                what = f"{exit_meaning(e.get('exitCode'))}. {what}"
            rows.append([step_title(s(e.get("key"))), mark, s(e.get("attempt")), duration(e.get("startedAt"), e.get("finishedAt")),
                         short_text(what, 200), step_log(run_dir, e) if state != "done" else "-"])
        intro = ("Every step of the run, in order (one row per attempt). A FAILED or RUNNING row is where to look: "
                 "its last column is the log to open.")
    else:
        st = a["status"] or {}
        for x in items(st.get("steps")):
            mark = {"done": "done", "failed": "FAILED"}.get(s(x.get("state")), s(x.get("state")))
            rows.append([x.get("name"), mark, "1", duration(x.get("startedAt"), x.get("finishedAt")),
                         short_text(x.get("reason") or "", 200), "-"])
        intro = "Steps recorded in status.json (a scripts-only run has no orchestration ledger)."
    out = ["## 8. Run trace (for troubleshooting)", "", intro, ""]
    out += table(["Step", "Status", "Try", "Took", "Result or reason", "Log if not done"], rows)
    out += ["Where everything is:", "",
            f"- Logs: `{os.path.join(run_dir, 'logs')}` (one per stage; `build-*.log` for the Maven builds, `tests-*.log` per new-test run)",
            f"- Stage output: `{os.path.join(run_dir, 'orchestration')}` (`<step>.out.log`), and the ledger `ledger.json`",
            f"- Results: `{os.path.join(run_dir, 'artifacts')}`; detailed report: `{os.path.join(run_dir, 'reports', 'report.md')}`", ""]
    return out


# ---------- per-flow evidence and action items ----------

def flow_view(a: dict, tests: list, probes: list, proven: list) -> list[dict]:
    """For each flow: does an existing test run its code, what the probes and the new tests saw, and the result."""
    tests_by_flow = {}
    for t in tests:
        for fid in as_list(t.get("flowIds")):
            tests_by_flow.setdefault(fid, []).append(t)
    reach_by_id = {f.get("id"): f for f in items(get(a["reach"], "flows"))}
    flow_ids = list(reach_by_id)
    if any(p.get("flowId") == "infrastructure" for p in probes) or "infrastructure" in tests_by_flow:
        flow_ids.append("infrastructure")
    out = []
    for fid in flow_ids:
        untraced = as_list(get(reach_by_id.get(fid), "untraced"))
        fp = [p for p in probes if p.get("flowId") == fid]
        ft = tests_by_flow.get(fid, [])
        if fid == "infrastructure":
            existing = "not measured (actuator endpoints)"
        elif untraced:
            existing = f"PARTLY: never run: {', '.join(untraced)}"
        else:
            existing = "yes"
        changed_probes = [p for p in fp if p.get("outcome") == "different"]
        changed_tests = [t for t in ft if t in proven]
        if changed_probes or changed_tests:
            result = "BEHAVIOR CHANGED"
        elif any(p.get("outcome") == "error" for p in fp):
            result = "check did not run"
        elif untraced and not ft:
            result = "not fully tested"
        elif untraced:
            result = "same on both branches (new tests now run the code existing tests never ran)"
        else:
            result = "same on both branches"
        same = len([p for p in fp if p.get("outcome") == "same"])
        out.append({
            "id": fid, "existing": existing, "untraced": untraced, "probes": fp, "tests": ft, "result": result,
            "changedProbes": changed_probes, "changedTests": changed_tests,
            "probeText": f"{same} same, {len(changed_probes)} different" if fp else "none",
            "testText": ", ".join(f"{t.get('id')} ({pass_fail(t.get('base'))} / {pass_fail(t.get('head'))})" for t in ft) or "none",
        })
    return out


def manual_item_ids(a: dict, kind: str, refs) -> list[str]:
    refs = {s(r) for r in refs}
    return [s(i.get("id")) for i in items(get(a["manual"], "items")) if i.get("kind") == kind and s(i.get("ref")) in refs]


def action_items(run_dir, a, base, head, flows_view, findings, status_by_id, differences, proven, existing_changed, tests,
                 reviews) -> list[str]:
    """What each role does next, from the evidence: decisions on each difference, where QA spends regression time
    (and where a smoke test is enough), and the developers' follow-ups.
    Open findings from the analyst are runtime risks with a concrete manual check (QA); the other open findings
    come from build and dependency rules (developers)."""
    md = ["## 2. Action items", ""]
    open_f = [f for f in findings if status_by_id.get(f.get("id")) in ("open", "hypothesis")]
    qa_open = [f for f in open_f if f.get("source") == "analyst"]
    dev_open = [f for f in open_f if f.get("source") != "analyst"]
    decisions_file = os.path.join(run_dir, "review", "manual-decisions.json")

    # Decide: one item per flow whose behavior differs (a probe and a test can show the same change).
    md += ["### Decide (product owner, BA, risk acceptor)", ""]
    n = 0

    def test_mi(t):
        rules = set(as_list(t.get("linkedRules")))
        return manual_item_ids(a, "finding", [f.get("id") for f in findings if f.get("ruleId") in rules])

    shown_probes, shown_tests = set(), set()
    for f in [x for x in flows_view if x["changedProbes"] or x["changedTests"]]:
        n += 1
        evidence, mi = [], []
        for p in f["changedProbes"]:
            shown_probes.add(p.get("id"))
            mi += manual_item_ids(a, "probe-difference", [p.get("id")])
            evidence.append(f"{p.get('request')}: {base} answers `{get(p, 'base', 'status')} {short_text(get(p, 'base', 'body'), 100)}`, "
                            f"{head} answers `{get(p, 'head', 'status')} {short_text(get(p, 'head', 'body'), 100)}`")
        for t in f["changedTests"]:
            shown_tests.add(t.get("id"))
            mi += test_mi(t)
            evidence.append(f"new test {t.get('id')} passes on {base} and fails on {head} ({short_text(get(t, 'head', 'detail'), 110)}); "
                            f"it checks: {first_sentence(t.get('purpose'), 140)}")
        label = "Application infrastructure (actuator)" if f["id"] == "infrastructure" else f"Flow {f['id']}"
        mi = sorted(set(mi))
        md.append(f"{n}. **{label} behaves differently on {head}.** " + "; also ".join(e.rstrip(". ") for e in evidence) + ". "
                  "Ask who depends on it (API clients, monitoring, load balancer, Kubernetes probes), then accept the change or "
                  "ask for a fix" + (f" (decision items {', '.join(mi)})." if mi else "."))
    for p in [x for x in differences if x.get("id") not in shown_probes]:
        n += 1
        md.append(f"{n}. **{p.get('request')} answers differently** ({'; '.join(as_list(p.get('differences')))}). Accept or ask for a fix.")
    for t in [x for x in proven if x.get("id") not in shown_tests]:
        n += 1
        md.append(f"{n}. **New test {t.get('id')} passes on {base} and fails on {head}** ({short_text(get(t, 'head', 'detail'), 110)}). "
                  "Accept or ask for a fix" + (f" (decision items {', '.join(test_mi(t))})." if test_mi(t) else "."))
    for t in existing_changed:
        n += 1
        md.append(f"{n}. **Existing test {t.get('test')} changed: {t.get('base') or '-'} -> {t.get('head') or '-'}.** A developer "
                  f"must explain it before release.")
    if n == 0:
        md.append("Nothing to decide: no behavior difference was found.")
    else:
        md += ["", f"Record each decision (`accept`, `reject`, `needs-test` or `defer`, with your name) in `{decisions_file}`. "
               "Until then each difference blocks the release."]
    md.append("")

    # QA: where regression time goes.
    md += ["### QA: where to spend regression time", ""]
    rows = []
    smoke = 0
    for f in flows_view:
        flow_risks = [x for x in qa_open if f["id"] in as_list(x.get("affectedFlows")) and x.get("scope") == "symbol"]
        if f["result"] == "BEHAVIOR CHANGED":
            what = "; ".join([f"{p.get('request')}: {'; '.join(as_list(p.get('differences')))}" for p in f["changedProbes"]]
                             + [f"{t.get('id')} fails on {head}: {short_text(get(t, 'head', 'detail'), 80)}" for t in f["changedTests"]])
            rows.append([f["id"], "RETEST", "behavior differs between the branches", what])
        elif f["result"] == "check did not run":
            rows.append([f["id"], "RETEST", "a probe could not run", "rerun the probe, or test the flow by hand"])
        elif flow_risks:
            rows.append([f["id"], "TARGETED TEST", "a risk that no probe or test checked",
                         "; ".join(short_text(x.get("check"), 160) for x in flow_risks)])
        elif f["result"] == "not fully tested":
            rows.append([f["id"], "FULL TEST", "no automated test runs part of its code", f"never run: {', '.join(f['untraced'])}"])
        else:
            smoke += 1
            covered = []
            if f["existing"] == "yes":
                covered.append("existing tests run its code")
            if f["tests"]:
                covered.append("new tests " + ", ".join(t.get("id") for t in f["tests"]) + " pass on both")
            if f["probes"]:
                covered.append(f"{len(f['probes'])} probe(s) same on both")
            rows.append([f["id"], "SMOKE ONLY", "; ".join(covered) or "same on both branches", "one happy-path request"])
    md += table(["Flow", "Plan", "Why", "What to test"], rows)
    if flows_view:
        md.append(f"{smoke} of {len(flows_view)} flow(s) need only a smoke test: the automation already ran them on both "
                  "branches with the same result. Spend the regression time on the RETEST and TARGETED rows.")
    cross = [x for x in qa_open if x.get("scope") != "symbol"]
    if cross:
        md += ["", "Also check once, across the application (no probe or test covered these):", ""]
        md += [f"- {x.get('id')}: {short_text(x.get('title'), 120)} How: {short_text(x.get('check'), 220)}" for x in cross]
    md += ["", "The formal flow scope in the detailed report (section 9) stays `full` while any app-wide risk is unchecked; "
           "this plan is the evidence-based focus.", ""]

    # Developers.
    md += ["### Developers", ""]
    dev = []
    placed = items(get(a["newTests"], "placed"))
    if placed:
        dev.append(f"Review and commit the {len(placed)} validated new test(s), placed uncommitted in `{get(a['newTests'], 'location')}` "
                   f"(or apply `{os.path.join(run_dir, 'artifacts', 'new-tests.patch')}`). The commands are in section 10 of the detailed report.")
    for t in tests:
        rev = reviews.get(t.get("id"))
        if rev and rev.get("verdict") == "fail":
            dev.append(f"Fix test {t.get('id')} ({t.get('className')}): the reviewer failed it: {short_text(rev.get('notes'), 260)}")
    for t in tests:
        if not get(t, "base", "passed"):
            dev.append(f"Fix test {t.get('id')}: it fails on {base}, so it does not describe {base}'s behavior.")
    for x in dev_open:
        dev.append(f"{x.get('id')}: {short_text(x.get('title'), 100)}: {short_text(x.get('evidence'), 140)}")
    not_found = [i for i in items(get(a["notes"], "items")) if i.get("result") == "not-found"]
    if not_found:
        dev.append(f"{len(not_found)} library change(s) the code touches are not mentioned in the release notes "
                   f"({', '.join(s(i.get('ref')) for i in not_found)}): check them in the library's changelog or source.")
    md += [f"{k}. {line}" for k, line in enumerate(dev, start=1)] or ["Nothing."]
    md.append("")
    return md


# ---------- the summary ----------

def load(run_dir: str) -> dict:
    art = lambda n: read_json_safe(os.path.join(run_dir, "artifacts", *n.split("/")))  # noqa: E731
    return {
        "status": read_json_safe(os.path.join(run_dir, "status.json")), "environment": art("environment.json"),
        "builds": art("build-results.json"), "testResults": art("test-results.json"), "inventory": art("change-inventory.json"),
        "deps": art("dependency-diff.json"), "library": art("library-api-diff.json"), "impact": art("impact-findings.json"),
        "reach": art("reach.json"), "behavior": art("behavior-results.json"), "flows": art("flows.json"),
        "manual": art("manual-items.json"), "gaps": art("test-gaps.json"), "notRun": art("gaps.json"), "notes": art("agent/release-notes.json"),
        "hypotheses": art("agent/hypotheses.json"), "manifest": art("agent/tests-manifest.json"),
        "reviewAnswer": art("agent/review-answer.json"), "newTests": art("new-tests.json"),
        "review": read_json_safe(os.path.join(run_dir, "review", "review-record.json")),
        "verdict": read_json_safe(os.path.join(run_dir, "verdict.json")),
    }


def build_summary(run_dir: str, run_id: str) -> list[str]:
    a = load(run_dir)
    env, v, b = a["environment"] or {}, a["verdict"], a["behavior"] or {}
    base = get(env, "branches", "base", "name") or "base"
    head = get(env, "branches", "head", "name") or "head"
    tests = items(b.get("tests"))
    probes = items(b.get("probes"))
    reviews = {r.get("testId"): r for r in items(get(a["reviewAnswer"], "reviews"))}
    status_by_id = {x.get("id"): x.get("status") for x in items(get(v, "findingStatus") or get(a["flows"], "findingStatus"))}
    findings = items(get(a["impact"], "findings"))
    md = [f"# Upgrade check summary: {base} -> {head} (run {run_id})", ""]

    # 1. The answer
    differences = [p for p in probes if p.get("outcome") == "different"]
    proven = [t for t in tests if get(t, "base", "passed") and not get(t, "head", "passed")]
    existing_changed = [t for t in items(get(a["testResults"], "tests")) if t.get("change") not in ("same", None)]
    failed_steps = [x for x in items(get(a["status"], "steps")) if x.get("state") == "failed"]
    md += ["## 1. The answer", ""]
    if v:
        word = {"blocked": "NOT ready to release", "safe-with-listed-tests": "can be released once the listed items are handled",
                "safe": "safe to release"}.get(v.get("verdict"), v.get("verdict"))
        md += [f"**{head} is {word}** (verdict `{v.get('verdict')}`).", ""]
    elif failed_steps:
        md += [f"**No verdict: the run stopped at step `{failed_steps[-1].get('name')}`.** See the run trace (section 8).", ""]
    else:
        md += ["**No verdict yet: the run has not reached the last step.** See the run trace (section 8).", ""]
    n_diff = len(differences) + len(proven) + len(existing_changed)
    md.append(f"- Behavior differences found between the branches: **{n_diff}** (section 6).")
    if a["testResults"]:
        sm = a["testResults"].get("summary") or {}
        md.append(f"- Existing tests: {sm.get('tests')} run on both branches; {sm.get('same')} give the same result, "
                  f"{sm.get('nowFailing')} now fail on {head}.")
    if tests:
        md.append(f"- New tests written for what the existing tests did not cover: {len(tests)}; "
                  f"{len([t for t in tests if t.get('validated')])} validated, {len(proven)} prove a behavior change.")
    checked = len([f for f in findings if status_by_id.get(f.get("id")) in ("verified", "confirmed", "accepted")])
    if findings:
        md.append(f"- Potential impacts of the change: {len(findings)}; checked by a probe or test: {checked}; not checked: "
                  f"{len(findings) - checked} (section 7).")
    if a["manual"]:
        md.append(f"- Decisions waiting for a person: {get(a['manual'], 'summary', 'items')} (section 7).")
    md.append("")

    # 2. Action items, by role
    flows_view = flow_view(a, tests, probes, proven)
    md += action_items(run_dir, a, base, head, flows_view, findings, status_by_id, differences, proven, existing_changed, tests, reviews)

    # 3. What changed
    md += ["## 3. What changed", ""]
    inv = a["inventory"]
    if inv:
        by_cat = get(inv, "summary", "byCategory") or {}
        cats = ", ".join(f"{n} {k}" for k, n in by_cat.items() if n)
        md.append(f"- {get(inv, 'summary', 'files')} file(s) changed ({cats}), in {get(inv, 'summary', 'commits')} commit(s): "
                  + "; ".join(f"`{f.get('path')}`" for f in items(inv.get("files"))[:8]))
        commits = items(inv.get("commits"))
        if commits:
            md.append("- Commits: " + "; ".join(f"{s(c.get('commit'))[:10]} {c.get('subject')}" for c in commits[:5]))
        main = by_cat.get("main-code") or 0
        md.append(f"- Application code changed: {'yes, ' + str(main) + ' file(s)' if main else 'no (the change is in the build and dependencies)'}.")
    else:
        md.append("- Not known: step `Find changed files` did not run.")
    d = a["deps"]
    if d:
        direct = [c for c in items(d.get("changed")) if c.get("direct")]
        md.append(f"- Dependencies: {get(d, 'summary', 'changed')} version change(s) ({get(d, 'summary', 'majorChanged')} major), "
                  f"{get(d, 'summary', 'added')} added, {get(d, 'summary', 'removed')} removed. Direct: "
                  + ("; ".join(f"{c.get('ga')} {c.get('base')} -> {c.get('head')}" for c in direct[:6]) or "none") + ".")
    lib = a["library"]
    if get(lib, "available"):
        important = [i for i in items(lib.get("items")) if i.get("severity") in ("high", "medium")]
        md.append(f"- Library APIs the code uses that changed: {len(important)}"
                  + (" (e.g. " + "; ".join(short_text(i.get("class"), 70) for i in important[:3]) + ")" if important else "") + ".")
    if env:
        md.append(f"- Runtime: {base} on `{get(env, 'branches', 'base', 'java')}`, {head} on `{get(env, 'branches', 'head', 'java')}`.")
    md.append("")

    # 4. Existing tests and coverage, per flow
    md += ["## 4. Did the existing tests run the changed code?", ""]
    builds = a["builds"]
    if builds:
        for side in ("base", "head"):
            sd = get(builds, "sides", side) or {}
            md.append(f"- {sd.get('branch')}: build {'passed' if sd.get('exitCode') == 0 else 'FAILED'}; "
                      f"{get(sd, 'tests', 'passed')} of {get(sd, 'tests', 'total')} existing tests pass.")
        if existing_changed:
            md.append("- Existing tests whose result differs: " + "; ".join(f"{t.get('test')} ({t.get('base') or '-'} -> {t.get('head') or '-'})"
                                                                      for t in existing_changed[:8]) + ".")
        else:
            md.append("- Every existing test gives the same result on both branches.")
        md.append("")
    flow_rows = [[f["id"], f["existing"], f["probeText"], f["testText"], f["result"]] for f in flows_view]
    md += ["Per flow (an endpoint, job or listener): does an existing test run its code, what the probes saw, and the new "
           f"tests (result on {base} / {head}).", ""]
    md += table(["Flow", "Existing tests run its code", "Probes (same, different)", f"New tests ({base} / {head})", "Result"], flow_rows)

    # 5. New tests
    md += ["## 5. New tests for what was not covered", ""]
    gaps = {g.get("id"): g for g in items(get(a["gaps"], "gaps"))}
    if tests:
        md += [f"Each test was written against {base}, run unchanged on both branches, then run again with a small bug injected "
               "into the code: a useful test must catch every injected bug.", ""]
        rows = []
        manifest_by_id = {t.get("id"): t for t in items(get(a["manifest"], "tests"))}
        for t in tests:
            m = manifest_by_id.get(t.get("id"), {})
            reasons = [short_text(gaps[g].get("detail"), 110) for g in as_list(m.get("gapIds")) if g in gaps]
            why = "; ".join(reasons[:2]) + (f" (+{len(reasons) - 2} more)" if len(reasons) > 2 else "") if reasons else short_text(t.get("gap"), 110)
            inj = items(t.get("injections"))
            caught = f"{len([i for i in inj if i.get('detected')])} of {len(inj)}"
            rev = reviews.get(t.get("id"))
            if t in proven:
                outcome = f"PROVES A CHANGE: fails on {head} ({short_text(get(t, 'head', 'detail'), 90)})"
            elif not get(t, "base", "passed"):
                outcome = f"broken: fails on {base}"
            elif t.get("validated"):
                outcome = "validated, placed for commit"
            else:
                outcome = "not validated: misses an injected bug"
            if rev and rev.get("verdict") == "fail":
                outcome += f"; reviewer FAILED it: {first_sentence(rev.get('notes'), 140)}"
            rows.append([t.get("id"), first_sentence(t.get("purpose"), 130), why, pass_fail(t.get("base")), pass_fail(t.get("head")),
                         caught, outcome])
        md += table(["Test", "What it checks", "Why it was needed", base, head, "Bugs caught", "Outcome"], rows)
        unc = items(get(a["manifest"], "uncovered"))
        if unc:
            md += ["No test was possible for:", ""]
            md += [f"- {u.get('gapId')}: {short_text(gaps.get(u.get('gapId'), {}).get('detail'), 110)} Reason: {short_text(u.get('reason'), 200)}"
                   for u in unc] + [""]
    elif b.get("testState") == "stale":
        md += ["The test writer's output is stale (its inputs changed after it ran), so no new test ran.", ""]
    else:
        md += ["No new tests: every changed flow was already covered, or the test writer did not run (see the run trace).", ""]

    # 6. Differences
    md += [f"## 6. Behavior differences between {base} and {head}", ""]
    diff_rows = []
    for p in differences:
        diff_rows.append([f"probe {p.get('id')}", p.get("request"),
                          f"{get(p, 'base', 'status')} {short_text(get(p, 'base', 'body'), 120)}",
                          f"{get(p, 'head', 'status')} {short_text(get(p, 'head', 'body'), 120)}", "; ".join(as_list(p.get("differences")))])
    for t in proven:
        diff_rows.append([f"test {t.get('id')}", first_sentence(t.get("purpose"), 110), "pass",
                          f"FAIL: {short_text(get(t, 'head', 'detail'), 120)}", "test passes on " + base + " and fails on " + head])
    for t in existing_changed:
        diff_rows.append(["existing test", t.get("test"), t.get("base"), t.get("head"), short_text(t.get("message"), 120)])
    md += table(["Source", "What", base, head, "Difference"], diff_rows)
    if diff_rows:
        md += ["Each difference blocks the release until a person accepts it (an `accept` decision in "
               f"`{os.path.join(run_dir, 'review', 'manual-decisions.json')}`), or the code is fixed.", ""]

    # 7. Not checked and decisions
    md += ["## 7. Not checked, and decisions for a person", ""]
    open_f = [f for f in findings if status_by_id.get(f.get("id")) in ("open", "hypothesis")]
    if open_f:
        md += [f"Potential impacts no probe or test checked ({len(open_f)}):", ""]
        md += [f"- {f.get('id')}: {short_text(f.get('title'), 150)}" for f in open_f] + [""]
    for g in items(get(a["notRun"], "gaps")):
        md.append(f"- Not run: {g.get('item')}. {short_text(g.get('reason'), 150)}")
    if a["manual"]:
        kinds = {}
        for i in items(get(a["manual"], "items")):
            kinds[i.get("kind")] = kinds.get(i.get("kind"), 0) + 1
        pending = len(as_list(get(a["review"], "manual", "pending")))
        md += ["", f"Decisions for a person: {get(a['manual'], 'summary', 'items')} item(s), {pending} still without a decision ("
               + ", ".join(f"{n} {k}" for k, n in kinds.items()) + f"). The questions are in `{os.path.join(run_dir, 'artifacts', 'manual-items.json')}` "
               f"and section 11 of the detailed report.", ""]

    # 8. Trace
    md += run_trace(run_dir, a)
    return md


# ---------- HTML for the same markdown ----------

def markdown_to_html(lines: list[str], title: str) -> str:
    """HTML for the small markdown subset build_summary writes: headings, paragraphs, bullets, tables, **bold**, `code`."""
    def inline(t: str) -> str:
        t = html.escape(t, quote=True)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        return re.sub(r"`(.+?)`", r"<code>\1</code>", t)

    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
           f"<title>{html.escape(title)}</title>",
           "<style>:root{--bg:#FFFFFF;--fg:#2C2C2A;--line:#D3D1C7;--head:#F1EFE8;--bad:#A32D2D}@media (prefers-color-scheme: dark)"
           "{:root{--bg:#1F1E1B;--fg:#EDEBE4;--line:#5F5E5A;--head:#2C2C2A;--bad:#F09595}}body{font-family:Arial,Helvetica,sans-serif;"
           "margin:24px;color:var(--fg);background:var(--bg);line-height:1.45;max-width:1200px}h1{font-size:22px}h2{font-size:17px;"
           "margin-top:28px;border-bottom:1px solid var(--line);padding-bottom:4px}.scroll{overflow-x:auto}table{border-collapse:"
           "collapse;width:100%;font-size:13px;margin:8px 0 16px}th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;"
           "vertical-align:top}th{background:var(--head)}code{font-size:12px;background:var(--head);padding:1px 4px;border-radius:3px}"
           "strong{color:var(--bad)}@media (max-width:700px){body{margin:16px}}</style></head><body>"]
    i = 0
    in_list = None
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^(\d+)\. (.*)$", line)
        kind = "ul" if line.startswith("- ") else ("ol" if m else None)
        if kind:
            if in_list != kind:
                if in_list:
                    out.append(f"</{in_list}>")
                out.append(f"<{kind}>")
                in_list = kind
            out.append(f"<li>{inline(line[2:] if kind == 'ul' else m.group(2))}</li>")
            i += 1
            continue
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None
        if line.startswith("### "):
            out.append(f"<h3>{inline(line[4:])}</h3>")
        elif line.startswith("# "):
            out.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("| "):
            block = []
            while i < len(lines) and lines[i].startswith("|"):
                block.append(lines[i])
                i += 1
            split = lambda r: [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", r.strip())[1:-1]]  # noqa: E731
            head_cells = split(block[0])
            out.append("<div class='scroll'><table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head_cells) + "</tr></thead><tbody>")
            for r in block[2:]:
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in split(r)) + "</tr>")
            out.append("</tbody></table></div>")
            continue
        elif line.strip():
            out.append(f"<p>{inline(line)}</p>")
        i += 1
    if in_list:
        out.append(f"</{in_list}>")
    out.append("</body></html>")
    return "\n".join(out) + "\n"
