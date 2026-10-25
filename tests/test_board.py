from __future__ import annotations

import numpy as np
import pytest

from brainsync import board as bd
from brainsync.board import CerelogBoard, FakeBoard, detect_serial_port, record

FS = 250.0


def test_fake_read_returns_samples_since_last_read() -> None:
    b = FakeBoard([15.0, 17.0], t0=1000.0)
    X, t = b.read()
    assert X.shape == (8, 0) and t.shape == (0,)
    b.advance(1.0)
    X, t = b.read()
    assert X.shape == (8, 250)
    np.testing.assert_allclose(t, 1000.0 + np.arange(250) / FS)
    assert b.read()[0].shape == (8, 0)
    b.advance(0.5)
    b.advance(0.5)
    X, t = b.read()
    assert X.shape == (8, 250)
    assert t[0] == pytest.approx(1001.0)


def test_fake_fractional_advance_accumulates() -> None:
    b = FakeBoard([15.0])
    for _ in range(1000):
        b.advance(0.001)
    assert b.read()[0].shape[1] == 250
    assert b.elapsed == pytest.approx(1.0)


def test_fake_tone_only_on_posterior_rows() -> None:
    b = FakeBoard([12.0], noise=0.0, amp=3.0)
    b.advance(2.0)
    X, _ = b.read()
    assert np.all(X[:4] == 0.0)
    for row in X[4:]:
        assert np.max(np.abs(row)) == pytest.approx(3.0, rel=1e-3)
    spec = np.abs(np.fft.rfft(X[6]))
    assert np.fft.rfftfreq(X.shape[1], 1 / FS)[np.argmax(spec)] == pytest.approx(12.0)


def test_fake_switching_schedule_and_phase_continuity() -> None:
    b = FakeBoard([15.0, 17.0], switch_s=2.0, noise=0.0, amp=1.0)
    assert b.active_target() == 0
    b.advance(1.99)
    assert b.active_target() == 0
    b.advance(0.01)
    assert b.active_target() == 1
    b.advance(2.0)
    assert b.active_target() == 0
    # split reads must give the same waveform as one continuous one
    pieces = []
    c = FakeBoard([15.0, 17.0], switch_s=2.0, noise=0.0, amp=1.0)
    for _ in range(40):
        c.advance(0.1)
        pieces.append(c.read()[0])
    split = np.concatenate(pieces, axis=1)
    whole_board = FakeBoard([15.0, 17.0], switch_s=2.0, noise=0.0, amp=1.0)
    whole_board.advance(4.0)
    whole = whole_board.read()[0]
    np.testing.assert_allclose(split, whole, atol=1e-12)
    # no jump at the switch: every step is bounded by the fastest phase increment
    step = np.abs(np.diff(whole[4]))
    assert step.max() <= 2 * np.pi * 17.0 / FS + 1e-9


def test_fake_set_target_holds_and_resumes() -> None:
    b = FakeBoard([15.0, 17.0], switch_s=1.0, noise=0.0)
    b.set_target(-1)
    assert b.active_target() == -1
    b.advance(1.0)
    assert np.all(b.read()[0] == 0.0)
    b.set_target(1)
    b.advance(1.0)
    X, _ = b.read()
    spec = np.abs(np.fft.rfft(X[7]))
    assert np.fft.rfftfreq(X.shape[1], 1 / FS)[np.argmax(spec)] == pytest.approx(17.0)
    b.set_target(None)
    assert b.active_target() == 0  # 2 s elapsed at 1 s per switch
    with pytest.raises(ValueError, match="5"):
        b.set_target(5)


def test_fake_with_clock_generates_elapsed_time() -> None:
    now = [500.0]
    b = FakeBoard([15.0], clock=lambda: now[0])
    assert b.t0 == 500.0
    now[0] += 0.4
    X, t = b.read()
    assert X.shape == (8, 100)
    assert t[0] == pytest.approx(500.0)
    now[0] += 0.004
    assert b.read()[0].shape[1] == 1
    with pytest.raises(ValueError, match="clock"):
        b.advance(1.0)


def test_fake_seeded_noise_is_reproducible() -> None:
    a, b = FakeBoard([15.0], seed=3), FakeBoard([15.0], seed=3)
    a.advance(1.0)
    b.advance(1.0)
    np.testing.assert_array_equal(a.read()[0], b.read()[0])
    c = FakeBoard([15.0], seed=0, noise=8.0, amp=0.0)
    c.advance(10.0)
    assert np.std(c.read()[0]) == pytest.approx(8.0, rel=0.05)


