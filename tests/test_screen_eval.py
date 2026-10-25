"""tools/screen_eval: dataset loading, the env file, scoring, totals and the CLI, on a stub detector."""
from __future__ import annotations

import base64
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import screen_eval as se

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes\xff\xd9"
VIEWPORT = {"w": 1000, "h": 500, "dpr": 2}
# two hints in viewport CSS px: a "Sign in" button and an icon link without text
HINTS = [{"id": 0, "x": 100, "y": 50, "w": 100, "h": 50, "role": "button", "text": "Sign in",
          "tag": "button"},
         {"id": 7, "x": 600, "y": 300, "w": 200, "h": 100, "role": "link", "text": "",
          "tag": "a"}]


def el(label="x", x=0.0, y=0.0, w=0.1, h=0.1, hint_id=None, p_button=0.9):
    return SimpleNamespace(label=label, x=x, y=y, w=w, h=h, hint_id=hint_id, p_button=p_button,
                           role="button", p_next=0.5, risky=False, enabled=True)


def det(elements=(), ms=1000, input_tokens=2000, output_tokens=400, model="claude-haiku-5-5"):
    return SimpleNamespace(elements=tuple(elements), ms=ms, input_tokens=input_tokens,
                           output_tokens=output_tokens, model=model, summary="A page")


# predictions on HINTS: one centre in the Sign in box (hint 0), one in the link box (hint 7),
# one centre in neither box, and a low-p_button element (ignored) in the link box
GOOD = [el("Sign in", 0.1, 0.1, 0.1, 0.1, hint_id=0),
        el("Profile", 0.6, 0.6, 0.2, 0.2, hint_id=7),
        el("Nothing", 0.4, 0.8, 0.05, 0.05),
        el("Faint", 0.65, 0.65, 0.01, 0.01, p_button=0.2)]


class StubDetector:
    """Records each call and answers from a list (an exception in the list is raised)."""

    def __init__(self, answers=None, enabled=True, why_disabled=""):
        self.answers = list(answers or [])
        self.calls: list[dict] = []
        self.enabled = enabled
        self.why_disabled = why_disabled

    def detect(self, image_b64, media_type, image_w, image_h, viewport_w, viewport_h,
               hints=(), history=(), url="", title=""):
        self.calls.append(dict(image_b64=image_b64, media_type=media_type, image_w=image_w,
                               image_h=image_h, viewport_w=viewport_w, viewport_h=viewport_h,
                               hints=hints, history=history, url=url, title=title))
        answer = self.answers.pop(0) if self.answers else det(GOOD)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def write_page(folder: Path, name: str, *, meta=None, image=JPEG, ext=".jpg", **over) -> Path:
    data = {"url": f"https://{name}.example/", "title": name.title(), "viewport": VIEWPORT,
            "image": {"w": 1280, "h": 640}, "hints": HINTS}
    data.update(over)
    path = folder / f"{name}.json"
    path.write_text(json.dumps(data if meta is None else meta), encoding="utf-8")
    if image is not None:
        (folder / f"{name}{ext}").write_bytes(image)
    return path


@pytest.fixture
def dataset(tmp_path):
    for name in ("alpha", "bravo", "charlie"):
        write_page(tmp_path, name)
    return tmp_path


# -- dataset ----------------------------------------------------------------------------------

def test_load_page_reads_fields_and_normalizes_truth(dataset):
    page = se.load_page(dataset / "alpha.json")
    assert page.name == "alpha" and page.media_type == "image/jpeg"
    assert page.url == "https://alpha.example/" and page.title == "Alpha"
    assert (page.viewport_w, page.viewport_h, page.image_w, page.image_h) == (1000, 500, 1280, 640)
    assert page.truth() == [(0.1, 0.1, 0.1, 0.1), (0.6, 0.6, 0.2, 0.2)]
    assert set(page.hint_by_id()) == {0, 7}


@pytest.mark.parametrize("ext,media", [(".png", "image/png"), (".jpeg", "image/jpeg"),
                                       (".webp", "image/webp")])
