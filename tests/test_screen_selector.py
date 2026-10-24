"""TopKSelector, patch_rect and corner_rect: ranking, packing, paging and the confirm stage."""
from __future__ import annotations

import itertools
import math
import random

import pytest

from brainsync.screen.detect import Element
from brainsync.screen.selector import (CORNERS, TopKSelector, corner_rect, patch_rect)
from brainsync.server import protocol
from brainsync.server.protocol import EPS, Rect

FREQS = (15.0, 17.0, 12.0, 20.0)
WIDE = 16 / 9
_ids = itertools.count(100)


def el(x, y, w=0.04, h=0.04, *, p_button=0.9, p_next=0.5, risky=False, enabled=True,
       label=None, role="button", id=None, hint_id=None) -> Element:
    i = next(_ids) if id is None else id
    return Element(id=i, label=f"e{i}" if label is None else label, role=role, x=x, y=y, w=w,
                   h=h, hint_id=hint_id, p_button=p_button, p_next=p_next, risky=risky,
                   enabled=enabled)


def inside(r: Rect) -> bool:
    return (0 <= r.x <= 1 and 0 <= r.y <= 1 and 0 <= r.w <= 1 and 0 <= r.h <= 1
            and r.x + r.w <= 1 + EPS and r.y + r.h <= 1 + EPS)


def grid(n, *, step=0.2, p=None):
    """n small elements on a grid in the top-left area, ranked in order (best first)."""
    out = []
    for i in range(n):
        r, c = divmod(i, 4)
        out.append(el(0.05 + c * step, 0.05 + r * step, p_button=0.95,
                      p_next=(1.0 - i * 0.01) if p is None else p[i], label=f"g{i}"))
    return out


# -- patch_rect / corner_rect ---------------------------------------------------------------

def test_patch_grows_small_element_to_square_pixels_around_center():
    e = el(0.5, 0.5, 0.01, 0.02)
    p = patch_rect(e, WIDE)
    assert p.h == pytest.approx(0.10)
    assert p.w == pytest.approx(0.10 / WIDE)
    assert p.w * WIDE == pytest.approx(p.h)            # square in pixels
    assert p.cx == pytest.approx(e.cx) and p.cy == pytest.approx(e.cy)


def test_patch_keeps_large_element_size():
    e = el(0.2, 0.3, 0.4, 0.25)
    p = patch_rect(e, WIDE)
    assert (p.x, p.y, p.w, p.h) == pytest.approx((0.2, 0.3, 0.4, 0.25))


def test_patch_grows_only_the_short_side():
    e = el(0.1, 0.5, 0.5, 0.01)                         # a wide, thin link
    p = patch_rect(e, WIDE)
    assert p.w == pytest.approx(0.5) and p.h == pytest.approx(0.10)
    assert p.cy == pytest.approx(e.cy)


@pytest.mark.parametrize("x,y", [(0.0, 0.0), (0.99, 0.99), (0.0, 0.99), (0.99, 0.0)])
def test_patch_is_clamped_into_viewport_at_edges(x, y):
    e = el(x, y, 0.01, 0.01)
    p = patch_rect(e, WIDE)
    assert inside(p)
    assert p.h == pytest.approx(0.10) and p.w == pytest.approx(0.10 / WIDE)
    # The element still lies inside its patch.
    assert p.x <= e.x + EPS and e.x + e.w <= p.x + p.w + EPS
    assert p.y <= e.y + EPS and e.y + e.h <= p.y + p.h + EPS


def test_patch_portrait_aspect_is_wider_in_normalized_units():
    p = patch_rect(el(0.5, 0.5, 0.01, 0.01), 9 / 16)
    assert p.w == pytest.approx(0.10 * 16 / 9)
    assert p.h == pytest.approx(0.10)


def test_patch_caps_at_full_viewport_for_extreme_aspect():
    p = patch_rect(el(0.5, 0.5, 0.01, 0.01), 0.05)      # min width would be 2.0
    assert p.w == pytest.approx(1.0) and p.x == pytest.approx(0.0)
    assert inside(p)


