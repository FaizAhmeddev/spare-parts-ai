"""Global machine-learning forecaster.

One gradient-boosted model is trained across all part-site series (a
"global" model), which lets slow parts borrow strength from similar parts.
Poisson loss suits non-negative count demand. Uses LightGBM when installed,
otherwise scikit-learn's HistGradientBoostingRegressor (no extra install).
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)
MIN_HIST = 13


def _make_model():
    try:
        import lightgbm as lgb  # type: ignore
        return lgb.LGBMRegressor(objective="poisson", n_estimators=300, learning_rate=0.05,
                                 num_leaves=15, min_child_samples=30, subsample=0.8,
                                 colsample_bytree=0.8, verbose=-1, random_state=42)
    except ImportError:
        from sklearn.ensemble import HistGradientBoostingRegressor
        return HistGradientBoostingRegressor(loss="poisson", max_iter=250, learning_rate=0.05,
                                             max_leaf_nodes=15, min_samples_leaf=30,
                                             l2_regularization=1.0, random_state=42)


def _features(Y: np.ndarray, t: int, months: pd.PeriodIndex, static: np.ndarray) -> np.ndarray:
    """Features for predicting column t using columns < t. Y: (n_series, T)."""
    past = Y[:, :t]
    def lag(k): return past[:, -k] if t - k >= 0 else np.zeros(len(Y))
    def rmean(k): return past[:, -k:].mean(axis=1)
    nz12 = (past[:, -12:] > 0).mean(axis=1)
    # months since last non-zero demand
    rev = (past[:, ::-1] > 0)
    since = np.where(rev.any(axis=1), rev.argmax(axis=1), t)
    month = (months[0].month - 1 + t) % 12
    feats = np.column_stack([
        lag(1), lag(2), lag(3), lag(12), rmean(3), rmean(6), rmean(12),
        past.mean(axis=1), nz12, since,
        np.full(len(Y), np.sin(2 * np.pi * month / 12)),
        np.full(len(Y), np.cos(2 * np.pi * month / 12)),
        static,
    ])
    return feats


class GlobalGBMForecaster:
    def __init__(self, months: pd.PeriodIndex, static: np.ndarray):
        self.months = months
        self.static = static
        self.model = None

    def fit(self, Y: np.ndarray, until: int) -> "GlobalGBMForecaster":
        X, y = [], []
        for t in range(MIN_HIST, until):
            X.append(_features(Y, t, self.months, self.static))
            y.append(Y[:, t])
        X, y = np.vstack(X), np.concatenate(y)
        self.model = _make_model().fit(X, y)
        return self

    def predict(self, Y: np.ndarray, origin: int, h: int) -> np.ndarray:
        """Recursive multi-step forecast from `origin` (exclusive end of history)."""
        work = np.concatenate([Y[:, :origin], np.zeros((len(Y), h))], axis=1)
        for step in range(h):
            t = origin + step
            pred = self.model.predict(_features(work, t, self.months, self.static))
            work[:, t] = np.clip(pred, 0, None)
        return work[:, origin:origin + h]


def static_features(keys: pd.DataFrame, ds) -> np.ndarray:
    pm = ds["part_master"].set_index("part_id")
    cost = np.log1p(keys["part_id"].map(pm["unit_cost_usd"]).fillna(0).values)
    ved = keys["part_id"].map(pm["criticality_ved"]).map({"V": 2, "E": 1, "D": 0}).fillna(0).values
    cat = keys["part_id"].map(pm["category"]).astype("category").cat.codes.values
    bom_n = ds["bom"].merge(ds["assets"][["asset_id", "site_id"]], on="asset_id") \
                     .groupby(["part_id", "site_id"])["qty_per_asset"].sum()
    installed = pd.MultiIndex.from_frame(keys[["part_id", "site_id"]]).map(
        lambda k: bom_n.get(k, 0)).values.astype(float)
    return np.column_stack([cost, ved, cat, installed])
