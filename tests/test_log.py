"""Tests for app/log.py (JSONL event log)."""

from __future__ import annotations

import enum
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.log import EventLog, read_events

T0 = 1761268201.5


class FakeClock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Msg(BaseModel):
    type: str
    trial_id: int
    path: Path


@dataclass
class Point:
    x: int
    tags: set[str]


class Color(enum.Enum):
    RED = "red"


def test_file_naming_and_path(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "logs", session_id="abc123", clock=FakeClock())
    stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(T0))
    assert log.path == tmp_path / "logs" / f"{stamp}_abc123.jsonl"
    assert log.path.exists()
    log.close()


def test_default_session_id_is_short_hex(tmp_path: Path) -> None:
    with EventLog(tmp_path, clock=FakeClock()) as log:
        int(log.session_id, 16)
        assert 1 <= len(log.session_id) <= 8
        assert log.path.name.endswith(f"_{log.session_id}.jsonl")


def test_one_line_per_event_and_flush_before_close(tmp_path: Path) -> None:
    clock = FakeClock()
    log = EventLog(tmp_path, session_id="s", clock=clock)
    log.log("stim_start", trial_id=1)
    clock.t += 2.5
    rec = log.log("selection", trial_id=1, slot="s2")
    # Read while the file is still open: flush per event.
    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"t_unix": T0, "event": "stim_start", "trial_id": 1}
    assert json.loads(lines[1]) == rec == {
        "t_unix": T0 + 2.5, "event": "selection", "trial_id": 1, "slot": "s2"}
    log.close()
    assert read_events(log.path) == [json.loads(line) for line in lines]


def test_model_and_odd_value_serialization(tmp_path: Path) -> None:
    class Weird:
        def __str__(self) -> str:
            return "weird!"

    class Broken:
        def model_dump(self, **kwargs: object) -> dict:
            raise RuntimeError("boom")

        def __str__(self) -> str:
            return "broken"

    with EventLog(tmp_path, session_id="s", clock=FakeClock()) as log:
        log.log(
            "odd",
            msg=Msg(type="hello", trial_id=3, path=Path("a/b")),
            point=Point(x=1, tags={"b", "a"}),
            path=Path("x/y.txt"),
            items={3, 1, 2},
            tup=(1, "two"),
            color=Color.RED,
            weird=Weird(),
            broken=Broken(),
            nan=float("nan"),
            nested={1: [Weird()]},
            raw=b"\x00",
        )
        [ev] = read_events(log.path)
    assert ev["msg"] == {"type": "hello", "trial_id": 3, "path": str(Path("a/b"))}
    assert ev["point"] == {"x": 1, "tags": ["a", "b"]}
    assert ev["path"] == str(Path("x/y.txt"))
    assert ev["items"] == [1, 2, 3]
    assert ev["tup"] == [1, "two"]
    assert ev["color"] == "red"
    assert ev["weird"] == "weird!"
    assert ev["broken"] == "broken"
    assert ev["nan"] == "nan"
    assert ev["nested"] == {"1": ["weird!"]}
    assert isinstance(ev["raw"], str)


def test_envelope_fields_are_not_overwritten(tmp_path: Path) -> None:
    with EventLog(tmp_path, session_id="s", clock=FakeClock()) as log:
        rec = log.log("stim_onset", trial_id=7, t_unix=123.0, event="inner")
        assert rec["t_unix"] == T0
        assert rec["event"] == "stim_onset"
        assert rec["field_t_unix"] == 123.0
        assert rec["field_event"] == "inner"
        with pytest.raises(ValueError):
            log.log("x", t_unix=1.0, field_t_unix=2.0)
        assert read_events(log.path) == [rec]


def test_close_is_idempotent_and_log_after_close_raises(tmp_path: Path) -> None:
    log = EventLog(tmp_path, session_id="s", clock=FakeClock())
    log.log("a")
    log.close()
    log.close()
    assert log.closed
    with pytest.raises(ValueError):
        log.log("b")
    assert [e["event"] for e in read_events(log.path)] == ["a"]


def test_context_manager_closes(tmp_path: Path) -> None:
    with EventLog(tmp_path, session_id="s", clock=FakeClock()) as log:
        log.log("a")
    assert log.closed


def test_concurrent_writes_produce_valid_lines(tmp_path: Path) -> None:
    n_threads, per_thread = 8, 200
    log = EventLog(tmp_path, session_id="s", clock=FakeClock())

    def worker(i: int) -> None:
        for j in range(per_thread):
            log.log("tick", thread=i, n=j, pad="x" * 500)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.close()

    events = read_events(log.path)
    assert len(events) == n_threads * per_thread
    for i in range(n_threads):
        assert [e["n"] for e in events if e["thread"] == i] == list(range(per_thread))


def test_read_events_skips_truncated_last_line(tmp_path: Path) -> None:
    with EventLog(tmp_path, session_id="s", clock=FakeClock()) as log:
        log.log("a", n=1)
        log.log("b", n=2)
    # Simulate a crash mid-write: a partial line, cut inside a multi-byte character.
    partial = '{"t_unix": 1.0, "event": "c", "text": "café'.encode("utf-8")[:-1]
    with open(log.path, "ab") as f:
        f.write(partial)
    assert [e["event"] for e in read_events(log.path)] == ["a", "b"]


def test_read_events_raises_on_corrupt_middle_line(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"event": "a"}\nnot json\n{"event": "b"}\n', encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        read_events(path)
