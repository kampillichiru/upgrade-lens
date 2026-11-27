"""UpgradeLens library API functions (stage 3, library API diff).

The method, without any tool beyond the JDK:
  1. jdeps -verbose:class on each side's compiled classes lists every library class the code refers to.
  2. javap -c -p -l on each side's compiled classes lists every library method and field the code calls,
     with the source line (from the LineNumberTable).
  3. Each referenced library class is found in the jars of that side's classpath, and javap -public -s
     prints its public API with JVM descriptors, so overloads compare exactly.
  4. The two APIs are compared per class; a removed class is matched by simple name to a moved one.
  5. Each change is tied to the code that uses it, on master and on change_branch.

Parsers and the comparison are pure functions (tests\\test_upgradelens.py calls them).
jar_class_index reads jar files.
"""

from __future__ import annotations

import os
import re
import zipfile

from .core import as_list, get, ieq, ps_sorted, s, sort_key, unique_sorted


def parse_jdeps_verbose(lines) -> list[dict]:
    """"   a.b.App   -> x.y.Lib   not found" (or a module or jar name as the location)."""
    rows = []
    rx = re.compile(r"^\s+(?P<frm>[\w$.]+)\s+->\s+(?P<to>[\w$.]+)\s+(?P<loc>\S.*?)\s*$", re.I)
    for line in as_list(lines):
        m = rx.search(s(line))
        if m:
            rows.append({"from": m.group("frm"), "to": m.group("to"), "location": m.group("loc")})
    return rows


def javap_member_name(declaration: str, class_name: str) -> str:
    """Member name from a javap declaration line (without the trailing ";"). Constructors are "<init>"."""
    d = re.sub(r"\s+throws\s+.*$", "", s(declaration), flags=re.I)
    if re.search(r"^\s*static\s*\{\s*\}\s*$", d, re.I):
        return "<clinit>"
    paren = d.find("(")
    if paren >= 0:
        before = d[:paren].rstrip()
        token = re.split(r"\s+", before)[-1]
        if ieq(token, class_name):
            return "<init>"
        return token
    tokens = re.split(r"\s+", re.sub(r"\s*=.*$", "", d).strip())
    return tokens[-1]


def javap_class_name(header: str) -> str | None:
    """fqcn from a javap class header line ("public final class a.b.C<T> extends ... {")."""
    m = re.search(r"(?:^|\s)(?:class|interface|enum|record|@interface)\s+(?P<n>[\w$.]+)", s(header), re.I)
    return m.group("n") if m else None


_DECL_RX = re.compile(r"^\s{2}(?P<d>\S.*);$")


def parse_javap_api(lines) -> dict:
    """Output of "javap -public -s" for one or more classes. Returns fqcn -> class API:
      header   the class declaration line (modifiers, supertypes)
      members  list of {key = name + descriptor; name; descriptor; declaration}"""
    classes = {}
    cur = None
    pending = None
    for raw in as_list(lines):
        line = s(raw).rstrip()
        if re.search(r"^(Compiled from|Error:|Warning:)", line, re.I):
            continue
        if cur is None and re.search(r"^\S.*\{$", line):
            name = javap_class_name(line)
            if name:
                cur = {"fqcn": name, "header": line.rstrip("{").strip(), "members": []}
            continue
        if cur is not None and line == "}":
            classes[cur["fqcn"]] = cur
            cur = None
            pending = None
            continue
        if cur is None:
            continue
        m = re.search(r"^\s+descriptor:\s+(?P<d>\S+)\s*$", line, re.I)
        if m:
            if pending:
                name = javap_member_name(pending, cur["fqcn"])
                cur["members"].append({"key": f"{name}{m.group('d')}", "name": name, "descriptor": m.group("d"),
                                       "declaration": pending})
                pending = None
            continue
        m = _DECL_RX.search(line)
        if m:
            pending = m.group("d").strip()
    return classes


_CODE_REF_RX = re.compile(r"^\s+(?P<off>\d+):\s+\w+.*?//\s+(?P<kind>Method|InterfaceMethod|Field|class)\s+(?P<ref>.+)$", re.I)
_MEMBER_REF_RX = re.compile(r'^(?:(?P<owner>[\w/$]+)\.)?"?(?P<name>[\w$<>]+)"?:(?P<desc>\S+)$', re.I)