@pytest.mark.parametrize("kw, match", [
    ({"freqs": []}, r"\[\]"),
    ({"freqs": [200.0]}, "200"),
    ({"freqs": [15.0], "switch_s": 0.0}, "switch_s"),
    ({"freqs": [15.0], "ch_names": ["a", "b"]}, "8 channel names"),
])
def test_fake_rejects_bad_arguments(kw: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        FakeBoard(**kw)


def test_record_virtual_board_exact_length_and_flush() -> None:
    b = FakeBoard([15.0], t0=0.0)
    b.advance(3.0)  # stale data, discarded by flush
    ticks: list[float] = []
    X, t = record(b, 2.0, progress=lambda e, total: ticks.append(e))
    assert X.shape == (8, 500)
    assert t[0] == pytest.approx(3.0, abs=0.1)
    np.testing.assert_allclose(np.diff(t), 1 / FS)
    assert ticks and ticks[-1] <= 2.0


def test_record_without_flush_keeps_buffer() -> None:
    b = FakeBoard([15.0], t0=0.0)
    b.advance(1.0)
    X, t = record(b, 2.0, flush=False)
    assert X.shape == (8, 500)
    assert t[0] == 0.0


def test_record_stop_callback_ends_early() -> None:
    b = FakeBoard([15.0])
    X, _ = record(b, 10.0, stop=lambda: b.elapsed >= 1.0)
    assert 250 <= X.shape[1] < 300


class _DeadBoard:
    fs = FS
    ch_names = tuple(f"c{i}" for i in range(8))

    def read(self) -> tuple[np.ndarray, np.ndarray]:
        return np.empty((8, 0)), np.empty(0)


def test_record_stops_when_board_stops_delivering(capsys: pytest.CaptureFixture) -> None:
    now = [0.0]

    def sleep(s: float) -> None:
        now[0] += s

    X, t = record(_DeadBoard(), 60.0, sleep=sleep, clock=lambda: now[0], stall_s=2.0)
    assert X.shape == (8, 0) and t.shape == (0,)
    assert now[0] < 3.0
    assert "sent nothing" in capsys.readouterr().err


def test_record_rejects_negative_seconds() -> None:
    with pytest.raises(ValueError, match="-1"):
        record(FakeBoard([15.0]), -1.0)


class _FakeShim:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.prepared = False
        self.data = np.zeros((11, 0))

    def prepare_session(self) -> None:
        self.calls.append("prepare")
        self.prepared = True

    def start_stream(self, n: int) -> None:
        self.calls.append(f"start:{n}")

    def get_board_data(self) -> np.ndarray:
        out, self.data = self.data, np.zeros((11, 0))
        return out

    def is_prepared(self) -> bool:
        return self.prepared

    def stop_stream(self) -> None:
        self.calls.append("stop")

    def release_session(self) -> None:
        self.calls.append("release")
        self.prepared = False


def test_cerelog_board_converts_volts_and_closes() -> None:
    shim = _FakeShim()
    board = CerelogBoard(serial_port="/dev/null", shim=shim, buffer_s=10.0, rebase=False)
    with pytest.raises(RuntimeError, match="not streaming"):
        board.read()
    with board as b:
        assert b.fs == 250.0
        assert b.ch_names == ("Fp1", "Fp2", "C3", "C4", "P7", "P8", "O1", "O2")
        rows = np.zeros((11, 3))
        rows[1:9] = np.arange(1, 9)[:, None] * 1e-6  # volts
        rows[10] = [1.0, 1.004, 1.008]
        shim.data = rows
        X, t = b.read()
        np.testing.assert_allclose(X[:, 0], np.arange(1, 9))
        np.testing.assert_allclose(t, [1.0, 1.004, 1.008])
        assert b.read()[0].shape == (8, 0)
    assert shim.calls == ["prepare", "start:2500", "stop", "release"]


def test_cerelog_board_rebases_stamps_onto_the_host_clock() -> None:
    """The driver's stamps lag the host clock by a constant 0-1 s per connection; read()
    adds the smallest lag seen over the first reads and then keeps that offset."""
    from brainsync.board import REBASE_READS

    shim = _FakeShim()
    host = [1000.0]
    board = CerelogBoard(serial_port="/dev/null", shim=shim, buffer_s=10.0, clock=lambda: host[0])
    lag = 0.7          # stamps behind the host clock
    with board as b:
        seen = []
        for k in range(REBASE_READS + 5):
            rows = np.zeros((11, 2))
            rows[10] = [host[0] - lag - 0.004, host[0] - lag]          # newest stamp = host - lag
            # the transport sometimes delivers late, which must not pull the offset down
            host[0] += 0.03 if k % 7 else 0.09
            shim.data = rows
            _, t = b.read()
            seen.append((host[0], t[-1]))
        assert b.stamp_offset == pytest.approx(lag + 0.03, abs=1e-9)      # smallest lag wins
        # after the estimate is frozen the newest sample sits a link latency behind the host
        host_now, newest = seen[-1]
        assert 0.0 <= host_now - newest <= 0.1
        shim.data = np.zeros((11, 0))
        assert b.read()[1].size == 0
    raw = CerelogBoard(serial_port="/dev/null", shim=_FakeShim(), rebase=False)
    assert raw.stamp_offset == 0.0 and not raw.rebase


def test_cerelog_board_rejects_bad_names() -> None:
    with pytest.raises(ValueError, match="8 channel names"):
        CerelogBoard(serial_port="", shim=_FakeShim(), ch_names=["a"])


def test_cerelog_board_real_shim_constructs_without_connecting() -> None:
    pytest.importorskip("brainflow")
    board = CerelogBoard(serial_port="")
    assert board.fs == 250.0
    assert len(board.ch_names) == 8
    board.close()  # never prepared: nothing to release


def test_detect_serial_port(tmp_path) -> None:
    assert detect_serial_port([str(tmp_path / "cu.usbserial-*")]) == ""
    for name in ("cu.usbserial-12", "cu.usbserial-110"):
        (tmp_path / name).touch()
    (tmp_path / "ttyUSB0").touch()
    found = detect_serial_port([str(tmp_path / "cu.usbserial-*"), str(tmp_path / "ttyUSB*")])
    assert found == str(tmp_path / "cu.usbserial-110")
    assert detect_serial_port([str(tmp_path / "ttyUSB*")]) == str(tmp_path / "ttyUSB0")


class _Port:
    def __init__(self, device: str, vid: int | None) -> None:
        self.device, self.vid = device, vid


def test_detect_com_port_ranks_by_vendor_then_number() -> None:
    assert bd.detect_com_port(lambda: []) == ""
    assert bd.detect_com_port(lambda: [_Port("COM1", None), _Port("COM3", 0x8087)]) == ""
    ports = [_Port("COM12", 0x1A86), _Port("COM5", 0x1A86), _Port("COM2", 0x0403)]
    assert bd.detect_com_port(lambda: ports) == "COM5"      # CH340 beats FTDI, 5 before 12
    assert bd.detect_com_port(lambda: [_Port("COM9", 0x303A), _Port("COM4", 0x10C4)]) == "COM9"


def test_detect_serial_port_default_follows_platform(monkeypatch) -> None:
    monkeypatch.setattr(bd, "detect_com_port", lambda: "COM7")
    monkeypatch.setattr(bd.sys, "platform", "win32")
    assert detect_serial_port() == "COM7"
    assert "COM ports" in bd.serial_search_note()
    monkeypatch.setattr(bd.sys, "platform", "darwin")
    monkeypatch.setattr(bd, "SERIAL_PATTERNS", ())          # nothing matches: no COM scan
    assert detect_serial_port() == ""
    assert "COM" not in bd.serial_search_note()


def test_full_scale_constant() -> None:
    assert bd.ADC_FULL_SCALE_UV == pytest.approx(187_500.0)


def test_cerelog_board_connect_failure_names_the_port() -> None:
    class Failing(_FakeShim):
        def prepare_session(self) -> None:
            raise OSError("BOARD_NOT_READY_ERROR:7")

    board = CerelogBoard(serial_port="/dev/cu.usbserial-9", shim=Failing())
    with pytest.raises(RuntimeError, match="usbserial-9.*BOARD_NOT_READY"):
        board.open()


class _BurstBoard:
    """Delivers 300 samples per read, numbered so trimming can be checked."""

    fs = FS
    ch_names = tuple(f"c{i}" for i in range(8))

    def __init__(self) -> None:
        self.n = 0

    def read(self) -> tuple[np.ndarray, np.ndarray]:
        idx = self.n + np.arange(300)
        self.n += 300
        return np.tile(idx.astype(float), (8, 1)), idx / FS


def test_record_trims_the_last_burst_and_keeps_order() -> None:
    b = _BurstBoard()
    X, t = record(b, 2.0, sleep=lambda s: None, clock=lambda: 0.0)  # first read is the flush
    assert X.shape == (8, 500)
    np.testing.assert_array_equal(X[0], np.arange(300, 800))
    np.testing.assert_allclose(t, np.arange(300, 800) / FS)


def test_record_zero_seconds_is_empty() -> None:
    X, t = record(FakeBoard([15.0]), 0.0)
    assert X.shape == (8, 0) and t.shape == (0,)


def test_record_progress_once_per_whole_second() -> None:
    seen: list[float] = []
    record(FakeBoard([15.0]), 3.0, progress=lambda e, total: seen.append(e), poll_s=0.1)
    assert [int(e) for e in seen] == [1, 2, 3]


class _CtrlCAfter(FakeBoard):
    """FakeBoard whose read raises KeyboardInterrupt on read number ``at``."""

    def __init__(self, *a, at: int = 5, **kw) -> None:
        super().__init__(*a, **kw)
        self.at, self.reads = at, 0

    def read(self):
        self.reads += 1
        if self.reads == self.at:
            raise KeyboardInterrupt
        return super().read()


def test_record_lets_ctrl_c_through_by_default():
    # regression: swallowing Ctrl-C let callers go on to start the stimulus
    with pytest.raises(KeyboardInterrupt):
        record(_CtrlCAfter([15.0], at=5), 10.0)


def test_record_keeps_partial_data_when_asked(capsys):
    X, t = record(_CtrlCAfter([15.0], at=5), 10.0, poll_s=0.1, keep_on_interrupt=True)
    assert 0 < X.shape[1] < 2500 and X.shape[1] == t.size     # 3 reads of 0.1 s after flush
    assert "interrupted; keeping" in capsys.readouterr().err


def test_fake_tone_rows_follow_the_posterior_names():
    from brainsync.recordings import MONTAGE, POSTERIOR
    assert [MONTAGE[i] for i in bd.POSTERIOR_ROWS] == list(POSTERIOR)
