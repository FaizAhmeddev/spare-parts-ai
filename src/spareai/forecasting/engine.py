"""Model tournament: every series is backtested on rolling origins and the
method with the lowest cumulative-demand error wins for that series.

Why cumulative error? Per-month MAE rewards forecasting zero for
intermittent parts (most months ARE zero) which would starve the stock.
Inventory decisions depend on demand over the lead time, so we score each
method on the error of total demand over a multi-month window.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .methods import METHOD_ORDER, STAT_METHODS
from .ml import GlobalGBMForecaster, static_features

log = logging.getLogger(__name__)


def _score(fc_sums: np.ndarray, act_sums: np.ndarray) -> tuple[float, float, float]:
    """Return (score, wape, bias) over all origins for one series."""
    err = np.abs(fc_sums - act_sums).sum()
    tot = act_sums.sum()
    bias = (fc_sums.sum() - tot) / tot if tot > 0 else (fc_sums.sum() > 0) * 1.0
    if tot > 0:
        wape = err / tot
    else:
        wape = err / len(act_sums)       # no demand: penalise any forecast
    return wape + 0.25 * abs(bias), wape, bias


def run_forecasting(matrix: pd.DataFrame, ds, cfg, use_ml: bool = True):
    """
    matrix: rows = part-site, columns = monthly periods (unplanned demand).
    Returns (forecast_df_long, choice_df, metrics_df).
    """
    Y = matrix.values.astype(float)
    n, T = Y.shape
    months = matrix.columns
    keys = matrix.index.to_frame(index=False)
    H = int(cfg.get("analysis.forecast_horizon_months", 12))
    n_orig = int(cfg.get("analysis.backtest_origins", 4))
    win = int(cfg.get("analysis.backtest_window_months", 3))
    origins = [T - win * (i + 1) for i in range(n_orig)][::-1]
    origins = [o for o in origins if o >= 12]

    methods = list(STAT_METHODS)
    fc_sums = {m: np.zeros((n, len(origins))) for m in methods}
    act = np.column_stack([Y[:, o:o + win].sum(axis=1) for o in origins])

    for j, o in enumerate(origins):
        for m in methods:
            f = STAT_METHODS[m]
            fc_sums[m][:, j] = [f(Y[i, :o], win).sum() for i in range(n)]

    ml_ok = use_ml and T >= 24
    static = static_features(keys, ds) if ml_ok else None
    if ml_ok:
        methods.append("ml_gbm")
        fc_sums["ml_gbm"] = np.zeros((n, len(origins)))
        for j, o in enumerate(origins):
            model = GlobalGBMForecaster(months, static).fit(Y, until=o)
            fc_sums["ml_gbm"][:, j] = model.predict(Y, o, win).sum(axis=1)
        log.info("ML global model backtested on %d origins", len(origins))

    # ---- ensemble: median of all candidate forecasts (robust combination)
    base_methods = list(methods)
    fc_sums["ensemble"] = np.median(np.stack([fc_sums[m] for m in base_methods]), axis=0)
    methods.append("ensemble")
    margin = float(cfg.get("forecasting.single_model_margin", 0.40))

    # ---- choose per series on all origins but the last; the last origin is
    # kept out-of-sample so the reported accuracy is honest.
    sel = slice(0, len(origins) - 1) if len(origins) > 1 else slice(0, 1)
    metric_rows, best = [], []
    order = {m: i for i, m in enumerate(METHOD_ORDER)}
    for i in range(n):
        scores = {}
        for m in methods:
            s, w, b = _score(fc_sums[m][i, sel], act[i, sel])
            scores[m] = s
            metric_rows.append((i, m, s, w, b, fc_sums[m][i, -1], act[i, -1]))
        single = min(base_methods, key=lambda m: (round(scores[m], 6), order.get(m, 99)))
        # a single model must beat the ensemble clearly, otherwise combine
        best.append(single if scores[single] < (1 - margin) * scores["ensemble"] else "ensemble")
    metrics = pd.DataFrame(metric_rows, columns=["row", "method", "score", "wape", "bias",
                                                 "holdout_fc", "holdout_actual"])
    metrics = pd.concat([keys.iloc[metrics["row"]].reset_index(drop=True),
                         metrics.drop(columns="row")], axis=1)
    metrics["actual_window_qty"] = np.repeat(act[:, sel].sum(axis=1), len(methods))
    metrics["selected"] = [best[r // len(methods)] == m for r, m in enumerate(metrics["method"])]

    # ---- final forecasts (refit on full history)
    final = np.zeros((n, H))
    ml_full = GlobalGBMForecaster(months, static).fit(Y, until=T).predict(Y, T, H) if ml_ok else None
    for i in range(n):
        m = best[i]
        if m == "ensemble":
            cands = [STAT_METHODS[b](Y[i], H) for b in STAT_METHODS]
            if ml_ok:
                cands.append(ml_full[i])
            final[i] = np.median(np.stack(cands), axis=0)
        else:
            final[i] = ml_full[i] if m == "ml_gbm" else STAT_METHODS[m](Y[i], H)

    # residual std of monthly demand around the chosen level (last 24 months)
    recent = Y[:, -24:]
    level = final[:, :1]
    sigma = np.sqrt(((recent - level) ** 2).mean(axis=1))

    choice = keys.copy()
    choice["method"] = best
    choice["fc_monthly_rate"] = final[:, :3].mean(axis=1)
    choice["fc_12m_qty"] = final.sum(axis=1)
    choice["sigma_monthly"] = sigma
    best_scores = metrics.set_index(["part_id", "site_id", "method"])["wape"]
    choice["backtest_wape"] = [best_scores.get((k.part_id, k.site_id, m), np.nan)
                               for k, m in zip(keys.itertuples(), best)]

    fut = pd.period_range(months[-1] + 1, periods=H, freq="M")
    fc_long = pd.DataFrame(final, columns=fut)
    fc_long = pd.concat([keys, fc_long], axis=1).melt(id_vars=["part_id", "site_id"],
                                                     var_name="month", value_name="unplanned_fc")
    fc_long["month"] = fc_long["month"].astype(str)
    log.info("Forecast method mix: %s", pd.Series(best).value_counts().to_dict())
    return fc_long, choice, final, metrics
