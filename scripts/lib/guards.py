"""Write-scope guards around an agent step: fingerprints of what the agent must not change, and the
comparison after it returns. Used by agent-step.py (orchestrated runs) and run-agent.py (one agent on its own).
"""

from __future__ import annotations

import os

from .core import as_list, file_sha256, like, ps_sorted, run_native, s, unique_sorted

# Never part of a fingerprint: the git worktrees (their git state is fingerprinted instead), the clone, and logs.
ALWAYS_EXCLUDED = ("worktrees\\*", "repo.git\\*", "logs\\*")


# ---------- pure part ----------

def compare_fingerprint(before: dict, after: dict) -> list[str]:
    """Keys added, removed or changed between two fingerprints (dicts key -> hash)."""
    changed = [k for k in before if k not in after or after[k] != before[k]]
    changed += [k for k in after if k not in before]
    return unique_sorted(changed)


def outside_scope(changed_files, allowed_patterns) -> list[str]:
    """The changed files (relative paths) that match none of the allowed patterns (wildcards, / or \\)."""
    allowed = [a.replace("\\", "/") for a in as_list(allowed_patterns)]
    return [c for c in as_list(changed_files)
            if not any(like(s(c).replace("\\", "/"), a) for a in allowed)]


# ---------- file system ----------

def _excluded(rel: str, patterns) -> bool:
    r = rel.replace("\\", "/")
    return any(like(r, p.replace("\\", "/")) for p in patterns)


def file_tree_hashes(root: str, exclude=(), prefix: str = "") -> dict:
    """relative path -> SHA-256 for every file under root (OS path separators in the keys), skipping relative
    paths that match an exclude pattern (wildcards, / or \\)."""
    result = {}
    if not os.path.isdir(root):
        return result
    root_full = os.path.abspath(root)
    patterns = list(exclude)
    for d, dirs, files in os.walk(root_full):
        rel_dir = os.path.relpath(d, root_full)
        rel_dir = "" if rel_dir == "." else rel_dir
        # A folder whose every path is excluded ("worktrees\*") is not walked at all.
        dirs[:] = [x for x in dirs if not any(p.endswith("*") and _excluded(os.path.join(rel_dir, x) + os.sep, [p]) for p in patterns)]
        for name in files:
            rel = os.path.join(rel_dir, name) if rel_dir else name
            if _excluded(rel, patterns):
                continue
            try:
                result[prefix + rel] = file_sha256(os.path.join(d, name))
            except OSError:
                result[prefix + rel] = "unreadable"
    return result


def protected_fingerprint(run_dir: str, tool_root: str, git_cmd: str = "git",
                          writable=("artifacts\\agent\\*", "orchestration\\*")) -> dict:
    """What an agent must not change: the run folder outside its writable folders (status, config snapshot,
    stage artifacts such as test-gaps.json, reports, review decisions), the git state of both worktrees, and
    the tool itself (scripts, config, agent and skill definitions). Python bytecode caches are not part of the
    tool.

    writable (relative to the run folder) is what the guards and the agent may write around the step:
      orchestrated run   artifacts\\agent (the agent; its own files are checked separately) and orchestration
                         (the guards; the guard record is protected by the hash kept in the ledger)
      one agent alone    standalone\\<session> only: the stage artifacts, earlier agent files and the ledger
                         are all protected"""
    fp = file_tree_hashes(run_dir, prefix="run:", exclude=list(ALWAYS_EXCLUDED) + list(writable))
    for d in ("scripts", "config", ".github", ".claude"):
        part = file_tree_hashes(os.path.join(tool_root, d), prefix=f"tool:{d}{os.sep}",
                                exclude=["__pycache__\\*", "*\\__pycache__\\*", "*.pyc"])
        fp.update(part)
    claude_md = os.path.join(tool_root, "CLAUDE.md")
    if os.path.isfile(claude_md):
        fp["tool:CLAUDE.md"] = file_sha256(claude_md)
    wt_root = os.path.join(run_dir, "worktrees")
    if os.path.isdir(wt_root):
        for name in ps_sorted(os.listdir(wt_root)):
            wt = os.path.join(wt_root, name)
            if not os.path.isdir(wt):
                continue
            st = run_native(git_cmd, ["-C", wt, "status", "--porcelain=v1", "--untracked-files=all"])
            hd = run_native(git_cmd, ["-C", wt, "rev-parse", "HEAD"])
            fp[f"worktree:{name}"] = "\n".join(hd.output + st.output)
    return fp
