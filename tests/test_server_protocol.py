"""Tests for the pure PROTOCOL-v3 module: parsers, client checks and message builders."""
from __future__ import annotations

import base64
import json
import math
import subprocess
import sys

import numpy as np
import pytest

from brainsync.server import protocol as p
from brainsync.server.protocol import Hello, ProtocolError, Rect, Target

JPEG = base64.b64encode(b"\xff\xd8\xff\xe0" + bytes(60)).decode()
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + bytes(60)).decode()


def controller_hello(**over) -> dict:
    msg = {"type": "hello", "protocol": 3, "token": "tok", "role": "controller",
           "takeover": False,
           "client": {"name": "brainpoint-extension", "kind": "extension", "version": "0.1.0"},
           "mode": "slots", "slots": ["yes", "no"], "refresh_hz": 59.98, "accepted": True,
           "viewport": {"w": 1512, "h": 857, "dpr": 2}}
    msg.update(over)
    return msg


def scan_msg(**over) -> dict:
    msg = {"type": "scan", "scan_id": 4,
           "image": {"media_type": "image/jpeg", "data": JPEG, "w": 1280, "h": 725},
           "viewport": {"w": 1512, "h": 857, "dpr": 2},
           "url": "https://example.com/", "title": "Example",
           "hints": [{"id": 0, "x": 12, "y": 40, "w": 80, "h": 32, "role": "button",
                      "text": "Sign in", "tag": "button"}]}
    msg.update(over)
    return msg


def bad(code: str, fn, *args, match: str | None = None):
    with pytest.raises(ProtocolError, match=match) as info:
        fn(*args)
    assert info.value.code == code
    return info.value


# -- constants ----------------------------------------------------------------------------------

def test_constants_match_the_contract():
    assert p.PROTOCOL == 3 and p.DEFAULT_PORT == 8765 and p.MAX_FRAME == 4 * 1024 * 1024
    assert (p.CLOSE_REPLACED, p.CLOSE_BAD_TOKEN, p.CLOSE_BUSY, p.CLOSE_HELLO_FIRST,
            p.CLOSE_HELLO_TIMEOUT) == (4000, 4001, 4002, 4003, 4008)
    assert p.PHASES == ("wait", "calibrate", "train", "run", "pause", "stop")
    assert p.FLICKER_PHASES == ("train", "run")
    assert p.STAGES == ("slots", "pick", "confirm", "idle", "train")
    assert p.PARADIGMS == ("code", "freq") and p.CODE_MAX_SLOTS == 3
    assert p.KINDS == ("slot", "button", "more", "confirm", "back")
    assert p.BOARD_STATES == ("unknown", "connecting", "streaming", "no-data", "error")
    assert p.ERROR_CODES == {"bad_token", "busy", "bad_hello", "refresh", "too_many_slots",
                             "protocol", "not_allowed", "detect_unavailable", "scan_failed",
                             "internal"}
    assert p.CLIENT_TYPES == {"hello", "onset", "cycle", "frames", "ping", "pause", "resume",
                              "bye", "gaze", "scan", "clicked", "rescan"}
    assert p.VIEWER_TYPES == {"ping", "bye"} and p.VIEWER_TYPES < p.CLIENT_TYPES
    assert (p.LATENCY_S, p.ONSET_TIMEOUT_S, p.COOLDOWN_S, p.SETTLE_MS) == (0.14, 2.0, 1.5, 800)
    assert p.PING == {"type": "ping"}


def test_importing_the_packages_stays_light():
    code = ("import sys; import brainsync.server.protocol, brainsync.screen; "
            "bad = [m for m in ('websockets', 'numpy', 'anthropic', 'scipy') if m in sys.modules];"
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True).stdout.strip()
    assert out == "[]"


def test_protocol_error_carries_a_known_code():
    e = ProtocolError("busy", "someone else controls the headset")
    assert isinstance(e, ValueError) and e.code == "busy"
    assert str(e) == e.message == "someone else controls the headset"
    with pytest.raises(ValueError, match="error code"):
        ProtocolError("nope", "x")


# -- parse_frame --------------------------------------------------------------------------------

def test_parse_frame_returns_the_object():
    assert p.parse_frame('{"type": "ping", "ping_id": 1, "t_client": 2.5}') == \
        {"type": "ping", "ping_id": 1, "t_client": 2.5}


@pytest.mark.parametrize("text", [
    "not json", "", "[1, 2]", '"hello"', "3", "null", '{"no_type": 1}', '{"type": 3}',
    '{"type": null}', '{"type": "x", "v": NaN}', '{"type": "x", "v": Infinity}',
    '{"type": "x", "v": -Infinity}', "[" * 100_000 + "]" * 100_000])
def test_parse_frame_rejects_malformed_text(text):
    bad("protocol", p.parse_frame, text)


@pytest.mark.parametrize("frame", [b'{"type": "ping"}', bytearray(b"x"), memoryview(b"x")])
def test_parse_frame_rejects_binary(frame):
    bad("protocol", p.parse_frame, frame, match="binary")


def test_parse_frame_rejects_non_text():
    bad("protocol", p.parse_frame, 12)


def test_parse_frame_error_excerpt_is_short():
    e = bad("protocol", p.parse_frame, "x" * 10_000)
    assert len(str(e)) < 200


# -- parse_hello --------------------------------------------------------------------------------

def test_parse_hello_controller_slots():
    h = p.parse_hello(controller_hello(), max_slots=4)
    assert h == Hello(role="controller", token="tok", mode="slots", slots=("yes", "no"),
                      refresh_hz=59.98, accepted=True, takeover=False,
                      client={"name": "brainpoint-extension", "kind": "extension",
                              "version": "0.1.0"},
                      viewport={"w": 1512.0, "h": 857.0, "dpr": 2.0})


def test_parse_hello_point_mode_and_defaults():
    h = p.parse_hello(controller_hello(mode="point", slots=None, takeover=True, viewport=None,
                                       client=None, token=None), 4)
    assert (h.mode, h.slots, h.takeover, h.viewport, h.client, h.token) == \
        ("point", None, True, None, {}, None)
    msg = controller_hello(mode="point")
    del msg["slots"], msg["takeover"], msg["client"], msg["viewport"], msg["token"]
    h = p.parse_hello(msg, 4)
    assert (h.slots, h.takeover, h.client, h.viewport, h.token) == (None, False, {}, None, None)


