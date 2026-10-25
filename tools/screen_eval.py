"""Score BrainPoint's Haiku element detection against the page's own clickable elements.

    python -m tools.screen_eval data/screens --with-hints               # every page, hints sent
    python -m tools.screen_eval data/screens --no-hints --limit 5       # first 5 pages, image only
    python -m tools.screen_eval data/screens --with-hints --env-file .env --out run1.json
    python -m tools.screen_eval --ax data/axdumps --env-file .env       # desktop AX ranking

The dataset (tools/screen_capture.md) is a folder of pages: ``<name>.jpg`` (or .jpeg, .png,
.webp) plus ``<name>.json`` holding ``{url, title, viewport: {w, h, dpr}, image: {w, h},
hints: [{id, x, y, w, h, role, text, tag}, ...]}``. The hints are in viewport CSS pixels and
come from the same logic as the extension's hints.js, so they are the ground truth: the
clickable elements the page itself declares. Each page goes through ``Detector.detect`` once,
in name order. With ``--with-hints`` the hints are sent along, as the extension does; with
``--no-hints`` the model sees only the image (the ground truth is the same either way).

Metrics, per page and in total ("buttons" are predicted elements with p_button >= 0.5,
``--min-p-button``; a point on a box edge counts as inside):

    recall      the share of ground-truth boxes that contain the centre of some button
    precision   the share of buttons whose centre lies inside some ground-truth box
    top3        the precision of the first page top-K shows: the 3 buttons ranked highest by
                p_button * p_next, as the selector ranks them
    recall@cap  recall out of at most ``max_elements`` boxes (the Detector's cap, 15): a
                detector asked for 15 elements can hit at most 15 of a page's 164 links
    hint_id     with hints only: of the predicted elements that name a hint_id, the share
                whose label shares a word with that hint's text or whose centre lies inside
                that hint's box. Detector snaps a valid hint_id to the hint's exact box, so
                the box test passes for any hint that exists; ``hint_label`` is the stricter
                label-only share.
    latency     Detection.ms, median and p90 over the pages that worked
    cost        input and output tokens at $0.10 and $0.50 per million (Haiku list price,
                no cache discount)

Totals pool the boxes of every page (``recall`` = all boxes hit / all boxes); ``*_page_mean``
is the plain mean of the per-page values. A page whose detection fails is kept as a row with
``ok: false`` and its error, and is left out of the metrics. A page whose files are broken is
listed under ``skipped`` with the reason. The report goes to stdout and the full result (every
row, every total, the settings) to the ``--out`` JSON file, by default
``<DIR>/results/<with|no>-hints.json``. Ctrl-C stops after the current page and still writes
what was scored.

``--env-file`` reads KEY=VALUE lines (``#`` comments, an optional ``export`` and matching
quotes are allowed) into the environment without overriding variables already set, so
ANTHROPIC_API_KEY can live in a gitignored file. Values are never printed or logged. Exit
codes: 0 when at least one page was scored, 1 when none could be (no API key, every detection
failed, the output cannot be written), 2 for bad arguments or an unusable dataset. Each
detection is a paid API call: about $0.001 per page.

``--ax DIR`` scores ``Detector.rank`` (protocol v3.1, the text-only ranking of a desktop
window's accessibility tree) instead. DIR holds ``<bundle id>.json`` dumps written by
``bp-probe axdump``: ``{app: {bundle_id, name, pid}, window: {title, frame: {x, y, w, h}},
items: [{id, role, subrole, label, frame: {x, y, w, h}, enabled, actions}], walk_ms,
truncated}``, frames in global screen points. Each dump becomes the scan hints a desktop
client sends (window-relative points, the first 200 items, role = AX role, tag = subrole,
text = label) and is ranked once, in name order. There is no ground truth for what the user
wants next, so the report measures the cost and the sanity of the answer: latency (median,
p90), tokens and cost, ``n_elements`` returned, and ``pressable``, the share of returned
elements whose item is enabled and offers a press action (AXPress, AXPick, AXConfirm,
AXShowMenu or AXOpen). The full result goes to ``--out``, by default
``<DIR>/results/ax-rank.json``. Exit codes are as above.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    # Run as a plain script: make the repo root importable so `brainsync` resolves.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brainsync.envfile import ENV_KEY, load_env_file  # noqa: E402  (re-exported)

PRICE_IN_PER_M = 0.10
PRICE_OUT_PER_M = 0.50
MIN_P_BUTTON = 0.5
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp"}
_WORD = re.compile(r"\w+")


def _note(text: str) -> None:
    print(f"screen_eval: {text}", file=sys.stderr, flush=True)


# -- the dataset ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Page:
    """One captured page: the screenshot's path and the JSON's fields, checked."""
    name: str
    image_path: Path
    media_type: str
    url: str
    title: str
    viewport_w: float
    viewport_h: float
    image_w: int
    image_h: int
    hints: tuple[dict, ...] = field(default=())

    def truth(self) -> list[tuple[float, float, float, float]]:
        """The ground-truth boxes: the hints as normalized viewport (x, y, w, h), sizes > 0."""
        return [(h["x"] / self.viewport_w, h["y"] / self.viewport_h,
                 h["w"] / self.viewport_w, h["h"] / self.viewport_h)
                for h in self.hints if h["w"] > 0 and h["h"] > 0]

    def hint_by_id(self) -> dict[int, dict]:
        """Hints keyed by their ``id`` (their index when a hint has no id)."""
        return {int(h.get("id", i)): h for i, h in enumerate(self.hints)}


def _number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _size(obj: Any, what: str, where: Path) -> tuple[float, float]:
    if not (isinstance(obj, dict) and _number(obj.get("w")) and _number(obj.get("h"))
            and obj["w"] > 0 and obj["h"] > 0):
        raise ValueError(f"{where.name}: {what} must be an object with positive numbers w and h; "
                         f"recapture the page as tools/screen_capture.md describes")
    return obj["w"], obj["h"]


def load_page(json_path: str | Path) -> Page:
    """Read ``<name>.json`` and find its image next to it; ValueError says what is wrong."""
    json_path = Path(json_path)
    try:
        meta = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{json_path.name}: cannot read it as JSON ({exc}); fix or delete "
                         f"the file") from None
    if not isinstance(meta, dict):
        raise ValueError(f"{json_path.name}: the top level must be an object with url, title, "
                         f"viewport, image and hints")
    images = [json_path.with_suffix(ext) for ext in IMAGE_TYPES
              if json_path.with_suffix(ext).is_file()]
    if not images:
        raise ValueError(f"{json_path.name}: no image next to it; save the screenshot as "
                         f"{json_path.stem}.jpg")
    image = images[0]
    if image.stat().st_size == 0:
        raise ValueError(f"{image.name}: the image is empty; recapture the page")
    vw, vh = _size(meta.get("viewport"), "viewport", json_path)
    iw, ih = _size(meta.get("image"), "image", json_path)
    if iw != int(iw) or ih != int(ih):
        raise ValueError(f"{json_path.name}: image w and h are pixel counts and must be whole "
                         f"numbers, got {iw} x {ih}")
    hints = meta.get("hints", [])
    if not isinstance(hints, list):
        raise ValueError(f"{json_path.name}: hints must be a list (use [] for none)")
    for i, h in enumerate(hints):
        if not (isinstance(h, dict) and all(_number(h.get(k)) for k in ("x", "y", "w", "h"))):
            raise ValueError(f"{json_path.name}: hint {i} needs numbers x, y, w and h in "
                             f"viewport CSS pixels")
        if "id" in h and not (isinstance(h["id"], int) and not isinstance(h["id"], bool)):
            raise ValueError(f"{json_path.name}: hint {i} has an id that is not an integer")
    ids = [int(h.get("id", i)) for i, h in enumerate(hints)]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{json_path.name}: two hints share an id; give each hint its own id")
    url, title = meta.get("url", ""), meta.get("title", "")
    return Page(name=json_path.stem, image_path=image, media_type=IMAGE_TYPES[image.suffix],
                url=url if isinstance(url, str) else "",
                title=title if isinstance(title, str) else "",
                viewport_w=vw, viewport_h=vh, image_w=int(iw), image_h=int(ih),
                hints=tuple(hints))


def load_dataset(directory: str | Path, limit: int | None = None
                 ) -> tuple[list[Page], list[dict]]:
    """Pages of ``directory`` in name order (the first ``limit``), and the skipped files.

    A broken page is skipped with a note and a ``{"name", "reason"}`` entry. ValueError when
    the folder is missing or holds no usable page.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError(f"{directory} is not a folder; pass the dataset folder "
                         f"(data/screens, see tools/screen_capture.md)")
    if limit is not None and limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")
    pages: list[Page] = []
    skipped: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        if limit is not None and len(pages) >= limit:
            break
        try:
            pages.append(load_page(path))
        except ValueError as exc:
            _note(f"skipped {path.name}: {exc}")
            skipped.append({"name": path.stem, "reason": str(exc)})
    if not pages:
        raise ValueError(f"no usable page in {directory}: it needs <name>.json files with a "
                         f"<name>.jpg beside each (see tools/screen_capture.md)")
    return pages, skipped


