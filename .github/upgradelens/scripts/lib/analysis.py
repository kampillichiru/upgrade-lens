"""UpgradeLens analysis functions for stages 1 to 10: parsers, rule matching, dependency assessment, flow
reach and coverage, finding status and report helpers. Pure functions, so tests\\ can call them."""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET

from .core import (as_list, get, icontains, items, like, ps_sorted, rule_applies, s, sort_key, symbol_for_line, to_int,
                   unique, unique_sorted)


def parse_dependency_analyze(lines) -> dict:
    """maven-dependency-plugin prints two blocks of "[WARNING]    group:artifact:type:version:scope" lines."""
    unused, undeclared = [], []
    mode = ""
    for raw in as_list(lines):
        line = s(raw)
        if re.search(r"Unused declared dependencies found", line, re.I):
            mode = "unused"
            continue
        if re.search(r"Used undeclared dependencies found", line, re.I):
            mode = "undeclared"
            continue
        m = re.search(r"^\[WARNING\]\s+([A-Za-z0-9_.\-]+:\S+)\s*$", line, re.I) if mode else None
        if m:
            (unused if mode == "unused" else undeclared).append(m.group(1))
            continue
        if mode:
            mode = ""
    return {"unused": unused, "undeclared": undeclared}


def read_unified_patch(lines) -> dict:
    """Parses "git diff -U0" output: path -> {"added": [{line, text}], "removed": [...]}.
    Line numbers are on each side's own numbering (new for added, old for removed)."""
    files: dict[str, dict] = {}
    entry = None
    in_hunk = False
    old_ln = new_ln = 0
    for raw in as_list(lines):
        line = s(raw).rstrip("\r")
        if line.startswith("diff --git "):
            entry = None
            in_hunk = False
            continue
        if in_hunk and entry is not None:
            if line.startswith("+"):
                entry["added"].append({"line": new_ln, "text": line[1:]})
                new_ln += 1
                continue
            if line.startswith("-"):
                entry["removed"].append({"line": old_ln, "text": line[1:]})
                old_ln += 1
                continue
            if line.startswith("\\"):
                continue
        m = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m:
            old_ln, new_ln = int(m.group(1)), int(m.group(2))
            in_hunk = True
            continue
        m = re.match(r"^\+\+\+ b/(.+)$", line)
        if m:
            entry = {"added": [], "removed": []}
            files[m.group(1)] = entry
            in_hunk = False
            continue
    return files


def group_key(coordinate: str) -> str:
    """"g:a:jar:v:scope" -> "g:a"."""
    p = s(coordinate).split(":")
    if len(p) >= 2:
        return f"{p[0]}:{p[1]}"
    return s(coordinate)


def symbol_key(symbol: str | None, file_simple_name: str) -> list[str]:
    """"method:X" in a file Foo.java -> ["Foo#X"]; "type:X" -> ["X#*"]; None -> []."""
    if not symbol:
        return []
    if like(symbol, "method:*"):
        return [f"{file_simple_name}#{symbol[7:]}"]
    if like(symbol, "type:*"):
        return [symbol[5:] + "#*"]
    return []


def new_rule_finding(rule: dict, file: str, line, evidence: str, symbols=(), scope: str = "") -> dict:
    effective = scope
    symbols = list(symbols)
    if effective == "symbol" and not symbols:
        effective = "global"
    return {"ruleId": rule.get("id"), "title": rule.get("title"), "severity": rule.get("severity"),
            "scope": effective, "file": file, "line": line, "symbols": symbols, "evidence": evidence,
            "impact": rule.get("impact"), "check": rule.get("check"), "source": "rule"}


def _simple_name(path: str) -> str:
    return os.path.splitext(os.path.basename(s(path).replace("\\", "/").split("/")[-1]))[0]


