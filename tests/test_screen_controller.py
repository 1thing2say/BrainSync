"""PointMode: scan_request -> scan -> detection -> pages -> click -> clicked -> next scan."""
from __future__ import annotations

import base64
import concurrent.futures
import json
import threading
import time
from types import SimpleNamespace

import pytest

from brainsync.screen.controller import PointMode
from brainsync.screen.detect import DetectError, Detection, Detector, Element
from brainsync.screen.selector import corner_rect, patch_rect
from brainsync.server import protocol

FREQS = (15.0, 17.0, 12.0, 20.0)
JPEG = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\0" * 32).decode()
T0 = 1000.0


class SyncExecutor(concurrent.futures.Executor):
    """Runs each call at submit, so a detection is done before tick."""

    def __init__(self):
        self.calls = 0
        self.down = False

    def submit(self, fn, /, *args, **kwargs):
        self.calls += 1
        fut = concurrent.futures.Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - mirror a real executor
            fut.set_exception(exc)
        return fut

    def shutdown(self, wait=True, *, cancel_futures=False):
        self.down = True


class ManualExecutor(concurrent.futures.Executor):
    """Keeps the calls; the test runs them with finish()."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, /, *args, **kwargs):
        fut = concurrent.futures.Future()
        self.jobs.append((fut, fn, args, kwargs))
        return fut

    def finish(self, i=-1):
        fut, fn, args, kwargs = self.jobs[i]
        if not fut.set_running_or_notify_cancel():
            return
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001
            fut.set_exception(exc)


class StubDetector:
    """Answers each detect() with the next item of ``answers`` (a Detection or an exception)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def detect(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, BaseException):
            raise a
        return a


def el(i, x, y, w=0.04, h=0.04, *, risky=False, p_button=0.9, p_next=None, label=None,
       hint_id=None):
    return Element(id=i, label=f"e{i}" if label is None else label, role="button", x=x, y=y,
                   w=w, h=h, hint_id=hint_id, p_button=p_button,
                   p_next=(1.0 - i * 0.05) if p_next is None else p_next, risky=risky,
                   enabled=True)


def detection(*elements, summary="A page", ms=1234):
    return Detection(elements=tuple(elements), summary=summary, ms=ms, input_tokens=100,
                     output_tokens=50, model="claude-haiku-5-5")


def scan(scan_id, *, vw=1600.0, vh=900.0, hints=None, url="https://example.com/",
         title="Example"):
    msg = {"type": "scan", "scan_id": scan_id,
           "image": {"media_type": "image/jpeg", "data": JPEG, "w": 1280, "h": 720},
           "viewport": {"w": vw, "h": vh, "dpr": 2}, "url": url, "title": title,
           "hints": hints or []}
    return protocol.validate_client(msg)                 # what the transport hands over


def clicked(click_id, ok=True):
    return protocol.validate_client({"type": "clicked", "click_id": click_id, "ok": ok,
                                     "method": "element.click", "detail": ""})


def kinds(msgs):
    """The messages' types, with the stage of a layout and the name of a phase."""
    out = []
    for m in msgs:
        protocol.dumps(m)                                # every message is JSON-safe
        if m["type"] == "layout":
            out.append(f"layout:{m['stage']}")
        elif m["type"] == "phase":
            out.append(f"phase:{m['phase']}")
        else:
            out.append(m["type"])
    return out


def find(msgs, type_):
    hits = [m for m in msgs if m["type"] == type_]
    assert len(hits) == 1, (type_, msgs)
    return hits[0]


THREE = (el(0, 0.1, 0.1), el(1, 0.4, 0.1), el(2, 0.1, 0.5))


def ready(det=None, **kwargs):
    """A PointMode that has shown its first pick layout."""
    stub = det if isinstance(det, StubDetector) else StubDetector(det or detection(*THREE))
    mode = PointMode(stub, FREQS, executor=SyncExecutor(), **kwargs)
    mode.start(T0)
    assert mode.on_message(scan(1), T0 + 1) == []
    out = mode.tick(T0 + 2)
    assert kinds(out) == ["scan_result", "layout:pick", "phase:run"]
    return mode, stub, out