# -- scoring --------------------------------------------------------------------------------

def _inside(px: float, py: float, box: Sequence[float]) -> bool:
    x, y, w, h = box
    return x <= px <= x + w and y <= py <= y + h


def _center(el: Any) -> tuple[float, float]:
    return el.x + el.w / 2, el.y + el.h / 2


def labels_overlap(label: str, text: str) -> bool:
    """True when the two strings share a word, ignoring case (empty strings never do)."""
    a, b = set(_WORD.findall(label.casefold())), set(_WORD.findall(text.casefold()))
    return bool(a & b)


def cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Estimated price of the tokens at the Haiku list price, without cache discounts."""
    return (input_tokens * PRICE_IN_PER_M + output_tokens * PRICE_OUT_PER_M) / 1e6


def percentile(values: Sequence[float], q: float) -> float | None:
    """The q-th percentile (0..100) with linear interpolation; None for no values."""
    if not values:
        return None
    s = sorted(values)
    pos = (len(s) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def score_page(page: Page, detection: Any, *, with_hints: bool,
               min_p_button: float = MIN_P_BUTTON, cap: int | None = None) -> dict:
    """One page's row: counts, recall, precision, hint_id accuracy, latency, tokens, cost.

    ``detection`` needs ``elements`` (each with label, x, y, w, h normalized to the viewport,
    hint_id and p_button), ``ms``, ``input_tokens`` and ``output_tokens``.
    """
    truth = page.truth()
    buttons = [el for el in detection.elements if el.p_button >= min_p_button]
    centers = [_center(el) for el in buttons]
    truth_hit = sum(any(_inside(cx, cy, box) for cx, cy in centers) for box in truth)
    pred_hit = sum(any(_inside(cx, cy, box) for box in truth) for cx, cy in centers)
    # The selector's ranking (stable: the detector's order breaks ties).
    top3 = sorted(buttons, key=lambda el: -el.p_button * getattr(el, "p_next", 1.0))[:3]
    top3_hit = sum(any(_inside(*_center(el), box) for box in truth) for el in top3)
    row: dict[str, Any] = {
        "name": page.name, "ok": True, "url": page.url,
        "n_truth": len(truth), "n_elements": len(detection.elements),
        "n_buttons": len(buttons), "truth_hit": truth_hit, "buttons_hit": pred_hit,
        "recall": _ratio(truth_hit, len(truth)), "precision": _ratio(pred_hit, len(buttons)),
        "n_top3": len(top3), "top3_hit": top3_hit, "top3_precision": _ratio(top3_hit, len(top3)),
        # nested boxes (a link inside a list item) let one centre hit several, so the hits
        # are capped too, keeping the share <= 1
        "recall_capped": (_ratio(min(truth_hit, cap), min(len(truth), cap)) if cap else None),
        "n_hint_ids": None, "hint_id_ok": None, "hint_label_ok": None,
        "hint_id_accuracy": None, "hint_label_accuracy": None,
        "ms": int(detection.ms), "input_tokens": int(detection.input_tokens),
        "output_tokens": int(detection.output_tokens),
        "cost_usd": cost_usd(detection.input_tokens, detection.output_tokens),
    }
    if with_hints:
        by_id = page.hint_by_id()
        named = [el for el in detection.elements if el.hint_id is not None]
        id_ok = label_ok = 0
        for el in named:
            hint = by_id.get(el.hint_id)
            if hint is None:
                continue
            by_label = labels_overlap(el.label, str(hint.get("text") or ""))
            box = (hint["x"] / page.viewport_w, hint["y"] / page.viewport_h,
                   hint["w"] / page.viewport_w, hint["h"] / page.viewport_h)
            label_ok += by_label
            id_ok += by_label or _inside(*_center(el), box)
        row.update(n_hint_ids=len(named), hint_id_ok=id_ok, hint_label_ok=label_ok,
                   hint_id_accuracy=_ratio(id_ok, len(named)),
                   hint_label_accuracy=_ratio(label_ok, len(named)))
    return row


def _mean(values: Sequence[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def summarize(rows: Sequence[dict]) -> dict:
    """Totals over the rows: pooled and per-page-mean metrics, latency, tokens and cost."""
    good = [r for r in rows if r["ok"]]
    total = {
        "n_pages": len(rows), "n_ok": len(good), "n_failed": len(rows) - len(good),
        "n_truth": sum(r["n_truth"] for r in good),
        "n_buttons": sum(r["n_buttons"] for r in good),
        "n_elements": sum(r["n_elements"] for r in good),
    }
    truth_hit = sum(r["truth_hit"] for r in good)
    buttons_hit = sum(r["buttons_hit"] for r in good)
    with_ids = [r for r in good if r["n_hint_ids"] is not None]
    n_ids = sum(r["n_hint_ids"] for r in with_ids)
    ms = [r["ms"] for r in good]
    tokens_in = sum(r["input_tokens"] for r in good)
    tokens_out = sum(r["output_tokens"] for r in good)
    total.update(
        recall=_ratio(truth_hit, total["n_truth"]),
        precision=_ratio(buttons_hit, total["n_buttons"]),
        recall_page_mean=_mean([r["recall"] for r in good]),
        precision_page_mean=_mean([r["precision"] for r in good]),
        # .get: rows scored before these metrics existed (an older --out file) lack them
        top3_precision=_ratio(sum(r.get("top3_hit", 0) for r in good),
                              sum(r.get("n_top3", 0) for r in good)),
        recall_capped_page_mean=_mean([r.get("recall_capped") for r in good]),
        n_hint_ids=n_ids if with_ids else None,
        hint_id_accuracy=(_ratio(sum(r["hint_id_ok"] for r in with_ids), n_ids)
                          if with_ids else None),
        hint_label_accuracy=(_ratio(sum(r["hint_label_ok"] for r in with_ids), n_ids)
                             if with_ids else None),
        ms_median=percentile(ms, 50), ms_p90=percentile(ms, 90),
        input_tokens=tokens_in, output_tokens=tokens_out,
        cost_usd=cost_usd(tokens_in, tokens_out),
        cost_usd_per_page=cost_usd(tokens_in, tokens_out) / len(good) if good else None,
    )
    return total


def evaluate(pages: Sequence[Page], detector: Any, *, with_hints: bool,
             min_p_button: float = MIN_P_BUTTON,
             progress: Callable[[dict], None] | None = None) -> dict:
    """Run ``detector.detect`` on every page and score it: ``{"pages", "totals", "interrupted"}``.

    A detection that raises becomes a row with ``ok: false`` and the error. Ctrl-C stops
    after the page in progress and returns what was scored, with ``interrupted`` true.
    ``progress`` is called with each row as it is made.
    """
    rows: list[dict] = []
    interrupted = False
    cap = getattr(detector, "max_elements", None)
    cap = cap if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0 else None
    for page in pages:
        try:
            data = base64.b64encode(page.image_path.read_bytes()).decode("ascii")
            t0 = time.perf_counter()
            try:
                det = detector.detect(data, page.media_type, page.image_w, page.image_h,
                                      page.viewport_w, page.viewport_h,
                                      hints=list(page.hints) if with_hints else (),
                                      history=(), url=page.url, title=page.title)
                row = score_page(page, det, with_hints=with_hints, min_p_button=min_p_button,
                                 cap=cap)
                row["model"] = getattr(det, "model", None)
                row["summary"] = str(getattr(det, "summary", ""))[:200]
            except KeyboardInterrupt:
                raise
            except Exception as exc:   # DetectError, an SDK error, a broken stub: keep going
                row = {"name": page.name, "ok": False, "url": page.url,
                       "error": f"{type(exc).__name__}: {exc}"[:500],
                       "wall_ms": int((time.perf_counter() - t0) * 1000)}
        except KeyboardInterrupt:
            interrupted = True
            _note(f"interrupted at {page.name}; reporting the {len(rows)} page(s) done")
            break
        except OSError as exc:   # the image vanished or is unreadable since loading
            row = {"name": page.name, "ok": False, "url": page.url,
                   "error": f"cannot read {page.image_path.name}: {exc}"}
        rows.append(row)
        if progress is not None:
            progress(row)
    return {"pages": rows, "totals": summarize(rows), "interrupted": interrupted}


# -- AX dumps (protocol v3.1) -----------------------------------------------------------------

PRESS_ACTIONS = frozenset({"AXPress", "AXPick", "AXConfirm", "AXShowMenu", "AXOpen"})
MAX_AX_HINTS = 200               # what a desktop client sends at most (PROTOCOL-v3 MAX_HINTS)


@dataclass(frozen=True)
class AxDump:
    """One window's accessibility dump, as the scan hints a desktop client would send."""
    name: str
    app: dict
    window_title: str
    viewport_w: float
    viewport_h: float
    hints: tuple[dict, ...]
    n_items: int
    walk_ms: float | None
    truncated: bool

    def hint_by_id(self) -> dict[int, dict]:
        return {h["id"]: h for h in self.hints}


