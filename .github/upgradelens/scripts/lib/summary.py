"""The one-page summary of a run (reports\\summary.md and summary.html), written by stage 8 next to the detailed
report. It is written for people (BA, QA, developers, managers), in this order:

  1. The answer          can change_branch be released, in one sentence, with the three counts below
  2. Side by side        the two branches compared: build, existing tests, startup, each endpoint, new tests
  3. Issues found        each behavior that is proven to differ: what changed, a real before/after example,
                         who is affected, the decision needed
  4. Proven: no issue    each risk that was checked on both branches with the same result, and how
  5. Potential issues    each risk that nothing proved either way: why it is a risk, why the automation could
                         not test it, and how to check it
  6. What to do          a checklist per role
  7. Details             (collapsed) the new tests, reference ids, and the run trace for troubleshooting

Plain words come from the analyst's explanations (plain, example, productionRisk) when they exist; internal
ids (F-001, H-02, MI-013...) appear only where a person must use them (the decision items) and in section 7.
Everything comes from files the run already wrote. A missing file shows up as a step that did not run, so a
partial run still gets a summary.
"""

from __future__ import annotations

import datetime
import html
import os
import re

from .analysis import check_applies, test_flow_ids
from .core import as_list, get, items, read_json_safe, s
from .stages import exit_meaning as stage_exit_meaning

OK, BAD, WARN = "✅", "❌", "⚠️"


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


def humanize(name) -> str:
    """A test method name in plain words: healthBodyIsOnlyTheStatus -> health body is only the status."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s(name)).lower()


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


def exit_meaning(code, stage=None) -> str:
    """Plain words for a stage exit code (lib\\stages.py holds the meanings)."""
    c = s(code)
    meaning = stage_exit_meaning(c, stage)
    return f"exit {c} = {meaning}" if meaning else f"exit {c}"


# ---------- the run trace ----------

STEP_TITLES = {
    "stage-0": "Setup: clone both branches", "stage-1": "Build and run existing tests", "stage-2": "Find changed files",
    "stage-3": "Dependencies and library APIs", "agent-release-notes": "Agent: check release notes",
    "stage-4": "Impact rules, flows, coverage", "agent-analyst": "Agent: impact hypotheses",
    "stage-4-merge": "Impact again, with hypotheses", "stage-5-probes": "Start both apps, compare endpoints",
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
        text = f"endpoint checks: {ps.get('same')} same, {ps.get('different')} different, {ps.get('error')} error"
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
        return f"{get(a['manual'], 'summary', 'items')} question(s) for the reader" if a["manual"] else "-"
    if key == "stage-8":
        return f"{len(items(get(a['newTests'], 'placed')))} validated test file(s) placed, uncommitted" if a["newTests"] else "-"
    if key == "agent-reviewer":
        r = items(get(a["reviewAnswer"], "reviews"))
        return f"{len([x for x in r if x.get('verdict') == 'pass'])} pass, {len([x for x in r if x.get('verdict') == 'fail'])} fail" if r else "-"
    if key == "stage-9":
        m = get(a["review"], "manual") or {}
        return f"{len(items(m.get('decisions')))} decision(s) recorded by a person (optional)" if m else "-"
    if key == "stage-10":
        v = a["verdict"]
        return (f"{v.get('verdict')}: {len(items(v.get('differences')))} difference(s), {len(as_list(v.get('notProven')))} not "
                f"proven, {len(as_list(v.get('incomplete')))} incomplete") if v else "-"
    return "-"


# The job of each ul.py phase (runs\<id>\<phase>\status.json) -> the step key its title and output come from.
JOB_STEPS = {("00-setup", "setup"): "stage-0", ("01-build", "build"): "stage-1", ("02-impact", "changes"): "stage-2",
             ("02-impact", "deps"): "stage-3", ("02-impact", "rules"): "stage-4",
             ("03-analysis", "release-notes"): "agent-release-notes", ("03-analysis", "analyst"): "agent-analyst",
             ("03-analysis", "merge"): "stage-4-merge", ("04-tests", "probes"): "stage-5-probes",
             ("04-tests", "gaps"): "gaps", ("04-tests", "write"): "agent-tests", ("04-tests", "validate"): "stage-5-tests",
             ("04-tests", "repair"): "agent-tests-repair-1", ("04-tests", "revalidate"): "stage-5-repair-1",
             ("05-review", "review"): "agent-reviewer", ("06-report", "flows"): "stage-6", ("06-report", "manual"): "stage-7",
             ("06-report", "report"): "stage-8", ("06-report", "record"): "stage-9", ("06-report", "verdict"): "stage-10"}


def phase_entries(run_dir: str) -> list[tuple[str, str, dict]]:
    """(phase, job, record) for every job of every phase that ran, in phase order."""
    out = []
    if not os.path.isdir(run_dir):
        return out
    for name in sorted(n for n in os.listdir(run_dir) if re.match(r"^\d\d-[a-z]+$", n)):
        st = read_json_safe(os.path.join(run_dir, name, "status.json"))
        jobs = st.get("jobs") if isinstance(st, dict) else None
        if isinstance(jobs, dict):
            out += [(name, job, rec) for job, rec in jobs.items() if isinstance(rec, dict)]
    return out


def step_log(run_dir: str, phase: str, job: str, rec: dict) -> str:
    """Where to look for a job that is not done: its raw tool output, or the agent's brief and check errors."""
    for p in (os.path.join(run_dir, phase, job, "checks.json"), s(rec.get("brief")), s(rec.get("toolLog")),
              os.path.join(run_dir, phase, "log.txt")):
        if p and os.path.isfile(p):
            return p
    return "-"


