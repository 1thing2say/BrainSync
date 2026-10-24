"""tools/ui_shots.py: the screenshot harness is built for every state (no Chrome needed here)."""

from __future__ import annotations

import json
import re

import pytest

from tools import ui_shots


@pytest.mark.parametrize("name", list(ui_shots.STATES))
def test_every_state_builds_a_page_that_plays_its_messages(name):
    page = ui_shots.harness(name, "file:///repo/ui/")
    assert page.count('<base href="file:///repo/ui/">') == 1
    assert "window.WebSocket = ShotSocket" in page and page.index("ShotSocket") < page.index("app.js")
    played = json.loads(re.search(r"for \(const m of (\[.*?\])\) this\.onmessage", page).group(1))
    assert played[0]["type"] == "status" and any(m["type"] == "state" for m in played)
    assert page.rstrip().endswith("</html>") and "renderMic()" in page


def test_unknown_states_are_rejected(capsys):
    with pytest.raises(SystemExit):
        ui_shots.main(["no-such-state"])
    assert "--list" in capsys.readouterr().err


def test_list_names_every_state(capsys):
    assert ui_shots.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert all(name in out for name in ui_shots.STATES)