def _frame(obj: Any, what: str, where: Path) -> tuple[float, float, float, float]:
    if not (isinstance(obj, dict) and all(_number(obj.get(k)) for k in ("x", "y", "w", "h"))):
        raise ValueError(f"{where.name}: {what} must be an object with numbers x, y, w and h "
                         f"in screen points; re-run bp-probe axdump")
    return obj["x"], obj["y"], obj["w"], obj["h"]


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def load_ax_dump(json_path: str | Path) -> AxDump:
    """Read one ``bp-probe axdump`` file as scan hints; ValueError says what is wrong."""
    json_path = Path(json_path)
    try:
        meta = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{json_path.name}: cannot read it as JSON ({exc}); fix or delete "
                         f"the file") from None
    if not isinstance(meta, dict) or not isinstance(meta.get("app"), dict):
        raise ValueError(f"{json_path.name}: the top level must be an object with app, window "
                         f"and items (a bp-probe axdump file)")
    window = meta.get("window")
    if not isinstance(window, dict):
        raise ValueError(f"{json_path.name}: window must be an object with title and frame")
    wx, wy, ww, wh = _frame(window.get("frame"), "window.frame", json_path)
    if ww <= 0 or wh <= 0:
        raise ValueError(f"{json_path.name}: the window frame has no area ({ww} x {wh})")
    items = meta.get("items")
    if not isinstance(items, list):
        raise ValueError(f"{json_path.name}: items must be a list (use [] for none)")
    hints: list[dict] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or isinstance(item.get("id"), bool) \
                or not isinstance(item.get("id"), int):
            raise ValueError(f"{json_path.name}: item {i} needs an integer id")
        x, y, w, h = _frame(item.get("frame"), f"item {i} frame", json_path)
        actions = item.get("actions")
        hints.append({"id": item["id"], "x": x - wx, "y": y - wy, "w": w, "h": h,
                      "role": _text(item.get("role")), "text": _text(item.get("label")),
                      "tag": _text(item.get("subrole")),
                      "actions": [a for a in actions if isinstance(a, str)]
                      if isinstance(actions, list) else [],
                      "enabled": item.get("enabled") is not False})
    ids = [h["id"] for h in hints]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{json_path.name}: two items share an id; give each item its own id")
    app = meta["app"]
    pid = app.get("pid")
    walk_ms = meta.get("walk_ms")
    return AxDump(name=json_path.stem,
                  app={"bundle_id": _text(app.get("bundle_id")), "name": _text(app.get("name")),
                       "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else 0},
                  window_title=_text(window.get("title")), viewport_w=ww, viewport_h=wh,
                  hints=tuple(hints[:MAX_AX_HINTS]), n_items=len(items),
                  walk_ms=float(walk_ms) if _number(walk_ms) else None,
                  truncated=meta.get("truncated") is True)


def load_ax_dataset(directory: str | Path, limit: int | None = None
                    ) -> tuple[list[AxDump], list[dict]]:
    """The dumps of ``directory`` in name order (the first ``limit``), and the skipped files.
    ValueError when the folder is missing or holds no usable dump."""
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError(f"{directory} is not a folder; pass the folder bp-probe axdump writes "
                         f"to (data/axdumps)")
    if limit is not None and limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")
    dumps: list[AxDump] = []
    skipped: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        if limit is not None and len(dumps) >= limit:
            break
        try:
            dumps.append(load_ax_dump(path))
        except ValueError as exc:
            _note(f"skipped {path.name}: {exc}")
            skipped.append({"name": path.stem, "reason": str(exc)})
    if not dumps:
        raise ValueError(f"no usable dump in {directory}: it needs <bundle id>.json files from "
                         f"bp-probe axdump")
    return dumps, skipped


def pressable(hint: dict | None) -> bool:
    """True when the item is enabled and offers a press action."""
    return (hint is not None and hint.get("enabled") is not False
            and bool(PRESS_ACTIONS.intersection(hint.get("actions") or ())))


def score_ax(dump: AxDump, detection: Any) -> dict:
    """One dump's row: counts, the pressable share, latency, tokens and cost."""
    by_id = dump.hint_by_id()
    elements = list(detection.elements)
    n_pressable = sum(pressable(by_id.get(el.hint_id)) for el in elements)
    return {
        "name": dump.name, "ok": True, "app": dump.app.get("name", ""),
        "bundle_id": dump.app.get("bundle_id", ""), "window_title": dump.window_title,
        "n_items": dump.n_items, "n_hints": len(dump.hints),
        "n_hints_pressable": sum(pressable(h) for h in dump.hints),
        "walk_ms": dump.walk_ms, "truncated": dump.truncated,
        "n_elements": len(elements), "n_pressable": n_pressable,
        "pressable": _ratio(n_pressable, len(elements)),
        "n_risky": sum(bool(getattr(el, "risky", False)) for el in elements),
        "top3": [str(el.label)[:60] for el in elements[:3]],
        "ms": int(detection.ms), "input_tokens": int(detection.input_tokens),
        "output_tokens": int(detection.output_tokens),
        "cost_usd": cost_usd(detection.input_tokens, detection.output_tokens),
        "cached": bool(getattr(detection, "cached", False)),
    }


def summarize_ax(rows: Sequence[dict]) -> dict:
    """Totals over the ax rows: latency, tokens, cost, elements and the pressable share."""
    good = [r for r in rows if r["ok"]]
    ms = [r["ms"] for r in good]
    tokens_in = sum(r["input_tokens"] for r in good)
    tokens_out = sum(r["output_tokens"] for r in good)
    n_elements = sum(r["n_elements"] for r in good)
    return {
        "n_dumps": len(rows), "n_ok": len(good), "n_failed": len(rows) - len(good),
        "n_hints": sum(r["n_hints"] for r in good), "n_elements": n_elements,
        "n_elements_mean": n_elements / len(good) if good else None,
        "pressable": _ratio(sum(r["n_pressable"] for r in good), n_elements),
        "pressable_page_mean": _mean([r["pressable"] for r in good]),
        "ms_median": percentile(ms, 50), "ms_p90": percentile(ms, 90),
        "input_tokens": tokens_in, "output_tokens": tokens_out,
        "cost_usd": cost_usd(tokens_in, tokens_out),
        "cost_usd_per_dump": cost_usd(tokens_in, tokens_out) / len(good) if good else None,
    }


def evaluate_ax(dumps: Sequence[AxDump], detector: Any,
                progress: Callable[[dict], None] | None = None) -> dict:
    """Run ``detector.rank`` on every dump: ``{"dumps", "totals", "interrupted"}``.

    A ranking that raises becomes a row with ``ok: false`` and the error; Ctrl-C stops after
    the dump in progress and returns what was scored.
    """
    rows: list[dict] = []
    interrupted = False
    for dump in dumps:
        t0 = time.perf_counter()
        try:
            det = detector.rank(list(dump.hints), dump.viewport_w, dump.viewport_h,
                                app=dict(dump.app), window_title=dump.window_title,
                                history=(), url="")
            row = score_ax(dump, det)
            row["model"] = getattr(det, "model", None)
            row["summary"] = str(getattr(det, "summary", ""))[:200]
        except KeyboardInterrupt:
            interrupted = True
            _note(f"interrupted at {dump.name}; reporting the {len(rows)} dump(s) done")
            break
        except Exception as exc:   # DetectError, an SDK error, a broken stub: keep going
            row = {"name": dump.name, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:500],
                   "wall_ms": int((time.perf_counter() - t0) * 1000)}
        rows.append(row)
        if progress is not None:
            progress(row)
    return {"dumps": rows, "totals": summarize_ax(rows), "interrupted": interrupted}


