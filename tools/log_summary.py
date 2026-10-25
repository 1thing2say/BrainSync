"""Summary of session logs (PLAN.md V6 item 4 and the V3 "Done when").

Reads one or more JSONL session logs (app/log.py) and prints a short report per session, plus a
total when there is more than one:

- the session: length, keyboard / fake / real decoder, page or list mode, pages shown;
- trials: started, selections by role and source, not caught, how often a trial ended in a pick;
- the partner's C/W marks (judgment events) and the share marked correct;
- spoken replies: seconds per reply (from the partner's line or the previous reply), selections
  per reply, how often the reply was option 1 (``spoken.rank``), and which list it came from;
- Claude pages: requests and how each ended (shown, timed out, ignored, failed), median and p95
  of ``llm_ok`` ms and of the wall-clock time from the request to the page on screen (or given
  up), the fallback rate and tokens;
- Cartesia: clips ok / failed, median ms, replies spoken with a clip vs the browser voice;
- partner input: typed vs spoken lines, queued vs interrupting, mic states seen;
- warnings worth acting on.

Counts only by default: conversation text is printed only with ``--show-text``. Logs from before
a feature existed are fine (a missing event just counts as zero).

    python tools/log_summary.py                    # the newest session in logs/
    python tools/log_summary.py --all              # every session, then a total
    python tools/log_summary.py logs/2026-10-24_*.jsonl --json
    python tools/log_summary.py --show-text        # also the partner lines and spoken replies

Exit status: 0, 1 if a log could not be read, 2 if there was no log to read.
"""

from __future__ import annotations

import argparse
import bisect
import dataclasses
import json
import math
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    # Run as a plain script: make the repo root importable so `app` resolves.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.log import DEFAULT_LOG_DIR, read_events  # noqa: E402
from app.schemas import is_fake_name  # noqa: E402

TRIAL_STATES = ("STIMULATING", "CONFIRMING")
LLM_LIST = "llm"  # app/orchestrator.py: the name of a page Claude wrote
SECRET = re.compile(r"sk-ant-[A-Za-z0-9_-]+|sk_car_[A-Za-z0-9_-]+")
FALLBACK_OUTCOMES = ("timed out", "failed", "invalid")  # the fixed list was shown instead
BAD_MIC = ("denied", "nomic", "offline", "unsupported", "error", "needs_tap")

# Warning thresholds (a share of at least this much, over at least MIN_COUNT cases).
MIN_COUNT = 3
LLM_TIMEOUT_SHARE = 0.3
FALLBACK_SHARE = 0.5
NOT_CAUGHT_SHARE = 0.5
WRONG_SHARE = 0.3
TTS_FAILED_SHARE = 0.3
UNCONFIRMED_SHARE = 0.5


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------


