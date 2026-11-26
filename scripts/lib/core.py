"""UpgradeLens shared functions for every stage.

Import from a stage script (the script puts scripts\\ on sys.path first):
    from lib import core

Parsers, matchers and the verdict rule are pure functions: they take values and return values, so
tests\\test_upgradelens.py can call them without a Maven build or a running server.
"""

from __future__ import annotations

import datetime
import fnmatch
import hashlib
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import traceback
import xml.etree.ElementTree as ET

IS_WINDOWS = os.name == "nt"
TOOL_VERSION = "1.0.0"
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)

_stage_log: str | None = None


# ---------- small value helpers ----------

def now_iso() -> str:
    """Local time with offset, like PowerShell (Get-Date).ToString("o")."""
    return datetime.datetime.now().astimezone().isoformat()


def as_list(value) -> list:
    """None -> [], a list or tuple -> a list, anything else -> [value] (PowerShell @())."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def items(value) -> list:
    """as_list without None or empty entries (PowerShell @($x | Where-Object { $_ }))."""
    return [v for v in as_list(value) if v not in (None, "", [], {})]


def s(value) -> str:
    """PowerShell [string]: None -> "", True -> "True"."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


def get(obj, *path, default=None):
    """Nested lookup that never raises: get(doc, "branches", "head", "commit")."""
    node = obj
    for part in path:
        if node is None:
            return default
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, (list, tuple)) and isinstance(part, int):
            node = node[part] if -len(node) <= part < len(node) else None
        else:
            return default
    return default if node is None else node


def to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def sort_key(value) -> tuple:
    """String order of PowerShell Sort-Object (.NET culture compare): case-insensitive, hyphens and apostrophes
    ignored, punctuation before digits and letters. Ties are broken by the exact text, so the order is total."""
    t = s(value)
    folded = t.casefold()
    weights = tuple((1 if c.isalnum() else 0, c) for c in folded if c not in "-'")
    return (weights, folded, t)


def ps_sorted(values, key=None) -> list:
    if key is None:
        return sorted(values, key=sort_key)
    return sorted(values, key=lambda v: sort_key(key(v)))


def unique_sorted(values) -> list:
    """Sort-Object -Unique: case-insensitive order and uniqueness, first spelling kept."""
    seen = {}
    for v in values:
        k = s(v).casefold()
        if k not in seen:
            seen[k] = v
    return ps_sorted(seen.values())


def unique(values) -> list:
    """Select-Object -Unique: first occurrence kept, original order."""
    out = []
    for v in values:
        if v not in out:
            out.append(v)
    return out


def like(value, pattern) -> bool:
    """PowerShell -like: wildcard match (* ? [..]), case-insensitive."""
    return fnmatch.fnmatchcase(s(value).casefold(), s(pattern).casefold())


def ieq(a, b) -> bool:
    """PowerShell -eq on strings: case-insensitive."""
    return s(a).casefold() == s(b).casefold()


def icontains(collection, value) -> bool:
    """PowerShell -contains on strings: case-insensitive."""
    v = s(value).casefold()
    return any(s(c).casefold() == v for c in as_list(collection))


def native_path(rel: str) -> str:
    """A relative path with / or \\ separators, in this OS's form."""
    return s(rel).replace("/", os.sep).replace("\\", os.sep)


def short(sha) -> str:
    t = s(sha)
    return t[:10] if len(t) >= 10 else t


# ---------- files and hashes ----------

def write_json(path: str, value) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    text = json.dumps(value, indent=2, ensure_ascii=False)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text + "\n")


def read_json(path: str):
    """The parsed file, or None when it is missing or empty. Invalid JSON raises ValueError."""
    if not path or not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8-sig") as f:
        text = f.read()
    if not text.strip():
        return None
    return json.loads(text)


def read_json_safe(path: str):
    """read_json, but invalid JSON is None too."""
    try:
        return read_json(path)
    except (ValueError, OSError):
        return None


def read_text(path: str) -> str:
    """File text with its line endings kept (BOM dropped)."""
    with open(path, "rb") as f:
        data = f.read()
    return data.decode("utf-8-sig", errors="replace")


def read_lines(path: str) -> list[str]:
    """File lines, like Get-Content: no line ends."""
    return split_lines(read_text(path))