def format_ax_row(row: dict) -> str:
    """One line per dump for the report."""
    if not row["ok"]:
        return f"{row['name'][:32]:32}  FAILED  {row['error'][:90]}"
    return (f"{row['name'][:32]:32}  hints {row['n_hints']:3}  elements {row['n_elements']:2}  "
            f"pressable {_pct(row['pressable'])}  {row['ms']:5} ms  "
            f"{row['input_tokens']:6} in {row['output_tokens']:5} out  ${row['cost_usd']:.4f}")


def format_ax_totals(totals: dict) -> str:
    """The summary block printed after the ax rows."""
    t = totals
    ms = ("-" if t["ms_median"] is None
          else f"median {t['ms_median']:.0f} ms, p90 {t['ms_p90']:.0f} ms")
    mean = "-" if t["n_elements_mean"] is None else f"{t['n_elements_mean']:.1f}"
    per = "" if t["cost_usd_per_dump"] is None else f", ${t['cost_usd_per_dump']:.4f}/dump"
    return "\n".join([
        f"dumps      {t['n_ok']} ranked, {t['n_failed']} failed",
        f"elements   {t['n_elements']} returned ({mean} per dump) from {t['n_hints']} hints",
        f"pressable  {_pct(t['pressable']).strip()} of the returned elements are enabled and "
        f"pressable (dump mean {_pct(t['pressable_page_mean']).strip()})",
        f"latency    {ms}",
        f"tokens     {t['input_tokens']} in, {t['output_tokens']} out: "
        f"${t['cost_usd']:.4f}{per}"])