def percentile(values: list[float], q: float) -> float | None:
    """The ``q``-th percentile (0-100) with linear interpolation; None for no values."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    pos = (len(xs) - 1) * q / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def median(values: list[float]) -> float | None:
    return percentile(values, 50)


def share(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def spread(values: list[float]) -> dict[str, Any]:
    """Count, median, p95 and max of ``values`` (None when empty)."""
    return {"n": len(values), "median": median(values), "p95": percentile(values, 95),
            "max": max(values) if values else None}


# ---------------------------------------------------------------------------
# Collecting one session
# ---------------------------------------------------------------------------


@dataclass
class Stats:
    """Raw counts and samples from one or more sessions; ``summarize`` turns them into numbers.

    Every field merges by type (counts add, lists extend, flags or), so a total over several
    sessions is ``merge`` of their Stats.
    """

    sessions: int = 0
    events: int = 0
    duration_s: float = 0.0
    keyboard_mode: bool = False
    fake_decoder: bool = False
    decoder_names: Counter = field(default_factory=Counter)
    screen_modes: Counter = field(default_factory=Counter)  # page / list / confirm / scan (v1)
    pages: int = 0  # distinct option pages (list_id) shown
    screens: int = 0  # show_page / show_option messages, repeats included
    trials: int = 0
    confirm_trials: int = 0
    stop_reasons: Counter = field(default_factory=Counter)
    selections_by_role: Counter = field(default_factory=Counter)
    selections_by_source: Counter = field(default_factory=Counter)
    not_caught: Counter = field(default_factory=Counter)  # why: timeout / no_selection
    keyboard_idle: int = 0  # of those, in keyboard mode with no decoder: nobody pressed a key
    not_confirmed: int = 0  # not caught on the confirm screen (counts as Back)
    confirm_back: int = 0
    marks: Counter = field(default_factory=Counter)  # correct / wrong
    replies: list = field(default_factory=list)  # one dict per spoken reply
    llm_requests: list = field(default_factory=list)  # one dict per Claude page request
    llm_ok_ms: list = field(default_factory=list)
    llm_attempt_failures: Counter = field(default_factory=Counter)  # reason (status) per attempt
    llm_invalid: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    llm_warm_failed: int = 0
    tts_ok_ms: list = field(default_factory=list)
    tts_failed: Counter = field(default_factory=Counter)  # reason (status)
    tts_retries: int = 0
    tts_disabled: list = field(default_factory=list)
    speak_timeouts: int = 0  # the page never reported tts_done (speech.max_speak_s)
    partner_lines: Counter = field(default_factory=Counter)  # typed / stt
    partner_queued: int = 0
    stt_ignored_muted: int = 0
    mic_reports: Counter = field(default_factory=Counter)
    health: Counter = field(default_factory=Counter)  # disconnects, invalid messages, ...
    texts: list = field(default_factory=list)  # (t_unix, who, text), only printed on request


def merge(parts: list[Stats]) -> Stats:
    """One Stats over several sessions."""
    total = Stats()
    for part in parts:
        for f in dataclasses.fields(Stats):
            mine, theirs = getattr(total, f.name), getattr(part, f.name)
            if isinstance(mine, bool):
                setattr(total, f.name, mine or theirs)
            elif isinstance(mine, (Counter, list)):
                mine += theirs  # in place
            else:
                setattr(total, f.name, mine + theirs)
    return total


def _msg(event: dict) -> dict:
    msg = event.get("message")
    return msg if isinstance(msg, dict) else {}


def _page_utterances(msg: dict) -> list[str]:
    """The option sentences on a page, in rank order (page mode: slots; list mode: options)."""
    if msg.get("mode") == "list" and msg.get("options"):
        return [o.get("utterance", "") for o in msg["options"] if isinstance(o, dict)]
    return [s.get("utterance", "") for s in msg.get("slots") or []
            if isinstance(s, dict) and s.get("role") == "option"]


class _Session:
    """Walks one session's events in order and fills a Stats."""

    def __init__(self) -> None:
        self.s = Stats(sessions=1)
        self.page_ids: set[str] = set()
        self.page: list[str] = []  # the option sentences of the last page shown
        self.scan: dict[str, dict[int, str]] = {}  # v1 scan mode: list_id -> index -> sentence
        self.llm_sentences: set[str] = set()  # every sentence Claude wrote this session
        self.spoken_info: dict[str, dict] = {}  # utterance_id -> spoken event
        self.requests: dict[Any, dict] = {}
        self.waiting_show: dict[Any, set[str]] = {}  # request_id -> its options, until shown
        # Anchors for "seconds per reply": the partner's last line or the end of the last reply;
        # after a pause (or with neither), the first page after the next baseline.
        self.partner_t: float | None = None
        self.before_partner_t: float | None = None
        self.reply_end_t: float | None = None
        self.last_speak: tuple[str | None, float | None] | None = None
        self.after_baseline = False
        self.baseline_t: float | None = None  # the last BASELINE state
        self.resume_t: float | None = None
        self.pause_t: float | None = None
        self.selection_times: list[float] = []
        self.trial_times: list[float] = []
        self.board_ok = True

    def run(self, events: list[dict]) -> Stats:
        events = [e for e in events if isinstance(e, dict)]
        times = [e["t_unix"] for e in events if isinstance(e.get("t_unix"), (int, float))]
        self.s.events = len(events)
        if times:
            self.s.duration_s = max(times) - min(times)
        for event in events:
            t = event.get("t_unix")
            t = float(t) if isinstance(t, (int, float)) else None
            handler = getattr(self, f"_on_{event.get('event')}", None)
            if handler is not None:
                handler(t, event)
            elif str(event.get("event", "")).startswith("invalid_from_"):
                self.s.health["invalid messages"] += 1
        self._finish_requests()
        if self.s.keyboard_mode and not self.s.decoder_names:
            self.s.keyboard_idle = sum(self.s.not_caught.values())
        return self.s

    # -- server and connections ----------------------------------------------

    def _on_server_start(self, t: float | None, e: dict) -> None:
        self.s.keyboard_mode |= bool(e.get("keyboard_mode"))
        self.s.fake_decoder |= bool(e.get("fake_decoder"))

    def _on_decoder_disconnected(self, t: float | None, e: dict) -> None:
        if e.get("state") not in ("IDLE", "PAUSED", None):
            self.s.health["decoder lost mid-session"] += 1

    def _on_decoder_replaced(self, t: float | None, e: dict) -> None:
        self.s.health["decoder replaced"] += 1

    def _on_baseline_timeout(self, t: float | None, e: dict) -> None:
        self.s.health["baseline timeouts"] += 1

    def _on_stale_selection(self, t: float | None, e: dict) -> None:
        self.s.health["stale selections"] += 1

    def _on_inactive_slot(self, t: float | None, e: dict) -> None:
        self.s.health["selections of an inactive slot"] += 1

    # -- messages ------------------------------------------------------------

    def _on_from_decoder(self, t: float | None, e: dict) -> None:
        msg = _msg(e)
        kind = msg.get("type")
        if kind == "hello":
            self.s.decoder_names[str(msg.get("name") or "?")] += 1
        elif kind == "status":
            board_ok = msg.get("board_ok", True) is not False
            if self.board_ok and not board_ok:
                self.s.health["board_ok false"] += 1
            self.board_ok = board_ok

    def _on_to_decoder(self, t: float | None, e: dict) -> None:
        self._decoder_command(t, e)

    def _on_not_sent_before_ack(self, t: float | None, e: dict) -> None:
        self._decoder_command(t, e)  # keyboard mode without a bridge: the trial still ran

    def _decoder_command(self, t: float | None, e: dict) -> None:
        msg = _msg(e)
        if msg.get("type") == "stim_start":
            self.s.trials += 1
            if e.get("state") == "CONFIRMING":
                self.s.confirm_trials += 1
            if t is not None:
                self.trial_times.append(t)
        elif msg.get("type") == "stim_stop":
            self.s.stop_reasons[str(msg.get("reason") or "?")] += 1

    def _on_to_ui(self, t: float | None, e: dict) -> None:
        msg = _msg(e)
        kind = msg.get("type")
        if kind == "show_page":
            self._show_page(t, msg)
        elif kind == "show_option":  # v1 scan mode: one option at a time
            self.s.screens += 1
            self.s.screen_modes["scan"] += 1
            list_id = str(msg.get("list_id"))
            if list_id not in self.page_ids:
                self.page_ids.add(list_id)
                self.s.pages += 1
            index = msg.get("index")
            if isinstance(index, int):
                self.scan.setdefault(list_id, {})[index] = msg.get("utterance", "")
                sentences = self.scan[list_id]
                self.page = [sentences.get(i, "") for i in range(max(sentences) + 1)]
            self._page_seen(t)
        elif kind == "speak":
            self._speak(t, msg)
        elif kind == "state" and msg.get("name") == "BASELINE":
            self.after_baseline = True
            self.baseline_t = t
        elif kind == "state" and msg.get("name") in ("PAUSED", "IDLE") and t is not None:
            self.pause_t = t

    def _show_page(self, t: float | None, msg: dict) -> None:
        self.s.screens += 1
        mode = str(msg.get("mode") or "?")
        self.s.screen_modes[mode] += 1
        if mode == "confirm":
            return
        list_id = str(msg.get("list_id"))
        if list_id not in self.page_ids:
            self.page_ids.add(list_id)
            self.s.pages += 1
        self.page = _page_utterances(msg)
        shown = set(self.page)
        for request_id, options in list(self.waiting_show.items()):
            if options and options <= shown:
                self.requests[request_id]["shown_t"] = t
                del self.waiting_show[request_id]
        self._page_seen(t)

    def _page_seen(self, t: float | None) -> None:
        if self.after_baseline:
            self.after_baseline = False
            self.resume_t = t

    def _on_from_ui(self, t: float | None, e: dict) -> None:
        msg = _msg(e)
        kind = msg.get("type")
        if kind == "partner_text":
            source = str(msg.get("source") or "?")
            self.s.partner_lines[source] += 1
            self.before_partner_t, self.partner_t = self.partner_t, t
            self.s.texts.append((t, f"partner ({source})", str(msg.get("text", ""))))
        elif kind == "tts_done":
            if self.last_speak is not None and msg.get("utterance_id") == self.last_speak[0]:
                self.reply_end_t = t
        elif kind == "mic_report":
            self.s.mic_reports[str(msg.get("state") or "?")] += 1
        elif kind == "stop":
            self.s.health["STOP pressed"] += 1
        elif kind == "judgment":  # a page that sends the mark itself (today: C/W keys)
            self._mark(msg.get("correct"))

    def _on_stt_ignored_muted(self, t: float | None, e: dict) -> None:
        self.s.stt_ignored_muted += 1
        self.partner_t = self.before_partner_t  # a line the app did not take is no anchor

    def _on_partner_text_queued(self, t: float | None, e: dict) -> None:
        self.s.partner_queued += 1

    def _on_mic_muted(self, t: float | None, e: dict) -> None:
        if e.get("muted"):
            self.s.mic_reports["muted (button)"] += 1

    # -- selections and replies ----------------------------------------------

    def _on_selected(self, t: float | None, e: dict) -> None:
        self.s.selections_by_role[str(e.get("role") or "?")] += 1
        self.s.selections_by_source[str(e.get("source") or "?")] += 1
        if t is not None:
            self.selection_times.append(t)

    def _on_not_caught(self, t: float | None, e: dict) -> None:
        self.s.not_caught[str(e.get("why") or "?")] += 1
        if e.get("confirming"):
            self.s.not_confirmed += 1

    def _on_confirm_back(self, t: float | None, e: dict) -> None:
        self.s.confirm_back += 1

    def _on_judgment(self, t: float | None, e: dict) -> None:
        self._mark(e.get("correct"))

    def _mark(self, correct: Any) -> None:
        if isinstance(correct, bool):
            self.s.marks["correct" if correct else "wrong"] += 1

    def _on_spoken(self, t: float | None, e: dict) -> None:
        self.spoken_info[str(e.get("utterance_id"))] = e

    def _on_tts_timeout(self, t: float | None, e: dict) -> None:
        self.s.speak_timeouts += 1
        self.reply_end_t = t

    def _speak(self, t: float | None, msg: dict) -> None:
        text = str(msg.get("text", ""))
        utterance_id = msg.get("utterance_id")
        info = self.spoken_info.get(str(utterance_id))
        if info is not None:
            rank, source = info.get("rank"), info.get("list")
        else:  # a log from before the spoken event: find the sentence on the last page
            rank = self.page.index(text) + 1 if text in self.page else None
            source = None
        if source is None:
            source = LLM_LIST if text in self.llm_sentences else "fallback"
        if self.last_speak is not None and self.reply_end_t is None:
            self.reply_end_t = self.last_speak[1]  # no tts_done: count from the speak itself (or None)
        anchor = self._reply_start()
        reply: dict[str, Any] = {"rank": rank if isinstance(rank, int) else None,
                                 "list": str(source), "audio": bool(msg.get("audio_url")),
                                 "seconds": None, "selections": None, "trials": None}
        if t is not None and anchor is not None and t >= anchor:
            reply["seconds"] = t - anchor
            reply["selections"] = self._count_between(self.selection_times, anchor, t)
            reply["trials"] = self._count_between(self.trial_times, anchor, t)
        self.s.replies.append(reply)
        self.s.texts.append((t, "user", text))
        self.last_speak = (utterance_id, t)
        self.reply_end_t = None

    def _reply_start(self) -> float | None:
        """When the clock for the next reply started (None if unknown)."""
        anchors = [a for a in (self.partner_t, self.reply_end_t) if a is not None]
        base = max(anchors) if anchors else None
        resumed = self.resume_t is not None and (self.pause_t is None or self.resume_t > self.pause_t)
        if resumed and (base is None or (self.baseline_t is not None and base < self.baseline_t)):
            return self.resume_t  # the time paused (or idle) is not the reply's
        return base

    @staticmethod
    def _count_between(times: list[float], start: float, end: float) -> int:
        """How many of the (sorted) ``times`` fall in (start, end]."""
        return bisect.bisect_right(times, end) - bisect.bisect_right(times, start)

    # -- Claude --------------------------------------------------------------

    def _request(self, e: dict) -> dict:
        request_id = e.get("request_id")
        return self.requests.setdefault(request_id, {
            "reason": "?", "t": None, "ok_t": None, "shown_t": None, "timeout_t": None,
            "gave_up_t": None, "ignored_t": None, "failed_t": None, "failed_reason": None, "invalid": 0,
            "replaced": False})

    def _on_llm_request(self, t: float | None, e: dict) -> None:
        # A newer request replaces a page that came in but is not on screen yet (the
        # orchestrator's pending list becomes the new line's fallback, then its page).
        for request_id in self.waiting_show:
            self.requests[request_id]["replaced"] = True
        self.waiting_show.clear()
        req = self._request(e)
        req["reason"], req["t"] = str(e.get("reason") or "?"), t

    def _on_llm_ok(self, t: float | None, e: dict) -> None:
        req = self._request(e)
        req["ok_t"] = t
        if isinstance(e.get("ms"), (int, float)):
            self.s.llm_ok_ms.append(float(e["ms"]))
        self._tokens(e)
        options = [o for o in e.get("options") or [] if isinstance(o, str)]
        self.llm_sentences.update(options)
        if options and req["gave_up_t"] is None:
            self.waiting_show[e.get("request_id")] = set(options)

    def _on_llm_invalid(self, t: float | None, e: dict) -> None:
        self._request(e)["invalid"] += 1
        self.s.llm_invalid += 1
        self._tokens(e)

    def _on_llm_failed(self, t: float | None, e: dict) -> None:
        req = self._request(e)
        reason = str(e.get("reason") or "?")
        status = e.get("status")
        self.s.llm_attempt_failures[f"{reason} ({status})" if status else reason] += 1
        req["failed_t"], req["failed_reason"] = t, reason
        if reason == "timeout" and req["timeout_t"] is None:
            req["timeout_t"] = t

    def _on_llm_page_timeout(self, t: float | None, e: dict) -> None:
        req = self._request(e)  # the orchestrator gave up on it: the fixed list was shown
        if req["gave_up_t"] is None:
            req["gave_up_t"] = t
        self.waiting_show.pop(e.get("request_id"), None)

    def _on_llm_page_ignored(self, t: float | None, e: dict) -> None:
        req = self._request(e)
        if req["ignored_t"] is None:
            req["ignored_t"] = t
        self.waiting_show.pop(e.get("request_id"), None)

    def _on_llm_warm_failed(self, t: float | None, e: dict) -> None:
        self.s.llm_warm_failed += 1

    def _tokens(self, e: dict) -> None:
        for key, attr in (("input_tokens", "tokens_in"), ("output_tokens", "tokens_out")):
            if isinstance(e.get(key), int):
                setattr(self.s, attr, getattr(self.s, attr) + e[key])

    def _finish_requests(self) -> None:
        """One record per request: how it ended and how long it took."""
        for req in self.requests.values():
            if req["shown_t"] is not None:
                outcome, end = "shown", req["shown_t"]
            elif req["gave_up_t"] is not None:
                outcome, end = "timed out", req["gave_up_t"]
            elif req["ignored_t"] is not None or req["replaced"]:
                # A newer partner line came first (even if menus timed out on this one later).
                outcome, end = "replaced", None
            elif req["timeout_t"] is not None:
                outcome, end = "timed out", req["timeout_t"]  # menus' deadline, still awaited
            elif req["ok_t"] is not None:
                outcome, end = "not shown", None  # the session ended first
            elif req["failed_t"] is not None:
                outcome, end = "failed", req["failed_t"]
            elif req["invalid"]:
                outcome, end = "invalid", None
            else:
                outcome, end = "no answer", None
            answer = req["ok_t"] if req["ok_t"] is not None else req["failed_t"]
            start = req["t"]
            self.s.llm_requests.append({
                "reason": req["reason"], "outcome": outcome,
                "answer_s": answer - start if start is not None and answer is not None else None,
                "screen_s": end - start if start is not None and end is not None else None,
            })

    # -- Cartesia ------------------------------------------------------------

    def _on_tts_ok(self, t: float | None, e: dict) -> None:
        if isinstance(e.get("ms"), (int, float)):
            self.s.tts_ok_ms.append(float(e["ms"]))

    def _on_tts_failed(self, t: float | None, e: dict) -> None:
        reason, status = str(e.get("reason") or "?"), e.get("status")
        self.s.tts_failed[f"{reason} ({status})" if status else reason] += 1

    def _on_tts_retry(self, t: float | None, e: dict) -> None:
        self.s.tts_retries += 1

    def _on_tts_disabled(self, t: float | None, e: dict) -> None:
        self.s.tts_disabled.append(str(e.get("reason") or "?"))