def write_text(path: str, text: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def append_text(path: str, text: str) -> None:
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write(text)


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def input_hash(paths) -> str:
    """SHA-256 over the bytes of the given files, in order. A missing file contributes its path, so a
    missing input changes the hash too. Used to tie agent proposals to their inputs."""
    h = hashlib.sha256()
    for p in paths:
        if os.path.isfile(p):
            with open(p, "rb") as f:
                h.update(f.read())
        else:
            h.update(("missing:" + p).encode("utf-8"))
    return h.hexdigest().upper()


# ---------- logging and native commands ----------

def set_stage_log(path: str | None) -> None:
    global _stage_log
    _stage_log = path


def log(message: str) -> None:
    line = f"{datetime.datetime.now():%H:%M:%S} {message}"
    print(line, flush=True)
    if _stage_log:
        d = os.path.dirname(_stage_log)
        if d:
            os.makedirs(d, exist_ok=True)
        append_text(_stage_log, line + "\n")


class NativeResult:
    def __init__(self, exit_code: int, output: list[str]):
        self.exit_code = exit_code
        self.output = output

    def first(self) -> str:
        return self.output[0] if self.output else ""

    def text(self, sep: str = " ") -> str:
        return sep.join(self.output)


def decode_output(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False) or "latin-1", errors="replace")


def which(exe: str) -> str | None:
    """The full path of a command (finds mvn.cmd on Windows), or None."""
    if not exe:
        return None
    if os.path.isabs(exe) or os.sep in exe or (os.altsep and os.altsep in exe):
        return exe if os.path.isfile(exe) else None
    return shutil.which(exe)