def test_parse_hello_viewer_may_leave_out_mode_slots_refresh_accepted():
    h = p.parse_hello({"type": "hello", "protocol": 3, "token": "t", "role": "viewer"}, 4)
    assert (h.role, h.mode, h.slots, h.refresh_hz, h.accepted) == \
        ("viewer", None, None, None, False)
    h = p.parse_hello({"type": "hello", "protocol": 3, "role": "viewer", "mode": "slots",
                       "slots": ["a"], "refresh_hz": 120, "accepted": True}, 4)
    assert (h.mode, h.slots, h.refresh_hz, h.accepted) == ("slots", ("a",), 120.0, True)
    h = p.parse_hello({"type": "hello", "protocol": 3, "role": "viewer", "mode": "slots"}, 4)
    assert h.slots is None


def test_parse_hello_cleans_client_info():
    h = p.parse_hello(controller_hello(client={"name": "n" * 100, "kind": 3, "extra": "x"}), 4)
    assert h.client == {"name": "n" * 64}


def test_parse_hello_viewport_dpr_defaults_to_one():
    h = p.parse_hello(controller_hello(viewport={"w": 800, "h": 600}), 4)
    assert h.viewport == {"w": 800.0, "h": 600.0, "dpr": 1.0}


def test_parse_hello_slots_at_the_limit():
    names = [f"s{i}" for i in range(4)]
    assert p.parse_hello(controller_hello(slots=names), 4).slots == tuple(names)
    assert p.parse_hello(controller_hello(slots=["x" * 32]), 1).slots == ("x" * 32,)


def test_parse_hello_too_many_slots():
    bad("too_many_slots", p.parse_hello, controller_hello(slots=list("abcde")), 4,
        match="at most 4")


@pytest.mark.parametrize("over", [
    {"type": "ping"},
    {"protocol": 2}, {"protocol": "3"}, {"protocol": True}, {"protocol": None},
    {"role": "admin"}, {"role": None},
    {"token": 123}, {"takeover": "yes"}, {"takeover": 1}, {"client": "x"},
    {"mode": None}, {"mode": "draw"},
    {"slots": None}, {"slots": []}, {"slots": "yes"}, {"slots": ["yes", "yes"]},
    {"slots": ["", "no"]}, {"slots": ["x" * 33]}, {"slots": [1, 2]},
    {"mode": "point", "slots": ["yes"]},
    {"refresh_hz": None}, {"refresh_hz": "60"}, {"refresh_hz": True}, {"refresh_hz": 0},
    {"refresh_hz": -60}, {"refresh_hz": float("nan")}, {"refresh_hz": float("inf")},
    {"refresh_hz": 10 ** 400},
    {"accepted": False}, {"accepted": None}, {"accepted": 1}, {"accepted": "true"},
    {"viewport": {"w": 0, "h": 600}}, {"viewport": {"h": 600}}, {"viewport": [1, 2]},
    {"viewport": {"w": 800, "h": 600, "dpr": True}}, {"viewport": {"w": 800, "h": 600, "dpr": 0}},
])
def test_parse_hello_rejects(over):
    bad("bad_hello", p.parse_hello, controller_hello(**over), 4)


def test_parse_hello_controller_missing_fields():
    for key in ("mode", "refresh_hz", "accepted", "role", "protocol"):
        msg = controller_hello()
        del msg[key]
        bad("bad_hello", p.parse_hello, msg, 4)
    msg = controller_hello()
    del msg["slots"]
    bad("bad_hello", p.parse_hello, msg, 4)


def test_parse_hello_viewer_rejects_bad_optional_fields():
    base = {"type": "hello", "protocol": 3, "role": "viewer"}
    for over in ({"accepted": "y"}, {"refresh_hz": "60"}, {"mode": "x"},
                 {"mode": "slots", "slots": []}, {"slots": ["a"]}):
        bad("bad_hello", p.parse_hello, {**base, **over}, 4)


def test_parse_hello_rejects_non_mapping():
    bad("bad_hello", p.parse_hello, ["hello"], 4)


def test_parse_hello_errors_never_repeat_the_token():
    secret = "s3cr3t-token-value"
    for over in ({"accepted": False}, {"protocol": 9}, {"slots": []}, {"role": "x"}):
        e = bad("bad_hello", p.parse_hello, controller_hello(token=secret, **over), 4)
        assert secret not in str(e)


@pytest.mark.parametrize("max_slots", [0, -1, True, 2.0, None])
def test_parse_hello_needs_a_positive_max_slots(max_slots):
    with pytest.raises(ValueError, match="max_slots") as info:
        p.parse_hello(controller_hello(), max_slots)
    assert not isinstance(info.value, ProtocolError)


def test_hello_to_dict_drops_the_token():
    h = p.parse_hello(controller_hello(), 4)
    d = h.to_dict()
    assert "token" not in d and "tok" not in json.dumps(d)
    assert d["slots"] == ["yes", "no"] and d["role"] == "controller" and d["protocol"] == 3
    again = p.parse_hello({**d, "token": "tok"}, 4)
    assert again == h


# -- validate_client: simple types ---------------------------------------------------------------

def test_onset_ok():
    assert p.validate_client({"type": "onset", "layout_id": 7, "t_client": 1792809201.512,
                              "extra": 1}) == \
        {"type": "onset", "layout_id": 7, "t_client": 1792809201.512}
    assert p.validate_client({"type": "onset", "layout_id": 0, "t_client": 5})["t_client"] == 5.0


@pytest.mark.parametrize("msg", [
    {"type": "onset", "t_client": 1.0}, {"type": "onset", "layout_id": 7},
    {"type": "onset", "layout_id": True, "t_client": 1.0},
    {"type": "onset", "layout_id": 7.0, "t_client": 1.0},
    {"type": "onset", "layout_id": "7", "t_client": 1.0},
    {"type": "onset", "layout_id": -1, "t_client": 1.0},
    {"type": "onset", "layout_id": 7, "t_client": float("nan")},
    {"type": "onset", "layout_id": 7, "t_client": float("inf")},
    {"type": "onset", "layout_id": 7, "t_client": False},
    {"type": "onset", "layout_id": 7, "t_client": None},
    {"type": "onset", "layout_id": 7, "t_client": 10 ** 400},
])
def test_onset_rejects(msg):
    bad("protocol", p.validate_client, msg)


def test_frames_ok():
    assert p.validate_client({"type": "frames", "refresh_hz": 59.98, "dropped": 0}) == \
        {"type": "frames", "refresh_hz": 59.98, "dropped": 0}


@pytest.mark.parametrize("over", [
    {"refresh_hz": 0}, {"refresh_hz": -1}, {"refresh_hz": "60"}, {"refresh_hz": True},
    {"refresh_hz": float("nan")}, {"dropped": -1}, {"dropped": 1.5}, {"dropped": True},
    {"dropped": None}])