# -- the report -----------------------------------------------------------------------------

def _pct(v: float | None) -> str:
    return "   -" if v is None else f"{100 * v:3.0f}%"


def format_row(row: dict) -> str:
    """One line per page for the report."""
    if not row["ok"]:
        return f"{row['name'][:28]:28}  FAILED  {row['error'][:90]}"
    return (f"{row['name'][:28]:28}  truth {row['n_truth']:3}  buttons {row['n_buttons']:3}  "
            f"recall {_pct(row['recall'])}  precision {_pct(row['precision'])}  "
            f"top3 {_pct(row['top3_precision'])}  "
            f"hint_id {_pct(row['hint_id_accuracy'])}  {row['ms']:5} ms  "
            f"{row['input_tokens']:6} in {row['output_tokens']:5} out  "
            f"${row['cost_usd']:.4f}")


def format_totals(totals: dict, *, with_hints: bool) -> str:
    """The summary block printed after the rows."""
    t = totals
    ms = ("-" if t["ms_median"] is None
          else f"median {t['ms_median']:.0f} ms, p90 {t['ms_p90']:.0f} ms")
    lines = [
        f"pages      {t['n_ok']} scored, {t['n_failed']} failed",
        f"recall     {_pct(t['recall']).strip()} of {t['n_truth']} ground-truth boxes "
        f"(page mean {_pct(t['recall_page_mean']).strip()})",
        f"precision  {_pct(t['precision']).strip()} of {t['n_buttons']} buttons "
        f"(page mean {_pct(t['precision_page_mean']).strip()})",
        f"top3       {_pct(t['top3_precision']).strip()} of the first page's buttons "
        f"inside a ground-truth box",
    ]
    if t["recall_capped_page_mean"] is not None:
        lines.append(f"recall@cap {_pct(t['recall_capped_page_mean']).strip()} page mean "
                     f"(out of at most the detector's max_elements boxes per page)")
    if with_hints:
        lines.append(f"hint_id    {_pct(t['hint_id_accuracy']).strip()} of "
                     f"{t['n_hint_ids'] or 0} elements naming a hint "
                     f"(label match {_pct(t['hint_label_accuracy']).strip()})")
    per_page = "" if t["cost_usd_per_page"] is None else f", ${t['cost_usd_per_page']:.4f}/page"
    lines += [f"latency    {ms}",
              f"tokens     {t['input_tokens']} in, {t['output_tokens']} out: "
              f"${t['cost_usd']:.4f}{per_page}"]
    return "\n".join(lines)


