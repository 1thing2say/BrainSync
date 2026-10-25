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
    assert clf.meta["gate"][0].startswith("gate at 1 s")   # the nearest scored window
    assert clf.meta["held_out"]["n"] == 16 and clf.meta["held_out"]["folds"] == 4
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
    assert "label-permutation p=" in text and "use it with: python -m brainsync.demo -c four_diamond" in text


def test_calibrate_from_a_two_target_file_says_the_gate_needs_four(tmp_path: Path,
                                                                 capsys: pytest.CaptureFixture) -> None:
    code = calibrate.main(["-c", "two_targets", "--fake", "--seed", "4", "--trials", "8",
                           "--save-dir", str(tmp_path), "--no-save", "--methods", "fbcca", "--windows", "1"])
    assert code == 0
    rec = next(tmp_path.glob("*_cued.npz"))
    out = tmp_path / "two.npz"
    assert calibrate.main(["--from", str(rec), "--methods", "fbcca", "etrcar", "--windows", "1",
                           "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "needs the four_diamond session" in text and "keep page mode" not in text
    assert "python -m brainsync.demo -c two_targets --model" in text    # config derived from the file
    assert load_model(out).meta["config"] == "two_targets"


def test_calibrate_rejects_bad_options_and_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        calibrate.main(["--trials", "1", "--fake"])
    assert calibrate.main(["--from", str(tmp_path / "none_cued.npz")]) == 1
    assert calibrate.model_path(Path("x_cued.npz"), "etrca").name == "x_cued_etrca.npz"


def test_saved_model_drives_the_server_bank_and_the_demo_decoder(tmp_path: Path) -> None:
    from brainsync.server import app
    from brainsync.server.decoding import DecoderBank, build_decoder
    from brainsync.synthetic import rest_trials

    out = tmp_path / "model.npz"
    assert calibrate.main(["-c", "four_diamond", "--fake", "--seed", "2", "--trials", "16",
                           "--save-dir", str(tmp_path), "--out", str(out), "--methods", "fbcca",
                           "--windows", "1"]) == 0
    dec = build_decoder([13.0, 15.0, 17.0, 11.0], [0.0] * 4, model=out)
    assert dec.classifier.name == "etrcar" and dec.needs_onset
    from brainsync.online import TUNED
    assert dec.classifier.margin == TUNED.margin and dec.classifier.threshold == TUNED.threshold
    with pytest.raises(ValueError, match="trained for"):
        build_decoder([15.0, 17.0], [0.0, 0.0], model=out)
    with pytest.raises(ValueError, match="channels"):
        build_decoder([13.0, 15.0, 17.0, 11.0], [0.0] * 4, model=out, channels=("O1", "O2"))
    bank = DecoderBank(model=out)
    rng = np.random.default_rng(0)
    bank.calibrate_from(rng.standard_normal((8, 250 * 12)))
    bank.activate([13.0, 15.0, 17.0, 11.0], [0.0] * 4, now=100.0)
    assert bank.decoder is not None and bank.decoder.onset == 100.0
    bank.set_onset(100.5)
    assert bank.decoder.onset == 100.5
    X = rng.standard_normal((8, 250 * 6))
    out_decisions = bank.feed(X, 100.0 + np.arange(250 * 6) / 250.0)
    assert out_decisions and all(np.isfinite(d.offset_s) for d in out_decisions)
    assert all(d.offset_s >= 0.5 - 1e-9 for d in out_decisions)   # gated: no window before the onset
    args = app.build_parser().parse_args(["--decoder-model", str(out), "--board", "fake"])
    assert args.decoder_model == str(out)
