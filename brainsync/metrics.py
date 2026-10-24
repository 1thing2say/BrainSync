"""Scoring helpers: accuracy with abstention, balanced accuracy, Wolpaw ITR, confusion matrix,
binomial test."""
from __future__ import annotations

import numpy as np
from scipy import stats

ABSTAIN = -1


def _pair(y: np.ndarray, pred: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    y = np.asarray(y, dtype=int).reshape(-1)
    pred = np.asarray(pred, dtype=int).reshape(-1)
    if y.shape != pred.shape:
        raise ValueError(f"y and pred differ in length: {y.size} vs {pred.size}")
    if y.size == 0:
        raise ValueError("cannot score zero trials")
    return y, pred


def accuracy(y: np.ndarray, pred: np.ndarray) -> float:
    """Fraction correct; abstentions (-1) count as wrong."""
    y, pred = _pair(y, pred)
    return float(np.mean(pred == y))


def balanced_accuracy(y: np.ndarray, pred: np.ndarray) -> float:
    """Mean recall over the classes present in y; chance is 1/classes whatever their sizes."""
    y, pred = _pair(y, pred)
    return float(np.mean([np.mean(pred[y == k] == k) for k in np.unique(y)]))


def abstain_rate(pred: np.ndarray) -> float:
    """Fraction of predictions that abstained."""
    pred = np.asarray(pred, dtype=int).reshape(-1)
    if pred.size == 0:
        raise ValueError("cannot score zero trials")
    return float(np.mean(pred == ABSTAIN))


def itr_bits(n_targets: int, acc: float) -> float:
    """Wolpaw information transfer per selection, in bits; 0 at or below chance."""
    if n_targets < 2:
        raise ValueError(f"ITR needs at least 2 targets, got {n_targets}")
    if not 0.0 <= acc <= 1.0:
        raise ValueError(f"accuracy must lie in [0, 1], got {acc}")
    n = float(n_targets)
    if acc <= 1.0 / n:
        return 0.0
    bits = np.log2(n) + acc * np.log2(acc)
    if acc < 1.0:
        bits += (1.0 - acc) * np.log2((1.0 - acc) / (n - 1.0))
    return float(bits)


def itr_bpm(n_targets: int, acc: float, selection_s: float) -> float:
    """Wolpaw ITR in bits per minute for selections taking ``selection_s`` seconds."""
    if selection_s <= 0:
        raise ValueError(f"selection time must be positive, got {selection_s}")
    return itr_bits(n_targets, acc) * 60.0 / float(selection_s)


def confusion_matrix(y: np.ndarray, pred: np.ndarray, n_targets: int) -> np.ndarray:
    """Counts with rows = true target, columns = predicted; abstentions are left out."""
    y, pred = _pair(y, pred)
    if n_targets < 1:
        raise ValueError(f"n_targets must be >= 1, got {n_targets}")
    for name, v in (("y", y), ("pred", pred[pred != ABSTAIN])):
        bad = v[(v < 0) | (v >= n_targets)]
        if bad.size:
            raise ValueError(f"{name} has labels {sorted(set(bad.tolist()))} outside 0..{n_targets - 1}")
    keep = pred != ABSTAIN
    cm = np.zeros((n_targets, n_targets), dtype=int)
    np.add.at(cm, (y[keep], pred[keep]), 1)
    return cm


def binom_p(hits: int, n: int, chance: float) -> float:
    """One-sided P(X >= hits) for X ~ Binomial(n, chance)."""
    if n < 0 or not 0 <= hits <= n:
        raise ValueError(f"need 0 <= hits <= n, got hits={hits}, n={n}")
    if not 0.0 <= chance <= 1.0:
        raise ValueError(f"chance must lie in [0, 1], got {chance}")
    return float(stats.binom.sf(hits - 1, n, chance))
