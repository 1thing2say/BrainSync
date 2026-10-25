"""Nakanishi 2015 loader, epochs and accuracy CLI, on a fake .mat and on the real files."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from scipy.io import savemat

from brainsync import nakanishi as nk

N_SAMPLES = 1114
N_BLOCKS = 2
# the published stimulus matrix (rows 2 Hz apart, columns 0.5 Hz and pi/2 apart),
# stored in the .mat files column by column
GRID = np.array([[9.25, 9.75, 10.25, 10.75],
                 [11.25, 11.75, 12.25, 12.75],
                 [13.25, 13.75, 14.25, 14.75]])
FILE_HZ = GRID.T.ravel()
FILE_PHASE = np.repeat(np.arange(4) * (np.pi / 2), 3)
SIGNAL_START = 73     # 38 pre-stimulus samples + round(0.135 s * 256 Hz)


@pytest.fixture(scope="module")
def fake(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, np.ndarray]:
    """s1.mat shaped like the real files: SSVEP per file target from sample 73, large DC offset."""
    rng = np.random.default_rng(11)
    n = np.arange(N_SAMPLES)
    eeg = rng.standard_normal((12, 8, N_SAMPLES, N_BLOCKS))
    eeg += rng.uniform(3000, 20000, (1, 8, 1, 1))
    for j in range(12):
        tt = (n - SIGNAL_START) / 256.0
        eeg[j] += (0.5 * np.sin(2 * np.pi * FILE_HZ[j] * tt + FILE_PHASE[j]) * (tt >= 0))[None, :, None]
    folder = tmp_path_factory.mktemp("nakanishi2015")
    savemat(folder / "s1.mat", {"eeg": eeg})
    return folder, eeg


def test_constants_match_published_layout() -> None:
    np.testing.assert_allclose(nk.FREQS[nk.FILE_INDEX.argsort()], FILE_HZ)
    np.testing.assert_allclose(nk.FREQS, np.sort(GRID.ravel()))
    np.testing.assert_allclose(nk.PHASES, FILE_PHASE[nk.FILE_INDEX])


def test_load_orders_targets_by_frequency(fake) -> None:
    folder, _ = fake
    ds = nk.load(1, folder)
    assert ds.X.shape == (12 * N_BLOCKS, 8, N_SAMPLES)
    assert ds.freqs.tolist() == [9.25 + 0.5 * k for k in range(12)]
    assert ds.phases.tolist() == pytest.approx([(k % 4) * np.pi / 2 for k in range(12)])
    assert (ds.fs, ds.onset, ds.start) == (256.0, 38, SIGNAL_START)
    assert ds.latency == pytest.approx(0.135)
    assert ds.ch_names[6] == "Oz" and ds.subject == 1 and ds.n_blocks == N_BLOCKS
    assert np.bincount(ds.y).tolist() == [N_BLOCKS] * 12
    per_block = np.zeros((N_BLOCKS, 12), int)
    np.add.at(per_block, (ds.blocks, ds.y), 1)
    assert np.all(per_block == 1)  # every block holds each target exactly once


def test_labels_point_at_the_right_file_target(fake) -> None:
    folder, eeg = fake
    ds = nk.load(1, folder)
    for k, hz in enumerate(ds.freqs):
        j = int(np.flatnonzero(FILE_HZ == hz)[0])
        assert ds.phases[k] == pytest.approx(FILE_PHASE[j])
        mine = ds.X[ds.y == k][np.argsort(ds.blocks[ds.y == k])]
        np.testing.assert_array_equal(mine, np.moveaxis(eeg[j], -1, 0))


def test_epochs_start_at_onset_plus_latency() -> None:
    ramp = np.broadcast_to(np.arange(N_SAMPLES, dtype=float), (2, 8, N_SAMPLES)).copy()
    ds = nk.Dataset(X=ramp, y=np.zeros(2, int), blocks=np.arange(2),
                    freqs=nk.FREQS.copy(), phases=nk.PHASES.copy())
    X = nk.epochs(ds, 1.0, band=None)  # band None only removes each trial's mean
    want = np.arange(SIGNAL_START, SIGNAL_START + 256) - ramp[0, 0].mean()
    assert X.shape == (2, 8, 256)
    np.testing.assert_allclose(X[1, 3], want)


def test_epochs_window_and_dc(fake) -> None:
    ds = nk.load(1, fake[0])
    X = nk.epochs(ds, 1.0)
    assert X.shape == (24, 8, 256)
    assert np.abs(X.mean(axis=-1)).max() < 1.0  # the multi-thousand µV offset is gone
    assert nk.epochs(ds, 4.0).shape[-1] == 1024
    with pytest.raises(ValueError, match="does not fit"):
        nk.epochs(ds, 5.0)
    with pytest.raises(ValueError, match="0.001"):
        nk.epochs(ds, 0.001)


def test_classifiers_on_fake_subject(fake) -> None:
    ds = nk.load(1, fake[0])
    assert nk.subject_accuracy(ds, "cca", 2.0) == 1.0
    assert nk.subject_accuracy(ds, "fbcca", 2.0) == 1.0


def test_load_errors(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="11"):
        nk.load(11, tmp_path)
    with pytest.raises(FileNotFoundError, match="s2.mat"):
        nk.load(2, tmp_path)
    savemat(tmp_path / "s3.mat", {"eeg": np.zeros((10, 8, 50, 2))})
    with pytest.raises(ValueError, match=r"\(10, 8, 50, 2\)"):
        nk.load(3, tmp_path)


def test_cli_table(fake, capsys: pytest.CaptureFixture[str]) -> None:
    rc = nk.main(["--data-dir", str(fake[0]), "--subjects", "1", "--windows", "1", "2"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert "subject 1" in err
    lines = out.strip().splitlines()
    assert lines[1].split() == ["method", "1s", "2s"]
    assert lines[2].split()[0] == "cca" and lines[3].split()[0] == "fbcca"
    assert lines[2].split()[-1] == "100.0%"


def test_cli_missing_data(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert nk.main(["--data-dir", str(tmp_path), "--subjects", "1"]) == 1
    assert "not found" in capsys.readouterr().err


@pytest.mark.realdata("nakanishi2015/s1.mat")
@pytest.mark.slow
def test_subject1_real_accuracy() -> None:
    ds = nk.load(1)
    assert ds.X.shape == (180, 8, N_SAMPLES)
    # Subject 1 is the hardest of the ten. Measured at 4 s: CCA 64.4%, FBCCA 76.1%;
    # at 2 s: 43.9%, 50.6% (windows cut from sample 0 instead give 32.2% / 35.0%). Chance 8.3%.
    assert nk.subject_accuracy(ds, "cca", 4.0) >= 0.60
    assert nk.subject_accuracy(ds, "fbcca", 4.0) >= 0.70
    assert nk.subject_accuracy(ds, "cca", 2.0) >= 0.40
    assert nk.subject_accuracy(ds, "fbcca", 2.0) >= 0.45


@pytest.mark.realdata("nakanishi2015/s8.mat")
@pytest.mark.slow
def test_subject8_real_1s_accuracy() -> None:
    # measured 90.6% (CCA) at 1 s; with the window started at sample 0 it falls to 48.9%
    assert nk.subject_accuracy(nk.load(8), "cca", 1.0) >= 0.80


def test_trained_classifier_is_held_out_by_block(fake) -> None:
    ds = nk.load(1, fake[0])
    assert nk.subject_accuracy(ds, "mscca", 2.0) == 1.0          # one training block is enough
    with pytest.raises(ValueError, match="etrca with block 0 held out of 2.*at least 2"):
        nk.subject_accuracy(ds, "etrca", 2.0)                     # TRCA needs 2 trials per target
    # with three blocks the fit has two trials per target and runs
    three = nk.Dataset(X=np.concatenate([ds.X, ds.X[ds.blocks == 0]]),
                       y=np.concatenate([ds.y, ds.y[ds.blocks == 0]]),
                       blocks=np.concatenate([ds.blocks, np.full(12, 2)]),
                       freqs=ds.freqs, phases=ds.phases)
    assert nk.subject_accuracy(three, "etrca", 2.0) == 1.0


def test_cli_accepts_trained_methods(fake, capsys: pytest.CaptureFixture[str]) -> None:
    rc = nk.main(["--data-dir", str(fake[0]), "--subjects", "1", "--windows", "2",
                  "--methods", "cca", "mscca"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip().splitlines()[-1].split() == ["mscca", "100.0%"]