# -- command line -------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tools.screen_eval", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dir", metavar="DIR", nargs="?", default=None,
                   help="dataset folder of <name>.jpg + <name>.json pages")
    p.add_argument("--ax", metavar="DIR", default=None,
                   help="score Detector.rank on the bp-probe axdump files in DIR instead "
                        "(no screenshots; DIR and --with-hints/--no-hints are not used)")
    hints = p.add_mutually_exclusive_group()
    hints.add_argument("--with-hints", dest="with_hints", action="store_true",
                       help="send the page's hints along with the image, as the extension does")
    hints.add_argument("--no-hints", dest="with_hints", action="store_false",
                       help="send the image only")
    p.set_defaults(with_hints=None)
    p.add_argument("--limit", type=int, default=None,
                   help="score only the first N pages in name order")
    p.add_argument("--out", metavar="PATH", default=None,
                   help="JSON result file (default DIR/results/<with|no>-hints.json)")
    p.add_argument("--env-file", metavar="PATH", default=None,
                   help="KEY=VALUE file read into the environment without overriding it "
                        "(for ANTHROPIC_API_KEY)")
    p.add_argument("--model", default=None,
                   help="model id (default: the Detector's, claude-haiku-5-5)")
    p.add_argument("--min-p-button", type=float, default=MIN_P_BUTTON,
                   help=f"p_button at which a predicted element counts as a button "
                        f"(default {MIN_P_BUTTON})")
    return p