def rule_findings(rule: dict, ctx: dict) -> list[dict]:
    """One rule against the context. Context keys:
      patch (read_unified_patch output), head_lines and base_lines (path -> lines, for symbol lookup),
      inventory (change-inventory files), inventory_findings, dep_diff, dep_analyze (base and head),
      api_diff, library_api, it_files (relative paths of *IT.java in head), it_mode."""
    out = []
    source = rule.get("source")
    scope = rule.get("scope")

    if source in ("diff-added", "diff-removed", "diff-any"):
        sides = {"diff-added": ["added"], "diff-removed": ["removed"]}.get(source, ["added", "removed"])
        patch = ctx.get("patch") or {}
        pattern = re.compile(s(rule.get("pattern")), re.I)
        for path in ps_sorted(patch.keys()):
            if not rule_applies(rule, path):
                continue
            entry = patch[path]
            simple = _simple_name(path)
            for side in sides:
                entries = entry["added"] if side == "added" else entry["removed"]
                lines = (ctx.get("head_lines") or {}).get(path) if side == "added" else (ctx.get("base_lines") or {}).get(path)
                for ln in entries:
                    if not pattern.search(ln["text"]):
                        continue
                    sym = symbol_for_line(lines, ln["line"]) if path.lower().endswith(".java") else None
                    sym_keys = symbol_key(sym, simple)
                    evidence = f"{side} line {ln['line']}: {ln['text'].strip()}"
                    out.append(new_rule_finding(rule, path, ln["line"], evidence, sym_keys, scope))

    elif source == "inventory":
        for f in as_list(ctx.get("inventory")):
            if not f or f.get("category") != rule.get("category"):
                continue
            entries = as_list(f.get(rule.get("field")))
            if not entries:
                continue
            simple = _simple_name(f.get("path"))
            sym_keys = [f"{simple}#{m}" for m in as_list(f.get("changedMethods"))]
            if not sym_keys:
                sym_keys = [f"{simple}#*"]
            evidence = "; ".join(s(x) for x in entries[:4])
            out.append(new_rule_finding(rule, f.get("path"), None, evidence, sym_keys, scope))

    elif source == "inventory-findings":
        for f in as_list(ctx.get("inventory_findings")):
            if f and icontains(rule.get("types"), f.get("type")):
                out.append(new_rule_finding(rule, f.get("file"), None, f"{f.get('type')}: {f.get('detail')}", [], scope))

    elif source == "dependency":
        dd = ctx.get("dep_diff") or {}
        match = rule.get("match")
        if match == "major-direct":
            hits = [x for x in as_list(dd.get("changed")) if x.get("direct") and x.get("major")]
        elif match == "major-transitive":
            hits = [x for x in as_list(dd.get("changed")) if not x.get("direct") and x.get("major")]
        elif match == "added-direct":
            hits = [x for x in as_list(dd.get("added")) if x.get("direct")]
        elif match == "removed-direct":
            hits = [x for x in as_list(dd.get("removed")) if x.get("direct")]
        else:
            hits = []
        if hits:
            # Dependency findings are grouped per rule: one row lists every artifact in the group.
            evidence = "; ".join(f"{x['ga']} {x.get('base')} -> {x.get('head')}" if "base" in x else f"{x['ga']} {x.get('version')}"
                                 for x in hits)
            out.append(new_rule_finding(rule, "pom.xml", None, f"{len(hits)} artifact(s): {evidence}", [], scope))

    elif source == "dependency-analyze":
        da = ctx.get("dep_analyze") or {}
        base_undeclared = [group_key(x) for x in as_list(get(da, "base", "undeclared"))]
        base_unused = [group_key(x) for x in as_list(get(da, "base", "unused"))]
        if rule.get("match") == "undeclared-new":
            hits = [x for x in as_list(get(da, "head", "undeclared")) if not icontains(base_undeclared, group_key(x))]
        else:
            hits = [x for x in as_list(get(da, "head", "unused")) if not icontains(base_unused, group_key(x))]
        if hits:
            out.append(new_rule_finding(rule, "pom.xml", None, f"{len(hits)} new: {'; '.join(hits)}", [], scope))

    elif source == "api":
        for c in as_list(get(ctx, "api_diff", "classes")):
            raw = c.get("removedMembers") if rule.get("match") == "removed" else c.get("addedMembers")
            members = [m for m in as_list(raw) if m]
            simple = s(c.get("fqcn")).split(".")[-1]
            for m in members:
                mm = re.search(r"(\w+)\s*\(", m, re.I)
                name = mm.group(1) if mm else "*"
                out.append(new_rule_finding(rule, c.get("fqcn"), None, m, [f"{simple}#{name}"], scope))

    elif source == "it-not-run":
        # it_mode comes from stage 1 (build-results.json): failsafe, surefire (run by UpgradeLens because the
        # pom has no failsafe plugin), not-run, or none (no IT classes).
        if ctx.get("it_mode") == "not-run":
            for it in as_list(ctx.get("it_files")):
                leaf = s(it).replace("\\", "/").split("/")[-1]
                out.append(new_rule_finding(rule, it, None, f"no maven-failsafe-plugin in pom.xml and build.integrationTests is "
                                            f"'failsafe-only', so {leaf} did not run in stage 1", [], scope))

    elif source == "library-api":
        # Library API changes (stage 3, library-api-diff.json) that the application code touches.
        #   removed-used   a library class or member that master's code used is gone on change_branch
        #   changed-used   a library class that change_branch's code uses has a changed public API
        for item in as_list(get(ctx, "library_api", "items")):
            if not item:
                continue
            match = rule.get("match")
            hit = (match == "removed-used" and item.get("severity") == "high") or \
                  (match == "changed-used" and item.get("severity") == "medium")
            if not hit:
                continue
            users = [u for u in as_list(get(item, "usage", "base")) + as_list(get(item, "usage", "head")) if u]
            keys = [f"{re.split(r'[.$]', s(u.get('appClass')))[-1]}#{s(u.get('method'))}" for u in users]
            sym_keys = unique_sorted([k for k in keys if not k.endswith("#")])
            first = users[0] if users else None
            file = first["source"] if first and first.get("source") else item.get("class")
            line = first.get("line") if first else None
            evidence = (f"{item.get('id')} {item.get('class')} [{get(item, 'artifact', 'base')} -> "
                        f"{get(item, 'artifact', 'head')}]: {item.get('summary')}")
            out.append(new_rule_finding(rule, file, line, evidence, sym_keys, scope))
    return out