def parse_javap_code(lines) -> list[dict]:
    """Output of "javap -c -p -l" for application classes. Returns one row per library or application
    reference made from code: {appClass; method; line; kind (method|field|class); owner; member; descriptor}.
    "method" is the source method: lambdas (lambda$name$0) map to their enclosing method, constructors to
    the simple class name, static initializers to "<clinit>"."""
    rows = []
    cls = None
    method = None
    refs = []
    table = []
    mode = ""

    def flush():
        for r in refs:
            line, best = None, -1
            for t in table:
                if best < t["offset"] <= r["offset"]:
                    best, line = t["offset"], t["line"]
            rows.append({"appClass": cls, "method": method, "line": line, "kind": r["kind"], "owner": r["owner"],
                         "member": r["member"], "descriptor": r["descriptor"]})
        refs.clear()
        table.clear()

    for raw in as_list(lines):
        line = s(raw).rstrip()
        if re.search(r"^Compiled from", line, re.I):
            continue
        if cls is None and re.search(r"^\S.*\{$", line):
            cls = javap_class_name(line)
            method = None
            mode = ""
            continue
        if cls is not None and line == "}":
            flush()
            cls = None
            method = None
            continue
        if cls is None:
            continue
        m = _DECL_RX.search(line)
        if m:
            flush()
            name = javap_member_name(m.group("d"), cls)
            if name == "<init>":
                name = re.split(r"[.$]", cls)[-1]
            lm = re.search(r"^lambda\$(.+)\$\d+$", name, re.I)
            if lm:
                name = lm.group(1)
            method = name
            mode = ""
            continue
        if re.search(r"^\s+LineNumberTable:", line, re.I):
            mode = "lnt"
            continue
        if re.search(r"^\s+(LocalVariableTable|LocalVariableTypeTable|StackMapTable|Exceptions|RuntimeVisible\w*):", line, re.I):
            mode = ""
            continue
        if re.search(r"^\s+Code:", line, re.I):
            mode = "code"
            continue
        if mode == "lnt":
            lm = re.search(r"^\s+line (?P<l>\d+): (?P<o>\d+)", line, re.I)
            if lm:
                table.append({"line": int(lm.group("l")), "offset": int(lm.group("o"))})
                continue
        if mode == "code":
            cm = _CODE_REF_RX.search(line)
            if cm:
                off, kind, ref = int(cm.group("off")), cm.group("kind"), cm.group("ref").strip()
                if ref.startswith('"['):
                    continue
                if kind.lower() == "class":
                    if ref.startswith('"'):
                        continue
                    refs.append({"offset": off, "kind": "class", "owner": ref.replace("/", "."), "member": "", "descriptor": ""})
                    continue
                mm = _MEMBER_REF_RX.search(ref)
                if mm:
                    owner = mm.group("owner").replace("/", ".") if mm.group("owner") else cls
                    refs.append({"offset": off, "kind": "field" if kind.lower() == "field" else "method", "owner": owner,
                                 "member": mm.group("name"), "descriptor": mm.group("desc")})
    flush()
    return rows


def jar_coordinates(jar_path: str) -> dict:
    """Maven coordinates from a jar path in a local repository (".../repository/g/r/p/artifact/version/artifact-version.jar").
    A jar outside a repository layout is named by its file name."""
    p = s(jar_path).replace("\\", "/")
    m = re.search(r"/repository/(?P<g>.+)/(?P<a>[^/]+)/(?P<v>[^/]+)/[^/]+\.jar$", p, re.I)
    if m:
        g = m.group("g").replace("/", ".")
        return {"ga": f"{g}:{m.group('a')}", "version": m.group("v"), "label": f"{g}:{m.group('a')}:{m.group('v')}"}
    leaf = os.path.splitext(p.split("/")[-1])[0]
    return {"ga": leaf, "version": "", "label": leaf}


def jar_class_index(jars, classes=(), simple_names=()) -> dict:
    """Finds the jar of each wanted class, and the classes whose simple name is in simple_names (to match a
    removed class with a moved one). Reads the jar directories only; no class is loaded."""
    wanted = {c.replace(".", "/") + ".class": c for c in as_list(classes)}
    simple = set(as_list(simple_names))
    found: dict[str, str] = {}
    by_simple: dict[str, list[str]] = {}
    for jar in as_list(jars):
        if not jar or not jar.endswith(".jar") or not os.path.isfile(jar):
            continue
        try:
            with zipfile.ZipFile(jar) as z:
                for n in z.namelist():
                    if not n.endswith(".class") or n.startswith("META-INF/"):
                        continue
                    if n in wanted and wanted[n] not in found:
                        found[wanted[n]] = jar
                    if simple:
                        leaf = os.path.splitext(n.split("/")[-1])[0]
                        if leaf in simple:
                            fq = n[:-6].replace("/", ".")
                            lst = by_simple.setdefault(leaf, [])
                            if fq not in lst:
                                lst.append(fq)
        except (zipfile.BadZipFile, OSError):
            # An unreadable jar is skipped; its classes count as not found, which the diff reports.
            continue
    return {"jarByClass": found, "bySimpleName": by_simple}