def test_frames_rejects(over):
    bad("protocol", p.validate_client, {"type": "frames", "refresh_hz": 60, "dropped": 0, **over})
    msg = {"type": "frames", "refresh_hz": 60, "dropped": 0}
    for key in msg.keys() - {"type"}:
        bad("protocol", p.validate_client, {k: v for k, v in msg.items() if k != key})


def test_ping_ok():
    assert p.validate_client({"type": "ping", "ping_id": 3, "t_client": 1792809201.0}) == \
        {"type": "ping", "ping_id": 3, "t_client": 1792809201.0}


@pytest.mark.parametrize("msg", [
    {"type": "ping"}, {"type": "ping", "ping_id": 3}, {"type": "ping", "t_client": 1.0},
    {"type": "ping", "ping_id": False, "t_client": 1.0},
    {"type": "ping", "ping_id": 1.5, "t_client": 1.0},
    {"type": "ping", "ping_id": 3, "t_client": "now"},
    {"type": "ping", "ping_id": 3, "t_client": float("-inf")},
])
def test_ping_rejects(msg):
    bad("protocol", p.validate_client, msg)


@pytest.mark.parametrize("kind", ["pause", "resume", "bye"])
def test_bare_types(kind):
    assert p.validate_client({"type": kind, "junk": [1]}) == {"type": kind}


def test_gaze():
    assert p.validate_client({"type": "gaze", "slot": "b1"}) == {"type": "gaze", "slot": "b1"}
    assert p.validate_client({"type": "gaze", "slot": None}) == {"type": "gaze", "slot": None}
    for slot in (3, True, "", "x" * 33, ["b1"]):
        bad("protocol", p.validate_client, {"type": "gaze", "slot": slot})
    bad("protocol", p.validate_client, {"type": "gaze"}, match="slot")


def test_clicked():
    assert p.validate_client({"type": "clicked", "click_id": 2, "ok": True,
                              "method": "element.click", "detail": ""}) == \
        {"type": "clicked", "click_id": 2, "ok": True, "method": "element.click", "detail": ""}
    out = p.validate_client({"type": "clicked", "click_id": 0, "ok": False,
                             "detail": "d" * 900})
    assert out["method"] == "" and len(out["detail"]) == 500
    assert p.validate_client({"type": "clicked", "click_id": 1, "ok": True,
                              "method": None})["method"] == ""


@pytest.mark.parametrize("over", [
    {"click_id": True}, {"click_id": "2"}, {"click_id": 2.0}, {"click_id": -1},
    {"ok": 1}, {"ok": "true"}, {"ok": None}, {"method": 3}, {"detail": {"a": 1}}])
def test_clicked_rejects(over):
    bad("protocol", p.validate_client, {"type": "clicked", "click_id": 2, "ok": True, **over})


def test_clicked_requires_fields():
    bad("protocol", p.validate_client, {"type": "clicked", "ok": True})
    bad("protocol", p.validate_client, {"type": "clicked", "click_id": 1})


def test_validate_client_rejects_unknown_and_hello():
    bad("protocol", p.validate_client, {"type": "selection"}, match="unknown message type")
    bad("protocol", p.validate_client, {"type": "hello"}, match="first message")
    bad("protocol", p.validate_client, {"no": "type"})
    bad("protocol", p.validate_client, "ping")


def test_validate_client_returns_a_copy():
    msg = scan_msg()
    out = p.validate_client(msg)
    out["hints"][0]["text"] = "changed"
    out["viewport"]["w"] = 1
    assert msg["hints"][0]["text"] == "Sign in" and msg["viewport"]["w"] == 1512


# -- validate_client: scan -----------------------------------------------------------------------

def test_scan_ok():
    out = p.validate_client({**scan_msg(), "extra": True})
    assert out == {"type": "scan", "scan_id": 4,
                   "image": {"media_type": "image/jpeg", "data": JPEG, "w": 1280, "h": 725},
                   "viewport": {"w": 1512.0, "h": 857.0, "dpr": 2.0},
                   "url": "https://example.com/", "title": "Example",
                   "hints": [{"id": 0, "x": 12.0, "y": 40.0, "w": 80.0, "h": 32.0,
                              "role": "button", "text": "Sign in", "tag": "button"}]}


def test_scan_png_and_optional_fields():
    msg = scan_msg(image={"media_type": "image/png", "data": PNG, "w": 10, "h": 20})
    del msg["hints"], msg["url"], msg["title"]
    out = p.validate_client(msg)
    assert out["hints"] == [] and out["url"] == "" and out["title"] == ""
    assert out["image"]["media_type"] == "image/png"
    assert p.validate_client(scan_msg(hints=None))["hints"] == []


def test_scan_hint_optional_text_fields_and_negative_position():
    out = p.validate_client(scan_msg(hints=[{"id": 5, "x": -10, "y": -3.5, "w": 0, "h": 0}]))
    assert out["hints"] == [{"id": 5, "x": -10.0, "y": -3.5, "w": 0.0, "h": 0.0, "role": "",
                             "text": "", "tag": ""}]


def test_scan_clips_hints_and_text():
    hints = [{"id": i, "x": 0, "y": 0, "w": 1, "h": 1, "text": "t" * 500, "role": "r" * 99,
              "tag": "g" * 99} for i in range(250)]
    out = p.validate_client(scan_msg(hints=hints, url="u" * 5000, title="t" * 999))
    assert len(out["hints"]) == p.MAX_HINTS == 200
    assert [h["id"] for h in out["hints"]] == list(range(200))
    assert all(len(h["text"]) == 120 and len(h["role"]) == 40 and len(h["tag"]) == 40
               for h in out["hints"])
    assert len(out["url"]) == 2048 and len(out["title"]) == 200


def test_scan_hints_beyond_200_are_not_checked():
    hints = [{"id": i, "x": 0, "y": 0, "w": 1, "h": 1} for i in range(200)] + ["junk"]
    assert len(p.validate_client(scan_msg(hints=hints))["hints"]) == 200


def hint(**over) -> dict:
    return {"id": 0, "x": 12, "y": 40, "w": 80, "h": 32, **over}


@pytest.mark.parametrize("hints", [
    "nope", {"id": 0}, [1], [None],
    [hint(id=None)], [hint(id=True)], [hint(id=1.5)], [hint(id="0")], [hint(id=-1)],
    [hint(x="12")], [hint(y=None)], [hint(w=-1)], [hint(h=-0.5)], [hint(x=float("nan"))],
    [hint(w=float("inf"))], [hint(x=True)],
    [hint(text=5)], [hint(role=["button"])], [hint(tag=1)],
    [hint(), hint()],
    [{"x": 0, "y": 0, "w": 1, "h": 1}], [{"id": 0, "y": 0, "w": 1, "h": 1}],
    [{"id": 0, "x": 0, "w": 1, "h": 1}], [{"id": 0, "x": 0, "y": 0, "h": 1}],
    [{"id": 0, "x": 0, "y": 0, "w": 1}],
])
def test_scan_rejects_bad_hints(hints):
    bad("protocol", p.validate_client, scan_msg(hints=hints))