def collect(events: list[dict]) -> Stats:
    """The raw counts and samples from one session's events (pure)."""
    return _Session().run(events)


# ---------------------------------------------------------------------------
# Numbers and warnings
# ---------------------------------------------------------------------------


def summarize(s: Stats) -> dict[str, Any]:
    """The report's numbers from ``s`` (plain JSON; texts are left out)."""
    decided = sum(s.selections_by_role.values()) + sum(s.not_caught.values())
    replies = s.replies
    ranked = [r for r in replies if r["rank"] is not None]
    ranked_llm = [r for r in ranked if r["list"] == LLM_LIST]
    reqs = s.llm_requests
    outcomes = Counter(r["outcome"] for r in reqs)
    fallbacks = sum(outcomes.get(k, 0) for k in FALLBACK_OUTCOMES)
    tts_done = len(s.tts_ok_ms) + sum(s.tts_failed.values())
    marks = sum(s.marks.values())
    picks = s.selections_by_role.get("option", 0)
    summary = {
        "session": {
            "sessions": s.sessions, "events": s.events, "duration_s": s.duration_s,
            "keyboard_mode": s.keyboard_mode, "fake_decoder": s.fake_decoder,
            "decoder_names": dict(s.decoder_names), "screen_modes": dict(s.screen_modes),
            "pages": s.pages, "screens": s.screens,
        },
        "trials": {
            "started": s.trials, "confirm": s.confirm_trials,
            "selections": sum(s.selections_by_role.values()),
            "by_role": dict(s.selections_by_role), "by_source": dict(s.selections_by_source),
            "not_caught": dict(s.not_caught), "keyboard_idle": s.keyboard_idle,
            "not_confirmed": s.not_confirmed,
            "confirm_back": s.confirm_back, "stopped": dict(s.stop_reasons),
            "pick_rate": share(sum(s.selections_by_role.values()), decided),
            "option_picks": picks,
        },
        "marks": {"correct": s.marks.get("correct", 0), "wrong": s.marks.get("wrong", 0),
                  "correct_rate": share(s.marks.get("correct", 0), marks)},
        "replies": {
            "spoken": len(replies),
            "seconds": spread([r["seconds"] for r in replies if r["seconds"] is not None]),
            "mean_selections": _mean([r["selections"] for r in replies]),
            "mean_trials": _mean([r["trials"] for r in replies]),
            "option1": sum(1 for r in ranked if r["rank"] == 1), "ranked": len(ranked),
            "option1_rate": share(sum(1 for r in ranked if r["rank"] == 1), len(ranked)),
            # V3 Done-when: how often Claude's first option was the one spoken.
            "option1_llm": sum(1 for r in ranked_llm if r["rank"] == 1), "ranked_llm": len(ranked_llm),
            "option1_llm_rate": share(sum(1 for r in ranked_llm if r["rank"] == 1), len(ranked_llm)),
            "ranks": {str(k): v for k, v in sorted(Counter(r["rank"] for r in ranked).items())},
            "lists": dict(Counter(r["list"] for r in replies)),
            "with_audio": sum(1 for r in replies if r["audio"]),
        },
        "llm": {
            "requests": len(reqs), "by_reason": dict(Counter(r["reason"] for r in reqs)),
            "outcomes": dict(outcomes),
            "ok_ms": spread(s.llm_ok_ms),
            "answer_s": spread([r["answer_s"] for r in reqs if r["answer_s"] is not None]),
            "screen_s": spread([r["screen_s"] for r in reqs if r["screen_s"] is not None]),
            # Of the requests that decided a screen: how often the fixed list stood in.
            "fallback_rate": share(fallbacks, fallbacks + outcomes.get("shown", 0)),
            "attempt_failures": dict(s.llm_attempt_failures), "invalid": s.llm_invalid,
            "tokens_in": s.tokens_in, "tokens_out": s.tokens_out,
            "warm_failed": s.llm_warm_failed,
        },
        "tts": {
            "ok": len(s.tts_ok_ms), "ms": spread(s.tts_ok_ms), "failed": dict(s.tts_failed),
            "failed_rate": share(sum(s.tts_failed.values()), tts_done),
            "retries": s.tts_retries, "disabled": sorted(set(s.tts_disabled)),
            "disabled_sessions": len(s.tts_disabled),
            "speak_timeouts": s.speak_timeouts,
        },
        "partner": {
            "typed": s.partner_lines.get("typed", 0), "stt": s.partner_lines.get("stt", 0),
            "queued": s.partner_queued, "ignored_muted": s.stt_ignored_muted,
            "interrupted_trials": s.stop_reasons.get("context_changed", 0),
            "mic_reports": dict(s.mic_reports),
        },
        "health": dict(s.health),
    }
    summary["warnings"] = warnings(summary)
    return summary