def test_load_page_finds_other_image_types(tmp_path, ext, media):
    write_page(tmp_path, "p", ext=ext)
    assert se.load_page(tmp_path / "p.json").media_type == media


def test_hints_without_ids_use_their_index_and_zero_boxes_are_not_truth(tmp_path):
    hints = [{"x": 0, "y": 0, "w": 10, "h": 10}, {"x": 5, "y": 5, "w": 0, "h": 10}]
    page = se.load_page(write_page(tmp_path, "p", hints=hints))
    assert set(page.hint_by_id()) == {0, 1}
    assert len(page.truth()) == 1


def test_missing_url_title_and_hints_default(tmp_path):
    meta = {"viewport": VIEWPORT, "image": {"w": 100, "h": 50}, "title": 5}
    page = se.load_page(write_page(tmp_path, "p", meta=meta))
    assert (page.url, page.title, page.hints) == ("", "", ())


@pytest.mark.parametrize("meta,needle", [
    ([1, 2], "top level must be an object"),
    ({"viewport": {"w": 0, "h": 5}, "image": {"w": 1, "h": 1}}, "viewport must be"),
    ({"viewport": VIEWPORT}, "image must be"),
    ({"viewport": VIEWPORT, "image": {"w": "1", "h": 1}}, "image must be"),
    ({"viewport": VIEWPORT, "image": {"w": True, "h": 1}}, "image must be"),
    ({"viewport": {"w": float("inf"), "h": 1}, "image": {"w": 1, "h": 1}}, "viewport must be"),
    ({"viewport": VIEWPORT, "image": {"w": 10.5, "h": 1}}, "whole numbers"),
    ({"viewport": VIEWPORT, "image": {"w": 1, "h": 1}, "hints": {}}, "hints must be a list"),
    ({"viewport": VIEWPORT, "image": {"w": 1, "h": 1}, "hints": [{"x": 1}]}, "hint 0 needs"),
    ({"viewport": VIEWPORT, "image": {"w": 1, "h": 1}, "hints": ["b"]}, "hint 0 needs"),
    ({"viewport": VIEWPORT, "image": {"w": 1, "h": 1},
      "hints": [{"id": "a", "x": 0, "y": 0, "w": 1, "h": 1}]}, "not an integer"),
    ({"viewport": VIEWPORT, "image": {"w": 1, "h": 1},
      "hints": [{"id": 1, "x": 0, "y": 0, "w": 1, "h": 1},
                {"id": 1, "x": 0, "y": 0, "w": 1, "h": 1}]}, "share an id"),
])
def test_load_page_rejects_bad_json(tmp_path, meta, needle):
    with pytest.raises(ValueError, match=needle):
        se.load_page(write_page(tmp_path, "p", meta=meta))


def test_load_page_rejects_unreadable_missing_or_empty_image(tmp_path):
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot read it as JSON"):
        se.load_page(tmp_path / "broken.json")
    with pytest.raises(ValueError, match="no image next to it"):
        se.load_page(write_page(tmp_path, "noimg", image=None))
    with pytest.raises(ValueError, match="image is empty"):
        se.load_page(write_page(tmp_path, "empty", image=b""))


def test_load_dataset_sorts_limits_and_skips_broken_pages(dataset, capsys):
    write_page(dataset, "aaa-broken", image=None)
    pages, skipped = se.load_dataset(dataset)
    assert [p.name for p in pages] == ["alpha", "bravo", "charlie"]
    assert skipped == [{"name": "aaa-broken", "reason": skipped[0]["reason"]}]
    assert "skipped aaa-broken.json" in capsys.readouterr().err
    pages, _ = se.load_dataset(dataset, limit=2)
    assert [p.name for p in pages] == ["alpha", "bravo"]


def test_load_dataset_ignores_the_results_subfolder(dataset):
    (dataset / "results").mkdir()
    (dataset / "results" / "with-hints.json").write_text("{}", encoding="utf-8")
    pages, skipped = se.load_dataset(dataset)
    assert len(pages) == 3 and skipped == []