@pytest.mark.parametrize("aspect", [0, -1.0, math.nan, math.inf, True, "16/9", None])
def test_patch_rejects_bad_aspect(aspect):
    with pytest.raises(ValueError, match="aspect"):
        patch_rect(el(0.5, 0.5), aspect)


@pytest.mark.parametrize("m", [0, -0.1, 1.5, math.nan])
def test_patch_rejects_bad_min_patch(m):
    with pytest.raises(ValueError, match="min_patch"):
        patch_rect(el(0.5, 0.5), WIDE, m)


def test_corner_rect_bottom_right_square_with_equal_pixel_margins():
    c = corner_rect(WIDE)
    assert c.h == pytest.approx(0.10) and c.w * WIDE == pytest.approx(0.10)
    assert c.y + c.h == pytest.approx(1 - 0.02)
    assert (1 - (c.x + c.w)) * WIDE == pytest.approx(0.02)   # 0.02 heights in pixels


@pytest.mark.parametrize("corner", CORNERS)
@pytest.mark.parametrize("aspect", [WIDE, 1.0, 9 / 16, 0.05, 20.0])
def test_corner_rect_every_corner_inside(corner, aspect):
    c = corner_rect(aspect, corner=corner)
    assert inside(c)
    assert (c.cx > 0.5) == corner.endswith("right") or c.w == pytest.approx(1.0)
    assert (c.cy > 0.5) == corner.startswith("bottom")


def test_corner_rect_rejects_bad_arguments():
    with pytest.raises(ValueError, match="corner"):
        corner_rect(WIDE, corner="middle")
    with pytest.raises(ValueError, match="margin"):
        corner_rect(WIDE, margin=-0.1)
    with pytest.raises(ValueError, match="margin"):
        corner_rect(WIDE, margin=1.0)
    with pytest.raises(ValueError, match="aspect"):
        corner_rect(0)


# -- candidates -----------------------------------------------------------------------------

def test_candidates_filtered_by_p_button_and_enabled():
    keep = el(0.1, 0.1, p_button=0.5)                   # the threshold itself counts
    low = el(0.3, 0.1, p_button=0.49)
    off = el(0.5, 0.1, enabled=False)
    sel = TopKSelector([keep, low, off], FREQS, aspect=WIDE)
    assert sel.candidates == [keep]


def test_min_p_button_is_configurable():
    a, b = el(0.1, 0.1, p_button=0.3), el(0.4, 0.1, p_button=0.1)
    sel = TopKSelector([a, b], FREQS, aspect=WIDE, min_p_button=0.2)
    assert sel.candidates == [a]
    assert TopKSelector([a, b], FREQS, aspect=WIDE, min_p_button=0.0).candidates == [a, b]


def test_candidates_ranked_by_p_button_times_p_next():
    a = el(0.1, 0.1, p_button=1.0, p_next=0.2)          # 0.20
    b = el(0.3, 0.1, p_button=0.6, p_next=0.5)          # 0.30
    c = el(0.5, 0.1, p_button=0.9, p_next=0.9)          # 0.81
    assert TopKSelector([a, b, c], FREQS, aspect=WIDE).candidates == [c, b, a]


def test_ties_broken_by_y_then_x():
    lower = el(0.1, 0.6, p_next=0.5)
    right = el(0.6, 0.2, p_next=0.5)
    left = el(0.2, 0.2, p_next=0.5)
    sel = TopKSelector([lower, right, left], FREQS, aspect=WIDE)
    assert sel.candidates == [left, right, lower]


# -- packing --------------------------------------------------------------------------------

def test_pages_hold_k_minus_one_buttons_in_rank_order():
    els = grid(7)
    sel = TopKSelector(list(reversed(els)), FREQS, aspect=WIDE)
    assert [len(p) for p in sel.pages] == [3, 3, 1]
    assert [e for p in sel.pages for e in p] == els


def test_two_frequencies_mean_one_button_per_page():
    els = grid(3)
    sel = TopKSelector(els, (15.0, 20.0), aspect=WIDE)
    assert sel.pages == [(els[0],), (els[1],), (els[2],)]
    assert [t.slot for t in sel.targets()] == ["b1", "more"]