def tree_descendants(tree, ga: str) -> list[dict]:
    """Artifacts below one tree entry (the lines after it that are deeper), from parse_dependency_tree."""
    entries = as_list(tree)
    for i, e in enumerate(entries):
        if e.get("ga") != ga or to_int(e.get("depth")) < 1:
            continue
        d = to_int(e.get("depth"))
        out = []
        for later in entries[i + 1:]:
            if to_int(later.get("depth")) <= d:
                break
            out.append(later)
        return out
    return []


def is_starter(ga: str) -> bool:
    """A starter is an aggregator pom-jar: it has no classes of its own, so "unused declared" says nothing."""
    return like(s(ga).split(":")[-1], "*-starter*")


def replacement_score(added: str, removed: str) -> int:
    """How likely the added artifact replaces the removed one. 0 = not related. Higher is closer.
      same artifactId, new groupId (a relocation, e.g. com.fasterxml.jackson.core -> tools.jackson.core)
      same groupId and one artifactId extends the other (spring-boot-starter-web -> spring-boot-starter-webmvc)
      same groupId and the artifactIds differ only in a trailing number"""
    ag, _, aa = s(added).partition(":")
    rg, _, ra = s(removed).partition(":")
    if aa.casefold() == ra.casefold() and ag.casefold() != rg.casefold():
        return 1000
    if ag.casefold() != rg.casefold():
        return 0
    if re.sub(r"\d+$", "", aa).casefold() == re.sub(r"\d+$", "", ra).casefold():
        return 900
    if aa.startswith(ra) or ra.startswith(aa):
        # A test variant of a main artifact is not its replacement.
        if like(aa, "*-test") != like(ra, "*-test"):
            return 0
        return 500 - abs(len(aa) - len(ra))
    return 0


def replacement_pairs(added_gas, removed_gas) -> dict:
    """Pairs each removed direct artifact with at most one added direct artifact (best score first, each
    artifact used once). Returns removed ga -> added ga."""
    cands = []
    for a in as_list(added_gas):
        for r in as_list(removed_gas):
            sc = replacement_score(a, r)
            if sc > 0:
                cands.append({"added": a, "removed": r, "score": sc})
    cands.sort(key=lambda c: (-c["score"], sort_key(c["added"]), sort_key(c["removed"])))
    pairs, used_added = {}, set()
    for c in cands:
        if c["removed"] in pairs or c["added"] in used_added:
            continue
        pairs[c["removed"]] = c["added"]
        used_added.add(c["added"])
    return pairs


