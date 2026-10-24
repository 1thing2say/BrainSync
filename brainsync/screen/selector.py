"""Choose which detected elements flicker, page by page (PROTOCOL-v3.md section 4, rule 4).

    sel = TopKSelector(detection.elements, freqs=(15.0, 17.0, 12.0, 20.0), aspect=16 / 9)
    send(layout(layout_id, "pick", sel.targets()))
    stage = sel.pick("b2")            # "pick", "confirm", "done" or "rescan"
    if stage == "done":
        click_at(sel.chosen.cx, sel.chosen.cy)

This is strategy B, top-K plus "more". With K frequencies, a page shows up to K - 1 buttons
plus one "more" target. The module is pure: no IO and no clock, so the controller and the tests
share one definition of a page.

Rules:
- Candidates are the elements that are ``enabled`` with ``p_button`` at least ``min_p_button``,
  ranked by ``p_button * p_next``. Ties go to the element higher up, then further left.
- Each patch is the element's rect grown to at least ``min_patch`` of the viewport height and
  as many pixels wide (square in pixels), around the element's center and clamped into the
  viewport. ``aspect`` is the viewport's width over its height in pixels.
- Pages are packed greedily in rank order. A candidate joins the current page when its patch
  keeps a gap of at least ``gap`` from the "more" corner and from every patch already on the
  page; otherwise it waits for a later page, keeping its rank.
- On a page, the candidate ranked highest is slot "b1" on ``freqs[0]``, the next "b2" on
  ``freqs[1]``, and so on; "more" is on ``freqs[K-1]``.
- "more" sits in the bottom-right corner. Only when every candidate left collides with that
  corner (an element in the bottom-right, such as a chat's Send button) does a page put "more"
  in the first other corner that leaves room: bottom-left, top-right, then top-left. A
  candidate that fits beside no corner at all cannot be shown; it goes to ``dropped`` with a
  note on stderr.
- Picking "more" shows the next page, and on the last page it asks for a fresh scan. Picking a
  button that is not ``risky`` finishes ("done"). A risky one shows the confirm stage first:
  "Do it" over that button's patch on ``freqs[0]`` and "Back" where "more" was, on
  ``freqs[1]``. "Back" returns to the same page.
"""
from __future__ import annotations

import math
import sys
from collections.abc import Sequence
from typing import Any

from brainsync.screen.detect import Element
from brainsync.server.protocol import Rect, Target

MIN_P_BUTTON = 0.5
MIN_PATCH = 0.10
GAP = 0.02
MARGIN = 0.02
CORNERS = ("bottom-right", "bottom-left", "top-right", "top-left")
STAGES = ("pick", "confirm", "done", "rescan")
MORE, CONFIRM, BACK = "more", "confirm", "back"


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


class TopKSelector:
    """The pages of top-K candidates plus "more" for one detection, and the user's place in them.

    ``candidates`` are the shown elements' pool in rank order, ``pages`` the packed pages
    (``[()]`` when there are no candidates, so the user still gets "more" to rescan),
    ``more_rects`` the "more" patch of each page, ``page_index`` the page on screen, ``stage``
    one of "pick", "confirm", "done" or "rescan", and ``chosen`` the picked element.
    """

    def __init__(self, elements: Sequence[Element], freqs: Sequence[float], *, aspect: float,
                 min_p_button: float = MIN_P_BUTTON, min_patch: float = MIN_PATCH,
                 gap: float = GAP, phases: Sequence[float] | None = None) -> None:
        if isinstance(freqs, (str, bytes)):
            raise ValueError("freqs must be a sequence of numbers, not a string")
        fs = tuple(_number(f, "freqs[i]") for f in freqs)
        if len(fs) < 2:
            raise ValueError(f"point mode needs at least 2 frequencies (K - 1 buttons plus "
                             f"\"more\"), got {len(fs)}; pass more --freqs")
        if any(f <= 0 for f in fs) or len(set(fs)) != len(fs):
            raise ValueError(f"freqs must be positive and distinct, got {list(fs)}")
        if phases is None:
            ps = (0.0,) * len(fs)
        else:
            ps = tuple(_number(p, "phases[i]") for p in phases)
            if len(ps) != len(fs):
                raise ValueError(f"phases needs one value per frequency ({len(fs)}), got "
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
        self.freqs = fs
        self.phases = ps
        self.k = len(fs)
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

    def targets(self) -> list[Target]:
        """What to draw for the current stage; empty once the stage is "done" or "rescan"."""
        i = self.page_index
        more_rect = self.more_rects[i]
        if self.stage == "pick":
            out = [Target(f"b{j + 1}", self.freqs[j], self.phases[j], el.label or el.role,
                          "button", (p,))
                   for j, (el, p) in enumerate(zip(self.pages[i], self._patches[i]))]
            k = self.k - 1
            out.append(Target(MORE, self.freqs[k], self.phases[k],
                              "Rescan" if self.last_page else "More", "more", (more_rect,)))
            return out
        if self.stage == "confirm":
            assert self.chosen is not None
            label = self.chosen.label or self.chosen.role
            return [Target(CONFIRM, self.freqs[0], self.phases[0], f"Do it: {label}", "confirm",
                           (self.patch(self.chosen),)),
                    Target(BACK, self.freqs[1], self.phases[1], "Back", "back", (more_rect,))]
        return []

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