def test_colliding_candidate_waits_for_later_page_and_next_fills_in():
    a = el(0.30, 0.30, p_next=0.9, label="a")
    b = el(0.32, 0.31, p_next=0.8, label="b")           # same spot as a
    c = el(0.60, 0.30, p_next=0.7, label="c")
    d = el(0.10, 0.10, p_next=0.6, label="d")
    sel = TopKSelector([a, b, c, d], FREQS, aspect=WIDE)
    assert sel.pages == [(a, c, d), (b,)]


def test_gap_is_enforced_between_patches():
    # Two minimum patches side by side, 0.01 apart horizontally: closer than the 0.02 gap.
    w = 0.10 / WIDE
    a = el(0.3, 0.3, w, 0.1, p_next=0.9)
    b = el(0.3 + w + 0.01, 0.3, w, 0.1, p_next=0.8)
    assert TopKSelector([a, b], FREQS, aspect=WIDE).pages == [(a,), (b,)]
    assert TopKSelector([a, b], FREQS, aspect=WIDE, gap=0.0).pages == [(a, b)]
    c = el(0.3 + w + 0.025, 0.3, w, 0.1, p_next=0.8)    # 0.025 apart: fits
    assert TopKSelector([a, c], FREQS, aspect=WIDE).pages == [(a, c)]


def test_candidate_colliding_with_more_corner_goes_to_later_page():
    send = el(0.95, 0.93, 0.04, 0.05, p_button=1.0, p_next=1.0, label="Send")  # best
    others = grid(3)
    sel = TopKSelector([send, *others], FREQS, aspect=WIDE)
    assert sel.candidates[0] is send
    assert sel.pages[0] == tuple(others)
    assert sel.pages[1] == (send,)
    assert sel.more_rects[0] == corner_rect(WIDE)
    # On its own page "more" moves to the first other corner with room.
    assert sel.more_rects[1] == corner_rect(WIDE, corner="bottom-left")
    sel.pick("more")
    t = sel.targets()
    assert [x.slot for x in t] == ["b1", "more"]
    assert not t[0].rects[0].overlaps(t[1].rects[0], 0.02)


def test_full_width_bottom_bar_pushes_more_to_the_top():
    bar = el(0.0, 0.88, 1.0, 0.12, p_next=1.0, label="bar")      # collides with both bottoms
    sel = TopKSelector([bar], FREQS, aspect=WIDE)
    assert sel.pages == [(bar,)]
    assert sel.more_rects[0] == corner_rect(WIDE, corner="top-right")


def test_element_that_fits_beside_no_corner_is_dropped_with_a_note(capsys):
    huge = el(0.0, 0.0, 1.0, 1.0, label="backdrop")
    ok = el(0.4, 0.4, p_next=0.1)
    sel = TopKSelector([huge, ok], FREQS, aspect=WIDE)
    assert sel.pages == [(ok,)]
    assert sel.dropped == [huge]
    assert "backdrop" in capsys.readouterr().err


def test_only_unplaceable_candidates_leave_an_empty_page():
    sel = TopKSelector([el(0.0, 0.0, 1.0, 1.0)], FREQS, aspect=WIDE)
    assert sel.pages == [()]
    assert [t.slot for t in sel.targets()] == ["more"]


