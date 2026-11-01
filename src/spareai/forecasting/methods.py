"""Statistical forecasting methods for spare-parts demand.

All methods take a 1-D array of monthly demand and return an array of
`h` future monthly forecasts. Intermittent-demand methods (Croston, SBA,
TSB) are the industry standard for slow/lumpy spare parts and are
implemented here directly so every number is transparent and auditable.

References
- Croston (1972), Forecasting and stock control for intermittent demands.
- Syntetos & Boylan (2005), The accuracy of intermittent demand estimates (SBA).
- Teunter, Syntetos & Babai (2011), Intermittent demand: linking forecasting
  to inventory obsolescence (TSB).
"""
from __future__ import annotations

import numpy as np

ALPHAS = (0.05, 0.1, 0.2, 0.3)


def moving_average(y: np.ndarray, h: int, k: int = 6) -> np.ndarray:
    if len(y) == 0:
        return np.zeros(h)
    return np.full(h, y[-k:].mean())


def _ses_fit(y: np.ndarray, alpha: float) -> tuple[float, float]:
    level = y[: min(3, len(y))].mean()
    sse = 0.0
    for v in y:
        sse += (v - level) ** 2
        level = level + alpha * (v - level)
    return level, sse


def ses(y: np.ndarray, h: int) -> np.ndarray:
    if len(y) == 0:
        return np.zeros(h)
    best = min((_ses_fit(y, a) for a in (0.1, 0.2, 0.3, 0.5)), key=lambda t: t[1])
    return np.full(h, max(best[0], 0.0))


def _croston_core(y: np.ndarray, alpha: float) -> tuple[float, float, float]:
    """Returns (size_level, interval_level, in-sample SSE)."""
    nz = np.flatnonzero(y > 0)
    if len(nz) == 0:
        return 0.0, 1.0, float((y ** 2).sum())
    z = y[nz[0]]
    p = float(nz[0] + 1)
    q = 1
    sse = 0.0
    for t in range(nz[0] + 1, len(y)):
        f = z / p
        sse += (y[t] - f) ** 2
        if y[t] > 0:
            z = z + alpha * (y[t] - z)
            p = p + alpha * (q - p)
            q = 1
        else:
            q += 1
    return z, p, sse


def croston(y: np.ndarray, h: int, debias: bool = False) -> np.ndarray:
    if len(y) == 0 or (y > 0).sum() == 0:
        return np.zeros(h)
    fits = [(a, *_croston_core(y, a)) for a in ALPHAS]
    a, z, p, _ = min(fits, key=lambda t: t[3])
    rate = z / p
    if debias:                       # Syntetos-Boylan Approximation
        rate *= (1 - a / 2)
    return np.full(h, rate)


def sba(y: np.ndarray, h: int) -> np.ndarray:
    return croston(y, h, debias=True)


def _tsb_core(y: np.ndarray, a: float, b: float) -> tuple[float, float]:
    nz = y[y > 0]
    if len(nz) == 0:
        return 0.0, float((y ** 2).sum())
    z = nz[0]
    prob = (y > 0).mean()
    sse = 0.0
    for v in y:
        f = prob * z
        sse += (v - f) ** 2
        occ = 1.0 if v > 0 else 0.0
        prob = prob + b * (occ - prob)
        if v > 0:
            z = z + a * (v - z)
    return prob * z, sse


def tsb(y: np.ndarray, h: int) -> np.ndarray:
    """TSB updates demand probability every period, so forecasts decay
    towards zero for parts that stop moving - ideal to detect obsolescence."""
    if len(y) == 0 or (y > 0).sum() == 0:
        return np.zeros(h)
    best = min((_tsb_core(y, a, b) for a in (0.1, 0.2) for b in (0.05, 0.1, 0.2)),
               key=lambda t: t[1])
    return np.full(h, best[0])


def seasonal_ses(y: np.ndarray, h: int) -> np.ndarray:
    """SES on deseasonalised data with multiplicative monthly indices
    (needs >= 24 months and a reasonably dense series)."""
    n = len(y)
    if n < 24 or (y > 0).mean() < 0.5:
        return ses(y, h)
    m = 12
    idx = np.array([y[i::m].mean() for i in range(m)])
    overall = y.mean()
    if overall <= 0:
        return np.zeros(h)
    seas = np.where(idx > 0, idx / overall, 1.0)
    seas = seas / seas.mean()
    pos = np.arange(n) % m
    deseason = y / seas[pos]
    level = ses(deseason, 1)[0]
    fpos = (np.arange(n, n + h)) % m
    return level * seas[fpos]


STAT_METHODS = {
    "ma6": lambda y, h: moving_average(y, h, 6),
    "ma12": lambda y, h: moving_average(y, h, 12),
    "ses": ses,
    "seasonal_ses": seasonal_ses,
    "croston": croston,
    "sba": sba,
    "tsb": tsb,
}
# simpler methods first: used to break ties
METHOD_ORDER = ["ensemble", "ma12", "ma6", "ses", "sba", "tsb", "croston", "seasonal_ses", "ml_gbm"]