def run_trace(run_dir: str, a: dict) -> list[str]:
    entries = phase_entries(run_dir)
    rows = []
    if entries:
        for phase, job, e in entries:
            key = JOB_STEPS.get((phase, job), job)
            state = s(e.get("state"))
            mark = {"done": "done", "skipped": "skipped", "failed": "FAILED", "waiting": "WAITING"}.get(state, state)
            what = s(e.get("reason")) if state in ("skipped", "failed", "waiting") else step_output(key, a)
            if state == "failed" and e.get("stageExit", e.get("code")) not in (None, ""):
                what = f"{exit_meaning(e.get('stageExit', e.get('code')))}. {what}"
            title = step_title(key) if key in STEP_TITLES else f"{phase} / {job}"
            rows.append([title, mark, s(e.get("attempt") or 1), duration(e.get("startedAt"), e.get("finishedAt")),
                         short_text(what, 200), step_log(run_dir, phase, job, e) if state != "done" else "-"])
        intro = ("Every job of every phase that ran, in order. A FAILED or WAITING row is where to look: its last "
                 "column is the file to open. `python .github/upgradelens/ul.py why <phase>` explains it.")
    else:
        for x in items(get(a["status"], "steps")):
            mark = {"done": "done", "failed": "FAILED"}.get(s(x.get("state")), s(x.get("state")))
            rows.append([x.get("name"), mark, "1", duration(x.get("startedAt"), x.get("finishedAt")),
                         short_text(x.get("reason") or "", 200), "-"])
        intro = "Steps recorded in status.json (no phase has recorded its jobs yet)."
    out = ["### Run trace (for troubleshooting)", "", intro, ""]
    out += table(["Step", "Status", "Try", "Took", "Result or reason", "Log if not done"], rows)
    out += ["Where everything is:", "",
            f"- Timeline: `{os.path.join(run_dir, 'run.log')}`; each phase: `<phase>\\log.txt`, its quality in `quality.json`, "
            "raw tool output in `tools\\<job>.log`, agent briefs and check results in `<job>\\`",
            f"- Stage logs: `{os.path.join(run_dir, 'logs')}` (`build-*.log` for the Maven builds, `tests-*.log` per new-test run)",
            f"- Results: `{os.path.join(run_dir, 'artifacts')}`; detailed report: `{os.path.join(run_dir, 'reports', 'report.md')}`", ""]
    return out


# ---------- inputs ----------

def load(run_dir: str, inp=None) -> dict:
    """The run's files. inp (lib\\standalone.py StagePaths.inp) maps a path inside the run folder to the file to read:
    a standalone stage reads its own outputs first."""
    inp = inp or (lambda *parts: os.path.join(run_dir, *parts))
    art = lambda n: read_json_safe(inp("artifacts", *n.split("/")))  # noqa: E731
    return {
        "status": read_json_safe(inp("status.json")), "environment": art("environment.json"),
        "builds": art("build-results.json"), "testResults": art("test-results.json"), "inventory": art("change-inventory.json"),
        "deps": art("dependency-diff.json"), "library": art("library-api-diff.json"), "impact": art("impact-findings.json"),
        "reach": art("reach.json"), "behavior": art("behavior-results.json"), "flows": art("flows.json"),
        "manual": art("manual-items.json"), "gaps": art("test-gaps.json"), "notRun": art("gaps.json"),
        "notes": art("agent/release-notes.json"), "hypotheses": art("agent/hypotheses.json"),
        "manifest": art("agent/tests-manifest.json"), "reviewAnswer": art("agent/review-answer.json"),
        "newTests": art("new-tests.json"), "review": read_json_safe(inp("review", "review-record.json")),
        "verdict": read_json_safe(inp("verdict.json")), "changedMethods": art("changed-methods.json"),
        "asserts": art("assert-strength.json"),
    }