def compare_library_class_api(base, head) -> dict | None:
    """Compares one class's public API on the two sides. State: same, changed, removed, added.
    Members with the same name on both sides but a different descriptor are "changed" (an overload whose
    signature changed), not removed plus added."""
    if base is None and head is None:
        return None
    if head is None:
        return {"state": "removed", "headerChanged": False, "removed": list(as_list(base.get("members"))), "added": [], "changed": []}
    if base is None:
        return {"state": "added", "headerChanged": False, "removed": [], "added": list(as_list(head.get("members"))), "changed": []}
    bmem, hmem = as_list(base.get("members")), as_list(head.get("members"))
    bk = {m["key"] for m in bmem}
    hk = {m["key"] for m in hmem}
    removed = [m for m in bmem if m["key"] not in hk]
    added = [m for m in hmem if m["key"] not in bk]
    changed = []
    for n in unique_sorted([m["name"] for m in removed]):
        a = [m for m in added if ieq(m["name"], n)]
        if a:
            gone = [m for m in removed if ieq(m["name"], n)]
            changed.append({"name": n, "base": [m["declaration"] for m in gone], "head": [m["declaration"] for m in a],
                            "baseKeys": [m["key"] for m in gone]})
    changed_names = {c["name"].casefold() for c in changed}
    removed_only = [m for m in removed if m["name"].casefold() not in changed_names]
    added_only = [m for m in added if m["name"].casefold() not in changed_names]
    header_changed = base.get("header") != head.get("header")
    state = "same" if not removed and not added and not header_changed else "changed"
    return {"state": state, "headerChanged": header_changed, "removed": removed_only, "added": added_only, "changed": changed}