def check_arguments(p: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject bad values through ``p.error`` (exit code 2)."""
    if args.ax is not None:
        if args.dir is not None or args.with_hints is not None:
            p.error("--ax DIR scores accessibility dumps on its own; drop the dataset DIR and "
                    "--with-hints/--no-hints")
    elif args.dir is None:
        p.error("give the dataset folder DIR (data/screens), or --ax DIR for accessibility "
                "dumps")
    elif args.with_hints is None:
        p.error("one of the arguments --with-hints --no-hints is required")
    if args.limit is not None and args.limit < 1:
        p.error(f"--limit must be at least 1, got {args.limit}")
    if not 0.0 <= args.min_p_button <= 1.0:   # also rejects NaN
        p.error(f"--min-p-button must lie in [0, 1], got {args.min_p_button}")


def _make_detector(model: str | None) -> Any:
    from brainsync.screen.detect import Detector   # imported here: the SDK loads lazily
    return Detector() if model is None else Detector(model=model)


def main(argv: list[str] | None = None, *,
         detector_factory: Callable[[str | None], Any] | None = None) -> int:
    """Run the evaluation; ``detector_factory(model)`` replaces the real Detector in tests."""
    p = build_parser()
    args = p.parse_args(argv)
    check_arguments(p, args)
    if args.env_file is not None:
        try:
            names = load_env_file(args.env_file)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        _note(f"read {len(names)} variable(s) from {args.env_file}"
              + (f": {', '.join(names)}" if names else ""))
    try:
        if args.ax is not None:
            dumps, skipped = load_ax_dataset(args.ax, args.limit)
        else:
            pages, skipped = load_dataset(args.dir, args.limit)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        detector = (detector_factory or _make_detector)(args.model)
    except ValueError as exc:   # e.g. an empty --model
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:   # e.g. the anthropic SDK is not installed
        print(f"error: cannot set up the detector: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if not getattr(detector, "enabled", True):
        why = (getattr(detector, "why_disabled", "")
               or "the detector is disabled; set ANTHROPIC_API_KEY or pass --env-file")
        print(f"error: {why}", file=sys.stderr)
        return 1
    if args.ax is not None:
        return _main_ax(args, dumps, skipped, detector)
    hints_word = "with" if args.with_hints else "no"
    out = Path(args.out) if args.out else Path(args.dir) / "results" / f"{hints_word}-hints.json"
    _note(f"scoring {len(pages)} page(s) {hints_word} hints"
          + (f", {len(skipped)} skipped" if skipped else ""))
    result = evaluate(pages, detector, with_hints=args.with_hints,
                      min_p_button=args.min_p_button,
                      progress=lambda row: print(format_row(row), flush=True))
    model = next((r["model"] for r in result["pages"] if r.get("model")), args.model)
    result = {
        "settings": {"dir": str(args.dir), "with_hints": args.with_hints, "limit": args.limit,
                     "model": model, "min_p_button": args.min_p_button,
                     "price_in_per_m": PRICE_IN_PER_M, "price_out_per_m": PRICE_OUT_PER_M,
                     "time": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
        "totals": result["totals"], "interrupted": result["interrupted"],
        "pages": result["pages"], "skipped": skipped,
    }
    print()
    print(format_totals(result["totals"], with_hints=args.with_hints))
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot write {out}: {exc}", file=sys.stderr)
        return 1
    print(f"results    {out}")
    if result["totals"]["n_ok"] == 0:
        print("error: no page was scored; see the errors above", file=sys.stderr)
        return 1
    return 0


def _main_ax(args: argparse.Namespace, dumps: list[AxDump], skipped: list[dict],
             detector: Any) -> int:
    """The --ax run: rank every dump, print the report and write the JSON result."""
    out = Path(args.out) if args.out else Path(args.ax) / "results" / "ax-rank.json"
    _note(f"ranking {len(dumps)} dump(s)" + (f", {len(skipped)} skipped" if skipped else ""))
    result = evaluate_ax(dumps, detector,
                         progress=lambda row: print(format_ax_row(row), flush=True))
    model = next((r["model"] for r in result["dumps"] if r.get("model")), args.model)
    result = {
        "settings": {"ax": str(args.ax), "limit": args.limit, "model": model,
                     "price_in_per_m": PRICE_IN_PER_M, "price_out_per_m": PRICE_OUT_PER_M,
                     "time": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
        "totals": result["totals"], "interrupted": result["interrupted"],
        "dumps": result["dumps"], "skipped": skipped,
    }
    print()
    print(format_ax_totals(result["totals"]))
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot write {out}: {exc}", file=sys.stderr)
        return 1
    print(f"results    {out}")
    if result["totals"]["n_ok"] == 0:
        print("error: no dump was ranked; see the errors above", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