def test_random_pages_never_overlap_and_stay_inside():
    rng = random.Random(7)
    for trial in range(200):
        aspect = rng.choice([WIDE, 1.0, 9 / 16, 4 / 3, 21 / 9])
        k = rng.choice([2, 3, 4, 6])
        freqs = tuple(10.0 + i for i in range(k))
        els = []
        for _ in range(rng.randint(0, 40)):
            w, h = rng.uniform(0.0, 0.4) ** 2 + 1e-3, rng.uniform(0.0, 0.4) ** 2 + 1e-3
            els.append(el(rng.uniform(0, 1 - w), rng.uniform(0, 1 - h), w, h,
                          p_button=rng.random(), p_next=rng.random(),
                          enabled=rng.random() > 0.1, risky=rng.random() < 0.2))
        sel = TopKSelector(els, freqs, aspect=aspect)
        shown = [e for p in sel.pages for e in p]
        # Every candidate is shown exactly once, or dropped.
        assert sorted(e.id for e in shown + sel.dropped) == sorted(e.id for e in sel.candidates)
        for i, page in enumerate(sel.pages):
            assert len(page) <= k - 1
            sel.page_index = i
            targets = sel.targets()
            rects = [r for t in targets for r in t.rects]
            assert all(inside(r) for r in rects)
            for a, b in itertools.combinations(rects, 2):
                assert not a.overlaps(b, 0.02), (trial, i)
            protocol.layout(1, "pick", targets)          # the builder agrees


# -- targets and frequencies ----------------------------------------------------------------

def test_frequencies_and_phases_assigned_by_rank():
    els = grid(3)
    phases = (0.0, 0.5, 1.0, 1.5)
    sel = TopKSelector(els, FREQS, aspect=WIDE, phases=phases)
    t = sel.targets()
    assert [x.slot for x in t] == ["b1", "b2", "b3", "more"]
    assert [x.freq for x in t] == list(FREQS)
    assert [x.phase for x in t] == list(phases)
    assert [x.kind for x in t] == ["button", "button", "button", "more"]
    assert [x.label for x in t[:3]] == ["g0", "g1", "g2"]
    for x, e in zip(t, els):
        assert x.rects == (patch_rect(e, WIDE),)
    assert t[3].rects == (corner_rect(WIDE),)


def test_short_page_still_puts_more_on_the_last_frequency():
    sel = TopKSelector(grid(1), FREQS, aspect=WIDE)
    t = sel.targets()
    assert [(x.slot, x.freq) for x in t] == [("b1", 15.0), ("more", 20.0)]


def test_empty_label_falls_back_to_role():
    sel = TopKSelector([el(0.1, 0.1, label="", role="icon_button")], FREQS, aspect=WIDE)
    assert sel.targets()[0].label == "icon_button"


def test_more_label_says_rescan_on_last_page():
    sel = TopKSelector(grid(4), FREQS, aspect=WIDE)
    assert sel.targets()[-1].label == "More"
    sel.pick("more")
    assert sel.targets()[-1].label == "Rescan"


# -- pick -----------------------------------------------------------------------------------

def test_empty_candidates_only_more_then_rescan():
    sel = TopKSelector([el(0.1, 0.1, p_button=0.1)], FREQS, aspect=WIDE)
    assert sel.candidates == [] and sel.pages == [()]
    t = sel.targets()
    assert [(x.slot, x.kind, x.freq) for x in t] == [("more", "more", 20.0)]
    protocol.layout(1, "pick", t)
    assert sel.pick("more") == "rescan"
    assert sel.stage == "rescan" and sel.targets() == []
    with pytest.raises(ValueError, match="finished"):
        sel.pick("more")


def test_more_pages_through_then_rescans():
    sel = TopKSelector(grid(7), FREQS, aspect=WIDE)
    assert sel.page_index == 0
    assert sel.pick("more") == "pick" and sel.page_index == 1
    assert sel.pick("more") == "pick" and sel.page_index == 2
    assert [x.slot for x in sel.targets()] == ["b1", "more"]
    assert sel.pick("more") == "rescan"


def test_pick_safe_button_is_done():
    els = grid(3)
    sel = TopKSelector(els, FREQS, aspect=WIDE)
    assert sel.pick("b2") == "done"
    assert sel.chosen is els[1] and sel.stage == "done"
    assert sel.targets() == []


def test_pick_on_second_page_chooses_from_that_page():
    els = grid(5)
    sel = TopKSelector(els, FREQS, aspect=WIDE)
    sel.pick("more")
    assert sel.pick("b2") == "done" and sel.chosen is els[4]


