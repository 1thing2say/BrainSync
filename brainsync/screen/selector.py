"""Choose which detected elements flicker, page by page (PROTOCOL-v3.md section 4, rule 4).

    sel = TopKSelector(detection.elements, freqs=(15.0, 17.0, 12.0, 20.0), aspect=16 / 9)
    sel = TopKSelector(detection.elements, lags=(0, 21, 42), aspect=16 / 9)   # c-VEP
    send(layout(layout_id, "pick", sel.targets()))
    stage = sel.pick("b2")            # "pick", "confirm", "done" or "rescan"
    if stage == "done":
        click_at(sel.chosen.cx, sel.chosen.cy)

This is strategy B, top-K plus "more". With K targets, a page shows up to K - 1 buttons plus
one "more" target. The K targets are either K frequencies (``freqs``, SSVEP, K = 4 by default)
or K code lags in frames (``lags``, the c-VEP standard of docs/CVEP-STANDARD.md: K = 3, lags
0, 21 and 42). The module is pure: no IO and no clock, so the controller and the tests share
one definition of a page.

Target index i is what the decoder tells apart: ``freqs[i]`` (with ``phases[i]``) or
``lags[i]``. ``plan()`` gives each shown target its index; ``targets()`` turns the plan into
protocol Targets (``code_target`` in code mode).

Rules:
- Candidates are the elements that are ``enabled`` with ``p_button`` at least ``min_p_button``,
  ranked by ``p_button * p_next``. Ties go to the element higher up, then further left.
- Each patch is the element's rect grown to at least ``min_patch`` of the viewport height and
  as many pixels wide (square in pixels), around the element's center and clamped into the
  viewport. ``aspect`` is the viewport's width over its height in pixels.
- Pages are packed greedily in rank order. A candidate joins the current page when its patch
  keeps a gap of at least ``gap`` from the "more" corner and from every patch already on the
  page; otherwise it waits for a later page, keeping its rank.
- On a page, the candidate ranked highest is slot "b1" on target 0, the next "b2" on target 1,
  and so on; "more" is on target K - 1. In code mode (K = 3): best candidate, second
  candidate, More (CVEP-STANDARD section 5).
- "more" sits in the bottom-right corner. Only when every candidate left collides with that
  corner (an element in the bottom-right, such as a chat's Send button) does a page put "more"
  in the first other corner that leaves room: bottom-left, top-right, then top-left. A
  candidate that fits beside no corner at all cannot be shown; it goes to ``dropped`` with a
  note on stderr.
- Picking "more" shows the next page, and on the last page it asks for a fresh scan. Picking a
  button that is not ``risky`` finishes ("done"). A risky one shows the confirm stage first:
  "Do it" over that button's patch on target 0 and "Back" where "more" was, on target 1, or
  on target 2 when K = 3 (the c-VEP standard: "Do it" / "Back" on targets 0 and 2, the two
  lags furthest apart). "Back" returns to the same page.
- ``train_targets`` is the layout of the c-VEP training phase in point mode: K patches in a
  centred row, target 0 first (the cue goes on it).
"""
from __future__ import annotations

import math
import sys
from collections.abc import Sequence
from typing import Any, NamedTuple

from brainsync.screen.detect import Element
from brainsync.server.protocol import Rect, Target

MIN_P_BUTTON = 0.5
MIN_PATCH = 0.10
GAP = 0.02
MARGIN = 0.02
CORNERS = ("bottom-right", "bottom-left", "top-right", "top-left")
STAGES = ("pick", "confirm", "done", "rescan")
MORE, CONFIRM, BACK = "more", "confirm", "back"
TRAIN_LABELS = ("Look here", "Second", "More")   # the c-VEP training row, target 0 first
TRAIN_SIZE = 0.22      # a training patch's side, in viewport heights
TRAIN_GAP = 0.12       # between training patches, in viewport heights


def _note(text: str) -> None:
    print(f"selector: {text}", file=sys.stderr, flush=True)


