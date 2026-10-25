"""brainsync.calibrate end to end on a synthetic session, and the diamond layout config."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from brainsync import calibrate
from brainsync.stimulus import load_config
from brainsync.stimulus.config import explicit_positions, parse_config
from brainsync.templates import load_model


def test_four_diamond_config_positions() -> None:
    cfg = load_config("four_diamond")
    assert cfg.freqs.tolist() == [13.0, 15.0, 17.0, 11.0]
    assert [t.pos for t in cfg.targets] == [(0.0, 0.3), (-0.55, 0.0), (0.55, 0.0), (0.0, -0.3)]
    assert cfg.waveform == "sine" and cfg.hold_cue and cfg.session.blocks == 8


def test_explicit_positions_validation() -> None:
    assert explicit_positions([[0, 0.3], [-0.5, 0]], 2) == [(0.0, 0.3), (-0.5, 0.0)]
    with pytest.raises(ValueError, match="2 \\[x, y\\] pairs"):
        explicit_positions([[0, 0.3]], 2)
    with pytest.raises(ValueError, match=r"positions\[1\]"):
        explicit_positions([[0, 0.3], [1, "a"]], 2)
    data = {"targets": {"freqs": [15.0, 17.0]},
            "layout": {"size": 0.3, "spacing": 0.6, "positions": [[0.2, 0.0], [-0.2, 0.0]],
                       "x_offset": 0.1}}
    cfg = parse_config(data)
    np.testing.assert_allclose([t.pos for t in cfg.targets], [(0.3, 0.0), (-0.1, 0.0)])
    with pytest.raises(ValueError, match="past the screen edge"):
        parse_config({"targets": {"freqs": [15.0]},
                      "layout": {"size": 0.3, "positions": [[0.0, 0.45]]}})


def test_calibrate_fake_session_scores_and_saves(tmp_path: Path) -> None:
    out = tmp_path / "model.npz"
    code = calibrate.main(["-c", "four_diamond", "--fake", "--seed", "3", "--trials", "16",
                           "--save-dir", str(tmp_path), "--out", str(out),
                           "--methods", "fbcca", "etrcar", "--windows", "1", "2"])
    assert code == 0 and out.is_file()
    clf = load_model(out)
    assert clf.name == "etrcar" and clf.n_train == 16 and clf.has_baseline
    assert clf.freqs.tolist() == [13.0, 15.0, 17.0, 11.0] and clf.n_ch == 4
    assert clf.meta["config"] == "four_diamond"
    assert clf.meta["gate"].startswith("gate at 1 s")   # the nearest scored window
    recs = list(tmp_path.glob("*_cued.npz"))
    assert len(recs) == 1 and clf.meta["recording"] == str(recs[0])


def test_calibrate_from_file_no_save(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = calibrate.main(["-c", "four_diamond", "--fake", "--seed", "1", "--trials", "8",
                           "--save-dir", str(tmp_path), "--no-save", "--methods", "fbcca",
                           "--windows", "1"])
    assert code == 0
    rec = next(tmp_path.glob("*_cued.npz"))
    code = calibrate.main(["--from", str(rec), "--methods", "fbcca", "ecca", "--windows", "1",
                           "--out", str(tmp_path / "m.npz"), "--classifier", "ecca"])
    assert code == 0 and (tmp_path / "m.npz").is_file()
    text = capsys.readouterr().out
    assert "gate at" in text and "ecca*" in text and "keep page mode" in text


def test_calibrate_rejects_bad_options_and_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        calibrate.main(["--trials", "1", "--fake"])
    assert calibrate.main(["--from", str(tmp_path / "none_cued.npz")]) == 1
    assert calibrate.model_path(Path("x_cued.npz"), "etrca").name == "x_cued_etrca.npz"