def test_load_dataset_errors(tmp_path):
    with pytest.raises(ValueError, match="is not a folder"):
        se.load_dataset(tmp_path / "missing")
    with pytest.raises(ValueError, match="no usable page"):
        se.load_dataset(tmp_path)
    write_page(tmp_path, "only", image=None)
    with pytest.raises(ValueError, match="no usable page"):
        se.load_dataset(tmp_path)
    with pytest.raises(ValueError, match="limit must be at least 1"):
        se.load_dataset(tmp_path, limit=0)


# -- env file ---------------------------------------------------------------------------------

def test_env_file_sets_new_keys_only_and_strips_syntax(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# comment\n\nANTHROPIC_API_KEY=sk-new\nexport A='quoted value'\n"
                    "B = \"x=y\"\nC=\nKEEP=from-file\nD='mismatched\"\n", encoding="utf-8")
    env = {"KEEP": "from-env"}
    names = se.load_env_file(path, env)
    assert names == ["ANTHROPIC_API_KEY", "A", "B", "C", "D"]
    assert env == {"KEEP": "from-env", "ANTHROPIC_API_KEY": "sk-new", "A": "quoted value",
                   "B": "x=y", "C": "", "D": "'mismatched\""}


def test_env_file_keeps_an_existing_empty_value(tmp_path):
    path = tmp_path / ".env"
    path.write_text("K=v\n", encoding="utf-8")
    env = {"K": ""}
    assert se.load_env_file(path, env) == [] and env == {"K": ""}


@pytest.mark.parametrize("text", ["SECRET-VALUE-123\n", "BAD KEY=SECRET-VALUE-123\n",
                                  "1X=SECRET-VALUE-123\n", "=SECRET-VALUE-123\n"])
