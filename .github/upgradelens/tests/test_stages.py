"""The stage definitions (scripts\\lib\\stages.py). No Maven, server, git or agent is needed."""

import os

from lib.agents import AGENTS
from lib.core import write_json
from lib.stages import STAGES, exit_meaning, missing_message, missing_needs, stage_logs, written_by

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------- the definitions are complete and consistent ----------

def test_every_stage_script_exists():
    assert sorted(STAGES) == list(range(12))
    for spec in STAGES.values():
        assert os.path.isfile(os.path.join(ROOT, "scripts", spec.script)), spec.script


def test_every_stage_input_is_written_by_an_earlier_stage():
    for spec in STAGES.values():
        for need in spec.needs:
            writers = [o.stage for o in STAGES.values() if need in o.writes]
            assert writers and min(writers) < spec.stage, f"stage {spec.stage} needs {need}, written by {writers}"


def test_every_agent_input_has_a_known_writer():
    for spec in AGENTS.values():
        for name in list(spec.reads) + list(spec.repair_reads):
            assert written_by(f"artifacts\\{name}") != "unknown", f"{spec.step} reads {name}"


def test_exit_codes_have_stage_specific_meanings():
    assert exit_meaning(4, 1).startswith("a build failed on one branch")
    assert "Failing tests do not stop the stage" in exit_meaning(4, 1)
    assert exit_meaning(1, 10).startswith("verdict different or incomplete")
    assert exit_meaning(3221225786).startswith("killed")
    assert exit_meaning(99) == ""


def test_missing_inputs_name_the_stage_that_writes_them(tmp_path):
    write_json(str(tmp_path / "status.json"), {})
    assert set(missing_needs(str(tmp_path), 4)) == {"config\\upgrade-lens.json", "artifacts\\environment.json",
                                                   "artifacts\\change-inventory.json", "artifacts\\dependency-diff.json",
                                                   "artifacts\\api-diff.json"}
    msg = missing_message(str(tmp_path), 4)
    assert "artifacts\\change-inventory.json  (written by stage 2" in msg
    assert missing_message(str(tmp_path), 0) == ""


def test_stage_logs_show_the_newest_per_branch_logs(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    for name in ("build-main.log", "build-change.log", "phase1.log"):
        (logs / name).write_text("x", encoding="utf-8")
    found = [os.path.basename(p) for p in stage_logs(str(tmp_path), 1)]
    assert set(found[:2]) == {"build-main.log", "build-change.log"} and found[2] == "phase1.log"