def run_native(exe: str, args=(), env: dict | None = None, cwd: str | None = None,
               timeout: float | None = None) -> NativeResult:
    """Runs an external command and returns its exit code and output (stdout and stderr together) as
    lines. A command that cannot start gives exit code -1 and the error as its output."""
    resolved = which(exe) or exe
    try:
        p = subprocess.run([resolved] + [s(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           stdin=subprocess.DEVNULL, env=env, cwd=cwd, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return NativeResult(-1, [str(e)])
    return NativeResult(p.returncode, split_lines(decode_output(p.stdout)))


def split_lines(text: str) -> list[str]:
    """Lines split on \\n only (str.splitlines also splits on \\x1c-\\x1e, which git log formats use as markers)."""
    lines = [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def jdk_env(jdk_home: str, base: dict | None = None) -> dict:
    """An environment with JAVA_HOME and PATH pointing at one JDK (for a child process only)."""
    env = dict(os.environ if base is None else base)
    env["JAVA_HOME"] = jdk_home
    env["PATH"] = os.path.join(jdk_home, "bin") + os.pathsep + env.get("PATH", "")
    return env


def jdk_tool(jdk_home: str, name: str) -> str:
    """Path of a JDK tool: <jdk>\\bin\\<name>.exe on Windows."""
    return os.path.join(s(jdk_home), "bin", name + (".exe" if IS_WINDOWS else ""))


def run_script(script: str, args=(), env_extra: dict | None = None) -> int:
    """Runs another UpgradeLens script (scripts\\<script>) with this Python and returns its exit code.
    Its output goes to this process's output. A script that cannot start is a tool failure (3)."""
    env = dict(os.environ)
    env.update(env_extra or {})
    try:
        sys.stdout.flush()
        return subprocess.call([sys.executable, os.path.join(SCRIPTS_DIR, script)] + [s(a) for a in args],
                               env=env, stdin=subprocess.DEVNULL)
    except OSError as e:
        print(f"{script} could not start: {e}")
        return 3


def command(script: str, *args) -> str:
    """The command line an orchestrator runs for a script, from the UpgradeLens folder (works in PowerShell,
    cmd and bash)."""
    parts = [f"python scripts/{script}"]
    for a in args:
        t = s(a)
        parts.append(f'"{t}"' if (not t or any(ch in t for ch in ' \t&|<>^()')) else t)
    return " ".join(parts)


def remove_tree(path: str) -> None:
    """Deletes a folder, including read-only files (git object files are read-only on Windows)."""
    import stat

    def on_error(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    if os.path.isdir(path):
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=on_error)
        else:
            shutil.rmtree(path, onerror=on_error)
    elif os.path.exists(path):
        os.chmod(path, stat.S_IWRITE)
        os.remove(path)


def tail(path: str, n: int) -> list[str]:
    try:
        return read_lines(path)[-n:]
    except OSError:
        return []


INTERRUPTED_EXIT_CODES = {130, -2, 3221225786, -1073741510}  # Ctrl+C: POSIX SIGINT, Windows STATUS_CONTROL_C_EXIT


def ignore_interrupts() -> None:
    """A stage runs detached and must finish even when the terminal that started it sends Ctrl+C or Ctrl+Break
    (an interrupted wait, a closed chat tool). Inherited by the processes it starts (Maven, the JVMs)."""
    import signal
    for name in ("SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), signal.SIG_IGN)
            except (ValueError, OSError):
                pass
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleCtrlHandler(None, True)
        except (AttributeError, OSError):
            pass


def run_main(main) -> None:
    """Entry point of a script: its return value is the exit code; an unexpected error is a tool failure
    (exit 3), never a code that could look like a result (1 is 'blocked' for stage 10). A stage started by
    run-stage.py ignores Ctrl+C; anything else that is interrupted exits 130."""
    if os.environ.get("UPGRADELENS_INVOKER") == "run-stage":
        ignore_interrupts()
    try:
        code = main(sys.argv[1:])
    except SystemExit:
        raise
    except KeyboardInterrupt:
        code = 130
    except Exception as e:  # noqa: BLE001 - every failure must become exit 3
        traceback.print_exc()
        print(f"{os.path.basename(sys.argv[0])} failed: {e}")
        code = 3
    sys.stdout.flush()
    sys.exit(int(code or 0))


# ---------- configuration ----------

def resolve_config_path(config: str | None, root_dir: str = ROOT_DIR) -> str:
    """The config file a stage uses: --config if given, else UPGRADELENS_CONFIG (set by run-stage.py and
    run-pipeline.py so every stage and agent step of one run reads the same file), else
    config\\upgrade-lens.json."""
    p = config or os.environ.get("UPGRADELENS_CONFIG") or os.path.join(root_dir, "config", "upgrade-lens.json")
    if not os.path.isabs(p):
        p = os.path.join(os.getcwd(), p)
    return os.path.abspath(p)


def config_value(cfg, path: str, default=None):
    """Reads a dotted key ("timeouts.buildMinutes") from a config dict. Returns default when the key is
    missing, null or an empty string."""
    node = cfg
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
        if node is None:
            return default
    if isinstance(node, str) and not node.strip():
        return default
    return node


def resolve_project_path(path: str, root_dir: str = ROOT_DIR) -> str:
    """A config path relative to the UpgradeLens folder, or an absolute path as is."""
    if os.path.isabs(path):
        return path
    return os.path.join(root_dir, native_path(path))


# ---------- run folder and status ----------

def runs_root(cfg, root_dir: str = ROOT_DIR) -> str:
    folder = s(get(cfg, "runsFolder", default="runs"))
    if os.path.isabs(folder):
        return folder
    return os.path.join(root_dir, native_path(folder))


def is_orchestrated_run(run_dir: str) -> bool:
    """A run started by the orchestrator (run-stage.py --new) has a ledger. Its stages may only be run
    through run-stage.py, so the order, the lock and the agent gates cannot be bypassed."""
    return os.path.isfile(os.path.join(run_dir, "orchestration", "ledger.json"))


def resolve_run_id(runs_dir: str, run_id: str | None, states=(), any_state: bool = False) -> str | None:
    """The given run id, or the newest run folder whose status.json state is one of states.
    Guards (a stage prints the reason and gets None back, so it exits 2):
      - a given run must be in one of states (unless any_state): no stage runs out of order
      - an orchestrated run accepts a stage only from run-stage.py (UPGRADELENS_INVOKER)"""
    states = list(states)
    if run_id:
        run_dir = os.path.join(runs_dir, run_id)
        st = read_json_safe(os.path.join(run_dir, "status.json"))
        if not st:
            print(f"Run not found: {run_dir}")
            return None
        if is_orchestrated_run(run_dir) and os.environ.get("UPGRADELENS_INVOKER") != "run-stage":
            print(f"Run {run_id} is run by the UpgradeLens orchestrator. Run its stages through the "
                  "upgradelens-orchestrator agent (scripts\\next-step.py and scripts\\run-stage.py), not directly: "
                  "a direct call would bypass the order, the lock and the agent gates.")
            return None
        if not any_state and states and not icontains(states, st.get("state")):
            print(f"Run {run_id} is in state '{s(st.get('state'))}'. This stage needs one of: {', '.join(states)}. "
                  "Run the earlier stages first.")
            return None
        return run_id
    if not os.path.isdir(runs_dir):
        return None
    for name in sorted(os.listdir(runs_dir), key=sort_key, reverse=True):
        path = os.path.join(runs_dir, name)
        if not os.path.isdir(path):
            continue
        st = read_json_safe(os.path.join(path, "status.json"))
        if st and icontains(states, st.get("state")):
            return name
    return None


def set_stage_step(status: dict, name: str, state: str, reason: str = "", artifact: str = "") -> None:
    """Replaces the step with the same name (so a rerun of a stage does not duplicate it)."""
    steps = [x for x in as_list(status.get("steps")) if x and x.get("name") != name]
    steps.append({"name": name, "state": state, "reason": reason, "artifact": artifact, "finishedAt": now_iso()})
    status["steps"] = steps


def set_run_state(status: dict, state: str, phase: str | None = None) -> None:
    status["state"] = state
    if phase:
        status["phase"] = phase


# ---------- small helpers ----------

def major_version(version) -> int:
    m = re.match(r"^(\d+)", s(version))
    return int(m.group(1)) if m else -1


def glob_match(path: str, glob: str) -> bool:
    """Simple glob for file paths: * matches anything, including "/". Case-insensitive."""
    return like(s(path).replace("\\", "/"), s(glob).replace("\\", "/"))


# ---------- dependency tree ----------

def parse_maven_coordinate(coordinate: str):
    """groupId:artifactId:type[:classifier]:version[:scope] -> {ga, version, scope}. None when not one."""
    parts = coordinate.split(":")
    if len(parts) == 4:
        return {"ga": f"{parts[0]}:{parts[1]}", "version": parts[3], "scope": ""}
    if len(parts) == 5:
        return {"ga": f"{parts[0]}:{parts[1]}", "version": parts[3], "scope": parts[4]}
    if len(parts) == 6:
        return {"ga": f"{parts[0]}:{parts[1]}", "version": parts[4], "scope": parts[5]}
    return None


_TREE_RX = re.compile(r"^(?P<prefix>[ |+\\-]*)(?P<coord>[A-Za-z0-9_.\-]+:\S+)")
_OMITTED_RX = re.compile(r"^(?P<prefix>[ |+\\-]*)\((?P<coord>[A-Za-z0-9_.\-]+:[^\s)]+) - (?P<note>[^)]*)\)\s*$")
_KEPT_RX = re.compile(r"^(?P<prefix>[ |+\\-]*)(?P<coord>[A-Za-z0-9_.\-]+:\S+)(?:\s+\((?P<note>[^)]*)\))?\s*$")


def parse_dependency_tree(lines) -> list[dict]:
    """Parses "mvn dependency:tree -DoutputType=text" (plain or -Dverbose). Each resolved line: tree prefix,
    then groupId:artifactId:type[:classifier]:version[:scope]. Depth comes from the prefix width
    (3 characters per level); depth 0 is the project itself. "parent" is the ga one level up.
    Verbose-only lines, "(g:a:... - omitted for ...)", are not resolved artifacts and are skipped here
    (parse_dependency_tree_verbose reads them)."""
    out = []
    stack: dict[int, str] = {}
    for raw in as_list(lines):
        line = s(raw).rstrip()
        m = _TREE_RX.search(line)
        if not m:
            continue
        depth = len(m.group("prefix")) // 3
        c = parse_maven_coordinate(m.group("coord"))
        if not c:
            continue
        stack[depth] = c["ga"]
        parent = stack.get(depth - 1, "") if depth >= 1 else ""
        out.append({"ga": c["ga"], "version": c["version"], "scope": c["scope"], "depth": depth,
                    "direct": depth == 1, "parent": parent})
    return out


def parse_dependency_tree_verbose(lines) -> dict:
    """Reads the "-Dverbose" annotations of a dependency tree:
      conflicts   "(g:a:jar:1.0:compile - omitted for conflict with 2.0)": a dependency asked for a version
                  that lost to another one. Grouped per artifact and requested version, with who asked.
      duplicates  count of "omitted for duplicate" lines
      managed     resolved artifacts whose version a BOM or parent changed ("version managed from X")"""
    conflicts: dict[str, dict] = {}
    managed = []
    duplicates = 0
    stack: dict[int, str] = {}
    for raw in as_list(lines):
        line = s(raw).rstrip()
        m = _OMITTED_RX.search(line)
        if m:
            depth = len(m.group("prefix")) // 3
            note = m.group("note")
            c = parse_maven_coordinate(m.group("coord"))
            if not c:
                continue
            parent = stack.get(depth - 1, "")
            cm = re.search(r"omitted for conflict with ([^;\s)]+)", note, re.I)
            if cm:
                winner = cm.group(1)
                key = f"{c['ga']}|{c['version']}|{winner}"
                if key not in conflicts:
                    conflicts[key] = {"ga": c["ga"], "requested": c["version"], "resolved": winner,
                                      "major": major_version(c["version"]) != major_version(winner),
                                      "requiredBy": []}
                if parent and parent not in conflicts[key]["requiredBy"]:
                    conflicts[key]["requiredBy"].append(parent)
            elif re.search(r"omitted for duplicate", note, re.I):
                duplicates += 1
            continue
        m = _KEPT_RX.search(line)
        if m:
            depth = len(m.group("prefix")) // 3
            note = m.group("note") or ""
            c = parse_maven_coordinate(m.group("coord"))
            if not c:
                continue
            stack[depth] = c["ga"]
            mm = re.search(r"version managed from ([^;\s)]+)", note, re.I)
            if mm and mm.group(1) != c["version"]:
                managed.append({"ga": c["ga"], "declared": mm.group(1), "managed": c["version"]})
    rows = []
    for key in ps_sorted(conflicts.keys()):
        x = conflicts[key]
        rows.append({"ga": x["ga"], "requested": x["requested"], "resolved": x["resolved"], "major": x["major"],
                     "requiredBy": ps_sorted(x["requiredBy"])})
    return {"conflicts": rows, "duplicates": duplicates, "managed": ps_sorted(managed, key=lambda r: r["ga"])}


def compare_dependency_trees(base, head) -> dict:
    """Compares two parsed trees by groupId:artifactId. Keeps the shallowest occurrence of each artifact."""
    def to_map(entries):
        m = {}
        for d in as_list(entries):
            if to_int(d.get("depth")) < 1:
                continue
            if d["ga"] not in m or to_int(d.get("depth")) < to_int(m[d["ga"]].get("depth")):
                m[d["ga"]] = d
        return m

    bm, hm = to_map(base), to_map(head)
    added = [{"ga": ga, "version": hm[ga].get("version"), "direct": bool(hm[ga].get("direct"))}
             for ga in ps_sorted(hm) if ga not in bm]
    removed = [{"ga": ga, "version": bm[ga].get("version"), "direct": bool(bm[ga].get("direct"))}
               for ga in ps_sorted(bm) if ga not in hm]
    changed = []
    for ga in ps_sorted(hm):
        if ga in bm and bm[ga].get("version") != hm[ga].get("version"):
            bv, hv = bm[ga].get("version"), hm[ga].get("version")
            changed.append({"ga": ga, "base": bv, "head": hv, "major": major_version(bv) != major_version(hv),
                            "direct": bool(bm[ga].get("direct") or hm[ga].get("direct"))})
    return {"added": added, "removed": removed, "changed": changed}


# ---------- JaCoCo ----------

def parse_jacoco_xml(path: str) -> dict:
    """Covered INSTRUCTION counts:
      "com/example/Foo|method"  one entry per method name (overloads are summed)
      "com/example/Foo|*"       class total
    An empty dict means the report is missing."""
    result: dict[str, int] = {}
    if not os.path.isfile(path):
        return result
    root = ET.parse(path).getroot()
    for pkg in root.findall("package"):
        for cls in pkg.findall("class"):
            cname = s(cls.get("name"))
            class_covered = 0
            for counter in cls.findall("counter"):
                if counter.get("type") == "INSTRUCTION":
                    class_covered = to_int(counter.get("covered"))
            result[f"{cname}|*"] = class_covered
            for m in cls.findall("method"):
                covered = 0
                for counter in m.findall("counter"):
                    if counter.get("type") == "INSTRUCTION":
                        covered = to_int(counter.get("covered"))
                key = f"{cname}|{m.get('name')}"
                result[key] = result.get(key, 0) + covered
    return result


# ---------- Java source reading ----------

JAVA_NON_DECLARATION_WORDS = {
    "if", "for", "while", "switch", "catch", "return", "new", "throw", "else", "do", "try", "synchronized",
    "case", "assert", "yield", "super", "this", "instanceof", "record", "class", "interface", "enum", "var",
}

_TYPE_DECL_RX = re.compile(
    r"^\s*(?:@[\w.]+(?:\([^)]*\))?\s+)*(?:(?:public|protected|private|static|final|abstract|sealed|non-sealed|strictfp)\s+)*"
    r"(?:record|class|interface|enum|@interface)\s+([\w$]+)", re.I)
_METHOD_DECL_RX = re.compile(
    r"^\s+(?:@[\w.]+(?:\([^)]*\))?\s+)*"
    r"(?P<mods>(?:(?:public|protected|private|static|final|abstract|synchronized|default|native|strictfp)\s+)*)"
    r"(?:<[^()]*?>\s+)?(?:(?P<type>[\w$.]+(?:<[^()]*>)?(?:\[\])*)\s+)?(?P<name>[\w$]+)\s*\(")


def remove_java_literals(line: str) -> str:
    """Blanks string and char literals and drops a trailing // comment, so braces, parentheses and keywords
    inside them do not count. Text blocks and /* */ comments are not handled (rare on declaration lines)."""
    t = re.sub(r'"(?:\\.|[^"\\])*"', '""', s(line))
    t = re.sub(r"'(?:\\.|[^'\\])+'", "''", t)
    t = re.sub(r"//.*$", "", t)
    return t


def type_declaration_name(line: str) -> str | None:
    """Name of the class, interface, enum, record or annotation type declared on this line, or None."""
    t = remove_java_literals(s(line).rstrip("\r"))
    m = _TYPE_DECL_RX.search(t)
    return m.group(1) if m else None


def method_declaration_name(line: str) -> str | None:
    """Name of the method or constructor declared on this line, or None. Covers every access level
    (package-private too), generic methods, annotations on the same line, and declarations whose
    parameters continue on the next lines. A call statement, a local variable or a control statement
    is not a declaration. A constructor without an access modifier is accepted when its name is
    capitalized and the line has no ";" (a call statement ends with one)."""
    t = remove_java_literals(s(line).rstrip("\r"))
    if not re.search(r"^\s+[\w@<]", t):
        return None
    m = _METHOD_DECL_RX.search(t)
    if not m:
        return None
    name = m.group("name")
    type_ = m.group("type") or ""
    if name.lower() in JAVA_NON_DECLARATION_WORDS:
        return None
    if type_:
        if type_.lower() in JAVA_NON_DECLARATION_WORDS:
            return None
        return name
    # No return type: only a constructor.
    if m.group("mods").strip():
        return name
    if re.match(r"^[A-Z]", name) and ";" not in t:
        return name
    return None


def symbol_for_line(lines, line_no: int) -> str | None:
    """The symbol a changed line belongs to: "method:Name", "type:Name", or None (package, imports).
      1. A declaration line belongs to itself.
      2. An annotation line belongs to the declaration right below it (only annotations or blank lines
         in between).
      3. Any other line belongs to the nearest declaration above it whose block or parameter list is
         still open at that line (brace and parenthesis balance). So a field between two methods belongs
         to its type, not to the method above it. The search stops at a column-0 line."""
    if not lines or line_no < 1 or line_no > len(lines):
        return None
    i = line_no - 1
    text = s(lines[i]).rstrip("\r")

    t = type_declaration_name(text)
    if t:
        return f"type:{t}"
    m = method_declaration_name(text)
    if m:
        return f"method:{m}"

    if re.search(r"^\s*@", text):
        for j in range(i + 1, min(len(lines), i + 8)):
            nxt = s(lines[j]).rstrip("\r")
            t = type_declaration_name(nxt)
            if t:
                return f"type:{t}"
            m = method_declaration_name(nxt)
            if m:
                return f"method:{m}"
            if not re.search(r"^\s*(@.*)?$", nxt):
                break

    brace_net = 0
    paren_net = 0
    for j in range(i - 1, -1, -1):
        raw = s(lines[j]).rstrip("\r")
        clean = remove_java_literals(raw)
        brace_net += clean.count("{") - clean.count("}")
        paren_net += clean.count("(") - clean.count(")")
        if brace_net >= 1 or paren_net >= 1:
            t = type_declaration_name(raw)
            if t:
                return f"type:{t}"
            m = method_declaration_name(raw)
            if m:
                return f"method:{m}"
        if re.search(r"^\S", raw):
            break
    return None


def block_end(text: str, start: int) -> int:
    """Index of the "}" that closes the "{" at start, skipping string and char literals and comments."""
    depth = 0
    k = start
    n = len(text)
    while k < n:
        c = text[k]
        if c in ('"', "'"):
            q = c
            k += 1
            while k < n and text[k] != q:
                if text[k] == "\\":
                    k += 1
                k += 1
        elif c == "/" and k + 1 < n and text[k + 1] == "/":
            while k < n and text[k] != "\n":
                k += 1
        elif c == "/" and k + 1 < n and text[k + 1] == "*":
            end = text.find("*/", k + 2)
            k = n if end < 0 else end + 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return k
        k += 1
    return -1


def method_body(text: str, method: str) -> str | None:
    """Source text of one method body (from its "{" to the matching "}"), or None. The first declaration
    with that name that has a body wins (abstract and interface declarations are skipped)."""
    offset = 0
    for line in text.split("\n"):
        if method_declaration_name(line) == method:
            opening = text.find("{", offset)
            semi = text.find(";", offset)
            if opening >= 0 and (semi < 0 or opening < semi):
                end = block_end(text, opening)
                if end > opening:
                    return text[opening:end + 1]
        offset += len(line) + 1
    return None


_FIELD_RX = re.compile(r"^\s+(?:(?:private|protected|public|final|static|transient|volatile)\s+)*"
                       r"([A-Z][\w.]*)(?:<[^>;=]*>)?\s+(\w+)\s*(?:;|=)", re.M)


def java_files(root: str) -> list[str]:
    """Every *.java file under root, in a stable order."""
    out = []
    for d, dirs, files in os.walk(root):
        dirs.sort(key=sort_key)
        for f in ps_sorted(files):
            if f.lower().endswith(".java"):
                out.append(os.path.join(d, f))
    return out


def source_index(source_root: str) -> dict:
    """Index of the main classes: simple name -> {fqcn, path, text, fields, methods}."""
    index: dict[str, dict] = {}
    if not os.path.isdir(source_root):
        return index
    for path in java_files(source_root):
        text = read_text(path).replace("\r\n", "\n")
        simple = os.path.splitext(os.path.basename(path))[0]
        pm = re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.M)
        pkg = pm.group(1) if pm else ""
        fields = {}
        for fm in _FIELD_RX.finditer(text):
            fields[fm.group(2)] = fm.group(1)
        ctor = re.search(r"^\s+public\s+" + re.escape(simple) + r"\s*\(([^)]*)\)", text, re.M)
        if ctor:
            for param in ctor.group(1).split(","):
                cleaned = re.sub(r"@\w+(\([^)]*\))?", "", param, flags=re.I).strip()
                tokens = [x for x in re.split(r"\s+", cleaned) if x]
                if len(tokens) >= 2:
                    fields[tokens[-1]] = re.sub(r"<.*$", "", tokens[-2])
        methods = {}
        for line in text.split("\n"):
            mn = method_declaration_name(line)
            if mn:
                methods[mn] = True
        index[simple] = {"fqcn": f"{pkg}.{simple}" if pkg else simple, "path": path, "text": text,
                         "fields": fields, "methods": methods}
    return index


def mapping_path(annotation_args: str) -> str:
    m = re.search(r'(?:value|path)\s*=\s*"([^"]*)"', s(annotation_args), re.I)
    if m:
        return m.group(1)
    m = re.search(r'^\s*"([^"]*)"', s(annotation_args), re.I)
    if m:
        return m.group(1)
    return ""


def join_url_path(base: str, path: str) -> str:
    b = s(base).rstrip("/")
    p = s(path).lstrip("/")
    if not p:
        return b if b else "/"
    return f"{b}/{p}"


def source_flows(text: str, class_name: str) -> list[dict]:
    """Flows that start a path through the code: HTTP endpoints, @Scheduled jobs and listeners.
    Each flow: id, kind, class, method, and for HTTP the verb and full path."""
    flows = []
    lines = re.split(r"\r?\n", text)
    class_idx = -1
    for i, line in enumerate(lines):
        if type_declaration_name(line):
            class_idx = i
            break
    if class_idx < 0:
        return []

    base_path = ""
    for line in lines[:class_idx]:
        m = re.search(r'@RequestMapping\(\s*(?:value\s*=\s*|path\s*=\s*)?"([^"]*)"', line, re.I)
        if m:
            base_path = m.group(1)

    pending = None
    for line in lines[class_idx + 1:]:
        m = re.search(r"^\s*@(Get|Post|Put|Delete|Patch)Mapping\b(\((.*)\))?", line, re.I)
        if m:
            pending = {"kind": "http", "verb": m.group(1).upper(), "path": mapping_path(m.group(3) or "")}
            continue
        m = re.search(r"^\s*@RequestMapping\b(\((.*)\))?", line, re.I)
        if m:
            ann = m.group(2) or ""
            verb = "GET"
            vm = re.search(r"RequestMethod\.(\w+)", ann, re.I)
            if vm:
                verb = vm.group(1).upper()
            pending = {"kind": "http", "verb": verb, "path": mapping_path(ann)}
            continue
        if re.search(r"^\s*@Scheduled\b", line, re.I):
            pending = {"kind": "job", "verb": "", "path": ""}
            continue
        if re.search(r"^\s*@\w*Listener\b", line, re.I):
            pending = {"kind": "listener", "verb": "", "path": ""}
            continue
        if pending:
            method = method_declaration_name(line)
            if method:
                if pending["kind"] == "http":
                    full = join_url_path(base_path, pending["path"])
                    fid = f"{pending['verb']} {full}"
                else:
                    full = ""
                    fid = f"{pending['kind']} {class_name}.{method}"
                flows.append({"id": fid, "kind": pending["kind"], "class": class_name, "method": method,
                              "verb": pending["verb"], "path": full})
                pending = None
    return flows


_CALL_ON_RX = re.compile(r"(?<![\w.])(\w+)\.(\w+)\s*\(")
_CALL_RX = re.compile(r"(?<![\w.])(\w+)\s*\(")


def reach_set(index: dict, class_name: str, method: str, max_depth: int = 4) -> list[str]:
    """Symbols reached from one method, following calls through injected fields and same-class calls.
    Returns "Class#method" strings. Calls into JDK or library types are not followed."""
    seen: set[str] = set()
    result: list[str] = []

    def visit(cls: str, meth: str, depth: int) -> None:
        key = f"{cls}#{meth}"
        if key in seen:
            return
        seen.add(key)
        result.append(key)
        if depth >= max_depth or cls not in index:
            return
        entry = index[cls]
        body = method_body(entry["text"], meth)
        if not body:
            return
        for c in _CALL_ON_RX.finditer(body):
            recv, called = c.group(1), c.group(2)
            target = None
            if recv == "this":
                target = cls
            elif recv in entry["fields"]:
                target = entry["fields"][recv]
            if target and target in index and called in index[target]["methods"]:
                visit(target, called, depth + 1)
        for c in _CALL_RX.finditer(body):
            name = c.group(1)
            if name != meth and name in entry["methods"]:
                visit(cls, name, depth + 1)

    visit(class_name, method, 0)
    return result


# ---------- rules ----------

def rule_applies(rule: dict, path: str) -> bool:
    """Source-based rules run only on files that match the rule's fileGlob (default: every file)."""
    if not rule.get("fileGlob"):
        return True
    return glob_match(path, rule["fileGlob"])


# ---------- verdict and flow scope ----------

def flow_scope(affecting_statuses, untraced_count: int, any_confirmed: bool) -> str:
    """Approach section 6 and 7 (stage 6). "full" keeps the whole regression for the flow;
    "targeted" is the reduced scope (targeted tests plus a smoke suite); "smoke" is smoke only.
    A flow is reduced only when every affecting finding is verified, all reached code is covered
    and traced, and no finding is confirmed as a behavior change."""
    statuses = as_list(affecting_statuses)
    if not statuses and not any_confirmed and untraced_count == 0:
        return "smoke"
    if any_confirmed:
        return "full"
    if untraced_count > 0:
        return "full"
    if all(x == "verified" for x in statuses):
        return "targeted"
    return "full"


def verdict(blocking_reasons=(), listed_items=(), required_stages_ran: bool = True) -> dict:
    """Approach section 10. Input is the list of blocking reasons and listed items, and whether
    every required stage ran. Output: verdict (blocked | safe-with-listed-tests | safe) and exit code."""
    if as_list(blocking_reasons):
        return {"verdict": "blocked", "exitCode": 1}
    if not required_stages_ran or as_list(listed_items):
        return {"verdict": "safe-with-listed-tests", "exitCode": 0}
    return {"verdict": "safe", "exitCode": 0}