def dependency_assessment(diff: dict, base_tree, head_tree, analyze: dict, ignore_unused=()) -> list[dict]:
    """One row per added or removed direct dependency, and per direct version change: is it required,
    unnecessary, a replacement, or does it need a person to look; and what it can affect.
      required     code uses its classes (dependency:analyze), or code uses an artifact a starter brings
      replacement  it takes the place of a removed direct dependency
      unnecessary  declared, not a starter, and no compiled code uses it
      review       cannot be decided from the build: a starter whose artifacts no code calls, which works
                   through auto-configuration at runtime (the probes and the bean dump show its effect)
    analyze: {"base": {unused, undeclared}, "head": {...}} from parse_dependency_analyze."""
    rows = []
    base_gas = {b["ga"] for b in as_list(base_tree) if to_int(b.get("depth")) >= 1}
    head_unused = [group_key(x) for x in as_list(get(analyze, "head", "unused"))]
    head_used_undeclared = [group_key(x) for x in as_list(get(analyze, "head", "undeclared"))]
    base_unused = [group_key(x) for x in as_list(get(analyze, "base", "unused"))]
    base_used_undeclared = [group_key(x) for x in as_list(get(analyze, "base", "undeclared"))]
    removed_direct = [x["ga"] for x in as_list(diff.get("removed")) if x.get("direct")]
    added_direct = [x["ga"] for x in as_list(diff.get("added")) if x.get("direct")]
    pairs = replacement_pairs(added_direct, removed_direct)
    replaces_by_added = {v: k for k, v in pairs.items()}

    for a in [x for x in as_list(diff.get("added")) if x.get("direct")]:
        entry = next((t for t in as_list(head_tree) if t.get("ga") == a["ga"] and to_int(t.get("depth")) == 1), None)
        scope = entry.get("scope", "") if entry else ""
        desc = tree_descendants(head_tree, a["ga"])
        introduces = unique_sorted([f"{d['ga']}:{d.get('version')}" for d in desc if d["ga"] not in base_gas])
        used_through = unique_sorted([d["ga"] for d in desc if icontains(head_used_undeclared, d["ga"])])
        starter = is_starter(a["ga"])
        ignored = any(like(a["ga"], p) for p in as_list(ignore_unused))
        unused = icontains(head_unused, a["ga"]) and not ignored
        replaces = replaces_by_added.get(a["ga"])

        reasons = []
        if not starter and not unused:
            assessment = "required"
            reasons.append("compiled code uses its classes (dependency:analyze)")
        elif starter and used_through:
            assessment = "required"
            reasons.append(f"code uses {len(used_through)} artifact(s) it brings: {', '.join(used_through)}")
        elif starter:
            assessment = "review"
            reasons.append("starter whose artifacts no compiled code calls; it works through auto-configuration at "
                           "runtime, so only the probes and the bean dump show whether it is needed")
        elif scope == "runtime":
            assessment = "review"
            reasons.append("runtime scope: loaded by configuration, reflection or ServiceLoader, which "
                           "dependency:analyze cannot see")
        else:
            assessment = "unnecessary"
            reasons.append("declared, but no compiled main or test code uses it (dependency:analyze). Remove it, "
                           "or keep it only if it is loaded at runtime")
        if replaces:
            reasons.append(f"replaces {replaces}, which change_branch removed")
            if assessment != "required":
                assessment = "replacement"

        if introduces:
            impact = (f"Adds {len(introduces)} artifact(s) that master does not have: {', '.join(introduces[:8])}"
                      f"{', ...' if len(introduces) > 8 else ''}.")
        else:
            impact = "Adds no artifact that master does not already resolve."
        if starter:
            impact += " Its auto-configuration can register beans and change defaults for every flow."
        rows.append({"change": "added", "ga": a["ga"], "version": a.get("version"), "scope": scope, "starter": starter,
                     "assessment": assessment, "reason": "; ".join(reasons), "impact": impact, "replaces": replaces,
                     "introduces": introduces})

    for r in [x for x in as_list(diff.get("removed")) if x.get("direct")]:
        desc = tree_descendants(base_tree, r["ga"])
        used_through = unique_sorted([d["ga"] for d in desc if icontains(base_used_undeclared, d["ga"])])
        was_used = bool(used_through) if is_starter(r["ga"]) else not icontains(base_unused, r["ga"])
        replaced_by = pairs.get(r["ga"])
        if replaced_by:
            assessment, reason = "replaced", f"replaced by {replaced_by}"
        elif was_used:
            through = f" (through {', '.join(used_through)})" if used_through else ""
            assessment = "review"
            reason = (f"master code used it{through}; change_branch compiles without it, so check that the replacement "
                      "classes behave the same")
        else:
            assessment, reason = "safe-to-remove", "no compiled code on master used it"
        rows.append({"change": "removed", "ga": r["ga"], "version": r.get("version"), "scope": "",
                     "starter": is_starter(r["ga"]), "assessment": assessment, "reason": reason,
                     "impact": "Classes and auto-configuration it brought are gone unless the replacement brings them. "
                               "Runtime-only use is visible only in the probes and the bean dump.",
                     "replaces": None, "introduces": []})

    for c in [x for x in as_list(diff.get("changed")) if x.get("direct")]:
        major = bool(c.get("major"))
        rows.append({"change": "version", "ga": c["ga"], "version": f"{c.get('base')} -> {c.get('head')}", "scope": "",
                     "starter": is_starter(c["ga"]),
                     "assessment": "review" if major else "required",
                     "reason": "major version change" if major else "minor or patch version change",
                     "impact": ("A major version can remove APIs and change defaults. See the library API changes and the "
                                "release-notes validation." if major else
                                "Usually compatible; the library API diff lists any public change that the code touches."),
                     "replaces": None, "introduces": []})
    return rows


