"""BrainPoint: point mode for the session, from screenshot to click (PROTOCOL-v3.md section 4).

    mode = PointMode(Detector(), freqs=(15.0, 17.0, 12.0, 20.0), viewport=(1512, 857))
    out = mode.start(time.time())             # scan_request, idle layout, phase run
    out += mode.on_message(scan, now)         # the client's screenshot: detection starts
    out += mode.tick(now)                     # scan_result, pick layout, phase run when done
    out += mode.on_selection("b1", now)       # click + phase pause (or the next layout)
    out += mode.on_message(clicked, now)      # scan_request, idle layout, phase run

PointMode implements the session's Mode: every method returns the protocol messages to send,
and the session intercepts "layout" (it activates the decoder on the targets) and "phase" (the
mode's hold: "run", or "pause" while a click is in flight). Everything else goes out as is.

Rules:
- Each scan_request has a new ``scan_id``. A scan with any other id is stale and ignored, and
  so is a ``clicked`` whose ``click_id`` is not the click in flight.
- Detection runs on ``executor`` (a one-thread pool by default), because a Haiku call takes a
  second or two and the session's loop must keep reading the board. ``tick`` collects it.
- While a scan is pending or being analyzed, the layout is idle, so nothing flickers.
- After a detection, a TopKSelector makes the pages, and the pick layout shows the first one.
  A failed detection (DetectError or any other exception) is answered with ``scan_result``
  ``ok: false`` and ``error`` ``scan_failed``; the layout stays idle and a new scan_request
  goes out ``retry_s`` later.
- A pick of a button that is not risky, or "Do it" on a risky one, sends ``phase`` pause and
  then ``click`` at the center of the element's own rect (not the center of its patch, which
  may have been moved away from the viewport's edge). The hold stays "pause" until the client
  answers with ``clicked``; then the next scan is requested with ``settle_ms``.
- When the client does not answer a scan_request or a click within ``reply_timeout_s``, a
  fresh scan_request goes out, so a lost message never leaves the user stuck.
- The labels of the last ``history_len`` successful clicks go to the detector as history, so
  ``p_next`` can follow the task.
- Screenshots are never logged. ``log(kind, fields)`` gets "scan", "scan_failed", "pick" and
  "click" records with ids, timings, counts and labels only.
"""
from __future__ import annotations

import collections
import concurrent.futures
import itertools
import math
import sys
from collections.abc import Callable, Sequence
from typing import Any

from brainsync.screen.detect import DetectError, Detection, Detector, Element
from brainsync.screen.selector import GAP, MIN_P_BUTTON, MIN_PATCH, TopKSelector
from brainsync.server import protocol
from brainsync.server.protocol import SETTLE_MS

RETRY_S = 3.0
REPLY_TIMEOUT_S = 15.0
HISTORY_LEN = 5


def _note(text: str) -> None:
    print(f"point: {text}", file=sys.stderr, flush=True)