def _number(value: Any, name: str) -> float:
    """``value`` as a finite float; ValueError naming ``name`` otherwise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {value!r}")
    f = float(value)
    if not math.isfinite(f):
        raise ValueError(f"{name} must be finite, got {f}")
    return f


def _aspect(aspect: Any) -> float:
    a = _number(aspect, "aspect")
    if a <= 0:
        raise ValueError(f"aspect must be the viewport's width / height in pixels (> 0), got "
                         f"{a:g}; pass scan viewport w / h")
    return a


def _min_patch(min_patch: Any) -> float:
    m = _number(min_patch, "min_patch")
    if not 0 < m <= 1:
        raise ValueError(f"min_patch must be a share of the viewport height in (0, 1], got "
                         f"{m:g}; the default is {MIN_PATCH}")
    return m


def _clamp_span(center: float, size: float) -> tuple[float, float]:
    """(start, size) of a span of ``size`` around ``center``, moved inside [0, 1]."""
    size = min(max(size, 0.0), 1.0)
    start = min(max(center - size / 2, 0.0), 1.0 - size)
    return start, size


def patch_rect(el: Element, aspect: float, min_patch: float = MIN_PATCH) -> Rect:
    """The element's rect grown to at least ``min_patch`` high and ``min_patch / aspect`` wide.

    The minimum is square in pixels, since ``aspect`` is the viewport's width over its height.
    The patch keeps the element's center unless that would cross the viewport's edge, where it
    is moved inside instead.
    """
    a, m = _aspect(aspect), _min_patch(min_patch)
    x, w = _clamp_span(el.x + el.w / 2, max(el.w, m / a))
    y, h = _clamp_span(el.y + el.h / 2, max(el.h, m))
    return Rect(x, y, w, h)


def corner_rect(aspect: float, min_patch: float = MIN_PATCH, margin: float = MARGIN,
                corner: str = "bottom-right") -> Rect:
    """A square patch (in pixels) of ``min_patch`` viewport heights in a corner of the viewport.

    ``margin`` is in viewport heights too, so the gap to the side edge has as many pixels as the
    gap to the bottom (or top) edge. ``corner`` is one of CORNERS; "more" uses "bottom-right".
    """
    a, m = _aspect(aspect), _min_patch(min_patch)
    g = _number(margin, "margin")
    if not 0 <= g < 1:
        raise ValueError(f"margin must be a share of the viewport height in [0, 1), got {g:g}")
    if corner not in CORNERS:
        raise ValueError(f"corner must be one of {CORNERS}, got {corner!r}")
    w, h = min(m / a, 1.0), m
    gx = g / a
    x = min(gx, 1.0 - w) if corner.endswith("left") else max(0.0, 1.0 - gx - w)
    y = min(g, 1.0 - h) if corner.startswith("top") else max(0.0, 1.0 - g - h)
    return Rect(x, y, w, h)


def stimulus(freqs: Sequence[float] | None, lags: Sequence[int] | None
             ) -> tuple[tuple[float, ...] | None, tuple[int, ...] | None]:
    """(freqs, None) or (None, lags), checked: exactly one is given, with at least 2 values.

    An empty ``freqs`` next to ``lags`` counts as not given (a code-mode server's welcome
    has ``freqs: []``).
    """
    no_freqs = freqs is None or (not isinstance(freqs, (str, bytes)) and len(freqs) == 0)
    if lags is not None:
        if not no_freqs:
            raise ValueError("pass freqs (SSVEP) or lags (c-VEP), not both")
        if isinstance(lags, (str, bytes)):
            raise ValueError("lags must be a sequence of frames, not a string")
        ls = tuple(lags)
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in ls):
            raise ValueError(f"lags must be integer frames >= 0, got {list(ls)}")
        if len(ls) < 2:
            raise ValueError(f"point mode needs at least 2 code lags (K - 1 buttons plus "
                             f"\"more\"), got {len(ls)}")
        if len(set(ls)) != len(ls):
            raise ValueError(f"lags must be distinct, got {list(ls)}")
        return None, tuple(int(v) for v in ls)
    if freqs is None:
        raise ValueError("pass freqs (SSVEP frequencies) or lags (c-VEP code lags in frames)")
    if isinstance(freqs, (str, bytes)):
        raise ValueError("freqs must be a sequence of numbers, not a string")
    fs = tuple(_number(f, "freqs[i]") for f in freqs)
    if len(fs) < 2:
        raise ValueError(f"point mode needs at least 2 frequencies (K - 1 buttons plus "
                         f"\"more\"), got {len(fs)}; pass more --freqs")
    if any(f <= 0 for f in fs) or len(set(fs)) != len(fs):
        raise ValueError(f"freqs must be positive and distinct, got {list(fs)}")
    return fs, None


class Placement(NamedTuple):
    """One target to draw, before it gets its frequency or its code lag."""

    slot: str
    index: int                  # the target index: freqs[index] or lags[index]
    label: str
    kind: str
    rects: tuple[Rect, ...]


def code_target(slot: str, lag: int, label: str, kind: str, rects: Sequence[Rect]) -> Target:
    """A c-VEP layout target: ``lag`` in frames and ``freq`` None (CVEP-STANDARD 4.2).

    Needs a protocol.Target with a ``lag`` field that accepts ``freq=None`` (the server side
    of the standard). With an older protocol module this raises a ValueError that says so.
    """
    if "lag" not in getattr(Target, "__dataclass_fields__", {}):
        raise ValueError("brainsync.server.protocol.Target has no lag field yet: code-mode "
                         "point layouts need the c-VEP protocol (docs/CVEP-STANDARD.md 4.2)")
    return Target(slot=slot, freq=None, phase=0.0, label=label, kind=kind,  # type: ignore[arg-type]
                  rects=tuple(rects), lag=lag)  # type: ignore[call-arg]


def train_rects(k: int, aspect: float, size: float = TRAIN_SIZE, gap: float = TRAIN_GAP
                ) -> tuple[Rect, ...]:
    """``k`` patches, square in pixels, of ``size`` viewport heights in one row centred in the
    viewport, ``gap`` heights apart; all shrunk together when the row would not fit."""
    a = _aspect(aspect)
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError(f"k must be a positive number of targets, got {k!r}")
    size, gap = _number(size, "size"), _number(gap, "gap")
    if size <= 0 or gap < 0:
        raise ValueError(f"size must be > 0 and gap >= 0, got {size:g} and {gap:g}")
    row = k * size + (k - 1) * gap                  # in viewport heights
    scale = min(1.0, 0.9 * a / row, 0.9 / size)     # at least 5 % margin on each side
    side, g = size * scale, gap * scale
    w = side / a
    x0 = 0.5 - (row * scale / a) / 2
    y = 0.5 - side / 2
    return tuple(Rect(x0 + i * (side + g) / a, y, w, side) for i in range(k))


def train_targets(aspect: float, *, lags: Sequence[int] | None = None,
                  freqs: Sequence[float] | None = None,
                  labels: Sequence[str] | None = None) -> list[Target]:
    """The point-mode training layout (stage "train"): K patches in a centred row with target
    0 (slot "b1", where the cue goes) on the left and "more" last, slots named as on a page."""
    fs, ls = stimulus(freqs, lags)
    k = len(fs) if fs is not None else len(ls or ())
    if labels is None:
        labels = TRAIN_LABELS if k == 3 else tuple(f"{i + 1}" for i in range(k - 1)) + ("More",)
    labels = tuple(labels)
    if len(labels) != k or not all(isinstance(t, str) for t in labels):
        raise ValueError(f"labels needs one string per target ({k}), got {labels!r}")
    slots = [f"b{i + 1}" for i in range(k - 1)] + [MORE]
    kinds = ["button"] * (k - 1) + ["more"]
    out = []
    for i, r in enumerate(train_rects(k, aspect)):
        if ls is not None:
            out.append(code_target(slots[i], ls[i], labels[i], kinds[i], (r,)))
        else:
            assert fs is not None
            out.append(Target(slots[i], fs[i], 0.0, labels[i], kinds[i], (r,)))
    return out


class TopKSelector:
    """The pages of top-K candidates plus "more" for one detection, and the user's place in them.

    ``candidates`` are the shown elements' pool in rank order, ``pages`` the packed pages
    (``[()]`` when there are no candidates, so the user still gets "more" to rescan),
    ``more_rects`` the "more" patch of each page, ``page_index`` the page on screen, ``stage``
    one of "pick", "confirm", "done" or "rescan", and ``chosen`` the picked element.
    """

    def __init__(self, elements: Sequence[Element], freqs: Sequence[float] | None = None, *,
                 aspect: float, min_p_button: float = MIN_P_BUTTON,
                 min_patch: float = MIN_PATCH, gap: float = GAP,
                 phases: Sequence[float] | None = None,
                 lags: Sequence[int] | None = None) -> None:
        fs, ls = stimulus(freqs, lags)
        k = len(fs) if fs is not None else len(ls or ())
        if phases is None:
            ps = (0.0,) * k
        elif ls is not None:
            raise ValueError("phases go with freqs; code targets differ by their lag only")
        else:
            ps = tuple(_number(p, "phases[i]") for p in phases)
            if len(ps) != k:
                raise ValueError(f"phases needs one value per frequency ({k}), got "
                                 f"{len(ps)}")
        p_min = _number(min_p_button, "min_p_button")
        if not 0 <= p_min <= 1:
            raise ValueError(f"min_p_button must be a probability in [0, 1], got {p_min:g}")
        g = _number(gap, "gap")
        if g < 0:
            raise ValueError(f"gap must be at least 0 (a share of the viewport), got {g:g}")
        elements = list(elements)
        if not all(isinstance(e, Element) for e in elements):
            raise ValueError("elements must be brainsync.screen.detect.Element objects; pass "
                             "Detection.elements")
        self.freqs = fs                 # None in code mode
        self.lags = ls                  # None in freq mode
        self.phases = ps
        self.k = k
        self.aspect = _aspect(aspect)
        self.min_patch = _min_patch(min_patch)
        self.gap = g
        self.min_p_button = p_min
        self.candidates: list[Element] = sorted(
            (e for e in elements if e.enabled and e.p_button >= p_min),
            key=lambda e: (-(e.p_button * e.p_next), e.y, e.x, e.id))
        self.dropped: list[Element] = []
        self.pages: list[tuple[Element, ...]] = []
        self.more_rects: list[Rect] = []
        self._patches: list[tuple[Rect, ...]] = []
        self._pack()
        self.page_index = 0
        self.stage = "pick"
        self.chosen: Element | None = None

    # -- packing ----------------------------------------------------------------------------

    def _fill(self, pending: list[Element], corner: Rect
              ) -> tuple[list[Element], list[Rect], list[Element]]:
        """One page beside ``corner``: (placed, their patches, the rest in rank order)."""
        placed: list[Element] = []
        patches: list[Rect] = []
        rest: list[Element] = []
        for el in pending:
            if len(placed) == self.k - 1:
                rest.append(el)
                continue
            p = patch_rect(el, self.aspect, self.min_patch)
            if p.overlaps(corner, self.gap) or any(p.overlaps(q, self.gap) for q in patches):
                rest.append(el)
            else:
                placed.append(el)
                patches.append(p)
        return placed, patches, rest

    def _pack(self) -> None:
        corners = [corner_rect(self.aspect, self.min_patch, MARGIN, c) for c in CORNERS]
        pending = list(self.candidates)
        while pending:
            for corner in corners:
                placed, patches, rest = self._fill(pending, corner)
                if placed:
                    break
            if not placed:
                # Every candidate left collides with every corner: none of them can be shown.
                self.dropped.extend(pending)
                _note(f"{len(pending)} element(s) are too large to show beside the \"more\" "
                      f"patch and were left out: "
                      + ", ".join(repr(e.label) for e in pending[:5]))
                break
            self.pages.append(tuple(placed))
            self._patches.append(tuple(patches))
            self.more_rects.append(corner)
            pending = rest
        if not self.pages:
            self.pages.append(())
            self._patches.append(())
            self.more_rects.append(corners[0])

    # -- state ------------------------------------------------------------------------------

    @property
    def page(self) -> tuple[Element, ...]:
        """The elements on the page on screen, in slot order b1, b2, ..."""
        return self.pages[self.page_index]

    @property
    def last_page(self) -> bool:
        return self.page_index == len(self.pages) - 1

    def patch(self, el: Element) -> Rect:
        """The patch ``el`` has on the page on screen; ValueError if it is not on that page."""
        for e, p in zip(self.page, self._patches[self.page_index]):
            if e is el:
                return p
        raise ValueError(f"element {el.id} ({el.label!r}) is not on page {self.page_index}")

    @property
    def code(self) -> bool:
        """True in code mode (c-VEP lags), False with frequencies."""
        return self.lags is not None

    @property
    def back_index(self) -> int:
        """The target "Back" uses on the confirm stage: 2 when K = 3 (c-VEP), else 1."""
        return 2 if self.k == 3 else 1

    def plan(self) -> list[Placement]:
        """What to draw for the current stage without its stimulus: (slot, target index,
        label, kind, rects) each. Empty once the stage is "done" or "rescan"."""
        i = self.page_index
        more_rect = self.more_rects[i]
        if self.stage == "pick":
            out = [Placement(f"b{j + 1}", j, el.label or el.role, "button", (p,))
                   for j, (el, p) in enumerate(zip(self.pages[i], self._patches[i]))]
            out.append(Placement(MORE, self.k - 1, "Rescan" if self.last_page else "More",
                                 "more", (more_rect,)))
            return out
        if self.stage == "confirm":
            assert self.chosen is not None
            label = self.chosen.label or self.chosen.role
            return [Placement(CONFIRM, 0, f"Do it: {label}", "confirm",
                              (self.patch(self.chosen),)),
                    Placement(BACK, self.back_index, "Back", "back", (more_rect,))]
        return []

    def target(self, p: Placement) -> Target:
        """The protocol Target of ``p``: its frequency and phase, or its code lag."""
        if self.lags is not None:
            return code_target(p.slot, self.lags[p.index], p.label, p.kind, p.rects)
        assert self.freqs is not None
        return Target(p.slot, self.freqs[p.index], self.phases[p.index], p.label, p.kind,
                      p.rects)

    def targets(self) -> list[Target]:
        """What to draw for the current stage; empty once the stage is "done" or "rescan"."""
        return [self.target(p) for p in self.plan()]

    def pick(self, slot: str) -> str:
        """Apply the user's pick of ``slot`` and return the new stage.

        "pick" for the next page or after "back", "confirm" for a risky button, "done" when
        ``chosen`` should be clicked, and "rescan" after "more" on the last page. ValueError for
        a slot that is not among targets().
        """
        if self.stage == "pick":
            if slot == MORE:
                if self.last_page:
                    self.stage = "rescan"
                else:
                    self.page_index += 1
                return self.stage
            j = self._button_index(slot)
            if j is not None:
                self.chosen = self.page[j]
                self.stage = "confirm" if self.chosen.risky else "done"
                return self.stage
        elif self.stage == "confirm":
            if slot == CONFIRM:
                self.stage = "done"
                return self.stage
            if slot == BACK:
                self.stage = "pick"
                self.chosen = None
                return self.stage
        else:
            raise ValueError(f"the selector is finished (stage {self.stage!r}); start a new "
                             f"one for the next scan")
        names = [t.slot for t in self.targets()]
        raise ValueError(f"slot {slot!r} is not on screen in stage {self.stage!r}; pick one "
                         f"of {names}")

    def _button_index(self, slot: str) -> int | None:
        if not isinstance(slot, str) or not slot.startswith("b") or not slot[1:].isdigit():
            return None
        j = int(slot[1:]) - 1
        return j if 0 <= j < len(self.page) and slot == f"b{j + 1}" else None
