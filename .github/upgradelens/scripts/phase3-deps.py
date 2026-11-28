"""Stage 3 of UpgradeLens (dependencies and API): compares the resolved dependencies, the public API of the
application classes, and the API of every library class the application uses, between the base and head
worktrees of a run.

Needs stage 1 (compiled target\\classes on both sides). Steps, in order:
  1. tree        mvn dependency:tree -Dverbose on each side (falls back to the plain tree when the plugin
                 cannot do verbose), so transitive, BOM-driven and conflicting versions show
  2. analyze     mvn dependency:analyze on each side: unused declared and used undeclared dependencies
  3. diff        added, removed and version-changed artifacts (major changes flagged), direct or transitive
  4. assess      per added or removed direct dependency: required, unnecessary, replacement or review, and
                 what it can affect; version conflicts in the head tree (who asked for which version);
                 suggested commands for deeper analysis
  5. api         javap -public of each application class on both sides (head JDK's javap for both)
  6. library     the library API diff (lib\\library.py): library classes and members the code uses, compared
                 between the two classpaths, each change tied to the code lines that use it
Writes artifacts\\dependency-diff.json, artifacts\\api-diff.json and artifacts\\library-api-diff.json.
None of them holds a timestamp, so the same inputs give the same bytes (agent input hashes depend on it).

A library step that cannot run (for example jdeps fails) does not fail the stage: library-api-diff.json
records "available": false with the reason, and stage 5 lists it as a gap, so it is never a silent pass.

Exit codes:
  0  written
  2  usage or run problem (no run, worktree or compiled classes missing)
  3  Maven or javap could not run

Example:
  python .github/upgradelens/scripts/phase3-deps.py --run-id 20261126-131717
"""

import argparse
import os
import re
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.analysis import (dependency_assessment, dependency_suggestions, parse_dependency_analyze)  # noqa: E402
from lib.core import (MAVEN_NATIVE_GIT_ARGS, TOOL_VERSION, as_list, compare_dependency_trees, config_value, get, java_files, jdk_env, jdk_tool,  # noqa: E402
                      log, parse_dependency_tree, parse_dependency_tree_verbose, ps_sorted, read_json_safe, read_lines,
                      read_text, resolve_config_path, run_main, run_native, runs_root, s, set_run_state,
                      set_stage_log, set_stage_step, source_index, to_int, unique_sorted, write_json, write_text)
from lib.library import (jar_class_index, library_api_items, parse_javap_api, parse_javap_code,  # noqa: E402
                         parse_jdeps_verbose)
from lib.standalone import StagePaths, add_standalone_arg, select_run  # noqa: E402

from lib.core import VERDICT_STATES  # noqa: E402

LATER_STATES = ["phase2-done", "phase3-done", "phase4-done", "phase5-done", "phase6-done", "phase7-done", "phase8-done",
                "phase9-done"] + VERDICT_STATES


class StageError(Exception):
    pass