def test_env_file_bad_line_names_the_line_but_never_its_text(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text("OK=1\n" + text, encoding="utf-8")
    with pytest.raises(ValueError) as info:
        se.load_env_file(path, {})
    assert "line 2" in str(info.value) and "SECRET" not in str(info.value)


def test_env_file_missing(tmp_path):
    with pytest.raises(ValueError, match="cannot read the env file"):
        se.load_env_file(tmp_path / "nope.env", {})


def test_env_file_defaults_to_os_environ(tmp_path, monkeypatch):
    monkeypatch.delenv("SCREEN_EVAL_TEST_VAR", raising=False)
    path = tmp_path / ".env"
    path.write_text("SCREEN_EVAL_TEST_VAR=1\n", encoding="utf-8")
    assert se.load_env_file(path) == ["SCREEN_EVAL_TEST_VAR"]
    import os
    assert os.environ["SCREEN_EVAL_TEST_VAR"] == "1"
    monkeypatch.delenv("SCREEN_EVAL_TEST_VAR")


# -- scoring ----------------------------------------------------------------------------------

def test_score_page_with_hints(dataset):
    page = se.load_page(dataset / "alpha.json")
    row = se.score_page(page, det(GOOD), with_hints=True)
    assert row["ok"] and row["n_truth"] == 2 and row["n_elements"] == 4
    assert row["n_buttons"] == 3 and row["truth_hit"] == 2 and row["buttons_hit"] == 2
    assert row["recall"] == 1.0 and row["precision"] == pytest.approx(2 / 3)
    # "Sign in" matches by label; "Profile" by centre only (the hint has no text)
    assert (row["n_hint_ids"], row["hint_id_ok"], row["hint_label_ok"]) == (2, 2, 1)
    assert row["hint_id_accuracy"] == 1.0 and row["hint_label_accuracy"] == 0.5
    assert row["ms"] == 1000 and (row["input_tokens"], row["output_tokens"]) == (2000, 400)
    assert row["cost_usd"] == pytest.approx((2000 * 0.10 + 400 * 0.50) / 1e6)


def test_score_page_without_hints_leaves_hint_metrics_empty(dataset):
    row = se.score_page(se.load_page(dataset / "alpha.json"), det(GOOD), with_hints=False)
    assert row["n_hint_ids"] is None and row["hint_id_accuracy"] is None
    assert row["recall"] == 1.0


def test_wrong_and_unknown_hint_ids_count_as_wrong(dataset):
    page = se.load_page(dataset / "alpha.json")
    elements = [el("Help", 0.6, 0.6, 0.05, 0.05, hint_id=0),   # centre not in hint 0, no word
                el("Sign up", 0.6, 0.6, 0.05, 0.05, hint_id=0),  # shares "sign": right
                el("Ghost", 0.1, 0.1, 0.1, 0.1, hint_id=99)]     # no such hint
    row = se.score_page(page, det(elements), with_hints=True)
    assert (row["n_hint_ids"], row["hint_id_ok"], row["hint_label_ok"]) == (3, 1, 1)
    assert row["hint_id_accuracy"] == pytest.approx(1 / 3)


def test_hint_id_accuracy_counts_low_p_button_elements_too(dataset):
    page = se.load_page(dataset / "alpha.json")
    row = se.score_page(page, det([el("Sign in", 0.1, 0.1, 0.1, 0.1, hint_id=0, p_button=0.1)]),
                        with_hints=True)
    assert row["n_buttons"] == 0 and row["n_hint_ids"] == 1 and row["hint_id_ok"] == 1
    assert row["precision"] is None and row["recall"] == 0.0


def test_edge_centres_count_as_inside_and_threshold_is_inclusive(dataset):
    page = se.load_page(dataset / "alpha.json")
    # centre exactly on the right edge of the Sign in box (0.2, 0.15); p_button exactly 0.5
    row = se.score_page(page, det([el(x=0.15, y=0.1, w=0.1, h=0.1, p_button=0.5)]),
                        with_hints=False)
    assert row["truth_hit"] == 1 and row["buttons_hit"] == 1
    row = se.score_page(page, det([el(x=0.15, y=0.1, w=0.1, h=0.1, p_button=0.5)]),
                        with_hints=False, min_p_button=0.6)
    assert row["n_buttons"] == 0


def test_one_button_can_hit_overlapping_truth_boxes(tmp_path):
    hints = [{"id": 0, "x": 0, "y": 0, "w": 500, "h": 500},
             {"id": 1, "x": 100, "y": 100, "w": 100, "h": 100}]
    page = se.load_page(write_page(tmp_path, "p", hints=hints))
    row = se.score_page(page, det([el(x=0.1, y=0.2, w=0.1, h=0.2)]), with_hints=False)
    assert (row["truth_hit"], row["buttons_hit"], row["recall"], row["precision"]) == (2, 1, 1.0, 1.0)


def test_page_without_truth_or_elements(tmp_path):
    page = se.load_page(write_page(tmp_path, "p", hints=[]))
    row = se.score_page(page, det([]), with_hints=True)
    assert row["recall"] is None and row["precision"] is None
    assert row["n_hint_ids"] == 0 and row["hint_id_accuracy"] is None


@pytest.mark.parametrize("label,text,expected", [
    ("Sign in", "sign in", True), ("Sign in", "SIGN-UP", True), ("Cart (3)", "3 items", True),
    ("Search", "Go", False), ("", "", False), ("Help", "", False)])
def test_labels_overlap(label, text, expected):
    assert se.labels_overlap(label, text) is expected


def test_percentile_and_cost():
    assert se.percentile([], 50) is None
    assert se.percentile([5], 90) == 5
    assert se.percentile([3, 1, 2], 50) == 2
    assert se.percentile(list(range(1, 11)), 90) == pytest.approx(9.1)
    assert se.percentile([1, 2], 100) == 2 and se.percentile([1, 2], 0) == 1
    assert se.cost_usd(1_000_000, 1_000_000) == pytest.approx(0.60)
    assert se.cost_usd(0, 0) == 0


def test_top3_ranks_by_p_button_times_p_next_and_recall_is_capped(dataset):
    # alpha.json's ground truth: see the fixture. Two buttons hit a box, one misses; the miss
    # ranks first by p_button alone but last by p_button * p_next, so top3 holds all three
    # and its precision is 2/3, while a cap of 1 box makes one hit a full recall@cap.
    page = se.load_page(dataset / "alpha.json")
    truth = page.truth()
    hit = [el(f"hit{i}", x=b[0], y=b[1], w=b[2], h=b[3], p_button=0.8) for i, b in
           enumerate(truth[:2])]
    miss = el("miss", x=0.9, y=0.9, w=0.05, h=0.05, p_button=1.0)
    for e, pn in zip(hit + [miss], (0.9, 0.8, 0.1)):
        e.p_next = pn
    row = se.score_page(page, det(hit + [miss]), with_hints=False, cap=1)
    assert row["n_top3"] == 3 and row["top3_hit"] == 2
    assert row["top3_precision"] == pytest.approx(2 / 3)
    assert row["recall_capped"] == 1.0
    assert se.score_page(page, det(hit + [miss]), with_hints=False)["recall_capped"] is None
    t = se.summarize([row])
    assert t["top3_precision"] == pytest.approx(2 / 3) and t["recall_capped_page_mean"] == 1.0


def test_summarize_pools_and_averages():
    rows = [{"ok": True, "n_truth": 4, "truth_hit": 2, "n_buttons": 2, "buttons_hit": 2,
             "n_elements": 3, "recall": 0.5, "precision": 1.0, "n_hint_ids": 2,
             "hint_id_ok": 1, "hint_label_ok": 0, "ms": 1000, "input_tokens": 1000,
             "output_tokens": 100},
            {"ok": True, "n_truth": 1, "truth_hit": 1, "n_buttons": 0, "buttons_hit": 0,
             "n_elements": 0, "recall": 1.0, "precision": None, "n_hint_ids": 2,
             "hint_id_ok": 2, "hint_label_ok": 1, "ms": 3000, "input_tokens": 3000,
             "output_tokens": 300},
            {"ok": False, "name": "x", "error": "boom"}]
    t = se.summarize(rows)
    assert (t["n_pages"], t["n_ok"], t["n_failed"]) == (3, 2, 1)
    assert t["recall"] == pytest.approx(3 / 5) and t["recall_page_mean"] == pytest.approx(0.75)
    assert t["precision"] == 1.0 and t["precision_page_mean"] == 1.0
    assert t["n_hint_ids"] == 4 and t["hint_id_accuracy"] == 0.75
    assert t["hint_label_accuracy"] == 0.25
    assert t["ms_median"] == 2000 and t["ms_p90"] == pytest.approx(2800)
    assert (t["input_tokens"], t["output_tokens"]) == (4000, 400)
    assert t["cost_usd"] == pytest.approx(se.cost_usd(4000, 400))
    assert t["cost_usd_per_page"] == pytest.approx(se.cost_usd(4000, 400) / 2)


def test_summarize_of_nothing_and_of_no_hint_runs():
    t = se.summarize([])
    assert t["n_pages"] == 0 and t["recall"] is None and t["ms_median"] is None
    assert t["cost_usd"] == 0 and t["cost_usd_per_page"] is None and t["n_hint_ids"] is None
    row = {"ok": True, "n_truth": 1, "truth_hit": 1, "n_buttons": 1, "buttons_hit": 1,
           "n_elements": 1, "recall": 1.0, "precision": 1.0, "n_hint_ids": None,
           "hint_id_ok": None, "hint_label_ok": None, "ms": 5, "input_tokens": 1,
           "output_tokens": 1}
    t = se.summarize([row])
    assert t["n_hint_ids"] is None and t["hint_id_accuracy"] is None
    assert t["hint_label_accuracy"] is None


# -- evaluate ---------------------------------------------------------------------------------

def test_evaluate_calls_the_detector_as_the_plan_says(dataset):
    pages, _ = se.load_dataset(dataset)
    stub = StubDetector()
    seen = []
    result = se.evaluate(pages, stub, with_hints=True, progress=seen.append)
    assert len(stub.calls) == 3 and len(seen) == 3 and not result["interrupted"]
    call = stub.calls[0]
    assert base64.b64decode(call["image_b64"]) == JPEG
    assert call["media_type"] == "image/jpeg"
    assert (call["image_w"], call["image_h"], call["viewport_w"], call["viewport_h"]) == \
        (1280, 640, 1000, 500)
    assert call["hints"] == HINTS and tuple(call["history"]) == ()
    assert (call["url"], call["title"]) == ("https://alpha.example/", "Alpha")
    assert result["pages"][0]["model"] == "claude-haiku-5-5"
    assert result["pages"][0]["summary"] == "A page"
    assert result["totals"]["n_ok"] == 3 and result["totals"]["recall"] == 1.0


def test_evaluate_without_hints_sends_none(dataset):
    pages, _ = se.load_dataset(dataset, limit=1)
    stub = StubDetector()
    result = se.evaluate(pages, stub, with_hints=False)
    assert tuple(stub.calls[0]["hints"]) == ()
    assert result["pages"][0]["hint_id_accuracy"] is None
    assert result["pages"][0]["recall"] == 1.0   # ground truth is still the hints


def test_evaluate_keeps_going_after_a_failed_detection(dataset):
    pages, _ = se.load_dataset(dataset)
    stub = StubDetector([det(GOOD), RuntimeError("model refused"), det([])])
    result = se.evaluate(pages, stub, with_hints=True)
    rows = result["pages"]
    assert [r["ok"] for r in rows] == [True, False, True]
    assert rows[1]["error"] == "RuntimeError: model refused" and "wall_ms" in rows[1]
    assert result["totals"]["n_failed"] == 1 and result["totals"]["n_ok"] == 2
    assert result["totals"]["recall"] == pytest.approx(2 / 4)


def test_evaluate_stops_on_ctrl_c_and_keeps_the_rows(dataset, capsys):
    pages, _ = se.load_dataset(dataset)
    stub = StubDetector([det(GOOD), KeyboardInterrupt()])
    result = se.evaluate(pages, stub, with_hints=True)
    assert result["interrupted"] and len(result["pages"]) == 1
    assert len(stub.calls) == 2
    assert "interrupted at bravo" in capsys.readouterr().err


def test_evaluate_reports_an_image_that_vanished(dataset):
    pages, _ = se.load_dataset(dataset, limit=1)
    pages[0].image_path.unlink()
    stub = StubDetector()
    row = se.evaluate(pages, stub, with_hints=True)["pages"][0]
    assert not row["ok"] and "cannot read alpha.jpg" in row["error"] and stub.calls == []


def test_format_row_and_totals(dataset):
    pages, _ = se.load_dataset(dataset)
    result = se.evaluate(pages, StubDetector([det(GOOD), ValueError("x" * 300)]),
                         with_hints=True)
    lines = [se.format_row(r) for r in result["pages"]]
    assert "recall 100%" in lines[0] and "precision  67%" in lines[0]
    assert "FAILED" in lines[1] and len(lines[1]) < 140
    text = se.format_totals(result["totals"], with_hints=True)
    assert "pages      2 scored, 1 failed" in text and "hint_id" in text
    assert "median 1000 ms" in text
    assert "hint_id" not in se.format_totals(result["totals"], with_hints=False)
    empty = se.format_totals(se.summarize([]), with_hints=True)
    assert "latency    -" in empty and "-" in empty


# -- command line -----------------------------------------------------------------------------

def run_main(argv, stub=None, models=None):
    def factory(model):
        if models is not None:
            models.append(model)
        return stub if stub is not None else StubDetector()
    return se.main(argv, detector_factory=factory)


def test_main_writes_json_and_report(dataset, capsys):
    out = dataset / "out" / "r.json"
    models = []
    assert run_main([str(dataset), "--with-hints", "--out", str(out), "--model", "m-1"],
                    models=models) == 0
    assert models == ["m-1"]
    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data) == {"settings", "totals", "interrupted", "pages", "skipped"}
    s = data["settings"]
    assert s["with_hints"] is True and s["model"] == "claude-haiku-5-5"
    assert (s["price_in_per_m"], s["price_out_per_m"], s["min_p_button"]) == (0.10, 0.50, 0.5)
    assert len(data["pages"]) == 3 and data["totals"]["recall"] == 1.0
    stdout = capsys.readouterr().out
    assert stdout.count("truth   2") == 3 and "recall     100%" in stdout and str(out) in stdout