def library_api_items(deps: dict, code: dict, api: dict, jars: dict, sources: dict, moved_candidates: dict | None = None,
                      max_members: int = 40) -> dict:
    """Builds the library API change list. Inputs, per side ("base", "head"):
      deps      jdeps rows (parse_jdeps_verbose)
      code      code references (parse_javap_code)
      api       fqcn -> class API (parse_javap_api)
      jars      fqcn -> jar path
      sources   outer application fqcn -> {path = relative source path; lines}
    moved_candidates: removed fqcn -> list of head fqcn with the same simple name.
    Returns the items (state other than "same"), ordered by severity then class, and the counts."""
    moved_candidates = moved_candidates or {}
    sev_rank = {"high": 0, "medium": 1, "low": 2, "info": 3}
    app_classes = {r["from"] for side in ("base", "head") for r in as_list(deps.get(side))}
    lib_classes = {r["to"] for side in ("base", "head") for r in as_list(deps.get(side))
                   if r.get("location") == "not found" and r["to"] not in app_classes}

    def side_sources(side):
        return sources.get(side) or {}

    def usage(side: str, lib_class: str) -> list[dict]:
        out = []
        seen = set()
        for c in as_list(code.get(side)):
            if c.get("owner") != lib_class:
                continue
            outer = s(c.get("appClass")).split("$")[0]
            src = get(side_sources(side), outer, "path", default="")
            key = f"{c.get('appClass')}|{c.get('method')}|{c.get('line')}|{c.get('member')}{c.get('descriptor')}"
            if key in seen:
                continue
            seen.add(key)
            out.append({"appClass": c.get("appClass"), "method": c.get("method"), "source": src, "line": c.get("line"),
                        "member": c.get("member"), "descriptor": c.get("descriptor")})
        # Class-level references (annotations, field and parameter types) that no code line shows: the
        # source line that names the class (usually the import) is the usage site.
        simple = re.split(r"[.$]", lib_class)[-1]
        name_rx = re.compile(r"\b" + re.escape(simple) + r"\b", re.I)
        for r in as_list(deps.get(side)):
            if r.get("to") != lib_class:
                continue
            if any(u["appClass"] == r["from"] for u in out):
                continue
            outer = s(r["from"]).split("$")[0]
            src, line = "", None
            entry = side_sources(side).get(outer)
            if entry:
                src = entry.get("path", "")
                for i, text in enumerate(as_list(entry.get("lines"))):
                    if name_rx.search(s(text)):
                        line = i + 1
                        break
            key = f"{r['from']}|class"
            if key in seen:
                continue
            seen.add(key)
            out.append({"appClass": r["from"], "method": "", "source": src, "line": line, "member": "", "descriptor": ""})
        return out

    def artifact(side: str, cls: str) -> str:
        jar = (jars.get(side) or {}).get(cls)
        return jar_coordinates(jar)["label"] if jar else "-"

    moved_targets: dict[str, str] = {}
    counts = {"same": 0, "changed": 0, "removed": 0, "moved": 0, "added": 0}
    found = []
    for cls in ps_sorted(lib_classes):
        b = (api.get("base") or {}).get(cls)
        h = (api.get("head") or {}).get(cls)
        cmp = compare_library_class_api(b, h)
        if cmp is None:
            continue
        if cmp["state"] == "same":
            counts["same"] += 1
            continue
        use = {"base": usage("base", cls), "head": usage("head", cls)}
        state = cmp["state"]
        moved_to: list[str] = []
        if state == "removed" and cls in moved_candidates:
            moved_to = ps_sorted([t for t in as_list(moved_candidates[cls]) if t != cls])
            if moved_to:
                state = "moved"
                for t in moved_to:
                    moved_targets[t] = cls
        counts[state] += 1

        removed_keys = {m["key"]: m["declaration"] for m in cmp["removed"]}
        for c in cmp["changed"]:
            for k in c["baseKeys"]:
                removed_keys[k] = f"{c['name']} (signature changed)"
        removed_used = [u for u in use["base"] if u.get("member") and f"{u['member']}{u['descriptor']}" in removed_keys]

        severity, summary = "info", ""
        # A class that a removed class moved to is reported with the moved class, not again as "added".
        # (Classes sort by name, so the target may come first: it is filtered again after the loop.)
        if state == "added" and cls in moved_targets:
            counts["added"] -= 1
            continue
        if state in ("removed", "moved"):
            if use["base"]:
                severity = "high"
                moved_text = f"; moved to {', '.join(moved_to)}" if moved_to else ""
                n_classes = len(unique_sorted([u["appClass"] for u in use["base"]]))
                summary = f"not on the change_branch classpath{moved_text}. Master code used it in {n_classes} class(es)."
            else:
                summary = "not on the change_branch classpath; no master code uses it directly."
        elif state == "added":
            summary = "new library class used by change_branch code."
        elif state == "changed":
            if removed_used:
                severity = "high"
                summary = f"{len(removed_used)} member call(s) on master use a member that is removed or changed on change_branch."
            elif use["head"] and (cmp["removed"] or cmp["changed"] or cmp["headerChanged"]):
                severity = "medium"
                decl = ", class declaration changed" if cmp["headerChanged"] else ""
                summary = (f"public API changed ({len(cmp['removed'])} removed, {len(cmp['changed'])} changed, "
                           f"{len(cmp['added'])} added{decl}); change_branch code uses this class, but not the removed members.")
            elif use["head"]:
                severity = "low"
                summary = f"only additions ({len(cmp['added'])} member(s)); the members the code uses are unchanged."
            else:
                summary = "public API changed; no change_branch code uses this class directly."

        found.append({
            "class": cls,
            "state": state,
            "severity": severity,
            "summary": summary,
            "artifact": {"base": artifact("base", cls), "head": artifact("head", moved_to[0] if moved_to else cls)},
            "movedTo": moved_to,
            "header": {"base": b.get("header") if b else "", "head": h.get("header") if h else "", "changed": cmp["headerChanged"]},
            "removedMembers": [m["declaration"] for m in cmp["removed"][:max_members]],
            "changedMembers": [{"name": c["name"], "base": c["base"], "head": c["head"]} for c in cmp["changed"][:max_members]],
            "addedMembers": [m["declaration"] for m in cmp["added"][:max_members]],
            "memberCounts": {"removed": len(cmp["removed"]), "changed": len(cmp["changed"]), "added": len(cmp["added"])},
            "removedUsed": [{"appClass": u["appClass"], "method": u["method"], "source": u["source"], "line": u["line"],
                             "member": u["member"], "declaration": removed_keys[f"{u['member']}{u['descriptor']}"]}
                            for u in removed_used],
            "usage": use,
        })

    # Drop "added" items that turned out to be the target of a moved class later in the loop.
    kept = [it for it in found if not (it["state"] == "added" and it["class"] in moved_targets)]
    counts["added"] -= len(found) - len(kept)
    ordered = sorted(kept, key=lambda it: (sev_rank.get(it["severity"], 9), sort_key(it["class"])))
    result_items = [dict({"id": f"LIB-{n:03d}"}, **it) for n, it in enumerate(ordered, start=1)]
    return {"referencedClasses": len(lib_classes), "counts": counts, "items": result_items}