def _mean(values: list[float | None]) -> float | None:
    xs = [v for v in values if v is not None]
    return sum(xs) / len(xs) if xs else None


def warnings(summary: dict[str, Any]) -> list[str]:
    """Things worth acting on before the next run, most important first."""
    out: list[str] = []
    sess, trials, llm, tts = summary["session"], summary["trials"], summary["llm"], summary["tts"]
    marks, replies, partner = summary["marks"], summary["replies"], summary["partner"]
    outcomes = llm["outcomes"]

    timed_out = outcomes.get("timed out", 0)
    if llm["requests"] >= MIN_COUNT and timed_out / llm["requests"] >= LLM_TIMEOUT_SHARE:
        out.append(f"{timed_out} of {llm['requests']} Claude pages timed out: the network is slow "
                   "(check ping and TLS setup, V6 preflight); the fixed lists were used instead")
    elif llm["requests"] >= MIN_COUNT and (llm["fallback_rate"] or 0) >= FALLBACK_SHARE:
        out.append(f"the fixed lists replaced Claude's page on {_pct(llm['fallback_rate'])} of "
                   "requests: see the Claude line for why")
    refused = [k for k in llm["attempt_failures"] if re.search(r"\((401|403|404)\)", k)]
    if refused:
        out.append(f"Claude rejected the request ({', '.join(refused)}): check ANTHROPIC_API_KEY "
                   "and llm.model")
    if llm["warm_failed"]:
        out.append(f"the Claude keep-alive failed {llm['warm_failed']} time(s): the connection "
                   "was dropping")

    # Keyboard mode without a decoder: a trial nobody answered is an idle partner, not an abstain.
    idle = trials["keyboard_idle"]
    keyboard_picks = trials["by_source"].get("keyboard", 0)
    if idle >= MIN_COUNT * 2 and idle / (idle + keyboard_picks) >= NOT_CAUGHT_SHARE:
        out.append(f"{idle} trials ended with no key pressed (keyboard mode): fine while idle, "
                   "otherwise press 1-4 during the flicker")
    not_caught = sum(trials["not_caught"].values()) - idle
    decided = trials["selections"] - keyboard_picks + not_caught
    if decided >= MIN_COUNT * 2 and not_caught / decided >= NOT_CAUGHT_SHARE:
        out.append(f"the decoder gave no answer on {not_caught} of {decided} trials: check the "
                   "headset contacts (settled 20 min, alpha check) and the decoder's window / "
                   "votes / margin (PLAN section 13, item 10)")
    unconfirmed = trials["confirm_back"] + trials["not_confirmed"]
    if trials["option_picks"] >= MIN_COUNT and unconfirmed / trials["option_picks"] >= UNCONFIRMED_SHARE:
        out.append(f"{unconfirmed} of {trials['option_picks']} picks were not confirmed (Back or "
                   "timeout): the first pick is often wrong, or Say it is hard to hit")

    judged = marks["correct"] + marks["wrong"]
    if judged >= MIN_COUNT and marks["wrong"] / judged >= WRONG_SHARE:
        out.append(f"{marks['wrong']} of {judged} spoken replies were marked wrong")
    real_decoder = trials["by_source"].get("decoder") and not sess["fake_decoder"] and not any(
        is_fake_name(name) for name in sess["decoder_names"])
    if real_decoder and replies["spoken"] and not judged:
        out.append("no C/W marks: press C or W after each spoken reply so accuracy is measured (V5)")

    if tts["disabled"]:
        where = (f" in {tts['disabled_sessions']} of {sess['sessions']} sessions"
                 if sess["sessions"] > 1 else "")
        out.append(f"Cartesia was off{where} ({'; '.join(tts['disabled'])}): browser voice only")
    if any("(429)" in k for k in tts["failed"]):
        out.append("Cartesia refused clips with HTTP 429 (concurrency limit): keep "
                   "speech.max_concurrent at the plan limit")
    failed = sum(tts["failed"].values())
    if failed + tts["ok"] >= MIN_COUNT and (tts["failed_rate"] or 0) >= TTS_FAILED_SHARE:
        out.append(f"{failed} of {failed + tts['ok']} Cartesia clips failed: those replies used "
                   "the browser voice")
    if tts["speak_timeouts"]:
        out.append(f"{tts['speak_timeouts']} reply(ies) never reported tts_done (speech.max_speak_s"
                   "): check the partner page's audio output")

    bad_mic = sorted(k for k in partner["mic_reports"] if k in BAD_MIC)
    if bad_mic:
        out.append(f"speech recognition reported {', '.join(bad_mic)}: check the mic permission "
                   "and internet in Chrome (typing still works)")
    health = summary["health"]
    for key, hint in (("decoder lost mid-session", "the bridge disconnected during a session"),
                      ("baseline timeouts", "the bridge did not answer baseline in time"),
                      ("invalid messages", "a client sent messages that failed the schema"),
                      ("stale selections", "the decoder answered trials that had already ended"),
                      ("selections of an inactive slot", "the decoder picked a slot that was "
                       "not flickering")):
        if health.get(key):
            out.append(f"{hint} ({health[key]}x)")
    return out