def test_main_default_out_path_and_limit(dataset):
    assert run_main([str(dataset), "--no-hints", "--limit", "2"]) == 0
    data = json.loads((dataset / "results" / "no-hints.json").read_text(encoding="utf-8"))
    assert len(data["pages"]) == 2 and data["settings"]["limit"] == 2
    assert data["totals"]["hint_id_accuracy"] is None
    assert run_main([str(dataset), "--with-hints"]) == 0   # the results folder is not a page
    assert (dataset / "results" / "with-hints.json").is_file()


def test_main_lists_skipped_pages(dataset):
    write_page(dataset, "zulu", image=None)
    assert run_main([str(dataset), "--with-hints"]) == 0
    data = json.loads((dataset / "results" / "with-hints.json").read_text(encoding="utf-8"))
    assert [s["name"] for s in data["skipped"]] == ["zulu"]


@pytest.mark.parametrize("argv", [
    [], ["DIR"], ["DIR", "--with-hints", "--no-hints"], ["DIR", "--with-hints", "--limit", "0"],
    ["DIR", "--no-hints", "--min-p-button", "1.5"], ["DIR", "--no-hints", "--min-p-button", "nan"],
    ["DIR", "--no-hints", "--limit", "x"]])
def test_main_bad_arguments_exit_2(dataset, argv):
    argv = [str(dataset) if a == "DIR" else a for a in argv]
    with pytest.raises(SystemExit) as info:
        run_main(argv)
    assert info.value.code == 2