def flow_view(a: dict, tests: list, probes: list, proven: list) -> list[dict]:
    """For each endpoint (flow): does an existing test run its code, what the endpoint checks and the new tests saw."""
    tests_by_flow = {}
    for t in tests:
        for fid in test_flow_ids(t):
            tests_by_flow.setdefault(fid, []).append(t)
    reach_by_id = {f.get("id"): f for f in items(get(a["reach"], "flows"))}
    flow_ids = list(reach_by_id)
    if any(p.get("flowId") == "infrastructure" for p in probes) or "infrastructure" in tests_by_flow:
        flow_ids.append("infrastructure")
    out = []
    def changed_for(t, fid):
        # With per-method cases, a test changes a flow only through the failing methods that cover that flow.
        cases = items(t.get("cases"))
        if not cases:
            return t in proven
        return any(c.get("outcome") == "different" and fid in as_list(c.get("flowIds")) for c in cases)

    for fid in flow_ids:
        untraced = as_list(get(reach_by_id.get(fid), "untraced"))
        fp = [p for p in probes if p.get("flowId") == fid]
        ft = tests_by_flow.get(fid, [])
        changed_probes = [p for p in fp if p.get("outcome") == "different"]
        changed_tests = [t for t in ft if changed_for(t, fid)]
        out.append({"id": fid, "untraced": untraced, "probes": fp, "tests": ft,
                    "changedProbes": changed_probes, "changedTests": changed_tests,
                    "changed": bool(changed_probes or changed_tests),
                    "error": any(p.get("outcome") == "error" for p in fp)})
    return out


class Plain:
    """Plain-language text for a rule or hypothesis id: the analyst's hypothesis or explanation, else the rule."""

    def __init__(self, a: dict, findings: list):
        self.hyp = {s(h.get("id")): h for h in items(get(a["hypotheses"], "hypotheses"))}
        self.expl = {s(x.get("ruleId")): x for x in items(get(a["hypotheses"], "explanations"))}
        self.title = {s(f.get("ruleId")): f.get("title") for f in findings}
        self.impact = {s(f.get("ruleId")): f.get("impact") for f in findings}

    def text(self, rule_id, field: str = "plain"):
        rid = s(rule_id)
        src = self.hyp.get(rid) or self.expl.get(rid)
        if src and s(src.get(field)).strip():
            return s(src.get(field)).strip()
        if field == "plain":
            return s(self.title.get(rid))
        if field == "productionRisk":
            return s(self.impact.get(rid))
        return ""

    def headline(self, rule_id) -> str:
        return first_sentence(self.text(rule_id), 120)


def endpoint_name(fid: str) -> str:
    return "Health check and other platform endpoints" if fid == "infrastructure" else fid


def manual_item_ids(a: dict, kind: str, refs) -> list[str]:
    refs = {s(r) for r in refs}
    return [s(i.get("id")) for i in items(get(a["manual"], "items")) if i.get("kind") == kind and s(i.get("ref")) in refs]


# ---------- the summary ----------