def test_risky_confirm_then_back_returns_same_page():
    els = grid(5)
    risky = el(0.45, 0.45, risky=True, p_next=0.95, label="Delete")
    sel = TopKSelector([*els, risky], FREQS, aspect=WIDE)
    sel.pick("more")
    page = sel.page_index
    slot = f"b{sel.page.index(risky) + 1}"
    assert sel.pick(slot) == "confirm"
    t = sel.targets()
    assert [(x.slot, x.kind, x.freq) for x in t] == [("confirm", "confirm", 15.0),
                                                     ("back", "back", 17.0)]
    assert t[0].rects == (patch_rect(risky, WIDE),)
    assert t[1].rects == (sel.more_rects[page],)
    assert "Delete" in t[0].label and t[1].label == "Back"
    protocol.layout(1, "confirm", t)
    assert sel.pick("back") == "pick"
    assert sel.page_index == page and sel.chosen is None
    assert [x.kind for x in sel.targets()][-1] == "more"


def test_risky_confirm_then_do_it_is_done():
    risky = el(0.3, 0.3, risky=True, label="Pay")
    sel = TopKSelector([risky], FREQS, aspect=WIDE)
    assert sel.pick("b1") == "confirm"
    assert sel.pick("confirm") == "done"
    assert sel.chosen is risky


def test_confirm_phases_follow_freq_slots():
    sel = TopKSelector([el(0.3, 0.3, risky=True)], FREQS, aspect=WIDE,
                       phases=(0.1, 0.2, 0.3, 0.4))
    sel.pick("b1")
    assert [x.phase for x in sel.targets()] == [0.1, 0.2]


@pytest.mark.parametrize("slot", ["b3", "b0", "b01", "b", "x", "confirm", "back", "", None, 1])
def test_pick_rejects_slots_not_on_screen(slot):
    sel = TopKSelector(grid(2), FREQS, aspect=WIDE)
    with pytest.raises(ValueError, match="not on screen"):
        sel.pick(slot)
    assert sel.stage == "pick" and sel.chosen is None


@pytest.mark.parametrize("slot", ["b1", "more", "x"])
def test_confirm_stage_rejects_other_slots(slot):
    sel = TopKSelector([el(0.3, 0.3, risky=True)], FREQS, aspect=WIDE)
    sel.pick("b1")
    with pytest.raises(ValueError, match="confirm"):
        sel.pick(slot)
    assert sel.stage == "confirm"


def test_done_stage_rejects_everything():
    sel = TopKSelector(grid(1), FREQS, aspect=WIDE)
    sel.pick("b1")
    with pytest.raises(ValueError, match="finished"):
        sel.pick("b1")


# -- constructor errors ---------------------------------------------------------------------

@pytest.mark.parametrize("kwargs,match", [
    ({"freqs": (15.0,)}, "at least 2"),
    ({"freqs": ()}, "at least 2"),
    ({"freqs": (15.0, 15.0)}, "distinct"),
    ({"freqs": (15.0, -1.0)}, "positive"),
    ({"freqs": (15.0, math.nan)}, "finite"),
    ({"freqs": "15,17"}, "string"),
    ({"phases": (0.0,)}, "phases"),
    ({"phases": (0.0, 0.0, 0.0, math.inf)}, "finite"),
    ({"min_p_button": 1.5}, "min_p_button"),
    ({"min_p_button": -0.1}, "min_p_button"),
    ({"gap": -0.01}, "gap"),
    ({"aspect": 0}, "aspect"),
    ({"min_patch": 0}, "min_patch"),
])
def test_constructor_rejects_bad_arguments(kwargs, match):
    args = {"freqs": FREQS, "aspect": WIDE, **kwargs}
    freqs = args.pop("freqs")
    with pytest.raises(ValueError, match=match):
        TopKSelector([], freqs, **args)


def test_constructor_rejects_non_elements():
    with pytest.raises(ValueError, match="Element"):
        TopKSelector([{"x": 0.1}], FREQS, aspect=WIDE)


def test_patch_of_element_not_on_page_raises():
    a, b = grid(2)
    sel = TopKSelector([a], FREQS, aspect=WIDE)
    with pytest.raises(ValueError, match="not on page"):
        sel.patch(b)