def test_main_bad_dataset_exits_2(tmp_path, capsys):
    assert run_main([str(tmp_path / "missing"), "--with-hints"]) == 2
    assert "is not a folder" in capsys.readouterr().err
    assert run_main([str(tmp_path), "--with-hints"]) == 2


def test_main_disabled_detector_exits_1_without_calls(dataset, capsys):
    stub = StubDetector(enabled=False, why_disabled="ANTHROPIC_API_KEY is not set")
    assert run_main([str(dataset), "--with-hints"], stub=stub) == 1
    assert stub.calls == [] and "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err
    assert not (dataset / "results").exists()


def test_main_detector_setup_failure_exits_1(dataset, capsys):
    def factory(model):
        raise ImportError("No module named 'anthropic'")
    assert se.main([str(dataset), "--with-hints"], detector_factory=factory) == 1
    assert "cannot set up the detector" in capsys.readouterr().err

    def bad_model(model):
        raise ValueError("model must be a model id")
    assert se.main([str(dataset), "--with-hints"], detector_factory=bad_model) == 2


def test_main_disabled_without_reason_says_how_to_fix(dataset, capsys):
    assert run_main([str(dataset), "--no-hints"], stub=StubDetector(enabled=False)) == 1
    assert "set ANTHROPIC_API_KEY or pass --env-file" in capsys.readouterr().err