def image(**over) -> dict:
    return {"media_type": "image/jpeg", "data": JPEG, "w": 1280, "h": 725, **over}


# -- validate_client: ax scans (v3.1) -----------------------------------------------------------

def ax_scan(**over) -> dict:
    msg = {"type": "scan", "scan_id": 7, "source": "ax",
           "viewport": {"w": 900, "h": 600, "dpr": 2},
           "app": {"bundle_id": "com.apple.Notes", "name": "Notes", "pid": 412},
           "window": {"title": "All iCloud", "frame": {"x": 0, "y": 0, "w": 900, "h": 600}},
           "hints": [{"id": 0, "x": 10, "y": 20, "w": 30, "h": 20, "role": "AXButton",
                      "text": "New Note", "tag": "", "actions": ["AXPress"],
                      "enabled": True}]}
    msg.update(over)
    return msg


def test_ax_scan_ok_without_image():
    out = p.validate_client({**ax_scan(), "extra": 1})
    assert out == {"type": "scan", "scan_id": 7, "source": "ax", "image": None,
                   "viewport": {"w": 900.0, "h": 600.0, "dpr": 2.0}, "url": "", "title": "",
                   "hints": [{"id": 0, "x": 10.0, "y": 20.0, "w": 30.0, "h": 20.0,
                              "role": "AXButton", "text": "New Note", "tag": "",
                              "actions": ["AXPress"], "enabled": True}],
                   "app": {"bundle_id": "com.apple.Notes", "name": "Notes", "pid": 412},
                   "window": {"title": "All iCloud"}}


def test_ax_scan_with_an_image_and_no_hints():
    out = p.validate_client(ax_scan(image=image(), hints=[], app={"bundle_id": "", "name": "",
                                                                  "pid": 0}))
    assert out["image"]["media_type"] == "image/jpeg" and out["hints"] == []
    assert out["app"] == {"bundle_id": "", "name": "", "pid": 0}


def test_ax_hint_actions_are_cut_to_8_of_40_characters_and_fields_are_optional():
    hints = [{"id": 0, "x": 0, "y": 0, "w": 1, "h": 1, "actions": ["A" * 50] * 12},
             {"id": 1, "x": 0, "y": 0, "w": 1, "h": 1}]
    out = p.validate_client(ax_scan(hints=hints))
    assert out["hints"][0]["actions"] == ["A" * 40] * 8
    assert "actions" not in out["hints"][1] and "enabled" not in out["hints"][1]


def test_screenshot_scans_are_unchanged_and_drop_ax_fields():
    plain = p.validate_client(scan_msg())
    assert "source" not in plain and "app" not in plain
    explicit = p.validate_client(scan_msg(source="screenshot", app={"x": 1},
                                          hints=[hint(actions=["AXPress"], enabled=False)]))
    assert explicit == p.validate_client(scan_msg(hints=[hint()]))
    bad("protocol", p.validate_client, {**scan_msg(source="screenshot"), "image": None})


@pytest.mark.parametrize("over, words", [
    ({"source": "dom"}, "scan.source"),
    ({"source": None}, "scan.source"),
    ({"app": None}, "app"),
    ({"app": "Notes"}, "scan.app must be an object"),
    ({"app": {"name": "Notes", "pid": 1}}, "bundle_id"),
    ({"app": {"bundle_id": "x", "pid": 1}}, "name"),
    ({"app": {"bundle_id": "x", "name": "y"}}, "pid"),
    ({"app": {"bundle_id": "x", "name": "y", "pid": -1}}, "pid"),
    ({"app": {"bundle_id": "x", "name": "y", "pid": 1.5}}, "pid"),
    ({"app": {"bundle_id": 5, "name": "y", "pid": 1}}, "bundle_id"),
    ({"window": None}, "window"),
    ({"window": {}}, "title"),
    ({"window": {"title": 3}}, "title"),
    ({"viewport": None}, "viewport"),
    ({"image": image(media_type="image/gif")}, "media_type"),
    ({"hints": [hint(actions="AXPress")]}, "actions must be a list"),
    ({"hints": [hint(actions=["AXPress", 3])]}, r"actions\[1\]"),
    ({"hints": [hint(enabled="yes")]}, "enabled"),
])
def test_ax_scan_rejects_bad_fields(over, words):
    msg = ax_scan(**over)
    if over.get("app", 0) is None:
        del msg["app"]
    if over.get("window", 0) is None:
        del msg["window"]
    if over.get("viewport", 0) is None:
        del msg["viewport"]
    bad("protocol", p.validate_client, msg, match=words)


@pytest.mark.parametrize("img", [
    None, "data", [1],
    image(media_type="image/gif"), image(media_type=None), image(media_type="IMAGE/JPEG"),
    image(data=""), image(data=None), image(data=b"abc"), image(data=123),
    image(data="data:image/jpeg;base64," + JPEG),
    image(data=JPEG + "\n"), image(data=JPEG[:-1]), image(data="ab!d"), image(data="====")
    , image(data=JPEG.replace("/", "_")),
    image(data=PNG), image(media_type="image/png", data=JPEG),
    image(w=0), image(h=-5), image(w=1280.0), image(w=True), image(h="725"), image(w=None),
    image(w=9000),
])
def test_scan_rejects_bad_images(img):
    bad("protocol", p.validate_client, scan_msg(image=img))


def test_scan_rejects_missing_image_fields():
    for key in ("media_type", "data", "w", "h"):
        img = image()
        del img[key]
        bad("protocol", p.validate_client, scan_msg(image=img), match=key)


def test_scan_rejects_bad_ids_viewports_and_text():
    for over in ({"scan_id": None}, {"scan_id": True}, {"scan_id": "4"}, {"scan_id": -1},
                 {"scan_id": 4.0}, {"viewport": None}, {"viewport": {"w": 1, "h": 0}},
                 {"viewport": {"w": float("nan"), "h": 1}}, {"viewport": "1512x857"},
                 {"url": 3}, {"title": ["x"]}):
        bad("protocol", p.validate_client, scan_msg(**over))
    for key in ("scan_id", "image", "viewport"):
        msg = scan_msg()
        del msg[key]
        bad("protocol", p.validate_client, msg, match=key)


def test_scan_error_does_not_echo_the_image():
    e = bad("protocol", p.validate_client, scan_msg(image=image(data="!" * 100_000)))
    assert len(str(e)) < 200