def build_summary(run_dir: str, run_id: str, inp=None) -> list[str]:
    a = load(run_dir, inp)
    env, v, b = a["environment"] or {}, a["verdict"], a["behavior"] or {}
    base = get(env, "branches", "base", "name") or "base"
    head = get(env, "branches", "head", "name") or "head"
    tests = items(b.get("tests"))
    probes = items(b.get("probes"))
    checks = items(b.get("checks"))
    findings = items(get(a["impact"], "findings"))
    status_by_id = {x.get("id"): x.get("status") for x in items(get(v, "findingStatus") or get(a["flows"], "findingStatus"))}
    reviews = {r.get("testId"): r for r in items(get(a["reviewAnswer"], "reviews"))}
    proven = [t for t in tests if get(t, "base", "passed") and not get(t, "head", "passed")]
    existing_changed = [t for t in items(get(a["testResults"], "tests")) if t.get("change") not in ("same", None)]
    flows = flow_view(a, tests, probes, proven)
    plain = Plain(a, findings)

    # Risks, grouped by rule: proven same, proven changed, or not proven either way.
    by_rule = {}
    for f in findings:
        by_rule.setdefault(s(f.get("ruleId")), []).append(f)
    verified_rules = [r for r, fs in by_rule.items() if all(status_by_id.get(f.get("id")) in ("verified", "accepted") for f in fs)]
    open_rules = [r for r, fs in by_rule.items() if any(status_by_id.get(f.get("id")) in ("open", "hypothesis") for f in fs)]
    changed_flows = [f for f in flows if f["changed"]]
    n_issues = len(changed_flows) + len(existing_changed)

    md = [f"# Upgrade check: {base} vs {head}", "", f"Run `{run_id}`. Same code checked on both branches, results compared.", ""]

    # ---- 1. The answer
    md += ["## 1. The answer", ""]
    failed_steps = [x for x in items(get(a["status"], "steps")) if x.get("state") == "failed"]
    verdict_name = s(get(v, "verdict"))
    if verdict_name == "different":
        nd = len(as_list(get(v, "differences")))
        md.append(f"{BAD} **{head} behaves differently from {base}: {nd} check(s) give another result on {head}, on "
                  f"{n_issues} endpoint(s) or test(s)** (section 3).")
    elif verdict_name == "incomplete":
        md.append(f"{WARN} **The comparison is incomplete: {len(as_list(get(v, 'incomplete')))} check(s) or stage(s) did not "
                  "run** (section 6). The results below cover only what ran.")
    elif verdict_name == "same":
        md.append(f"{OK} **{head} behaves the same as {base}:** every change was checked on both branches with the same result.")
    elif verdict_name == "same-where-tested":
        md.append(f"{OK} **{head} behaves the same as {base} wherever it was tested.** {WARN} Some changes are not proven "
                  "either way (section 5).")
    elif v:
        md.append(f"{WARN} **Verdict {verdict_name}** (from an older version of the tool).")
    elif failed_steps:
        md.append(f"{WARN} **No answer: the run stopped at step `{failed_steps[-1].get('name')}`.** See the run trace in section 7.")
    else:
        md.append(f"{WARN} **No answer yet: the run has not finished.** See the run trace in section 7.")
    md += ["",
           f"- {BAD} Proven different: **{n_issues}** endpoint(s) or existing test(s) (section 3)",
           f"- {OK} Proven no issue: **{len(verified_rules)}** risk area(s) checked on both branches with the same result (section 4)",
           f"- {WARN} Not proven either way: **{len(open_rules)}** potential issue(s), and why (section 5)", ""]

    # ---- 2. Side by side
    md += [f"## 2. {base} and {head} side by side", ""]
    rows = []
    bjava, hjava = s(get(env, "branches", "base", "java")), s(get(env, "branches", "head", "java"))
    if bjava or hjava:
        rows.append(["Java", short_text(bjava, 60), short_text(hjava, 60), "changed (the upgrade)"])
    boot = next((c for c in items(get(a["deps"], "changed")) if c.get("direct") and "spring-boot" in s(c.get("ga"))), None)
    if boot:
        rows.append(["Spring Boot", boot.get("base"), boot.get("head"), "changed (the upgrade)"])
    if a["deps"]:
        sm = a["deps"].get("summary") or {}
        rows.append(["Libraries", f"{get(a['deps'], 'base', 'artifacts')} resolved", f"{get(a['deps'], 'head', 'artifacts')} resolved",
                     f"{sm.get('changed')} versions changed ({sm.get('majorChanged')} major), {sm.get('added')} added, {sm.get('removed')} removed"])
    builds = a["builds"]
    if builds:
        bs, hs = get(builds, "sides", "base") or {}, get(builds, "sides", "head") or {}
        ok = lambda sd: "builds" if sd.get("exitCode") == 0 else "BUILD FAILS"  # noqa: E731
        rows.append(["Build", ok(bs), ok(hs), OK if bs.get("exitCode") == 0 and hs.get("exitCode") == 0 else BAD])
        tp = lambda sd: f"{get(sd, 'tests', 'passed')} of {get(sd, 'tests', 'total')} pass"  # noqa: E731
        rows.append(["Existing tests", tp(bs), tp(hs), BAD + " " + str(len(existing_changed)) + " differ" if existing_changed else OK + " same"])
    srv = b.get("servers") or {}
    if srv:
        up = lambda sd: (f"starts in {round(to_s(sd.get('startupMs')), 1)} s" if sd.get("up") else "DOES NOT START")  # noqa: E731
        rows.append(["Application startup", up(srv.get("base") or {}), up(srv.get("head") or {}),
                     OK if get(srv, "base", "up") and get(srv, "head", "up") else BAD])
    if probes:
        diff = [p for p in probes if p.get("outcome") != "same"]
        rows.append(["Endpoint checks (same request to both)", f"{len(probes)} requests", f"{len(probes)} requests",
                     f"{OK} {len(probes) - len(diff)} same" + (f", {BAD} {len(diff)} different" if diff else "")])
    if tests:
        bp = len([t for t in tests if get(t, "base", "passed")])
        hp = len([t for t in tests if get(t, "head", "passed")])
        rows.append(["New tests (written against " + base + ")", f"{bp} of {len(tests)} pass", f"{hp} of {len(tests)} pass",
                     OK + " same" if bp == hp == len(tests) else f"{BAD} {bp - hp} fail only on {head}"])
    dumps = b.get("dumps") or {}
    if dumps.get("available"):
        bb = dumps.get("beans") or {}
        renamed = len(as_list(bb.get("typeChanged"))) + len(as_list(bb.get("added")))
        rows.append(["Spring components", f"{bb.get('base')}", f"{bb.get('head')}", f"{WARN} {renamed} renamed or moved by the framework"
                     if renamed else OK + " same"])
    md += table(["What", base, head, "Same?"], rows)

    erows = []
    for f in flows:
        nprobe, ntest = len(f["probes"]), len(f["tests"])
        existing = "existing tests" if f["id"] != "infrastructure" and not f["untraced"] else ""
        what = ", ".join(x for x in (existing, f"{nprobe} request(s)" if nprobe else "", f"{ntest} new test(s)" if ntest else "") if x) or "nothing"
        base_ok = all(get(t, "base", "passed") for t in f["tests"])
        main_text = "all pass" if base_ok else "some tests FAIL"
        if f["changed"]:
            head_text = "; ".join([f"different answer to {p.get('request')}" for p in f["changedProbes"]]
                                  + [f"new test fails: {humanize(get(t, 'head', 'detail'))}" for t in f["changedTests"]])
            mark = BAD + " different"
        elif f["error"]:
            head_text, mark = "a check could not run", WARN + " not compared"
        else:
            head_text, mark = "same as " + base, OK + " same"
        if f["untraced"] and not f["tests"]:
            mark += f" ({WARN} part of its code is never run by a test)"
        erows.append([endpoint_name(f["id"]), what, main_text, head_text, mark])
    if erows:
        md += ["Each endpoint, called the same way on both branches:", ""]
        md += table(["Endpoint", "Compared with", base, head, "Result"], erows)

    # ---- 3. Issues found
    md += [f"## 3. Issues found: proven different on {head}", ""]
    if not changed_flows and not existing_changed:
        md += [f"{OK} None. Every check gave the same result on both branches.", ""]
    rule_of_finding = {s(x.get("id")): s(x.get("ruleId")) for x in findings}

    def failing_cases(t, fid):
        cases = [c for c in items(t.get("cases")) if c.get("outcome") == "different" and fid in as_list(c.get("flowIds"))]
        return cases or ([{"method": n, "findingIds": []} for n in re.split(r",\s*", s(get(t, "head", "detail"))) if n]
                         if not items(t.get("cases")) else [])

    for k, f in enumerate(changed_flows, start=1):
        # The rule that explains the difference: the findings of the failing test methods first, then the probes'.
        rule_ids = [rule_of_finding.get(i) for t in f["changedTests"] for c in failing_cases(t, f["id"])
                    for i in as_list(c.get("findingIds")) if rule_of_finding.get(i)]
        rule_ids += [r for t in f["changedTests"] if not items(t.get("cases")) for r in as_list(t.get("linkedRules"))]
        rule_ids += [r for p in f["changedProbes"] for r in as_list(p.get("linkedRules"))]
        rule = next((r for r in rule_ids if r in plain.hyp), next((r for r in rule_ids if r in plain.expl), rule_ids[0] if rule_ids else ""))
        md += [f"### {k}. {endpoint_name(f['id'])}: {plain.headline(rule) or 'behaves differently'}", ""]
        explanation = plain.text(rule)
        rest = explanation[len(plain.headline(rule)):].strip() if explanation.startswith(plain.headline(rule)) else explanation
        if rest:
            md += [rest, ""]
        ex_rows = []
        for p in f["changedProbes"]:
            ex_rows.append([p.get("request"), f"{get(p, 'base', 'status')} {short_text(get(p, 'base', 'body'), 90)}",
                            f"{get(p, 'head', 'status')} {short_text(get(p, 'head', 'body'), 90)}"])
        for t in f["changedTests"]:
            for c in failing_cases(t, f["id"]):
                name = s(c.get("method"))
                ex_rows.append([f"New test {t.get('id')} `{name}`: expects {base}'s behavior ({humanize(name)})",
                                f"{OK} passes", f"{BAD} fails"])
        md += ["What was observed:", ""]
        md += table(["Check", base, head], ex_rows)
        risk = plain.text(rule, "productionRisk")
        if risk:
            md += [f"**Who is affected:** {risk}", ""]
    for t in existing_changed:
        md += [f"### Existing test changed result: {t.get('test')}", "",
               f"{base}: {t.get('base') or 'not present'}; {head}: {t.get('head') or 'not present'}. {short_text(t.get('message'), 200)}", ""]

    # ---- 4. Proven: no issue
    md += ["## 4. Proven: no issue", "",
           "These risks were checked by running the same requests or tests on both branches. The result was the same.", ""]
    prow = []
    for r in verified_rules:
        linked = [c for c in checks if c.get("outcome") == "same" and any(check_applies(c, f) for f in by_rule[r])]
        how = []
        nprobe = len({c.get("id") for c in linked if c.get("source") == "probe"})
        if nprobe:
            how.append(f"{nprobe} endpoint request(s)")
        tids = sorted({c.get("id") for c in linked if c.get("source") == "test"})
        if tids:
            how.append(f"new test(s) {', '.join(tids)}")
        prow.append([short_text(plain.text(r), 150) or r, "; ".join(how) or "checked", OK + " same on both"])
    md += table(["Risk", "How it was checked", "Result"], prow)
    if flows:
        covered = [f for f in flows if not f["changed"] and not f["error"] and (not f["untraced"] or f["tests"])]
        md += [f"Endpoints proven to behave the same: {len(covered)} of {len(flows)} "
               f"({', '.join(endpoint_name(f['id']) for f in covered) or 'none'}).", ""]

    # ---- 5. Potential issues
    md += ["## 5. Potential issues: not proven either way", "",
           "Each item below could break something, but no automated check proved it either way. The last column says why.", ""]
    gaps = {g.get("id"): g for g in items(get(a["gaps"], "gaps"))}
    uncovered = {}
    for u in items(get(a["manifest"], "uncovered")):
        ref = s(get(gaps.get(u.get("gapId")), "ref"))
        uncovered[ref] = u.get("reason")
    pot = []
    for r in open_rules:
        fs = [f for f in by_rule[r] if status_by_id.get(f.get("id")) in ("open", "hypothesis")]
        f0 = fs[0]
        why_risk = plain.text(r, "productionRisk") or plain.text(r)
        if any(f.get("id") in uncovered for f in fs):
            why_not = "The test writer judged it not testable: " + short_text(next(uncovered[f.get("id")] for f in fs if f.get("id") in uncovered), 220)
        elif f0.get("scope") in ("none", "test"):
            why_not = "Not runtime behavior (a build or test-setup change): there is nothing to call in the running application."
        elif s(f0.get("severity")) == "low":
            why_not = "Rated low risk, so no automated test was generated for it (tests are generated for high and medium risks)."
        else:
            why_not = "No endpoint request or test covers it."
        how = s(f0.get("check")) if f0.get("source") == "analyst" else ""
        who = "QA" if f0.get("source") == "analyst" else "Developer"
        pot.append([short_text(plain.text(r), 150) or r, short_text(why_risk, 220), why_not, f"{who}: {short_text(how, 200)}" if how else who])
    for m in items(get(a["changedMethods"], "methods")):
        if not m.get("tested") and m.get("kind") != "file":
            lines = as_list(get(m, "lines", "uncovered"))
            where = f"lines {', '.join(str(x) for x in lines)}" if lines else "the method"
            pot.append([f"Changed {m.get('kind')} {m.get('name')} ({m.get('file')})",
                        "A changed line that no test runs can break without any test noticing.",
                        f"No existing or new test runs {where} on {head}.", "Developer: add a test that runs it"])
    for w in items(get(a["asserts"], "methods")):
        if w.get("strength") == "weak":
            alive = [x for x in items(w.get("mutants")) if x.get("result") == "survived"]
            pot.append([f"Weak assertions for {w.get('name')} ({w.get('file')})",
                        "The existing tests run the changed lines but would not notice if they changed.",
                        "Changing " + "; ".join(f"line {x.get('line')}: {short_text(x.get('original'), 60)} -> "
                                                f"{short_text(x.get('mutated'), 60)}" for x in alive[:3])
                        + " made no existing test fail.", "Developer: strengthen the assertions (a new test covers it when written)"])
    for g in items(get(a["notRun"], "gaps")):
        pot.append([g.get("item"), "A part of the regression the automation did not run.", short_text(g.get("reason"), 200), "QA or test lead"])
    not_found = [i for i in items(get(a["notes"], "items")) if i.get("result") == "not-found"]
    if not_found:
        pot.append([f"{len(not_found)} library change(s) the code uses",
                    "A library the code calls changed its API between the versions.",
                    "The release notes do not mention them, so nothing confirms the change is harmless; the code does not call the "
                    "removed parts.", "Developer: check the library changelog or source"])
    md += table(["Potential issue", "Why it could be a problem", "Why the automation did not test it", "Who checks, and how"], pot)

    # ---- 6. What to do
    md += ["## 6. What to do", ""]
    md += ["**Business analyst / product owner**", ""]
    if changed_flows:
        md += [f"- [ ] Read section 3: each item is a proven behavior change of {head}. Keep it, or ask the developers for "
               f"{base}'s behavior."]
    else:
        md += ["- [ ] Nothing to read in section 3: no behavior difference was found."]
    md += ["", "**QA**", ""]
    smoke = [f for f in flows if not f["changed"] and not f["error"] and (not f["untraced"] or f["tests"])]
    for f in changed_flows:
        md.append(f"- [ ] Retest **{endpoint_name(f['id'])}** around the change in section 3.")
    for r in open_rules:
        f0 = [f for f in by_rule[r] if status_by_id.get(f.get("id")) in ("open", "hypothesis")][0]
        if f0.get("source") == "analyst":
            md.append(f"- [ ] Check: {short_text(plain.text(r), 150)} **How:** {short_text(f0.get('check'), 240)}")
    for f in flows:
        if f["untraced"] and not f["tests"] and not f["changed"]:
            md.append(f"- [ ] Fully test **{endpoint_name(f['id'])}**: no automated test runs part of its code.")
    if smoke:
        md.append(f"- [ ] Smoke test only (one normal request each): {', '.join(endpoint_name(f['id']) for f in smoke)}. "
                  "The automation proved them the same on both branches.")
    md += ["", "**Developers**", ""]
    dev = []
    placed = items(get(a["newTests"], "placed"))
    if placed:
        dev.append(f"Review and commit the {len(placed)} new test(s) placed in `{get(a['newTests'], 'location')}` "
                   "(commands in section 10 of the detailed report).")
    for t in tests:
        rev = reviews.get(t.get("id"))
        if rev and rev.get("verdict") == "fail":
            dev.append(f"Fix new test {t.get('className')}: the independent reviewer failed it: {first_sentence(rev.get('notes'), 220)}")
    for r in open_rules:
        f0 = [f for f in by_rule[r] if status_by_id.get(f.get("id")) in ("open", "hypothesis")][0]
        if f0.get("source") != "analyst":
            dev.append(f"{plain.headline(r).rstrip('.')}: {short_text(f0.get('evidence'), 160)}")
    md += [f"- [ ] {x}" for x in dev] or ["- [ ] Nothing."]
    md.append("")

    # ---- 7. Details (collapsed in HTML)
    md += ["## 7. Details", "", "<details><summary>New tests written by the automation</summary>", ""]
    gap_by_id = gaps
    trows = []
    for t in tests:
        m = next((x for x in items(get(a["manifest"], "tests")) if x.get("id") == t.get("id")), {})
        why = "; ".join(short_text(gap_by_id[g].get("detail"), 90) for g in as_list(m.get("gapIds"))[:2] if g in gap_by_id)
        inj = items(t.get("injections"))
        rev = reviews.get(t.get("id"))
        trows.append([t.get("id"), t.get("className"), first_sentence(t.get("purpose"), 120), why, pass_fail(t.get("base")),
                      pass_fail(t.get("head")), f"{len([i for i in inj if i.get('detected')])} of {len(inj)}",
                      (rev or {}).get("verdict") or "-"])
    md += ["Each test was written against " + base + ", run unchanged on both branches, then run again with a deliberately "
           "broken copy of the code: a useful test must catch every break.", ""]
    md += table(["Id", "Test class", "What it checks", "Why it was needed", base, head, "Breaks caught", "Reviewer"], trows)
    md += ["</details>", "", "<details><summary>Reference: ids used by the detailed report</summary>", ""]
    md += table(["Risk", "Ids", "Status"], [[plain.headline(r) or r, ", ".join(f.get("id") for f in fs) + f" (rule {r})",
                                             ", ".join(sorted({s(status_by_id.get(f.get('id'))) for f in fs}))] for r, fs in by_rule.items()])
    md += ["</details>", "", "<details><summary>Run trace (for troubleshooting)</summary>", ""]
    md += run_trace(run_dir, a)
    md += ["</details>", ""]
    return md


