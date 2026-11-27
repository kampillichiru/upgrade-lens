"""Removes the clone and the worktrees of one UpgradeLens run (or of every run). The run's artifacts, logs and
reports are kept unless --delete-run-folder is given.

Each run clones the repository into runs\\<id>\\repo.git and checks out both branches under
runs\\<id>\\worktrees\\. The change_branch worktree is a real branch: stage 8 places the validated new tests
there, uncommitted, for a developer to review, commit and push.

So this script first checks each worktree, and skips a run (exit 3) whose worktrees hold work that would be
lost:
  - uncommitted changes or untracked files (for example placed new tests not yet committed)
  - commits on the branch that are not pushed to origin
--force removes them anyway. artifacts\\new-tests.patch keeps the new tests in every case.

Runs made before the clone-per-run change used a shared repository; for those, the script runs
"git worktree remove" and "git worktree prune" in that repository, and changes nothing else there.

Exit codes:
  0  done
  2  usage problem (no such run, or neither --run-id nor --all)
  3  at least one run was skipped (work that would be lost) or git failed

Examples:
  python .github/upgradelens/scripts/cleanup-run.py --run-id 20261126-143429
  python .github/upgradelens/scripts/cleanup-run.py --all --delete-run-folder
"""

import argparse
import os
import re
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.core import (config_value, get, ps_sorted, read_json_safe, remove_tree, resolve_config_path, run_main,  # noqa: E402
                      run_native, runs_root, s, to_int)


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Remove the clone and worktrees of UpgradeLens runs.")
    p.add_argument("--run-id", help="run folder name under runs\\")
    p.add_argument("--all", action="store_true", help="every run folder under runs\\")
    p.add_argument("--force", action="store_true", help="remove worktrees even with uncommitted or unpushed work")
    p.add_argument("--delete-run-folder", action="store_true", help="also delete the run folder (artifacts, logs, reports)")
    p.add_argument("--config")
    a = p.parse_args(argv)

    config = resolve_config_path(a.config)
    if not os.path.isfile(config):
        print(f"Config not found: {config}")
        return 2
    cfg = read_json_safe(config)
    runs = runs_root(cfg)
    if not a.run_id and not a.all:
        print("Give --run-id <id> or --all.")
        return 2

    if a.all:
        names = ps_sorted([n for n in os.listdir(runs) if os.path.isdir(os.path.join(runs, n))]) if os.path.isdir(runs) else []
    else:
        names = [a.run_id] if os.path.isdir(os.path.join(runs, a.run_id)) else []
    if not names:
        named = f" named {a.run_id}" if a.run_id else ""
        print(f"No run found under {runs}{named}.")
        return 2

    git = s(config_value(cfg, "git.command", "git"))

    def unsaved_work(worktree: str) -> list[str]:
        """Reasons the worktree cannot be removed without losing work. Empty when it is safe."""
        why = []
        if not os.path.exists(os.path.join(worktree, ".git")):
            return why
        st = run_native(git, ["-C", worktree, "status", "--porcelain"])
        changes = [x for x in st.output if x]
        if st.exit_code == 0 and changes:
            why.append(f"{len(changes)} uncommitted change(s) or untracked file(s)")
        up = run_native(git, ["-C", worktree, "rev-list", "--count", "@{upstream}..HEAD"])
        if up.exit_code == 0 and to_int(up.first()) > 0:
            why.append(f"{up.first()} commit(s) not pushed")
        return why

    failed = 0
    for name in names:
        run_dir = os.path.join(runs, name)
        env_rec = read_json_safe(os.path.join(run_dir, "artifacts", "environment.json"))
        run_cfg = read_json_safe(os.path.join(run_dir, "config", "upgrade-lens.json"))
        own_repo = os.path.join(run_dir, "repo.git")
        is_own = os.path.isdir(own_repo)
        repo = own_repo if is_own else (get(env_rec, "repo") or get(run_cfg, "repo"))
        wt_root = os.path.join(run_dir, "worktrees")
        wts = [os.path.join(wt_root, n) for n in ps_sorted(os.listdir(wt_root))] if os.path.isdir(wt_root) else []
        wts = [w for w in wts if os.path.isdir(w)]

        if not a.force:
            blocked = [f"{os.path.basename(wt)}: {w}" for wt in wts for w in unsaved_work(wt)]
            if blocked:
                print(f"{name}: skipped, it holds work that would be lost: {'; '.join(blocked)}. Commit and push it, or use --force.")
                failed += 1
                continue

        for wt in wts:
            if repo and os.path.isdir(repo):
                r = run_native(git, ["-C", repo, "worktree", "remove", "--force", wt])
                if r.exit_code != 0 and not re.search(r"is not a working tree", r.text(), re.I):
                    print(f"{name}: git worktree remove failed for {wt}: {r.text()}")
                    failed += 1
                    continue
            if os.path.exists(wt):
                remove_tree(wt)
            print(f"{name}: removed worktree {os.path.basename(wt)}")
        if is_own:
            remove_tree(own_repo)
            print(f"{name}: removed its clone (repo.git)")
        elif repo and os.path.isdir(repo):
            prune = run_native(git, ["-C", repo, "worktree", "prune"])
            if prune.exit_code != 0:
                print(f"git worktree prune failed in {repo}: {prune.text()}")
                failed += 1
        if a.delete_run_folder:
            remove_tree(run_dir)
            print(f"{name}: run folder deleted")
    return 3 if failed else 0


if __name__ == "__main__":
    run_main(main)