# -- the main flow --------------------------------------------------------------------------

def test_start_requests_scan_with_idle_layout_and_run():
    mode = PointMode(StubDetector(detection()), FREQS, executor=SyncExecutor())
    out = mode.start(T0)
    assert kinds(out) == ["scan_request", "layout:idle", "phase:run"]
    assert out[0] == {"type": "scan_request", "scan_id": 1, "settle_ms": 800}
    assert out[1]["targets"] == []
    assert out[2]["flicker"] is True
    assert mode.hold == "run"


def test_settle_ms_is_passed_through():
    mode = PointMode(StubDetector(detection()), FREQS, executor=SyncExecutor(), settle_ms=250)
    assert mode.start(T0)[0]["settle_ms"] == 250


def test_scan_runs_detector_with_scan_fields_and_history():
    hints = [{"id": 0, "x": 10, "y": 20, "w": 30, "h": 40, "role": "button", "text": "Go",
              "tag": "button"}]
    stub = StubDetector(detection(*THREE))
    mode = PointMode(stub, FREQS, executor=SyncExecutor())
    mode.start(T0)
    mode.on_message(scan(1, hints=hints), T0 + 1)
    (args, kwargs), = stub.calls
    assert args == (JPEG, "image/jpeg", 1280, 720, 1600.0, 900.0)
    assert kwargs == {"hints": [dict(hints[0], x=10.0, y=20.0, w=30.0, h=40.0)],
                      "history": (), "url": "https://example.com/", "title": "Example"}


def test_detection_result_gives_scan_result_and_pick_layout():
    mode, _, out = ready()
    res = find(out, "scan_result")
    assert res["scan_id"] == 1 and res["ok"] is True and res["ms"] == 1234
    assert res["n_elements"] == 3 and res["n_candidates"] == 3
    assert res["summary"] == "A page"
    assert res["elements"] == [e.to_dict() for e in THREE]
    lay = find(out, "layout")
    assert [t["slot"] for t in lay["targets"]] == ["b1", "b2", "b3", "more"]
    assert [t["freq"] for t in lay["targets"]] == list(FREQS)
    aspect = 1600 / 900
    assert lay["targets"][0]["rects"] == [patch_rect(THREE[0], aspect).to_dict()]
    assert lay["targets"][3]["rects"] == [corner_rect(aspect).to_dict()]
    assert mode.layout_id == lay["layout_id"]


def test_layout_ids_increase():
    mode, _, out = ready()
    ids = [find(out, "layout")["layout_id"]]
    for step in (mode.on_selection("more", T0 + 3),):
        ids.append(find(step, "layout")["layout_id"])
    assert ids[0] > 1 and ids == sorted(set(ids))


def test_safe_pick_pauses_then_clicks_element_center_not_patch_center():
    # A tiny element in the top-left corner: its patch is moved inside the viewport, so the
    # patch center is far from the element center.
    corner_el = el(0, 0.0, 0.0, 0.01, 0.01, label="Logo")
    mode, _, _ = ready(detection(corner_el))
    out = mode.on_selection("b1", T0 + 3)
    assert kinds(out) == ["phase:pause", "click"]
    c = out[1]
    assert c["click_id"] == 1
    assert (c["x"], c["y"]) == pytest.approx((0.005, 0.005))
    patch = patch_rect(corner_el, 1600 / 900)
    assert (patch.cx, patch.cy) != pytest.approx((c["x"], c["y"]))
    assert c["element"] == {"id": 0, "label": "Logo", "role": "button", "hint_id": None,
                            "risky": False}
    assert out[0]["flicker"] is False and mode.hold == "pause"


