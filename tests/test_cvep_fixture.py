"""tools/cvep_fixture.py: the c-VEP frame fixture the iPhone app's tests read (ios/BrainSyncKit,
CVEPCodeTests) is current, and its frames follow the draft's rule (section 9) for cvep_2."""

from __future__ import annotations

import json

from app.schemas import Stimulus
from tools import cvep_fixture, ui_fixture
from tools.cvep_fixture import FIXTURE


def committed() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_committed_fixture_is_the_generated_one():
    assert FIXTURE.read_text(encoding="utf-8") == cvep_fixture.render(), \
        "stale fixture: run python tools/cvep_fixture.py"


def test_check_passes_on_the_committed_fixture_and_fails_on_a_stale_one(tmp_path):
    assert cvep_fixture.main(["--check"]) == 0
    stale = tmp_path / "cvep_frames.json"
    stale.write_text("{}\n")
    assert cvep_fixture.main(["--check", "--out", str(stale)]) == 1
    assert cvep_fixture.main(["--check", "--out", str(tmp_path / "missing.json")]) == 1
    assert cvep_fixture.main(["--out", str(stale)]) == 0 and stale.read_text() == cvep_fixture.render()


def test_cvep_2_is_the_code_the_server_sends():
    case = next(c for c in committed()["cases"] if c["name"] == "cvep_2")
    stim = Stimulus.model_validate(case["stimulus"])
    assert stim.bits == ui_fixture.CVEP_BITS and len(stim.bits) == 63
    assert stim.lags == {"s1": 0, "s2": 31} and stim.frames_per_bit == 1
    assert case["cycle_frames"] == 63 and case["frames"] == 140


def test_every_frame_follows_the_draft_rule():
    # bright on frame n when bits[((n - lag) mod cycle) // frames_per_bit] == "1"
    cases = committed()["cases"]
    assert any(c["stimulus"]["frames_per_bit"] > 1 for c in cases)
    for c in cases:
        stim = c["stimulus"]
        cycle = len(stim["bits"]) * stim["frames_per_bit"]
        assert c["cycle_frames"] == cycle
        for slot, lag in stim["lags"].items():
            expected = "".join(stim["bits"][((n - lag) % cycle) // stim["frames_per_bit"]]
                               for n in range(c["frames"]))
            assert c["bright"][slot] == expected, (c["name"], slot)
