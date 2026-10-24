from __future__ import annotations

import math

import numpy as np
import pytest

from brainsync.metrics import (ABSTAIN, abstain_rate, accuracy, balanced_accuracy, binom_p,
                               confusion_matrix, itr_bits, itr_bpm)


def test_accuracy_counts_abstain_as_wrong():
    assert accuracy([0, 1, 1, 0], [0, 1, 0, 0]) == 0.75
    assert accuracy([0, 1, 1, 0], [0, ABSTAIN, ABSTAIN, 0]) == 0.5
    assert accuracy(np.array([2]), np.array([2])) == 1.0


def test_accuracy_validates():
    with pytest.raises(ValueError, match="3 vs 2"):
        accuracy([0, 1, 1], [0, 1])
    with pytest.raises(ValueError):
        accuracy([], [])


def test_balanced_accuracy_ignores_class_sizes():
    y = [0] * 8 + [1] * 2
    assert accuracy(y, [0] * 10) == 0.8
    assert balanced_accuracy(y, [0] * 10) == 0.5          # always naming the big class
    assert balanced_accuracy(y, [0] * 6 + [1] * 2 + [1, 0]) == pytest.approx((6 / 8 + 1 / 2) / 2)
    assert balanced_accuracy([0, 1, 1], [0, ABSTAIN, 1]) == 0.75
    with pytest.raises(ValueError):
        balanced_accuracy([], [])


def test_abstain_rate():
    assert abstain_rate([ABSTAIN, 0, 1, ABSTAIN]) == 0.5


@pytest.mark.parametrize("n, acc, bits", [
    (2, 1.0, 1.0),
    (4, 1.0, 2.0),
    (12, 1.0, math.log2(12)),
    (2, 0.9, 1 + 0.9 * math.log2(0.9) + 0.1 * math.log2(0.1)),  # 0.5310
    (2, 0.5, 0.0),
    (2, 0.3, 0.0),
    (4, 0.25, 0.0),
    (3, 0.66, math.log2(3) + 0.66 * math.log2(0.66) + 0.34 * math.log2(0.34 / 2)),
])
def test_itr_bits_known_values(n, acc, bits):
    assert itr_bits(n, acc) == pytest.approx(bits, abs=1e-12)


def test_itr_bits_reference_number():
    assert itr_bits(2, 0.9) == pytest.approx(0.531, abs=5e-4)
    assert itr_bits(12, 0.9) == pytest.approx(2.770, abs=5e-4)


def test_itr_bpm():
    assert itr_bpm(2, 1.0, 1.0) == pytest.approx(60.0)
    assert itr_bpm(4, 1.0, 4.0) == pytest.approx(30.0)
    assert itr_bpm(2, 0.9, 2.0) == pytest.approx(itr_bits(2, 0.9) * 30.0)
    with pytest.raises(ValueError, match="0"):
        itr_bpm(2, 0.9, 0.0)


@pytest.mark.parametrize("n, acc", [(1, 0.9), (2, 1.2), (2, -0.1)])
def test_itr_bits_validates(n, acc):
    with pytest.raises(ValueError):
        itr_bits(n, acc)


def test_confusion_matrix_ignores_abstain():
    y = [0, 0, 1, 1, 1, 2]
    pred = [0, 1, 1, ABSTAIN, 1, 0]
    cm = confusion_matrix(y, pred, 3)
    np.testing.assert_array_equal(cm, [[1, 1, 0], [0, 2, 0], [1, 0, 0]])
    assert cm.sum() == 5


def test_confusion_matrix_validates():
    with pytest.raises(ValueError, match="3"):
        confusion_matrix([0, 3], [0, 1], 3)
    with pytest.raises(ValueError, match="5"):
        confusion_matrix([0, 1], [0, 5], 3)


def test_binom_p():
    assert binom_p(0, 10, 0.5) == pytest.approx(1.0)
    assert binom_p(10, 10, 0.5) == pytest.approx(0.5 ** 10)
    assert binom_p(8, 10, 0.5) == pytest.approx((45 + 10 + 1) / 1024)
    # user's pooled windows: 33 of 50 at 3-way chance is well beyond chance
    assert binom_p(33, 50, 1 / 3) < 1e-4
    with pytest.raises(ValueError, match="11"):
        binom_p(11, 10, 0.5)
    with pytest.raises(ValueError, match="1.5"):
        binom_p(1, 10, 1.5)