def test_clicked_requests_next_scan_and_records_history():
    mode, stub, _ = ready()
    mode.on_selection("b2", T0 + 3)
    out = mode.on_message(clicked(1), T0 + 4)
    assert kinds(out) == ["scan_request", "layout:idle", "phase:run"]
    assert out[0]["scan_id"] == 2 and out[0]["settle_ms"] == 800
    assert list(mode.history) == ["e1"]
    assert mode.hold == "run"
    # The next detection carries the history.
    mode.on_message(scan(2), T0 + 5)
    assert stub.calls[-1][1]["history"] == ("e1",)


def test_clicked_with_wrong_id_is_ignored(capsys):
    mode, _, _ = ready()
    mode.on_selection("b1", T0 + 3)
    assert mode.on_message(clicked(99), T0 + 4) == []
    assert mode.on_message(clicked(0), T0 + 4) == []
    assert "ignored clicked" in capsys.readouterr().err
    assert mode.hold == "pause"
    assert kinds(mode.on_message(clicked(1), T0 + 4))[0] == "scan_request"
    # A second clicked for the same click is stale now.
    assert mode.on_message(clicked(1), T0 + 5) == []


def test_failed_click_is_not_added_to_history():
    mode, _, _ = ready()
    mode.on_selection("b1", T0 + 3)
    out = mode.on_message(clicked(1, ok=False), T0 + 4)
    assert kinds(out) == ["scan_request", "layout:idle", "phase:run"]
    assert list(mode.history) == []


def test_history_keeps_last_history_len_labels():
    elements = tuple(el(i, 0.1, 0.1, label=f"L{i}") for i in range(3))
    answers = [detection(e) for e in elements]
    stub = StubDetector(*answers)
    mode = PointMode(stub, FREQS, executor=SyncExecutor(), history_len=2)
    mode.start(T0)
    for i in range(3):
        mode.on_message(scan(i + 1), T0)
        mode.tick(T0)
        mode.on_selection("b1", T0)
        mode.on_message(clicked(i + 1), T0)
    assert list(mode.history) == ["L1", "L2"]


def test_zero_history_len_sends_no_history():
    mode, stub, _ = ready(history_len=0)
    mode.on_selection("b1", T0 + 3)
    mode.on_message(clicked(1), T0 + 4)
    mode.on_message(scan(2), T0 + 5)
    assert stub.calls[-1][1]["history"] == ()


# -- risky, more and rescan -----------------------------------------------------------------

def test_risky_pick_confirms_and_back_returns_to_same_page():
    risky = el(0, 0.3, 0.3, risky=True, label="Delete")
    mode, _, first = ready(detection(risky, el(1, 0.6, 0.3)))
    pick_layout = find(first, "layout")
    out = mode.on_selection("b1", T0 + 3)
    assert kinds(out) == ["layout:confirm"]
    t = out[0]["targets"]
    assert [(x["slot"], x["freq"]) for x in t] == [("confirm", 15.0), ("back", 17.0)]
    assert mode.hold == "run"
    out = mode.on_selection("back", T0 + 4)
    assert kinds(out) == ["layout:pick"]
    assert out[0]["targets"] == pick_layout["targets"]
    assert out[0]["layout_id"] > pick_layout["layout_id"]


def test_risky_pick_confirm_then_click():
    risky = el(0, 0.3, 0.3, risky=True, label="Pay")
    mode, _, _ = ready(detection(risky))
    mode.on_selection("b1", T0 + 3)
    out = mode.on_selection("confirm", T0 + 4)
    assert kinds(out) == ["phase:pause", "click"]
    assert out[1]["element"]["risky"] is True
    assert out[1]["x"] == pytest.approx(risky.cx)