def dependency_suggestions(conflicts, assessment, analyze, verbose_available: bool) -> list[dict]:
    """Commands for deeper analysis, each with the reason it is suggested for this run."""
    out = []
    conflicts = as_list(conflicts)
    for c in [x for x in conflicts if x.get("major")][:10]:
        out.append({"command": f"mvn dependency:tree -Dverbose -Dincludes={c['ga']}",
                    "why": f"{c['ga']} {c.get('requested')} lost to {c.get('resolved')} (major); see which path brings each version"})
    if conflicts:
        out.append({"command": "mvn enforcer:enforce -Drules=dependencyConvergence",
                    "why": f"{len(conflicts)} version conflict(s) in the head tree; convergence lists each one that is not pinned"})
    for a in as_list(assessment):
        if a.get("assessment") == "unnecessary":
            out.append({"command": "mvn dependency:analyze -DignoreNonCompile=true",
                        "why": f"{a['ga']} looks unused; confirm before removing it"})
            break
    base_undeclared = as_list(get(analyze, "base", "undeclared"))
    new_undeclared = [x for x in as_list(get(analyze, "head", "undeclared")) if not icontains(base_undeclared, x)]
    if new_undeclared:
        out.append({"command": "mvn dependency:analyze -DfailOnWarning=true",
                    "why": f"{len(new_undeclared)} used-but-undeclared artifact(s) are new on change_branch; declare them so a "
                           "BOM change cannot remove them"})
    out.append({"command": "mvn dependency:analyze-duplicate", "why": "finds duplicate dependency declarations in the pom"})
    if not verbose_available:
        out.append({"command": "upgrade maven-dependency-plugin to 3.7.0 or later",
                    "why": "this run could not produce a verbose tree, so version conflicts were not listed"})
    return out


def parse_surefire_reports(paths) -> list[dict]:
    """One row per test case from surefire or failsafe TEST-*.xml files."""
    rows = []
    for p in as_list(paths):
        if not os.path.isfile(p):
            continue
        try:
            root = ET.parse(p).getroot()
        except (ET.ParseError, OSError):
            continue
        if root.tag != "testsuite":
            continue
        for tc in root.findall("testcase"):
            status, message = "passed", ""
            failure, error, skipped = tc.find("failure"), tc.find("error"), tc.find("skipped")
            if failure is not None:
                status, message = "failed", s(failure.get("message"))
            elif error is not None:
                status, message = "error", s(error.get("message"))
            elif skipped is not None:
                status = "skipped"
            rows.append({"id": f"{s(tc.get('classname'))}#{s(tc.get('name'))}", "status": status, "message": message})
    return rows


def compare_test_results(base, head) -> list[dict]:
    """Test-by-test comparison of the two builds. change: same, new (head only), removed (base only),
    now-failing, now-passing, now-skipped, now-running."""
    bm = {r["id"]: r for r in as_list(base) if r}
    hm = {r["id"]: r for r in as_list(head) if r}
    rows = []
    for tid in unique_sorted(list(bm) + list(hm)):
        b, h = bm.get(tid), hm.get(tid)
        bs = b.get("status", "") if b else ""
        hs = h.get("status", "") if h else ""
        ok_b, ok_h = bs == "passed", hs == "passed"
        if not b:
            change = "new"
        elif not h:
            change = "removed"
        elif bs == hs:
            change = "same"
        elif ok_b and hs in ("failed", "error"):
            change = "now-failing"
        elif ok_h:
            change = "now-running" if bs == "skipped" else "now-passing"
        elif hs == "skipped":
            change = "now-skipped"
        else:
            change = "now-failing"
        rows.append({"test": tid, "base": bs, "head": hs, "change": change, "message": h.get("message") if h else ""})
    return rows


def new_added_file_patch(relative_path: str, lines) -> str:
    """A git-style patch that adds one new file, so a reviewer can "git apply" the new tests."""
    p = s(relative_path).replace("\\", "/")
    lines = as_list(lines)
    out = [f"diff --git a/{p} b/{p}\n", "new file mode 100644\n", "--- /dev/null\n", f"+++ b/{p}\n",
           f"@@ -0,0 +1,{len(lines)} @@\n"]
    out += ["+" + s(line).rstrip("\r") + "\n" for line in lines]
    return "".join(out)