def _positive(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number of seconds, got {value!r}")
    f = float(value)
    if not math.isfinite(f) or f <= 0:
        raise ValueError(f"{name} must be a positive number of seconds, got {value!r}")
    return f


def _clamp01(v: float) -> float:
    return min(max(v, 0.0), 1.0)


class PointMode:
    """Point mode: scan, detect, show the top-K pages, click, and scan again."""

    def __init__(self, detector: Detector, freqs: Sequence[float], *,
                 viewport: tuple[float, float] = (16, 9),
                 executor: concurrent.futures.Executor | None = None,
                 settle_ms: int = SETTLE_MS, retry_s: float = RETRY_S,
                 min_p_button: float = MIN_P_BUTTON, min_patch: float = MIN_PATCH,
                 gap: float = GAP, history_len: int = HISTORY_LEN,
                 log: Callable[[str, dict], None] | None = None,
                 reply_timeout_s: float = REPLY_TIMEOUT_S,
                 phases: Sequence[float] | None = None) -> None:
        if not callable(getattr(detector, "detect", None)):
            raise ValueError("detector must have a detect() method; pass a "
                             "brainsync.screen.detect.Detector")
        self.freqs = tuple(freqs)
        self.phases = None if phases is None else tuple(phases)
        self.min_p_button, self.min_patch, self.gap = min_p_button, min_patch, gap
        # Fail now, not at the first scan, if these cannot make a page.
        TopKSelector((), self.freqs, aspect=1.0, min_p_button=min_p_button,
                     min_patch=min_patch, gap=gap, phases=self.phases)
        try:
            vw, vh = viewport
        except (TypeError, ValueError):
            raise ValueError(f"viewport must be (width, height), got {viewport!r}") from None
        self.aspect = _positive(vw, "viewport width") / _positive(vh, "viewport height")
        if isinstance(settle_ms, bool) or not isinstance(settle_ms, int) or settle_ms < 0:
            raise ValueError(f"settle_ms must be an integer >= 0, got {settle_ms!r}")
        if isinstance(history_len, bool) or not isinstance(history_len, int) or history_len < 0:
            raise ValueError(f"history_len must be an integer >= 0, got {history_len!r}")
        self.detector = detector
        self.settle_ms = settle_ms
        self.retry_s = _positive(retry_s, "retry_s")
        self.reply_timeout_s = _positive(reply_timeout_s, "reply_timeout_s")
        self.history: collections.deque[str] = collections.deque(maxlen=history_len)
        self._log = log
        self._executor = executor
        self._own_executor = executor is None
        self._scan_ids = itertools.count(1)
        self._layout_ids = itertools.count(1)
        self._click_ids = itertools.count(1)
        self.hold = "run"                           # the phase this mode asks for
        self.selector: TopKSelector | None = None
        self.scan_id: int | None = None             # the scan_request awaiting its scan
        self._asked_at: float | None = None         # when that request (or a click) went out
        self._future: concurrent.futures.Future | None = None
        self._future_scan: int | None = None
        self._future_t: float = 0.0
        self._future_aspect: float = self.aspect
        self._retry_at: float | None = None
        self.click_id: int | None = None            # the click awaiting its clicked
        self._clicked_el: Element | None = None
        self.layout_id: int | None = None
        self._stopped = False

    # -- message helpers --------------------------------------------------------------------

    def _emit(self, kind: str, fields: dict) -> None:
        if self._log is not None:
            try:
                self._log(kind, fields)
            except Exception as exc:  # a broken log hook must not stop the mode
                _note(f"log hook failed: {type(exc).__name__}: {exc}")

    def _phase(self, name: str) -> dict:
        self.hold = name
        return protocol.phase(name, name == "run")

    def _layout(self, stage: str, targets: Sequence[protocol.Target] = ()) -> dict:
        self.layout_id = next(self._layout_ids)
        return protocol.layout(self.layout_id, stage, targets)

    def _new_scan(self, now: float) -> list[dict]:
        """A scan_request and an idle layout; any selector, click or detection in flight is
        dropped (a detection already running finishes, and its result is ignored)."""
        if self._future is not None:
            self._future.cancel()
            self._future = None
            self._future_scan = None
        self.scan_id = next(self._scan_ids)
        self._asked_at = now
        self._retry_at = None
        self.selector = None
        self.click_id = None
        self._clicked_el = None
        return [protocol.scan_request(self.scan_id, self.settle_ms), self._layout("idle")]

    # -- Mode -------------------------------------------------------------------------------

    def start(self, now: float) -> list[dict]:
        """Ask for the first screenshot; the layout is idle, so nothing flickers yet."""
        if self._stopped:
            return []
        return self._new_scan(now) + [self._phase("run")]

    def on_message(self, msg: dict, now: float) -> list[dict]:
        """Handle the client's ``scan`` and ``clicked``; other types are not this mode's."""
        if self._stopped or not isinstance(msg, dict):
            return []
        kind = msg.get("type")
        if kind == "scan":
            return self._on_scan(msg, now)
        if kind == "clicked":
            return self._on_clicked(msg, now)
        return []

    def _on_scan(self, msg: dict, now: float) -> list[dict]:
        scan_id = msg.get("scan_id")
        if self.scan_id is None or scan_id != self.scan_id:
            _note(f"ignored scan {scan_id!r}: waiting for "
                  f"{'no scan' if self.scan_id is None else f'scan {self.scan_id}'}")
            return []
        try:
            image, viewport = msg["image"], msg["viewport"]
            vw, vh = float(viewport["w"]), float(viewport["h"])
            aspect = vw / vh if vw > 0 and vh > 0 else self.aspect
            args = (image["data"], image["media_type"], image["w"], image["h"], vw, vh)
            kwargs = {"hints": msg.get("hints") or (), "history": tuple(self.history),
                      "url": msg.get("url") or "", "title": msg.get("title") or ""}
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            return [protocol.error("protocol", f"scan {scan_id} is missing a field ({exc!r}); "
                                   f"send image {{media_type, data, w, h}} and viewport "
                                   f"{{w, h}}")]
        self.scan_id = None
        self._asked_at = None
        self._future_scan = scan_id
        self._future_t = now
        self._future_aspect = aspect
        try:
            self._future = self._pool().submit(self.detector.detect, *args, **kwargs)
        except Exception as exc:  # a shut-down executor
            self._future = concurrent.futures.Future()
            self._future.set_exception(exc)
        return []

    def _on_clicked(self, msg: dict, now: float) -> list[dict]:
        click_id = msg.get("click_id")
        if self.click_id is None or click_id != self.click_id:
            _note(f"ignored clicked {click_id!r}: no click {click_id!r} is in flight")
            return []
        el = self._clicked_el
        ok = msg.get("ok") is True
        if ok and el is not None and el.label and self.history.maxlen:
            self.history.append(el.label)
        if not ok:
            _note(f"click {click_id} failed: {msg.get('method', '')} {msg.get('detail', '')}"
                  .rstrip())
        self._emit("clicked", {"click_id": click_id, "ok": ok, "method": msg.get("method", ""),
                               "detail": msg.get("detail", "")})
        return self._new_scan(now) + [self._phase("run")]

    def on_selection(self, slot: str, now: float) -> list[dict]:
        """Apply the decoder's pick of ``slot`` to the page on screen."""
        sel = self.selector
        if self._stopped or sel is None or sel.stage not in ("pick", "confirm"):
            return []
        try:
            stage = sel.pick(slot)
        except ValueError as exc:
            _note(f"ignored selection: {exc}")
            return []
        self._emit("pick", {"slot": slot, "stage": stage, "page": sel.page_index,
                            "layout_id": self.layout_id,
                            "label": sel.chosen.label if sel.chosen is not None else None})
        if stage in ("pick", "confirm"):
            return [self._layout(stage, sel.targets())]
        if stage == "rescan":
            return self._new_scan(now)
        el = sel.chosen
        assert stage == "done" and el is not None
        self.click_id = next(self._click_ids)
        self._clicked_el = el
        self._asked_at = now
        self.selector = None
        x, y = _clamp01(el.cx), _clamp01(el.cy)
        self._emit("click", {"click_id": self.click_id, "x": round(x, 4), "y": round(y, 4),
                             "label": el.label, "role": el.role, "risky": el.risky})
        element = {"id": el.id, "label": el.label, "role": el.role, "hint_id": el.hint_id,
                   "risky": el.risky}
        # Pause first, so the flicker stops before the page under it changes.
        return [self._phase("pause"), protocol.click(self.click_id, x, y, element)]

    def tick(self, now: float) -> list[dict]:
        """Collect a finished detection, and send the scan_request a retry or timeout is due."""
        if self._stopped:
            return []
        out: list[dict] = []
        fut = self._future
        if fut is not None and fut.done():
            self._future = None
            out += self._finish(fut, now)
        if self._retry_at is not None and now >= self._retry_at:
            out += self._new_scan(now)
        elif self._asked_at is not None and now - self._asked_at >= self.reply_timeout_s:
            what = (f"click {self.click_id}" if self.click_id is not None
                    else f"scan_request {self.scan_id}")
            _note(f"no answer to {what} after {self.reply_timeout_s:g} s; asking for a new scan")
            out += self._new_scan(now)
            if self.hold != "run":
                out.append(self._phase("run"))
        return out

    def _finish(self, fut: concurrent.futures.Future, now: float) -> list[dict]:
        scan_id = self._future_scan
        assert scan_id is not None
        self._future_scan = None
        try:
            det = fut.result()
            if not isinstance(det, Detection):
                raise DetectError(f"the detector returned {type(det).__name__}, not a "
                                  f"Detection", "invalid")
            sel = TopKSelector(det.elements, self.freqs, aspect=self._future_aspect,
                               min_p_button=self.min_p_button, min_patch=self.min_patch,
                               gap=self.gap, phases=self.phases)
        except concurrent.futures.CancelledError:
            return []
        except Exception as exc:
            return self._failed(scan_id, exc, now)
        self.selector = sel
        self._emit("scan", {"scan_id": scan_id, "ok": True, "ms": det.ms,
                            "n_elements": len(det.elements), "n_candidates": len(sel.candidates),
                            "n_pages": len(sel.pages), "n_dropped": len(sel.dropped),
                            "input_tokens": det.input_tokens,
                            "output_tokens": det.output_tokens, "model": det.model,
                            "summary": det.summary})
        return [protocol.scan_result(scan_id, True, det.ms,
                                     [e.to_dict() for e in det.elements],
                                     len(sel.candidates), det.summary),
                self._layout("pick", sel.targets()),
                self._phase("run")]

    def _failed(self, scan_id: int, exc: BaseException, now: float) -> list[dict]:
        if isinstance(exc, DetectError):
            why = f"{exc.reason}: {exc.message}"
        else:
            why = f"{type(exc).__name__}: {' '.join(str(exc).split())[:300]}"
        ms = max(0.0, (now - self._future_t) * 1000)
        self._retry_at = now + self.retry_s
        _note(f"scan {scan_id} failed ({why}); new scan in {self.retry_s:g} s")
        self._emit("scan_failed", {"scan_id": scan_id, "ms": round(ms), "error": why})
        return [protocol.scan_result(scan_id, False, ms, [], 0),
                protocol.error("scan_failed", f"detection of scan {scan_id} failed ({why}); "
                                              f"a new scan follows in {self.retry_s:g} s")]

    def stop(self) -> None:
        """Cancel a pending detection and release the executor this mode made. Idempotent."""
        self._stopped = True
        if self._future is not None:
            self._future.cancel()
            self._future = None
        if self._own_executor and self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        self.selector = None
        self.scan_id = self.click_id = None
        self._asked_at = self._retry_at = None

    def _pool(self) -> concurrent.futures.Executor:
        if self._executor is None:
            self._executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="brainpoint-detect")
        return self._executor