# ---------------------------------------------------------------------------
# Text report
# ---------------------------------------------------------------------------


def _pct(rate: float | None) -> str:
    return "-" if rate is None else f"{rate * 100:.0f} %"


def _ratio(part: int, whole: int) -> str:
    return f"{part}/{whole} ({_pct(share(part, whole))})" if whole else f"{part}/0"


def _num(value: float | None, unit: str = "", digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}{unit}"


def _counts(counter: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(counter.items(), key=lambda kv: -kv[1])) or "none"


def _duration(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:.0f} s"
    if seconds < 7200:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def _spread_text(sp: dict[str, Any], unit: str, digits: int) -> str:
    if not sp["n"]:
        return "-"
    return (f"median {_num(sp['median'], unit, digits)}, p95 {_num(sp['p95'], unit, digits)} "
            f"(n={sp['n']})")


def mode_text(sess: dict[str, Any]) -> str:
    """Keyboard / fake / real decoder, as far as the log tells."""
    parts = []
    if sess["keyboard_mode"]:
        parts.append("keyboard mode")
    names = sorted(sess["decoder_names"])
    if sess["fake_decoder"] or any(is_fake_name(n) for n in names):
        parts.append("fake decoder")
    elif names:
        parts.append(f"decoder {', '.join(names)}")
    elif not sess["keyboard_mode"]:
        parts.append("no decoder connected")
    modes = [m for m in sess["screen_modes"] if m != "confirm"]
    if modes:
        parts.append(" + ".join(f"{m} mode" for m in sorted(modes)))
    return ", ".join(parts)