def test_more_shows_next_page_and_last_page_more_rescans():
    els = tuple(el(i, 0.05 + 0.2 * (i % 4), 0.05 + 0.2 * (i // 4)) for i in range(5))
    mode, stub, _ = ready(detection(*els))
    out = mode.on_selection("more", T0 + 3)
    assert kinds(out) == ["layout:pick"]
    assert [t["slot"] for t in out[0]["targets"]] == ["b1", "b2", "more"]
    out = mode.on_selection("more", T0 + 4)
    assert kinds(out) == ["scan_request", "layout:idle"]
    assert out[0]["scan_id"] == 2
    assert mode.selector is None
    # The new scan is accepted and detected again.
    mode.on_message(scan(2), T0 + 5)
    assert len(stub.calls) == 2


def test_empty_detection_shows_only_more_which_rescans():
    mode, _, out = ready(detection())
    res = find(out, "scan_result")
    assert res["n_elements"] == 0 and res["n_candidates"] == 0
    assert [t["slot"] for t in find(out, "layout")["targets"]] == ["more"]
    assert kinds(mode.on_selection("more", T0 + 3)) == ["scan_request", "layout:idle"]


def test_unlikely_elements_are_reported_but_not_shown():
    low = el(0, 0.1, 0.1, p_button=0.2)
    mode, _, out = ready(detection(low, el(1, 0.5, 0.5)), min_p_button=0.5)
    res = find(out, "scan_result")
    assert res["n_elements"] == 2 and res["n_candidates"] == 1
    assert [t["slot"] for t in find(out, "layout")["targets"]] == ["b1", "more"]


# -- stale and ignored input ----------------------------------------------------------------

def test_stale_scan_ids_are_ignored(capsys):
    stub = StubDetector(detection(*THREE))
    mode = PointMode(stub, FREQS, executor=SyncExecutor())
    assert mode.on_message(scan(1), T0) == []            # before start: nothing requested
    mode.start(T0)
    assert mode.on_message(scan(0), T0) == []
    assert mode.on_message(scan(7), T0) == []
    assert stub.calls == []
    assert "ignored scan" in capsys.readouterr().err
    mode.on_message(scan(1), T0)
    assert len(stub.calls) == 1
    assert mode.on_message(scan(1), T0) == []            # a duplicate of an accepted scan
    assert len(stub.calls) == 1


def test_scan_from_before_a_rescan_is_ignored():
    mode, stub, _ = ready()
    mode.on_selection("more", T0 + 3)                   # one page: this rescans (scan 2)
    assert mode.on_message(scan(1), T0 + 4) == []
    assert len(stub.calls) == 1


def test_selection_without_a_pick_layout_is_ignored():
    mode = PointMode(StubDetector(detection(*THREE)), FREQS, executor=SyncExecutor())
    assert mode.on_selection("b1", T0) == []            # not started
    mode.start(T0)
    assert mode.on_selection("b1", T0) == []            # waiting for the scan


def test_selection_while_click_in_flight_is_ignored():
    mode, _, _ = ready()
    mode.on_selection("b1", T0 + 3)
    assert mode.on_selection("b2", T0 + 3.1) == []
    assert mode.on_selection("more", T0 + 3.1) == []


def test_unknown_slot_is_ignored_with_a_note(capsys):
    mode, _, _ = ready()
    assert mode.on_selection("confirm", T0 + 3) == []
    assert mode.on_selection("b9", T0 + 3) == []
    assert "ignored selection" in capsys.readouterr().err
    assert kinds(mode.on_selection("b1", T0 + 4)) == ["phase:pause", "click"]


def test_other_message_types_are_not_handled():
    mode, _, _ = ready()
    for msg in ({"type": "onset", "layout_id": 1, "t_client": 1.0}, {"type": "pause"},
                {"type": "frames", "refresh_hz": 60.0, "dropped": 0}, "nonsense", None):
        assert mode.on_message(msg, T0 + 3) == []


def test_scan_missing_fields_answers_protocol_error():
    stub = StubDetector(detection())
    mode = PointMode(stub, FREQS, executor=SyncExecutor())
    mode.start(T0)
    out = mode.on_message({"type": "scan", "scan_id": 1}, T0)
    assert kinds(out) == ["error"] and out[0]["code"] == "protocol"
    assert stub.calls == []
    # The request is still open: a proper scan is accepted.
    mode.on_message(scan(1), T0)
    assert len(stub.calls) == 1


# -- failures and timeouts ------------------------------------------------------------------

@pytest.mark.parametrize("exc,word", [
    (DetectError("the answer is not valid JSON", "invalid"), "invalid"),
    (DetectError("detection is off: no key", "disabled"), "disabled"),
    (RuntimeError("boom"), "RuntimeError"),
    (ValueError("image_w must be an int"), "ValueError"),
])
def test_failure_reports_and_retries_after_retry_s(exc, word):
    mode = PointMode(StubDetector(exc), FREQS, executor=SyncExecutor(), retry_s=3.0)
    mode.start(T0)
    mode.on_message(scan(1), T0 + 1)
    out = mode.tick(T0 + 1.5)
    assert kinds(out) == ["scan_result", "error"]
    res, err = out
    assert res["ok"] is False and res["scan_id"] == 1 and res["n_elements"] == 0
    assert res["ms"] == 500
    assert err["code"] == "scan_failed" and err["fatal"] is False and word in err["message"]
    assert mode.layout_id == 1                          # still the idle layout from start
    assert mode.tick(T0 + 4.49) == []
    out = mode.tick(T0 + 4.5)
    assert kinds(out) == ["scan_request", "layout:idle"]
    assert out[0]["scan_id"] == 2
    assert mode.tick(T0 + 10) == []                     # the retry is sent once


def test_retry_succeeds_after_failure():
    stub = StubDetector(DetectError("timeout", "error"), detection(*THREE))
    mode = PointMode(stub, FREQS, executor=SyncExecutor(), retry_s=1.0)
    mode.start(T0)
    mode.on_message(scan(1), T0)
    mode.tick(T0)
    mode.tick(T0 + 1)
    mode.on_message(scan(2), T0 + 2)
    out = mode.tick(T0 + 3)
    assert kinds(out) == ["scan_result", "layout:pick", "phase:run"]
    assert out[0]["scan_id"] == 2 and out[0]["ok"] is True


def test_non_detection_result_is_a_failure():
    mode = PointMode(StubDetector({"elements": []}), FREQS, executor=SyncExecutor())
    mode.start(T0)
    mode.on_message(scan(1), T0)
    out = mode.tick(T0)
    assert kinds(out) == ["scan_result", "error"]
    assert "not a Detection" in out[1]["message"]


def test_no_scan_within_reply_timeout_asks_again(capsys):
    mode = PointMode(StubDetector(detection()), FREQS, executor=SyncExecutor(),
                     reply_timeout_s=15.0)
    mode.start(T0)
    assert mode.tick(T0 + 14.9) == []
    out = mode.tick(T0 + 15)
    assert kinds(out) == ["scan_request", "layout:idle"]
    assert out[0]["scan_id"] == 2
    assert "no answer to scan_request 1" in capsys.readouterr().err
    assert mode.on_message(scan(1), T0 + 16) == []      # the old id is stale now


def test_lost_clicked_unblocks_after_reply_timeout():
    mode, _, _ = ready(reply_timeout_s=5.0)
    mode.on_selection("b1", T0 + 3)
    assert mode.tick(T0 + 7.9) == []
    out = mode.tick(T0 + 8)
    assert kinds(out) == ["scan_request", "layout:idle", "phase:run"]
    assert mode.hold == "run"
    assert mode.on_message(clicked(1), T0 + 9) == []    # too late: ignored


def test_no_timeout_while_detecting_or_picking():
    ex = ManualExecutor()
    mode = PointMode(StubDetector(detection(*THREE)), FREQS, executor=ex, reply_timeout_s=5.0)
    mode.start(T0)
    mode.on_message(scan(1), T0 + 1)
    assert mode.tick(T0 + 100) == []                    # detection still running
    ex.finish()
    assert kinds(mode.tick(T0 + 101))[0] == "scan_result"
    assert mode.tick(T0 + 1000) == []                   # the user may look as long as needed


# -- executor behavior ----------------------------------------------------------------------

def test_tick_waits_for_the_future():
    ex = ManualExecutor()
    mode = PointMode(StubDetector(detection(*THREE)), FREQS, executor=ex)
    mode.start(T0)
    mode.on_message(scan(1), T0)
    assert len(ex.jobs) == 1
    assert mode.tick(T0 + 0.5) == []
    ex.finish()
    assert kinds(mode.tick(T0 + 1)) == ["scan_result", "layout:pick", "phase:run"]
    assert mode.tick(T0 + 1.1) == []


def test_aspect_comes_from_the_scan_viewport():
    e = el(0, 0.5, 0.5, 0.01, 0.01)
    mode = PointMode(StubDetector(detection(e)), FREQS, executor=SyncExecutor(),
                     viewport=(1600, 900))
    mode.start(T0)
    mode.on_message(scan(1, vw=900.0, vh=1600.0), T0)  # a portrait window
    lay = find(mode.tick(T0), "layout")
    assert lay["targets"][0]["rects"] == [patch_rect(e, 900 / 1600).to_dict()]
    w = lay["targets"][0]["rects"][0]["w"]
    assert w == pytest.approx(0.10 * 1600 / 900, abs=1e-4)


def test_default_thread_pool_runs_detection_and_stop_shuts_it_down():
    started = threading.Event()

    class Slow(StubDetector):
        def detect(self, *a, **k):
            started.set()
            return super().detect(*a, **k)

    mode = PointMode(Slow(detection(*THREE)), FREQS)
    mode.start(T0)
    mode.on_message(scan(1), T0)
    assert started.wait(5)
    deadline = time.monotonic() + 5
    out = []
    while not out and time.monotonic() < deadline:
        out = mode.tick(T0 + 1)
        time.sleep(0.005)
    assert kinds(out) == ["scan_result", "layout:pick", "phase:run"]
    pool = mode._executor
    mode.stop()
    assert pool._shutdown


def test_stop_cancels_pending_detection_and_silences_the_mode():
    ex = ManualExecutor()
    mode = PointMode(StubDetector(detection(*THREE)), FREQS, executor=ex)
    mode.start(T0)
    mode.on_message(scan(1), T0)
    fut = ex.jobs[0][0]
    mode.stop()
    assert fut.cancelled()
    ex.finish()                                         # a cancelled job does not run
    assert mode.tick(T0 + 1) == []
    assert mode.start(T0 + 1) == []
    assert mode.on_message(scan(1), T0 + 1) == []
    assert mode.on_selection("b1", T0 + 1) == []
    mode.stop()                                         # idempotent


def test_stop_leaves_an_injected_executor_running():
    ex = SyncExecutor()
    mode = PointMode(StubDetector(detection()), FREQS, executor=ex)
    mode.start(T0)
    mode.stop()
    assert not ex.down


def test_shut_down_executor_counts_as_a_failed_detection():
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    pool.shutdown()
    mode = PointMode(StubDetector(detection()), FREQS, executor=pool)
    mode.start(T0)
    mode.on_message(scan(1), T0)
    out = mode.tick(T0)
    assert kinds(out) == ["scan_result", "error"]
    assert out[1]["code"] == "scan_failed"


# -- logging --------------------------------------------------------------------------------

def test_log_hook_gets_records_without_the_image():
    records = []
    mode, _, _ = ready(log=lambda kind, fields: records.append((kind, fields)))
    mode.on_selection("b1", T0 + 3)
    mode.on_message(clicked(1), T0 + 4)
    assert [k for k, _ in records] == ["scan", "pick", "click", "clicked"]
    scan_rec = records[0][1]
    assert scan_rec["n_candidates"] == 3 and scan_rec["input_tokens"] == 100
    assert JPEG not in json.dumps(records)


def test_failing_log_hook_does_not_break_the_flow(capsys):
    def bad(kind, fields):
        raise RuntimeError("disk full")

    mode, _, _ = ready(log=bad)
    assert kinds(mode.on_selection("b1", T0 + 3)) == ["phase:pause", "click"]
    assert "log hook failed" in capsys.readouterr().err


# -- end to end with the real Detector and an injected create --------------------------------

def test_real_detector_with_injected_create():
    answer = {"summary": "Login page", "elements": [
        {"label": "Sign in", "role": "button", "hint_id": 0, "x": 0, "y": 0, "w": 1, "h": 1,
         "p_button": 0.95, "p_next": 0.9, "risky": False, "enabled": True},
        {"label": "Help", "role": "link", "hint_id": -1, "x": 1000, "y": 600, "w": 80,
         "h": 30, "p_button": 0.8, "p_next": 0.1, "risky": False, "enabled": True}]}
    seen = []

    def create(**params):
        seen.append(params)
        return SimpleNamespace(stop_reason="end_turn", model="claude-haiku-5-5",
                               content=[SimpleNamespace(type="text", text=json.dumps(answer))],
                               usage=SimpleNamespace(input_tokens=1500, output_tokens=200))

    hints = [{"id": 0, "x": 160, "y": 90, "w": 160, "h": 45, "role": "button",
              "text": "Sign in", "tag": "button"}]
    mode = PointMode(Detector(create=create), FREQS, executor=SyncExecutor())
    mode.start(T0)
    mode.on_message(scan(1, hints=hints), T0)
    out = mode.tick(T0 + 1)
    assert kinds(out) == ["scan_result", "layout:pick", "phase:run"]
    assert len(seen) == 1 and seen[0]["model"] == "claude-haiku-5-5"
    res = out[0]
    assert res["summary"] == "Login page" and res["n_candidates"] == 2
    sign_in = res["elements"][0]
    assert (sign_in["x"], sign_in["y"], sign_in["w"], sign_in["h"]) == (0.1, 0.1, 0.1, 0.05)
    click = mode.on_selection("b1", T0 + 2)[1]
    assert (click["x"], click["y"]) == pytest.approx((0.15, 0.125))
    assert click["element"]["hint_id"] == 0


# -- constructor ----------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs,match", [
    ({"freqs": (15.0,)}, "at least 2"),
    ({"freqs": (15.0, 15.0)}, "distinct"),
    ({"viewport": (0, 9)}, "viewport width"),
    ({"viewport": (16,)}, "viewport"),
    ({"viewport": None}, "viewport"),
    ({"retry_s": 0}, "retry_s"),
    ({"retry_s": float("nan")}, "retry_s"),
    ({"reply_timeout_s": -1}, "reply_timeout_s"),
    ({"settle_ms": -1}, "settle_ms"),
    ({"settle_ms": 0.5}, "settle_ms"),
    ({"history_len": -1}, "history_len"),
    ({"min_p_button": 2.0}, "min_p_button"),
    ({"min_patch": 0}, "min_patch"),
    ({"gap": -1.0}, "gap"),
    ({"phases": (0.0,)}, "phases"),
])
def test_constructor_rejects_bad_arguments(kwargs, match):
    freqs = kwargs.pop("freqs", FREQS)
    with pytest.raises(ValueError, match=match):
        PointMode(StubDetector(detection()), freqs, executor=SyncExecutor(), **kwargs)


def test_constructor_rejects_object_without_detect():
    with pytest.raises(ValueError, match="detect"):
        PointMode(object(), FREQS)


def test_phases_reach_the_layout():
    mode, _, out = ready(phases=(0.0, 1.0, 2.0, 3.0))
    assert [t["phase"] for t in find(out, "layout")["targets"]] == [0.0, 1.0, 2.0, 3.0]