def flow_coverage(reach, jacoco: dict, fqcn_by_simple: dict) -> list[dict]:
    """For each symbol a flow reaches: JaCoCo covered instructions on one side (None when the report has no
    entry). A symbol with no covered instruction is untraced."""
    rows = []
    for symbol in as_list(reach):
        cls, _, method = s(symbol).partition("#")
        fq = fqcn_by_simple[cls].replace(".", "/") if cls in fqcn_by_simple else None
        covered = None
        if fq:
            key = f"{fq}|*" if method == "*" else f"{fq}|{method}"
            if key in jacoco:
                covered = int(jacoco[key])
        rows.append({"symbol": symbol, "covered": covered, "traced": covered is not None and covered > 0})
    return rows


def finding_affects_flow(finding: dict, flow: dict) -> bool:
    """global findings touch every flow. symbol findings touch the flows whose reach holds the symbol
    ("Class#*" holds every method of the class). Other scopes do not change production flows."""
    scope = finding.get("scope")
    if scope == "global":
        return True
    if scope == "symbol":
        reach = as_list(flow.get("reach"))
        for sym in as_list(finding.get("symbols")):
            sym = s(sym)
            if "#" not in sym:
                continue
            cls = sym.split("#")[0]
            if sym.endswith("#*"):
                if any(like(r, f"{cls}#*") for r in reach):
                    return True
            elif icontains(reach, sym):
                return True
        return False
    return False


def check_applies(check: dict, finding: dict) -> bool:
    """A check row speaks about a finding when it names the finding (findingIds, from the gaps a test case covers),
    or, for a row that names no finding (a probe), when it is linked to the finding's rule."""
    ids = as_list(check.get("findingIds"))
    if ids:
        return finding.get("id") in ids
    return check.get("ruleId") == finding.get("ruleId")


def finding_status(finding: dict, checks, decisions=()) -> str:
    """Effective status of one finding after the behavior checks (stage 6 reads it).
      accepted  a person recorded that the difference is expected (optional; the verdict does not wait for it)
      confirmed a linked check shows a difference or a failure on head: a behavior change
      verified  at least one linked check ran and matched master
      open      no linked check ran
    checks: rows {ruleId; findingIds (optional); outcome = "same" | "different" | "error" | "unverified"}."""
    if any(d and d.get("findingId") == finding.get("id") and d.get("decision") == "accept" for d in as_list(decisions)):
        return "accepted"
    linked = [c for c in as_list(checks) if c and check_applies(c, finding)]
    # A "different" outcome is a behavior change. An "error" is a check that did not run: not a change.
    if any(c.get("outcome") == "different" for c in linked):
        return "confirmed"
    # A check that did not run, or a test that does not hold on master, verifies nothing.
    if any(c.get("outcome") in ("error", "unverified") for c in linked):
        return "open"
    if linked:
        return "verified"
    return "open"


# ---------- test cases and differences ----------

def failing_methods(side: dict) -> set:
    """The test methods that failed on one side. A class that passed lists none; a class that did not run is
    unknown (None)."""
    if not side or not side.get("ran"):
        return None
    if side.get("passed"):
        return set()
    return {x.strip() for x in re.split(r",\s*", s(side.get("detail"))) if x.strip()}


def outcome_of(base_failed, head_failed, name=None) -> str:
    """same | different | unverified | error for one test method (name) or a whole class (name None)."""
    if base_failed is None or head_failed is None:
        return "error"
    in_base = bool(base_failed) if name is None else name in base_failed
    in_head = bool(head_failed) if name is None else name in head_failed
    if in_base:
        return "unverified"
    return "different" if in_head else "same"


def case_results(manifest_test: dict, row: dict, gaps_by_id: dict) -> list[dict]:
    """Per test method: which gaps (and so which findings and flows) it covers, and its outcome on both branches.
    A manifest entry without "cases" is one case for the whole class, covering all its gaps."""
    base_failed, head_failed = failing_methods(row.get("base")), failing_methods(row.get("head"))
    declared = items(manifest_test.get("cases"))
    if not declared:
        declared = [{"method": None, "gapIds": as_list(manifest_test.get("gapIds")), "flowIds": as_list(manifest_test.get("flowIds"))}]
    out = []
    for c in declared:
        gap_ids = as_list(c.get("gapIds"))
        refs = [s(get(gaps_by_id.get(g), "ref")) for g in gap_ids]
        out.append({"method": c.get("method"), "gapIds": gap_ids,
                    "findingIds": [r for r, g in zip(refs, gap_ids) if s(get(gaps_by_id.get(g), "type")) == "unverified-impact"],
                    "flowIds": as_list(c.get("flowIds")) or as_list(manifest_test.get("flowIds")),
                    "outcome": outcome_of(base_failed, head_failed, c.get("method"))})
    return out