def main(argv) -> int:
    p = argparse.ArgumentParser(description="UpgradeLens stage 3: dependencies, application API and library API.")
    p.add_argument("--run-id")
    p.add_argument("--config")
    add_standalone_arg(p)
    a = p.parse_args(argv)

    config = resolve_config_path(a.config)
    if not os.path.isfile(config):
        print(f"Config not found: {config}")
        return 2
    runs = runs_root(read_json_safe(config))
    run_id = select_run(runs, a.run_id, a.standalone, LATER_STATES)
    if not run_id:
        print(f"No run with stage 2 done under {runs}. Run the earlier stages first.")
        return 2

    run_dir = os.path.join(runs, run_id)
    paths = StagePaths(run_dir, a.standalone)
    artifacts = paths.out("artifacts")
    logs_dir = paths.out("logs")
    status_file = paths.out("status.json")
    set_stage_log(os.path.join(logs_dir, "phase3.log"))
    cfg = read_json_safe(os.path.join(run_dir, "config", "upgrade-lens.json"))
    env_rec = paths.read("artifacts", "environment.json")
    status = paths.read("status.json")
    if not cfg or not env_rec or not status:
        print(f"Run incomplete: {run_dir}")
        return 2

    settings = s(get(cfg, "maven", "settingsFile"))
    settings = settings if settings and os.path.isfile(settings) else None
    ignore_unused = [s(x) for x in as_list(config_value(cfg, "dependencies.ignoreUnused", [])) if s(x)]
    library_enabled = bool(config_value(cfg, "library.enabled", True))
    max_members = to_int(config_value(cfg, "library.maxMembersPerClass", 40), 40)
    mvn = s(get(cfg, "maven", "command"))

    def stop_stage(code: int, reason: str) -> int:
        log(reason)
        set_stage_step(status, "dependencies-api", "failed", reason)
        set_run_state(status, "failed-tool", "phase3")
        write_json(status_file, status)
        return code

    log(f"phase 3 dependencies and API for run {run_id}")

    sides = [
        {"side": "base", "branch": cfg.get("baseBranch"), "jdk": get(cfg, "java", "base"),
         "worktree": get(env_rec, "branches", "base", "worktree"), "commit": get(env_rec, "branches", "base", "commit")},
        {"side": "head", "branch": cfg.get("headBranch"), "jdk": get(cfg, "java", "head"),
         "worktree": get(env_rec, "branches", "head", "worktree"), "commit": get(env_rec, "branches", "head", "commit")},
    ]
    for sd in sides:
        if not os.path.isfile(os.path.join(s(sd["worktree"]), "pom.xml")):
            print(f"No pom.xml in {sd['worktree']}")
            return 2
        if not os.path.isdir(os.path.join(sd["worktree"], "target", "classes")):
            print(f"{sd['side']}: no target\\classes in {sd['worktree']}. Run stage 1 first.")
            return 2
        if not os.path.isfile(jdk_tool(sd["jdk"], "javap")):
            print(f"{sd['side']}: no javap under {sd['jdk']}")
            return 2
    javap = jdk_tool(sides[1]["jdk"], "javap")
    jdeps = jdk_tool(sides[1]["jdk"], "jdeps")

    def side_maven(sd: dict, goals, log_name: str):
        args = ["-B", "-ntp"] + list(MAVEN_NATIVE_GIT_ARGS) + (["-s", settings] if settings else []) + ["-f", os.path.join(sd["worktree"], "pom.xml")] + list(goals)
        r = run_native(mvn, args, env=jdk_env(sd["jdk"]))
        path = os.path.join(logs_dir, log_name)
        write_text(path, "\n".join(r.output) + "\n")
        r.log = path
        return r

    # ---------- 1 to 2. tree (verbose when possible) and analyze ----------

    trees, verbose, analyze, classpaths = {}, {}, {}, {}
    verbose_available = True
    for sd in sides:
        side = sd["side"]
        tree_file = os.path.join(artifacts, f"deps-{side}.txt")
        if os.path.isfile(tree_file):
            os.remove(tree_file)
        r = side_maven(sd, ["dependency:tree", "-Dverbose", "-DoutputType=text", f"-DoutputFile={tree_file}"], f"deps-tree-{side}.log")
        if r.exit_code != 0 or not os.path.isfile(tree_file):
            log(f"{side}: verbose dependency tree not available (see {r.log}); using the plain tree")
            verbose_available = False
            r = side_maven(sd, ["dependency:tree", "-DoutputType=text", f"-DoutputFile={tree_file}"], f"deps-tree-{side}.log")
            if r.exit_code != 0 or not os.path.isfile(tree_file):
                return stop_stage(3, f"dependency:tree failed on {side}; see {r.log}")
        tree_lines = read_lines(tree_file)
        trees[side] = parse_dependency_tree(tree_lines)
        verbose[side] = parse_dependency_tree_verbose(tree_lines)
        log(f"{side}: {len(trees[side])} artifacts in the resolved tree, {len(verbose[side]['conflicts'])} version conflict(s)")

        r = side_maven(sd, ["dependency:analyze"], f"deps-analyze-{side}.log")
        if r.exit_code != 0:
            return stop_stage(3, f"dependency:analyze failed on {side}; see {r.log}")
        analyze[side] = parse_dependency_analyze(r.output)

        if library_enabled:
            cp_file = os.path.join(artifacts, f"classpath-{side}.txt")
            if os.path.isfile(cp_file):
                os.remove(cp_file)
            r = side_maven(sd, ["dependency:build-classpath", f"-Dmdep.outputFile={cp_file}"], f"deps-classpath-{side}.log")
            if r.exit_code == 0 and os.path.isfile(cp_file):
                classpaths[side] = [x.strip() for x in read_text(cp_file).split(os.pathsep) if x.strip()]
            else:
                classpaths[side] = None

    # ---------- 3 to 4. diff, assessment, conflicts, suggestions ----------

    diff = compare_dependency_trees(trees["base"], trees["head"])
    assessment = dependency_assessment(diff, trees["base"], trees["head"], analyze, ignore_unused)
    head_conflicts = verbose["head"]["conflicts"]
    base_keys = {f"{c['ga']}|{c['requested']}|{c['resolved']}" for c in verbose["base"]["conflicts"]}
    conflict_rows = [dict(c, newOnHead=f"{c['ga']}|{c['requested']}|{c['resolved']}" not in base_keys) for c in head_conflicts]
    suggestions = dependency_suggestions(head_conflicts, assessment, analyze, verbose_available)

    summary = {
        "added": len(diff["added"]), "removed": len(diff["removed"]), "changed": len(diff["changed"]),
        "majorChanged": len([c for c in diff["changed"] if c["major"]]),
        "directChanged": len([c for c in diff["changed"] if c["direct"]]),
        "directAdded": len([c for c in diff["added"] if c["direct"]]),
        "directRemoved": len([c for c in diff["removed"] if c["direct"]]),
        "unnecessary": len([x for x in assessment if x["assessment"] == "unnecessary"]),
        "review": len([x for x in assessment if x["assessment"] == "review"]),
        "conflicts": len(conflict_rows), "majorConflicts": len([c for c in conflict_rows if c["major"]]),
    }
    write_json(os.path.join(artifacts, "dependency-diff.json"), {
        "schemaVersion": 1, "toolVersion": TOOL_VERSION, "runId": run_id,
        "base": {"branch": cfg.get("baseBranch"), "commit": sides[0]["commit"], "artifacts": len(trees["base"]),
                 "analyze": analyze["base"], "conflicts": len(verbose["base"]["conflicts"])},
        "head": {"branch": cfg.get("headBranch"), "commit": sides[1]["commit"], "artifacts": len(trees["head"]),
                 "analyze": analyze["head"], "conflicts": len(verbose["head"]["conflicts"])},
        "verboseTree": verbose_available,
        "added": diff["added"], "removed": diff["removed"], "changed": diff["changed"],
        "assessment": assessment, "conflicts": conflict_rows,
        "duplicates": {"base": verbose["base"]["duplicates"], "head": verbose["head"]["duplicates"]},
        "managed": verbose["head"]["managed"],
        "suggestions": suggestions,
        "summary": summary,
    })
    log(f"dependencies: added {summary['added']} ({summary['directAdded']} direct), removed {summary['removed']} "
        f"({summary['directRemoved']} direct), version changed {summary['changed']} (major {summary['majorChanged']}, "
        f"direct {summary['directChanged']}); conflicts on head {summary['conflicts']} (major {summary['majorConflicts']})")
    for x in assessment:
        if x["change"] != "version":
            log(f"  {x['change']} {x['ga']}: {x['assessment']} - {x['reason']}")

    # ---------- 5. API: public signatures of application classes ----------

    head_index = source_index(os.path.join(sides[1]["worktree"], "src", "main", "java"))
    base_index = source_index(os.path.join(sides[0]["worktree"], "src", "main", "java"))

    def public_members(classes_dir: str, fqcn: str):
        if not os.path.isfile(os.path.join(classes_dir, *fqcn.split(".")) + ".class"):
            return None
        r = run_native(javap, ["-public", "-cp", classes_dir, fqcn])
        if r.exit_code != 0:
            raise StageError(f"javap failed for {fqcn} in {classes_dir}")
        return [x.strip() for x in r.output if x.strip() and not re.match(r"^Compiled from", x.strip(), re.I)]

    api_classes = []
    try:
        for simple in unique_sorted(list(head_index) + list(base_index)):
            fqcn = (head_index.get(simple) or base_index[simple])["fqcn"]
            bm = public_members(os.path.join(sides[0]["worktree"], "target", "classes"), fqcn)
            hm = public_members(os.path.join(sides[1]["worktree"], "target", "classes"), fqcn)
            if bm is None and hm is None:
                continue
            removed_m, added_m = [], []
            if bm is None:
                state = "added"
            elif hm is None:
                state = "removed"
            else:
                removed_m = [m for m in bm if m not in hm]
                added_m = [m for m in hm if m not in bm]
                state = "same" if not removed_m and not added_m else "changed"
            api_classes.append({"fqcn": fqcn, "state": state, "removedMembers": removed_m, "addedMembers": added_m})
    except StageError as e:
        return stop_stage(3, f"API step failed: {e}")

    count = lambda st: len([c for c in api_classes if c["state"] == st])  # noqa: E731
    api_summary = {"same": count("same"), "changed": count("changed"), "added": count("added"), "removed": count("removed")}
    write_json(os.path.join(artifacts, "api-diff.json"), {
        "schemaVersion": 1, "toolVersion": TOOL_VERSION, "runId": run_id, "scope": "application classes in target\\classes",
        "javap": javap, "summary": api_summary, "classes": api_classes,
    })
    log(f"application api: {api_summary['same']} same, {api_summary['changed']} changed, {api_summary['added']} added, "
        f"{api_summary['removed']} removed")

    # ---------- 6. library API diff ----------

    def class_dirs(sd):
        return [d for d in (os.path.join(sd["worktree"], "target", "classes"), os.path.join(sd["worktree"], "target", "test-classes"))
                if os.path.isdir(d)]

    def source_map(sd):
        """Outer application class fqcn -> {path (relative, "/"); lines}, from src\\main\\java and src\\test\\java."""
        out = {}
        for root in (("src", "main", "java"), ("src", "test", "java")):
            d = os.path.join(sd["worktree"], *root)
            if not os.path.isdir(d):
                continue
            for f in java_files(d):
                rel = os.path.relpath(f, d).replace("\\", "/")
                out[rel[:-5].replace("/", ".")] = {"path": "/".join(root) + "/" + rel, "lines": read_lines(f)}
        return out

    def javap_batch(args, targets, batch_size: int = 40):
        """javap over many classes, in batches that keep the command line short. A class javap cannot read
        is reported on stderr and skipped; the rest of the batch is still printed."""
        out = []
        for i in range(0, len(targets), batch_size):
            out += run_native(javap, list(args) + targets[i:i + batch_size]).output
        return out

    def unavailable(reason: str) -> dict:
        return {"schemaVersion": 1, "toolVersion": TOOL_VERSION, "runId": run_id, "available": False, "reason": reason, "items": []}

    if not library_enabled:
        lib_doc = unavailable("disabled in the config (library.enabled = false)")
    elif not os.path.isfile(jdeps):
        lib_doc = unavailable(f"no jdeps under {os.path.dirname(jdeps)}")
    elif classpaths.get("base") is None or classpaths.get("head") is None:
        lib_doc = unavailable("dependency:build-classpath failed; see logs\\deps-classpath-*.log")
    else:
        try:
            deps, code, jars, api, sources = {}, {}, {}, {}, {}
            for sd in sides:
                side = sd["side"]
                dirs = class_dirs(sd)
                jd = run_native(jdeps, ["-verbose:class"] + dirs)
                if jd.exit_code != 0:
                    raise StageError(f"jdeps failed on {side}: {' '.join(jd.output[:3])}")
                deps[side] = parse_jdeps_verbose(jd.output)
                class_files = []
                for d in dirs:
                    for root, _dirs, files in os.walk(d):
                        class_files += [os.path.join(root, f) for f in ps_sorted(files) if f.endswith(".class")]
                code[side] = parse_javap_code(javap_batch(["-c", "-p", "-l"], class_files))
                sources[side] = source_map(sd)
                log(f"{side}: {len(class_files)} application class file(s), {len(deps[side])} class reference(s), "
                    f"{len(code[side])} code reference(s)")
            app_classes = {r["from"] for side in ("base", "head") for r in deps[side]}
            lib_classes = unique_sorted([r["to"] for side in ("base", "head") for r in deps[side]
                                         if r["location"] == "not found" and r["to"] not in app_classes])

            base_idx = jar_class_index(classpaths["base"], lib_classes)
            jars["base"] = base_idx["jarByClass"]
            head_idx = jar_class_index(classpaths["head"], lib_classes)
            missing_on_head = [c for c in lib_classes if c in base_idx["jarByClass"] and c not in head_idx["jarByClass"]]
            if missing_on_head:
                head_idx = jar_class_index(classpaths["head"], lib_classes, unique_sorted([c.split(".")[-1] for c in missing_on_head]))
            jars["head"] = dict(head_idx["jarByClass"])
            moved = {}
            for m in missing_on_head:
                simple = m.split(".")[-1]
                if simple in head_idx["bySimpleName"]:
                    moved[m] = list(head_idx["bySimpleName"][simple])
            # Moved targets are compared too, so the moved item can show the new class's API.
            extra_head = unique_sorted([t for ts in moved.values() for t in ts if t not in jars["head"]])
            if extra_head:
                jars["head"].update(jar_class_index(classpaths["head"], extra_head)["jarByClass"])

            for side in ("base", "head"):
                urls = ["jar:file:///" + jars[side][c].replace("\\", "/") + "!/" + c.replace(".", "/") + ".class"
                        for c in ps_sorted(jars[side])]
                api[side] = parse_javap_api(javap_batch(["-public", "-s"], urls))

            result = library_api_items(deps, code, api, jars, sources, moved, max_members)
            lib_items = result["items"]
            sev = lambda level: len([i for i in lib_items if i["severity"] == level])  # noqa: E731
            lib_doc = {
                "schemaVersion": 1, "toolVersion": TOOL_VERSION, "runId": run_id, "available": True,
                "scope": "library classes referenced by the compiled main and test classes of either branch (jdeps), compared "
                         "with javap -public -s between the two resolved classpaths; member use from javap -c with source lines",
                "tools": {"jdeps": jdeps, "javap": javap},
                "summary": {"referencedClasses": result["referencedClasses"], **result["counts"],
                            "high": sev("high"), "medium": sev("medium"), "low": sev("low"), "info": sev("info")},
                "items": lib_items,
            }
            c = result["counts"]
            log(f"library api: {result['referencedClasses']} referenced class(es); {c['same']} same, {c['changed']} changed, "
                f"{c['removed']} removed, {c['moved']} moved, {c['added']} added; high {sev('high')}, medium {sev('medium')}")
            for it in lib_items:
                if it["severity"] in ("high", "medium"):
                    log(f"  {it['id']} [{it['severity']}] {it['state']} {it['class']}: {it['summary']}")
        except Exception as e:  # noqa: BLE001 - the library diff never fails the stage; it is recorded as unavailable
            lib_doc = unavailable(f"library API step failed: {e}")
            log(f"library api: NOT AVAILABLE - {lib_doc['reason']}")
    write_json(os.path.join(artifacts, "library-api-diff.json"), lib_doc)

    set_stage_step(status, "dependencies-api", "done", "" if lib_doc["available"] else f"library API diff not available: {lib_doc['reason']}",
                   os.path.join(artifacts, "dependency-diff.json"))
    set_run_state(status, "phase3-done", "phase3")
    write_json(status_file, status)
    log(f"phase 3 done: dependencies, application API and library API written for run {run_id}")
    return 0


if __name__ == "__main__":
    run_main(main)
