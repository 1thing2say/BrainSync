"""tools/ios_v3_fixture.py: the PROTOCOL-v3 fixture the iPhone app's tests read (ios/BrainSyncKit)
is current, covers every message the phone handles, and every client message in it is one the
server accepts unchanged."""

from __future__ import annotations

import json

from brainsync.server import protocol as P
from brainsync.vep.live import BITS
from tools import ios_v3_fixture
from tools.ios_v3_fixture import FIXTURE


def committed() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_committed_fixture_is_the_generated_one():
    assert FIXTURE.read_text(encoding="utf-8") == ios_v3_fixture.render(), \
        "stale fixture: run python tools/ios_v3_fixture.py"


def test_check_passes_on_the_committed_fixture_and_fails_on_a_stale_one(tmp_path):
    assert ios_v3_fixture.main(["--check"]) == 0
    stale = tmp_path / "v3_messages.json"
    stale.write_text("{}\n")
    assert ios_v3_fixture.main(["--check", "--out", str(stale)]) == 1
    assert ios_v3_fixture.main(["--check", "--out", str(tmp_path / "missing.json")]) == 1
    assert ios_v3_fixture.main(["--out", str(stale)]) == 0
    assert stale.read_text() == ios_v3_fixture.render()


def test_every_server_message_the_phone_reads_is_there():
    msgs = committed()["server_to_client"]
    types = {m["type"] for m in msgs}
    assert types == {"welcome", "error", "phase", "layout", "selection", "decision", "train",
                     "trained", "status", "pong", "ping", "scan_request"}
    assert {m["phase"] for m in msgs if m["type"] == "phase"} == set(P.PHASES)
    code = next(m for m in msgs if m["type"] == "welcome" and m["paradigm"] == "code")
    assert code["code"]["bits"] == list(BITS) and code["max_slots"] == P.CODE_MAX_SLOTS
    slots = next(m for m in msgs if m["type"] == "layout" and m["stage"] == "slots")
    assert [(t["slot"], t["lag"], t["freq"]) for t in slots["targets"]] == \
        [("t0", 0, None), ("t1", 21, None), ("t2", 42, None)]
    assert {m["committed"] for m in msgs if m["type"] == "decision"} == {None, "t0"}
    assert {m["accuracy"] for m in msgs if m["type"] == "trained"} == {0.83, None}
    assert any("refresh_rates" not in m for m in msgs if m["type"] == "welcome")
    for m in msgs:
        assert json.loads(P.dumps(m)) == m


def test_every_client_message_is_accepted_by_the_server_unchanged():
    msgs = committed()["client_to_server"]
    assert {m["type"] for m in msgs} == {"hello", "onset", "cycle", "frames", "ping", "pause",
                                         "resume", "gaze", "bye"}
    for m in msgs:
        text = P.dumps(m)
        parsed = P.parse_frame(text)
        if parsed["type"] == "hello":
            hello = P.parse_hello(parsed, max_slots=P.CODE_MAX_SLOTS)
            assert hello.to_dict() == {k: v for k, v in m.items() if k != "token"}
            assert hello.token == m["token"]
        else:
            assert P.validate_client(parsed) == m
    controller = next(m for m in msgs if m["type"] == "hello" and m["role"] == "controller")
    assert controller["slots"] == ["t0", "t1", "t2"] and controller["accepted"] is True
    assert controller["client"]["instance"]