def test_default_factory_uses_the_real_detector_without_calling_it(dataset, capsys,
                                                                     monkeypatch):
    pytest.importorskip("brainsync.screen.detect")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert se.main([str(dataset), "--no-hints"]) == 1   # no key: disabled, nothing sent
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert se.main([str(dataset), "--no-hints", "--model", " "]) == 2


def test_main_all_failed_exits_1_but_writes_results(dataset, capsys):
    stub = StubDetector([RuntimeError("down")] * 3)
    assert run_main([str(dataset), "--with-hints"], stub=stub) == 1
    data = json.loads((dataset / "results" / "with-hints.json").read_text(encoding="utf-8"))
    assert data["totals"]["n_failed"] == 3
    assert "no page was scored" in capsys.readouterr().err


def test_main_unwritable_out_exits_1(dataset, capsys):
    blocker = dataset / "file"
    blocker.write_text("x", encoding="utf-8")
    assert run_main([str(dataset), "--with-hints", "--out", str(blocker / "r.json")]) == 1
    assert "cannot write" in capsys.readouterr().err


def test_main_env_file_never_prints_values_and_does_not_override(dataset, tmp_path, capsys,
                                                                   monkeypatch):
    secret = "sk-ant-SECRET-should-never-print"
    monkeypatch.setenv("SCREEN_EVAL_KEEP", "env-value")
    monkeypatch.delenv("SCREEN_EVAL_NEW", raising=False)
    env = tmp_path / "keys.env"
    env.write_text(f"SCREEN_EVAL_NEW={secret}\nSCREEN_EVAL_KEEP=file-value\n", encoding="utf-8")
    seen = {}

    def factory(model):
        import os
        seen.update(new=os.environ.get("SCREEN_EVAL_NEW"), keep=os.environ.get("SCREEN_EVAL_KEEP"))
        return StubDetector()

    assert se.main([str(dataset), "--with-hints", "--env-file", str(env)],
                   detector_factory=factory) == 0
    assert seen == {"new": secret, "keep": "env-value"}
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert "file-value" not in captured.out + captured.err
    assert "SCREEN_EVAL_NEW" in captured.err
    assert secret not in (dataset / "results" / "with-hints.json").read_text(encoding="utf-8")
    monkeypatch.delenv("SCREEN_EVAL_NEW")