def format_report(title: str, summary: dict[str, Any]) -> str:
    """A short human-readable report of one ``summarize`` result."""
    sess, tr, mk = summary["session"], summary["trials"], summary["marks"]
    rp, llm, tts, pt = summary["replies"], summary["llm"], summary["tts"], summary["partner"]
    lines = [f"== {title} ==",
             f"Session   {_duration(sess['duration_s'])}, {sess['events']} events; {mode_text(sess)}"
             f"; {sess['pages']} pages, {sess['screens']} screens shown"]
    if not tr["started"] and not rp["spoken"] and not llm["requests"]:
        lines.append("          (no trials, replies or Claude pages)")
        return "\n".join(lines + _warning_lines(summary))
    caught = sum(tr["not_caught"].values())
    other_stops = {k: v for k, v in tr["stopped"].items() if k not in ("selected", *tr["not_caught"])}
    lines += [
        f"Trials    {tr['started']} started ({tr['confirm']} confirm); selected {tr['selections']}"
        f" ({_counts(tr['by_role'])}; by {_counts(tr['by_source'])}); not caught {caught}"
        + (f" ({_counts(tr['not_caught'])})" if caught else ""),
        f"          ended in a pick {_ratio(tr['selections'], tr['selections'] + caught)}; "
        f"confirm: back {tr['confirm_back']}, timed out {tr['not_confirmed']}"
        + (f"; also stopped by {_counts(other_stops)}" if other_stops else ""),
    ]
    if mk["correct"] + mk["wrong"]:
        lines.append(f"Marks     correct {mk['correct']}, wrong {mk['wrong']} "
                     f"({_pct(mk['correct_rate'])} correct)")
    else:
        lines.append("Marks     none (C / W after a spoken reply)")
    if rp["spoken"]:
        lines += [
            f"Replies   {rp['spoken']} spoken; seconds per reply "
            f"{_spread_text(rp['seconds'], ' s', 1)}; {_num(rp['mean_selections'])} selections / "
            f"{_num(rp['mean_trials'])} trials per reply",
            f"          option 1: {_ratio(rp['option1'], rp['ranked'])} (on Claude's pages "
            f"{_ratio(rp['option1_llm'], rp['ranked_llm'])}); ranks "
            f"{_counts(rp['ranks'])}; from {_counts(rp['lists'])}",
        ]
    else:
        lines.append("Replies   none spoken")
    if llm["requests"]:
        lines += [
            f"Claude    {llm['requests']} requests ({_counts(llm['by_reason'])}): "
            f"{_counts(llm['outcomes'])}; fallback {_pct(llm['fallback_rate'])}",
            f"          llm_ok {_spread_text(llm['ok_ms'], ' ms', 0)}; request to answer "
            f"{_spread_text(llm['answer_s'], ' s', 1)}",
            f"          request to screen or give-up {_spread_text(llm['screen_s'], ' s', 1)}; "
            f"tokens in {llm['tokens_in']}, out {llm['tokens_out']}"
            + (f"; failed attempts {_counts(llm['attempt_failures'])}" if llm["attempt_failures"] else "")
            + (f"; invalid {llm['invalid']}" if llm["invalid"] else ""),
        ]
    else:
        lines.append("Claude    no requests")
    failed = sum(tts["failed"].values())
    if tts["ok"] or failed or rp["spoken"]:
        lines.append(
            f"Cartesia  clips ok {tts['ok']} ({_spread_text(tts['ms'], ' ms', 0)}), failed {failed}"
            + (f" ({_counts(tts['failed'])})" if failed else "")
            + f", retries {tts['retries']}; replies with a clip {rp['with_audio']}, "
            f"browser voice {rp['spoken'] - rp['with_audio']}")
    else:
        lines.append("Cartesia  no clips")
    lines.append(
        f"Partner   typed {pt['typed']}, spoken {pt['stt']} (queued {pt['queued']}, ignored while "
        f"muted {pt['ignored_muted']}); trials interrupted {pt['interrupted_trials']}; "
        f"mic {_counts(pt['mic_reports'])}")
    if summary["health"]:
        lines.append(f"Health    {_counts(summary['health'])}")
    return "\n".join(lines + _warning_lines(summary))