def test_flow_ids(test: dict) -> list[str]:
    """The flows a new test speaks for: its own flowIds and those of its cases."""
    return list(dict.fromkeys(as_list(test.get("flowIds")) + [f for c in items(test.get("cases")) for f in as_list(c.get("flowIds"))]))


def flows_checked_by_new_tests(tests) -> set:
    """Flows that a new test method ran on both branches with a result (same or different), from a test that caught
    every injected regression. Code such a test runs is no longer untraced: stage 4's coverage predates the test."""
    out = set()
    for t in items(tests):
        if not t.get("detectsInjected") or not get(t, "base", "passed"):
            continue
        for c in items(t.get("cases")):
            if c.get("outcome") in ("same", "different"):
                out.update(as_list(c.get("flowIds")))
    return out


def case_checks(test_id: str, rules, cases) -> list[dict]:
    """The check rows of one new test: one per case. A case that names findings speaks only for them; a case
    without findings (an untraced flow) still counts for the test's rules."""
    rows = []
    for c in as_list(cases):
        ids = as_list(c.get("findingIds"))
        row = {"source": "test", "id": test_id, "case": c.get("method"), "outcome": c.get("outcome")}
        if ids:
            rows.append(dict(row, ruleId="", findingIds=ids))
        else:
            rows += [dict(row, ruleId=rule) for rule in as_list(rules)]
    return rows


def differences(behavior: dict, test_results: dict) -> list[dict]:
    """Every check that gave a different result on change_branch, each counted once:
      probe          the same request answered differently
      new-test       a new test method (or class) that passes on master and fails on change_branch
      existing-test  an existing test that passes on master and fails on change_branch, or exists only there and fails"""
    out = []
    for p in items(get(behavior, "probes")):
        if p.get("outcome") == "different":
            out.append({"kind": "probe", "id": p.get("id"), "flowIds": [p.get("flowId")], "request": p.get("request"),
                        "base": get(p, "base"), "head": get(p, "head"), "differences": as_list(p.get("differences")),
                        "text": f"probe {p.get('id')} ({p.get('request')}): {'; '.join(as_list(p.get('differences')))}"})
    for t in items(get(behavior, "tests")):
        cases = items(t.get("cases")) or [{"method": None, "flowIds": as_list(t.get("flowIds")), "gapIds": [],
                                           "outcome": outcome_of(failing_methods(t.get("base")), failing_methods(t.get("head")))}]
        for c in cases:
            if c.get("outcome") != "different":
                continue
            what = f"{t.get('className')}.{c['method']}" if c.get("method") else t.get("className")
            out.append({"kind": "new-test", "id": t.get("id"), "case": c.get("method"), "flowIds": as_list(c.get("flowIds")),
                        "gapIds": as_list(c.get("gapIds")), "findingIds": as_list(c.get("findingIds")),
                        "text": f"new test {t.get('id')} {what} passes on master and fails on change_branch"})
    for t in items(get(test_results, "tests")):
        if t.get("change") == "now-failing":
            out.append({"kind": "existing-test", "id": t.get("test"), "flowIds": [],
                        "text": f"existing test {t.get('test')} passes on master and fails on change_branch ({t.get('message')})"})
        elif t.get("change") == "new" and t.get("head") in ("failed", "error"):
            out.append({"kind": "existing-test", "id": t.get("test"), "flowIds": [],
                        "text": f"test {t.get('test')} exists only on change_branch and fails there ({t.get('message')})"})
    return out


# ---------- agent work lists ----------

def needs_repair(test: dict) -> bool:
    """A proposed test is broken when it fails on master (it does not pin master's behavior), or when it passes
    on both branches but misses an injected regression (it detects nothing). A test that passes on master and
    fails on change_branch is not broken: it proves a behavior change, and is kept as evidence."""
    if not get(test, "base", "passed"):
        return True
    if get(test, "head", "passed") and not test.get("validated"):
        return True
    return False


def proves_change(test: dict) -> bool:
    """A test that passes on master and fails on change_branch: evidence of a behavior change."""
    return bool(get(test, "base", "passed")) and not get(test, "head", "passed")