def test_main_bad_env_file_exits_2_without_echoing(dataset, tmp_path, capsys):
    env = tmp_path / "bad.env"
    env.write_text("sk-ant-SECRET-on-a-bad-line\n", encoding="utf-8")
    assert run_main([str(dataset), "--with-hints", "--env-file", str(env)]) == 2
    err = capsys.readouterr().err
    assert "line 1" in err and "SECRET" not in err
    assert run_main([str(dataset), "--with-hints", "--env-file", str(tmp_path / "none")]) == 2


def test_main_interrupted_run_still_writes(dataset):
    stub = StubDetector([det(GOOD), KeyboardInterrupt()])
    assert run_main([str(dataset), "--with-hints"], stub=stub) == 0
    data = json.loads((dataset / "results" / "with-hints.json").read_text(encoding="utf-8"))
    assert data["interrupted"] is True and len(data["pages"]) == 1


def test_result_json_has_no_nan(dataset):
    assert run_main([str(dataset), "--with-hints"], stub=StubDetector([det([])] * 3)) == 0
    text = (dataset / "results" / "with-hints.json").read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text
    data = json.loads(text)
    assert data["totals"]["precision"] is None and data["totals"]["recall"] == 0.0
    assert not any(isinstance(v, float) and math.isnan(v) for v in data["totals"].values())


def test_scores_the_real_detector_on_a_stub_create(dataset):
    """End to end through Detector.parse with a fake messages.create (no network)."""
    detect = pytest.importorskip("brainsync.screen.detect")
    # image px (1280 x 640 for a 1000 x 500 viewport): the Sign in hint by id, one new box
    answer = {"elements": [
        {"label": "Sign in", "role": "button", "x": 0, "y": 0, "w": 10, "h": 10, "hint_id": 0,
         "p_button": 0.9, "p_next": 0.8, "risky": False, "enabled": True},
        {"label": "Menu", "role": "icon_button", "x": 1100, "y": 10, "w": 64, "h": 64,
         "hint_id": -1, "p_button": 0.7, "p_next": 0.1, "risky": False, "enabled": True}],
        "summary": "Login page"}
    calls = []

    def create(**params):
        calls.append(params)
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=json.dumps(answer))],
                               usage=SimpleNamespace(input_tokens=1500, output_tokens=300))

    detector = detect.Detector(api_key="unused-test-key", create=create)
    pages, _ = se.load_dataset(dataset, limit=1)
    result = se.evaluate(pages, detector, with_hints=True)
    row = result["pages"][0]
    assert row["ok"], row.get("error")
    assert len(calls) == 1 and row["n_elements"] == 2
    assert (row["truth_hit"], row["n_truth"], row["buttons_hit"], row["n_buttons"]) == (1, 2, 1, 2)
    assert row["hint_id_accuracy"] == 1.0 and row["hint_label_accuracy"] == 1.0
    assert (row["input_tokens"], row["output_tokens"]) == (1500, 300)
    assert row["cost_usd"] == pytest.approx(se.cost_usd(1500, 300))