def to_s(ms) -> float:
    try:
        return float(ms) / 1000.0
    except (TypeError, ValueError):
        return 0.0


# ---------- HTML for the same markdown ----------

def markdown_to_html(lines: list[str], title: str) -> str:
    """HTML for the markdown subset build_summary writes: headings, paragraphs, bullet and numbered lists,
    checklists, tables, <details> blocks, **bold** and `code`."""
    def inline(t: str) -> str:
        t = html.escape(t, quote=True)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        return re.sub(r"`(.+?)`", r"<code>\1</code>", t)

    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
           f"<title>{html.escape(title)}</title>",
           "<style>:root{--bg:#FFFFFF;--fg:#2C2C2A;--line:#D3D1C7;--head:#F1EFE8;--muted:#5F5E5A}@media (prefers-color-scheme: dark)"
           "{:root{--bg:#1F1E1B;--fg:#EDEBE4;--line:#5F5E5A;--head:#2C2C2A;--muted:#B4B2A9}}body{font-family:Arial,Helvetica,sans-serif;"
           "margin:24px auto;padding:0 16px;color:var(--fg);background:var(--bg);line-height:1.5;max-width:1200px}h1{font-size:24px}"
           "h2{font-size:19px;margin-top:32px;border-bottom:1px solid var(--line);padding-bottom:4px}h3{font-size:16px;margin-top:22px}"
           ".scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px;margin:8px 0 16px}th,td{border:1px solid "
           "var(--line);padding:6px 8px;text-align:left;vertical-align:top}th{background:var(--head)}code{font-size:13px;background:"
           "var(--head);padding:1px 4px;border-radius:3px}details{margin:10px 0;border:1px solid var(--line);border-radius:6px;padding:"
           "8px 12px}summary{cursor:pointer;font-weight:bold}ul.check{list-style:none;padding-left:4px}</style></head><body>"]
    i = 0
    in_list = None
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^(\d+)\. (.*)$", line)
        if line.startswith("- [ ] "):
            kind, body = "check", "☐ " + line[6:]
        elif line.startswith("- "):
            kind, body = "ul", line[2:]
        elif m:
            kind, body = "ol", m.group(2)
        else:
            kind, body = None, ""
        if kind:
            if in_list != kind:
                if in_list:
                    out.append("</ol>" if in_list == "ol" else "</ul>")
                out.append("<ol>" if kind == "ol" else ("<ul class='check'>" if kind == "check" else "<ul>"))
                in_list = kind
            out.append(f"<li>{inline(body)}</li>")
            i += 1
            continue
        if in_list:
            out.append("</ol>" if in_list == "ol" else "</ul>")
            in_list = None
        if line.startswith("<details>"):
            sm = re.search(r"<summary>(.*?)</summary>", line)
            out.append(f"<details><summary>{inline(sm.group(1)) if sm else 'Details'}</summary>")
        elif line.startswith("</details>"):
            out.append("</details>")
        elif line.startswith("### "):
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
            out.append("<div class='scroll'><table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in split(block[0]))
                       + "</tr></thead><tbody>")
            for r in block[2:]:
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in split(r)) + "</tr>")
            out.append("</tbody></table></div>")
            continue
        elif line.strip():
            out.append(f"<p>{inline(line)}</p>")
        i += 1
    if in_list:
        out.append("</ol>" if in_list == "ol" else "</ul>")
    out.append("</body></html>")
    return "\n".join(out) + "\n"