def test_scan_short_base64_image_is_checked_by_prefix():
    tiny = base64.b64encode(b"\xff\xd8").decode()          # 2 bytes: a prefix of the magic
    assert p.validate_client(scan_msg(image=image(data=tiny)))["image"]["data"] == tiny


def test_scan_through_parse_frame():
    msg = p.validate_client(p.parse_frame(json.dumps(scan_msg())))
    assert msg["scan_id"] == 4 and msg["hints"][0]["id"] == 0


# -- check_refresh ------------------------------------------------------------------------------

@pytest.mark.parametrize("hz", [60.0, 59.98, 59.0, 61.0, 60])
def test_check_refresh_ok(hz):
    assert p.check_refresh(hz, 60.0, 1.0) is None


@pytest.mark.parametrize("hz", [58.99, 61.01, 75, 120, 144.0])
def test_check_refresh_out_of_tolerance(hz):
    text = p.check_refresh(hz, 60.0, 1.0)
    assert "60" in text and "--ignore-refresh" in text and f"{float(hz):.2f}" in text


@pytest.mark.parametrize("hz", [None, "60", True, float("nan"), float("inf"), 0, -60])
def test_check_refresh_unusable(hz):
    assert "refresh_hz" in p.check_refresh(hz, 60.0, 1.0)


RATES = (60.0, 90.0, 120.0, 144.0)


def test_default_refresh_rates():
    assert p.REFRESH_RATES == RATES and p.REFRESH_TOLERANCE_HZ == 1.0


@pytest.mark.parametrize("hz", [60, 59.2, 90.0, 89.5, 120.0, 119.98, 120.9, 144, 143.1])
def test_check_refresh_accepts_each_rate_of_a_set(hz):
    assert p.check_refresh(hz, RATES, 1.0) is None
    assert p.check_refresh(hz, list(RATES), 1.0) is None


@pytest.mark.parametrize("hz", [50.0, 58.9, 75, 100.0, 121.5, 165, 240])
def test_check_refresh_out_of_the_set_lists_the_rates(hz):
    text = p.check_refresh(hz, RATES, 1.0)
    assert f"{float(hz):.2f} Hz" in text
    assert "one of 60, 90, 120 or 144 Hz (± 1 Hz)" in text and "--ignore-refresh" in text


def test_check_refresh_one_rate_keeps_the_old_sentence():
    assert p.check_refresh(120, 60.0, 1.0) == p.check_refresh(120, [60.0], 1.0) == (
        "The display refreshes at 120.00 Hz, but the flicker needs 60 ± 1 Hz. Set the display "
        "to 60 Hz and reconnect, or start the server with --ignore-refresh.")
    assert p.check_refresh(90.4, (60.0, 90.0), 0.5) is None
    assert "one of 60 or 90 Hz (± 0.5 Hz)" in p.check_refresh(91, (60.0, 90.0), 0.5)
    assert "refresh_hz" in p.check_refresh(None, RATES, 1.0)


# -- Rect and Target ----------------------------------------------------------------------------