def release_notes_refs(lib_api, dep_diff) -> list[str]:
    """The library changes the release-notes agent must cover: high or medium library API items, and direct
    dependencies that changed by a major version. Stage 10 uses the same list."""
    refs = []
    if lib_api and lib_api.get("available"):
        refs += [s(i.get("id")) for i in items(lib_api.get("items")) if s(i.get("severity")) in ("high", "medium")]
    if dep_diff:
        refs += [s(c.get("ga")) for c in items(dep_diff.get("changed")) if c.get("direct") and c.get("major")]
    return unique(refs)


def find_test_gaps(changed_methods, reach, impact, checks=(), asserts=None) -> list[dict]:
    """Every item a test must cover, or the test writer must explain why it cannot (requirement: write a test
    for each potential impact). Ids are stable for one input: G-01, G-02, ... in this order:
      untested-change     a changed main-code method or type whose changed lines no test runs on change_branch
      weak-assertion      a changed method the existing tests run, but a deliberate change of a changed line made
                          no existing test fail (artifacts\\assert-strength.json): their assertions do not pin it
      untraced-flow       a flow that reaches code no test executes
      unverified-impact   a high or medium finding with production scope that no probe or test checked"""
    rows = []
    methods = [m for m in items(get(changed_methods, "methods")) if not m.get("tested") and m.get("kind") != "file"]
    for m in sorted(methods, key=lambda m: (sort_key(m.get("file")), sort_key(m.get("name")))):
        lines = as_list(get(m, "lines", "uncovered"))
        where = f": changed line(s) {', '.join(str(x) for x in lines)} are not run by any test" if lines else             " is not executed by any test"
        rows.append({"type": "untested-change", "ref": f"{m.get('file')}|{m.get('kind')}:{m.get('name')}", "severity": "high",
                     "lines": lines, "detail": f"changed {m.get('kind')} {m.get('name')} in {m.get('file')}{where} on change_branch"})
    for w in sorted([x for x in items(get(asserts, "methods")) if x.get("strength") == "weak"],
                    key=lambda w: (sort_key(w.get("file")), sort_key(w.get("name")))):
        alive = [x for x in items(w.get("mutants")) if x.get("result") == "survived"]
        rows.append({"type": "weak-assertion", "ref": f"{w.get('file')}|{w.get('kind')}:{w.get('name')}", "severity": "high",
                     "lines": [x.get("line") for x in alive], "mutants": alive,
                     "detail": f"existing tests run {w.get('kind')} {w.get('name')} in {w.get('file')}, but none fails when "
                               + "; ".join(f"line {x.get('line')} changes from `{x.get('original')}` to `{x.get('mutated')}`"
                                           for x in alive[:3])})
    for fl in ps_sorted([f for f in items(get(reach, "flows")) if as_list(f.get("untraced"))], key=lambda f: f.get("id")):
        rows.append({"type": "untraced-flow", "ref": s(fl.get("id")), "severity": "high",
                     "detail": f"flow {fl.get('id')} reaches code no test executes: {', '.join(s(x) for x in as_list(fl.get('untraced')))}"})
    for f in ps_sorted(items(get(impact, "findings")), key=lambda f: f.get("id")):
        if s(f.get("severity")) not in ("high", "medium"):
            continue
        if s(f.get("scope")) not in ("global", "symbol"):
            continue
        if finding_status(f, as_list(checks)) != "open":
            continue
        rows.append({"type": "unverified-impact", "ref": s(f.get("id")), "severity": s(f.get("severity")),
                     "detail": f"{f.get('id')} [{f.get('ruleId')}] {f.get('title')}: {f.get('impact')}"})
    return [{"id": f"G-{i:02d}", **r} for i, r in enumerate(rows, start=1)]


# ---------- actuator dumps (stage 5) ----------

def is_app_config_source(name) -> bool:
    """A property source loaded from the application's own config files. Spring Boot 2.4 and later name it
    "Config resource 'class path resource [application.yml]' via location 'optional:classpath:/'"; Boot 2.3 and
    earlier named it "applicationConfig: [classpath:/application.yml]"."""
    return like(name, "Config resource *") or like(name, "applicationConfig*")


def app_properties(doc) -> dict:
    """Application config properties from /actuator/env: name -> value. When the same name is in several
    sources (a profile file and application.yml), the first source wins, as in Spring's own precedence.
    Values masked by the server ("******") are kept as they are: the stage starts the app with
    management.endpoint.env.show-values=ALWAYS, so a masked value means a custom sanitizer hid it."""
    out = {}
    for src in items(get(doc, "propertySources")):
        if not is_app_config_source(src.get("name")):
            continue
        for name, prop in (src.get("properties") or {}).items():
            out.setdefault(name, s(get(prop, "value")))
    return out