def _warning_lines(summary: dict[str, Any]) -> list[str]:
    if not summary["warnings"]:
        return []
    return ["Warnings"] + [f"  - {w}" for w in summary["warnings"]]


def format_texts(stats: Stats) -> str:
    """The partner lines and spoken replies, in order (only with --show-text)."""
    lines = []
    for t, who, text in stats.texts:
        stamp = time.strftime("%H:%M:%S", time.localtime(t)) if t is not None else "--:--:--"
        lines.append(f"  {stamp}  {who}: {redact(text)}")
    return "\n".join(["Conversation"] + (lines or ["  (none)"]))


def redact(text: str) -> str:
    return SECRET.sub("<redacted>", text)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def find_logs(paths: list[Path], log_dir: Path, every: bool) -> list[Path]:
    """The logs to read: the given files (a directory means its logs), else the newest or all."""
    if paths:
        found: list[Path] = []
        for p in paths:
            found += sorted(p.glob("*.jsonl")) if p.is_dir() else [p]
        return found
    logs = sorted(log_dir.glob("*.jsonl"))  # names start with the date and time
    if every:
        return logs
    started = [p for p in logs if p.stat().st_size > 0]  # a server that just started is empty
    return (started or logs)[-1:]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="log_summary", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("logs", nargs="*", type=Path, help="session logs (default: the newest)")
    p.add_argument("--all", action="store_true", help="every session in the log directory")
    p.add_argument("--json", action="store_true", help="print the numbers as JSON")
    p.add_argument("--show-text", action="store_true",
                   help="also print the partner lines and spoken replies")
    p.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help="default: logs/")
    args = p.parse_args(argv)

    paths = find_logs(args.logs, args.log_dir, args.all)
    if not paths:
        print(f"no session logs in {args.log_dir}", file=sys.stderr)
        return 2
    status = 0
    sessions: list[tuple[Path, Stats]] = []
    for path in paths:
        try:
            sessions.append((path, collect(read_events(path))))
        except (OSError, ValueError) as exc:  # json.JSONDecodeError is a ValueError
            print(f"{path}: could not read ({type(exc).__name__}: {exc})", file=sys.stderr)
            status = 1
    if not sessions:
        return status or 2
    total = merge([s for _, s in sessions]) if len(sessions) > 1 else None

    if args.json:
        out: dict[str, Any] = {"sessions": []}
        for path, stats in sessions:
            entry = {"log": str(path), **summarize(stats)}
            if args.show_text:
                entry["texts"] = [{"t_unix": t, "who": w, "text": redact(x)} for t, w, x in stats.texts]
            out["sessions"].append(entry)
        if total is not None:
            out["total"] = summarize(total)
        print(redact(json.dumps(out, indent=2, ensure_ascii=False)))
        return status

    blocks = []
    for path, stats in sessions:
        block = format_report(path.stem, summarize(stats))
        if args.show_text:
            block += "\n" + format_texts(stats)
        blocks.append(block)
    if total is not None:
        blocks.append(format_report(f"total: {total.sessions} sessions", summarize(total)))
    print(redact("\n\n".join(blocks)))
    return status


if __name__ == "__main__":
    sys.exit(main())