def test_rect_basics():
    r = Rect(0.1, 0.2, 0.3, 0.4)
    assert r.cx == pytest.approx(0.25) and r.cy == pytest.approx(0.4)
    assert Rect(0.123456, 0.0, 0.5, 1.0).to_dict() == {"x": 0.1235, "y": 0.0, "w": 0.5, "h": 1.0}
    assert isinstance(Rect(np.float64(0.1), 0, 1 - 0.1, 1).x, float)
    assert Rect(0, 0, 1, 1).to_dict() == {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
    Rect(0.7, 0.7, 0.3, 0.3)                 # ends exactly at the edge (float slack)
    Rect(0.0, 0.0, 0.0, 0.0)                 # zero size is allowed


def test_rect_is_frozen_and_hashable():
    r = Rect(0.1, 0.1, 0.1, 0.1)
    with pytest.raises(AttributeError):
        r.x = 0.5
    assert {r, Rect(0.1, 0.1, 0.1, 0.1)} == {r}


@pytest.mark.parametrize("args", [
    (-0.01, 0, 0.1, 0.1), (0, 1.01, 0, 0), (0, 0, -0.1, 0.1), (0, 0, 0.1, 1.5),
    (0.5, 0, 0.6, 0.1), (0, 0.95, 0.1, 0.1), (float("nan"), 0, 0, 0), (0, 0, float("inf"), 0),
    (True, 0, 0.1, 0.1), ("0", 0, 0.1, 0.1), (None, 0, 0.1, 0.1)])
def test_rect_rejects(args):
    with pytest.raises(ValueError):
        Rect(*args)


def test_rect_overlaps():
    a = Rect(0.1, 0.1, 0.2, 0.2)
    assert a.overlaps(a)
    assert a.overlaps(Rect(0.2, 0.2, 0.2, 0.2)) and Rect(0.2, 0.2, 0.2, 0.2).overlaps(a)
    assert a.overlaps(Rect(0.15, 0.15, 0.01, 0.01))             # inside
    assert not a.overlaps(Rect(0.3, 0.1, 0.2, 0.2))             # touching edges
    assert not a.overlaps(Rect(0.1, 0.3, 0.2, 0.2))
    assert not a.overlaps(Rect(0.5, 0.5, 0.1, 0.1))
    near = Rect(0.31, 0.1, 0.2, 0.2)                            # 0.01 apart
    assert not a.overlaps(near) and a.overlaps(near, gap=0.02) and near.overlaps(a, gap=0.02)
    far = Rect(0.325, 0.1, 0.2, 0.2)                            # 0.025 apart
    assert not a.overlaps(far, gap=0.02)
    assert not a.overlaps(Rect(0.4, 0.4, 0.1, 0.1), gap=0.02)  # diagonal, apart on both axes
    with pytest.raises(ValueError, match="gap"):
        a.overlaps(near, gap=-0.1)


def test_target_to_dict_and_coercion():
    t = Target("b1", np.float64(15), 0, "Sign in", "button", [Rect(0.012, 0.04, 0.09, 0.12)])
    assert isinstance(t.rects, tuple) and isinstance(t.freq, float) and t.phase == 0.0
    assert t.to_dict() == {"slot": "b1", "freq": 15.0, "phase": 0.0, "label": "Sign in",
                           "kind": "button",
                           "rects": [{"x": 0.012, "y": 0.04, "w": 0.09, "h": 0.12}]}
    assert Target("yes", 15.0, 0.0, "Yes", "slot").to_dict()["rects"] == []


@pytest.mark.parametrize("args", [
    ("", 15.0, 0.0, "x", "slot"), ("x" * 33, 15.0, 0.0, "x", "slot"),
    (3, 15.0, 0.0, "x", "slot"), ("a", 0.0, 0.0, "x", "slot"), ("a", -15.0, 0.0, "x", "slot"),
    ("a", float("nan"), 0.0, "x", "slot"), ("a", 15.0, float("inf"), "x", "slot"),
    ("a", 15.0, 0.0, None, "slot"), ("a", 15.0, 0.0, "x", "square"),
    ("a", 15.0, 0.0, "x", "button", [{"x": 0, "y": 0, "w": 0.1, "h": 0.1}]),
    ("a", True, 0.0, "x", "slot")])
def test_target_rejects(args):
    with pytest.raises(ValueError):
        Target(*args)


# -- builders -----------------------------------------------------------------------------------

def test_welcome_matches_the_contract():
    w = p.welcome("controller", "point", fake=False, waveform="sine", refresh_hz=60,
                  refresh_tolerance_hz=1, freqs=(15, 17.0, np.float64(12), 20),
                  max_slots=4, detect=True)
    assert w == {"type": "welcome", "protocol": 3, "role": "controller", "mode": "point",
                 "paradigm": "freq",
                 "server": {"name": "brainsync", "version": "0.1.0"}, "fake": False,
                 "waveform": "sine", "refresh_hz": 60.0, "refresh_tolerance_hz": 1.0,
                 "freqs": [15.0, 17.0, 12.0, 20.0], "max_slots": 4, "detect": True}
    v = p.welcome("viewer", None, fake=True, waveform="sine", refresh_hz=60,
                  refresh_tolerance_hz=1, freqs=[15], max_slots=1, detect=False,
                  version="9.9")
    assert v["mode"] is None and v["server"]["version"] == "9.9" and v["fake"] is True
    json.loads(p.dumps(w))


def test_welcome_refresh_rates_is_an_additive_field():
    args = {"fake": False, "waveform": "sine", "refresh_hz": 60, "refresh_tolerance_hz": 1,
            "freqs": [15], "max_slots": 4, "detect": True}
    plain = p.welcome("controller", "slots", **args)
    assert "refresh_rates" not in plain
    w = p.welcome("controller", "slots", refresh_rates=(60, 90.0, np.float64(120), 144), **args)
    assert w == {**plain, "refresh_rates": [60.0, 90.0, 120.0, 144.0]}
    assert json.loads(p.dumps(w))["refresh_rates"] == [60.0, 90.0, 120.0, 144.0]
    for bad in ([], [60, float("nan")], [60, 0], [60, "90"], [True]):
        with pytest.raises(ValueError, match="refresh_rates"):
            p.welcome("controller", "slots", refresh_rates=bad, **args)


@pytest.mark.parametrize("kw", [{"role": "admin"}, {"mode": "draw"}, {"max_slots": 0},
                                {"freqs": [float("nan")]}, {"refresh_hz": float("inf")}])
def test_welcome_rejects(kw):
    args = {"role": "controller", "mode": "slots", "fake": False, "waveform": "sine",
            "refresh_hz": 60, "refresh_tolerance_hz": 1, "freqs": [15], "max_slots": 4,
            "detect": True, **kw}
    with pytest.raises(ValueError):
        p.welcome(args.pop("role"), args.pop("mode"), **args)


def test_builders_return_fresh_dicts():
    a, b = p.phase("wait", False), p.phase("wait", False)
    assert a == b and a is not b
    a["phase"] = "run"
    assert p.phase("wait", False)["phase"] == "wait"


def test_error():
    assert p.error("busy", "taken", fatal=True) == \
        {"type": "error", "code": "busy", "message": "taken", "fatal": True}
    assert p.error("refresh", "x")["fatal"] is False
    for code in p.ERROR_CODES:
        p.error(code, "m")
    with pytest.raises(ValueError, match="error code"):
        p.error("oops", "m")


def test_phase():
    assert p.phase("calibrate", False, 10, "Look at the screen and relax") == \
        {"type": "phase", "phase": "calibrate", "flicker": False, "seconds": 10.0,
         "text": "Look at the screen and relax"}
    assert p.phase("run", True) == {"type": "phase", "phase": "run", "flicker": True,
                                    "seconds": None, "text": ""}
    assert p.phase("run", False)["flicker"] is False
    for name in ("wait", "calibrate", "pause", "stop"):
        with pytest.raises(ValueError, match="must not flicker"):
            p.phase(name, True)
    with pytest.raises(ValueError, match="phase"):
        p.phase("dance", False)
    with pytest.raises(ValueError):
        p.phase("calibrate", False, float("nan"))


def pick_targets() -> list[Target]:
    return [Target("b1", 15.0, 0.0, "Sign in", "button", (Rect(0.012, 0.04, 0.09, 0.12),)),
            Target("more", 20.0, 0.0, "More", "more", (Rect(0.86, 0.84, 0.12, 0.14),))]


def test_layout_pick_matches_the_contract():
    msg = p.layout(7, "pick", pick_targets())
    assert msg == {"type": "layout", "layout_id": 7, "stage": "pick", "targets": [
        {"slot": "b1", "freq": 15.0, "phase": 0.0, "label": "Sign in", "kind": "button",
         "rects": [{"x": 0.012, "y": 0.04, "w": 0.09, "h": 0.12}]},
        {"slot": "more", "freq": 20.0, "phase": 0.0, "label": "More", "kind": "more",
         "rects": [{"x": 0.86, "y": 0.84, "w": 0.12, "h": 0.14}]}]}


def test_layout_slots_confirm_idle():
    msg = p.layout(np.int64(1), "slots", [Target("yes", 15.0, 0.0, "Yes", "slot"),
                                          Target("no", 17.0, 0.0, "No", "slot")])
    assert [t["slot"] for t in msg["targets"]] == ["yes", "no"] and msg["layout_id"] == 1
    assert all(t["rects"] == [] for t in msg["targets"])
    confirm = p.layout(2, "confirm", [
        Target("confirm", 15.0, 0.0, "Do it", "confirm", (Rect(0.1, 0.1, 0.1, 0.1),)),
        Target("back", 17.0, 0.0, "Back", "back", (Rect(0.86, 0.84, 0.12, 0.14),))])
    assert [t["kind"] for t in confirm["targets"]] == ["confirm", "back"]
    assert p.layout(3, "idle", []) == {"type": "layout", "layout_id": 3, "stage": "idle",
                                       "targets": []}


@pytest.mark.parametrize("stage, targets, match", [
    ("draw", [], "stage"),
    ("idle", pick_targets(), "idle"),
    ("pick", [], "at least one"),
    ("slots", [], "at least one"),
    ("pick", [Target("yes", 15.0, 0.0, "Yes", "slot")], "kind"),
    ("slots", pick_targets(), "kind"),
    ("confirm", pick_targets(), "kind"),
    ("slots", [Target("yes", 15.0, 0.0, "Yes", "slot", (Rect(0, 0, 0.1, 0.1),))], "no rects"),
    ("pick", [Target("b1", 15.0, 0.0, "x", "button")], "rect"),
    ("slots", [Target("a", 15.0, 0.0, "A", "slot"), Target("a", 17.0, 0.0, "A", "slot")],
     "unique slots"),
    ("slots", [Target("a", 15.0, 0.0, "A", "slot"), Target("b", 15.0, 0.0, "B", "slot")],
     "unique frequencies"),
    ("pick", [Target("b1", 15.0, 0.0, "x", "button", (Rect(0.1, 0.1, 0.2, 0.2),)),
              Target("b2", 17.0, 0.0, "y", "button", (Rect(0.2, 0.2, 0.2, 0.2),))], "overlap"),
    ("pick", [{"slot": "b1"}], "Target"),
])
def test_layout_rejects(stage, targets, match):
    with pytest.raises(ValueError, match=match):
        p.layout(1, stage, targets)


@pytest.mark.parametrize("layout_id", [-1, 1.5, True, "7", None])
def test_layout_rejects_bad_ids(layout_id):
    with pytest.raises(ValueError, match="layout_id"):
        p.layout(layout_id, "idle", [])


def test_layout_allows_several_rects_per_target_and_touching_patches():
    msg = p.layout(1, "pick", [
        Target("b1", 15.0, 0.0, "x", "button", (Rect(0.0, 0.0, 0.1, 0.1),
                                                Rect(0.05, 0.05, 0.1, 0.1))),
        Target("more", 20.0, 0.0, "More", "more", (Rect(0.15, 0.0, 0.1, 0.1),))])
    assert len(msg["targets"][0]["rects"]) == 2


def test_selection():
    msg = p.selection(12, 7, "b1", 1792809204.9, np.float64(1.8),
                      {"b1": 1.8, "b2": np.float32(0.25), "more": -0.1})
    assert msg == {"type": "selection", "selection_id": 12, "layout_id": 7, "slot": "b1",
                   "t_server": 1792809204.9, "score": 1.8,
                   "scores": {"b1": 1.8, "b2": 0.25, "more": -0.1}}
    assert all(type(v) is float for v in msg["scores"].values())
    json.loads(p.dumps(msg))
    with pytest.raises(ValueError, match="finite"):
        p.selection(1, 1, "b1", 1.0, float("nan"), {})
    with pytest.raises(ValueError, match="scores"):
        p.selection(1, 1, "b1", 1.0, 1.0, {"b1": float("inf")})


def test_scan_request():
    assert p.scan_request(4) == {"type": "scan_request", "scan_id": 4, "settle_ms": 800}
    assert p.scan_request(5, settle_ms=0)["settle_ms"] == 0
    with pytest.raises(ValueError):
        p.scan_request(-1)
    with pytest.raises(ValueError):
        p.scan_request(1, settle_ms=-5)


def test_scan_result():
    elements = [{"id": 0, "label": "Sign in"}]
    msg = p.scan_result(4, True, 1840.4, elements, 9, "Login page")
    assert msg == {"type": "scan_result", "scan_id": 4, "ok": True, "ms": 1840,
                   "n_elements": 1, "n_candidates": 9, "summary": "Login page",
                   "elements": [{"id": 0, "label": "Sign in"}], "cached": False}
    assert msg["elements"][0] is not elements[0]
    assert p.scan_result(6, True, 3, [], 0, cached=True)["cached"] is True
    assert p.scan_result(5, False, 0, [], 0)["n_elements"] == 0
    with pytest.raises(ValueError):
        p.scan_result(5, False, float("nan"), [], 0)


def test_click():
    el = {"id": 3, "label": "Sign in", "role": "button", "hint_id": 0, "risky": False}
    msg = p.click(2, 0.057, 0.1, el)
    assert msg == {"type": "click", "click_id": 2, "x": 0.057, "y": 0.1, "element": el}
    assert msg["element"] is not el
    p.click(0, 0.0, 1.0, {})
    for x, y in ((-0.01, 0.5), (0.5, 1.01), (float("nan"), 0.5)):
        with pytest.raises(ValueError):
            p.click(1, x, y, {})


def test_status():
    assert p.status("streaming", True, "posterior RMS 14 uV", 37.6) == \
        {"type": "status", "board": "streaming", "ok": True, "text": "posterior RMS 14 uV",
         "rtt_ms": 38}
    assert p.status("unknown", False)["rtt_ms"] is None
    for state in p.BOARD_STATES:
        p.status(state, True)
    with pytest.raises(ValueError, match="board"):
        p.status("connected", True)


def test_pong():
    assert p.pong(3, 1792809201.0, 1792809201.02) == \
        {"type": "pong", "ping_id": 3, "t_client": 1792809201.0, "t_server": 1792809201.02}
    assert p.pong(-2, 1, 2)["ping_id"] == -2       # client ids are echoed as they came
    with pytest.raises(ValueError):
        p.pong(True, 1.0, 2.0)


def test_dumps():
    assert p.dumps({"type": "status", "text": "µV"}) == '{"type":"status","text":"µV"}'
    with pytest.raises(ValueError):
        p.dumps({"type": "x", "v": math.nan})
    msg = p.layout(1, "pick", pick_targets())
    assert p.parse_frame(p.dumps(msg)) == msg


def test_ping_constant_round_trip():
    msg = dict(p.PING)
    assert p.parse_frame(p.dumps(msg)) == {"type": "ping"}


def test_overlaps_tolerates_float_rounding_at_touching_edges():
    left = Rect(0.1, 0.0, 0.2, 0.1)                 # ends at 0.30000000000000004
    assert not left.overlaps(Rect(0.3, 0.0, 0.1, 0.1))
    assert not left.overlaps(Rect(0.32, 0.0, 0.1, 0.1), gap=0.02)
    assert left.overlaps(Rect(0.3199, 0.0, 0.1, 0.1), gap=0.02)


@pytest.mark.parametrize("value", ["15", None, [15.0], complex(1, 1)])
def test_builders_refuse_non_numbers(value):
    with pytest.raises(ValueError):
        Target("a", value, 0.0, "A", "slot")
    with pytest.raises(ValueError):
        p.pong(1, value, 2.0)


def test_rescan_is_a_bare_message():
    assert p.validate_client({"type": "rescan", "why": "app changed"}) == {"type": "rescan"}


# -- code mode (v3.2, docs/CVEP-STANDARD.md section 4.2) ------------------------------------

BITS63 = [int(c) for c in "000001111110101011001101110110100100111000101111001010001100001"]


def code_obj(**over) -> dict:
    return {**p.code_spec(BITS63, 21, 63, 30), **over}


def test_code_welcome_carries_the_code_object_and_no_frequencies():
    w = p.welcome("controller", "slots", fake=True, waveform="sine", refresh_hz=60,
                  refresh_tolerance_hz=1, freqs=[], max_slots=3, detect=False,
                  refresh_rates=[60], paradigm="code", code=code_obj())
    assert w["paradigm"] == "code" and w["freqs"] == [] and w["max_slots"] == 3
    assert w["code"] == {"bits": BITS63, "lag_frames": 21, "cycle_frames": 63,
                         "train_cycles": 30, "fill": "#ffffff"}
    assert json.loads(p.dumps(w))["code"]["bits"] == BITS63
    args = {"fake": True, "waveform": "sine", "refresh_hz": 60, "refresh_tolerance_hz": 1,
            "max_slots": 3, "detect": False}
    with pytest.raises(ValueError, match="code object"):
        p.welcome("controller", "slots", freqs=[], paradigm="code", **args)
    with pytest.raises(ValueError, match="code object"):
        p.welcome("controller", "slots", freqs=[15], code=code_obj(), **args)
    with pytest.raises(ValueError, match="no frequencies"):
        p.welcome("controller", "slots", freqs=[15], paradigm="code", code=code_obj(), **args)
    with pytest.raises(ValueError, match="paradigm"):
        p.welcome("controller", "slots", freqs=[15], paradigm="time", **args)


@pytest.mark.parametrize("args, match", [
    (([], 21, 63, 30), "bits"), (([0, 2], 21, 2, 30), "bits"),
    ((BITS63, 21, 62, 30), "one bit a frame"), ((BITS63, 0, 63, 30), "lag_frames"),
    ((BITS63, 21, 63, 1), "train_cycles"), ((BITS63, 21.0, 63, 30), "lag_frames")])
def test_code_spec_rejects(args, match):
    with pytest.raises(ValueError, match=match):
        p.code_spec(*args)


def test_train_phase_names_its_cue_and_cycles():
    msg = p.phase("train", True, text="Look at the YES square", cue="yes", cycles=30)
    assert msg == {"type": "phase", "phase": "train", "flicker": True, "seconds": None,
                   "text": "Look at the YES square", "cue": "yes", "cycles": 30}
    assert "cue" not in p.phase("run", True)
    with pytest.raises(ValueError, match="cue"):
        p.phase("train", True)
    with pytest.raises(ValueError, match="cue"):
        p.phase("run", True, cue="yes", cycles=30)
    with pytest.raises(ValueError, match="cue"):
        p.phase("train", True, cue="yes")


def test_code_targets_carry_a_lag_and_no_frequency():
    t = Target("yes", None, 0.0, "yes", "slot", lag=21)
    assert t.to_dict() == {"slot": "yes", "freq": None, "phase": 0.0, "label": "yes",
                           "kind": "slot", "rects": [], "lag": 21}
    assert "lag" not in Target("yes", 15.0, 0.0, "yes", "slot").to_dict()
    for freq, lag in ((15.0, 21), (None, -1), (None, 1.5), (None, True)):
        with pytest.raises(ValueError):
            Target("a", freq, 0.0, "a", "slot", lag=lag)
    msg = p.layout(3, "slots", [Target(s, None, 0.0, s, "slot", lag=21 * i)
                                for i, s in enumerate(("yes", "no", "help"))])
    assert [t["lag"] for t in msg["targets"]] == [0, 21, 42]
    assert all(t["freq"] is None for t in msg["targets"])
    with pytest.raises(ValueError, match="unique lags"):
        p.layout(3, "slots", [Target("a", None, 0.0, "a", "slot", lag=0),
                              Target("b", None, 0.0, "b", "slot", lag=0)])
    with pytest.raises(ValueError, match="not a mix"):
        p.layout(3, "slots", [Target("a", None, 0.0, "a", "slot", lag=0),
                              Target("b", 15.0, 0.0, "b", "slot")])


def test_train_stage_is_point_mode_patches_of_kind_slot():
    targets = [Target(f"t{k}", None, 0.0, "x", "slot", (Rect(0.1 + 0.3 * k, 0.4, 0.2, 0.2),),
                      lag=21 * k) for k in range(3)]
    assert p.layout(1, "train", targets)["stage"] == "train"
    with pytest.raises(ValueError, match="needs at least one rect"):
        p.layout(1, "train", [Target("t0", None, 0.0, "x", "slot", lag=0)])
    with pytest.raises(ValueError, match="holds targets of kind"):
        p.layout(1, "train", [Target("t0", None, 0.0, "x", "confirm",
                                     (Rect(0.1, 0.1, 0.1, 0.1),), lag=0)])


def test_cycle_is_a_checked_client_message():
    msg = {"type": "cycle", "layout_id": 7, "n": 12, "t_client": 1792809201.512, "x": 1}
    assert p.validate_client(msg) == {"type": "cycle", "layout_id": 7, "n": 12,
                                      "t_client": 1792809201.512}
    for over in ({"n": -1}, {"n": 1.5}, {"layout_id": True}, {"t_client": float("nan")},
                 {"t_client": "now"}):
        bad("protocol", p.validate_client, {**msg, **over})
    bad("protocol", p.validate_client, {"type": "cycle", "layout_id": 7, "n": 1})


def test_train_trained_and_decision_builders():
    assert p.train_progress(12, 30) == {"type": "train", "cycle": 12, "of": 30}
    with pytest.raises(ValueError):
        p.train_progress(31, 30)
    assert p.trained(np.float64(0.8333), 1 / 3, 30) == \
        {"type": "trained", "accuracy": 0.833, "chance": 0.333, "cycles": 30}
    assert p.trained(float("nan"), 1 / 3, 30)["accuracy"] is None
    assert p.trained(None, 1 / 3, 30)["accuracy"] is None
    with pytest.raises(ValueError):
        p.trained(1.5, 1 / 3, 30)
    d = p.decision(7, {"yes": np.float64(0.42), "no": 0.11, "help": -0.03}, "yes", "yes",
                   0.31, False)
    assert d == {"type": "decision", "layout_id": 7,
                 "scores": {"yes": 0.42, "no": 0.11, "help": -0.03}, "raw": "yes",
                 "committed": "yes", "lead": 0.31, "changed": False}
    assert p.decision(7, {"yes": 0.1}, None, None, None, True)["committed"] is None
    with pytest.raises(ValueError, match="not one of the scored slots"):
        p.decision(7, {"yes": 0.1}, "no", None, None, False)
    with pytest.raises(ValueError):
        p.decision(7, {"yes": float("nan")}, None, None, None, False)
    json.loads(p.dumps(d))
