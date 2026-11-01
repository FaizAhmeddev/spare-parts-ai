"""SpareAI web dashboard + REST API (Flask).

Run:  python -m spareai serve        ->  http://localhost:8010
"""
from __future__ import annotations

import io
import logging
import threading
from functools import wraps
from pathlib import Path

import numpy as np
import pandas as pd
from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_file, url_for)

from ..config import Settings, load_settings
from ..storage.warehouse import Warehouse
from . import charts

log = logging.getLogger(__name__)
ACTIONS = ["REORDER", "STOCK", "CLEAR", "REVIEW", "ADJUST", "OK"]
_run_lock = threading.Lock()
_run_state = {"running": False, "last_error": None, "last_run_id": None}


def _clean(df: pd.DataFrame) -> list[dict]:
    return df.replace([np.inf, -np.inf], None).astype(object).where(df.notna(), None).to_dict("records")


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or load_settings()
    app = Flask(__name__, template_folder="templates", static_folder="static")
    wh = Warehouse(settings.path("warehouse.path"))
    app.config["SETTINGS"] = settings

    # ------------------------------------------------------------ helpers
    def latest():
        run = wh.latest_run()
        if not run:
            return None
        return run

    def table(name: str, run=None, **kw) -> pd.DataFrame:
        run = run or latest()
        return wh.read(name, run["run_id"], **kw) if run else pd.DataFrame()

    def require_key(f):
        @wraps(f)
        def inner(*a, **k):
            key = settings.get("security.api_key")
            if key and request.headers.get("X-API-Key") != key and request.form.get("api_key") != key:
                return jsonify({"error": "invalid or missing X-API-Key"}), 401
            return f(*a, **k)
        return inner

    def filter_recs(df: pd.DataFrame, args) -> pd.DataFrame:
        if df.empty:
            return df
        if args.get("action"):
            df = df[df["action"] == args["action"]]
        if args.get("site"):
            df = df[df["site_id"] == args["site"]]
        if args.get("urgency"):
            df = df[df["urgency"] == args["urgency"]]
        if args.get("category"):
            df = df[df["category"] == args["category"]]
        q = (args.get("q") or "").strip().lower()
        if q:
            df = df[df["part_id"].str.lower().str.contains(q) |
                    df["description"].fillna("").str.lower().str.contains(q)]
        if args.get("hide_ok", "1") == "1" and not args.get("action"):
            df = df[df["action"] != "OK"]
        return df

    @app.context_processor
    def inject():
        return {"run": latest(), "company": settings.get("project.company"),
                "run_state": _run_state}

    @app.template_filter("money")
    def money(v):
        try:
            return f"${float(v):,.0f}"
        except (TypeError, ValueError):
            return "-"

    @app.template_filter("pct")
    def pct(v):
        try:
            return f"{float(v):.1%}"
        except (TypeError, ValueError):
            return "-"

    @app.template_filter("num")
    def num(v, d=0):
        try:
            f = float(v)
            if not np.isfinite(f):
                return "∞"
            return f"{f:,.{d}f}"
        except (TypeError, ValueError):
            return "-"

    # -------------------------------------------------------------- pages
    @app.route("/")
    def overview():
        run = latest()
        if not run:
            return render_template("empty.html")
        s = run["summary"]
        recs = table("recommendations", run)
        pol = table("policy", run)
        act = recs[recs["action"] != "OK"].groupby("action").size().reindex(ACTIONS[:-1]).fillna(0)
        action_chart = charts.hbar([(a, float(v)) for a, v in act.items()])
        mv = pol.groupby("movement_class")["stock_value_usd"].sum().sort_values(ascending=False)
        movement_chart = charts.hbar([(k.replace("_", " "), float(v)) for k, v in mv.items()], "money")
        cat = recs[recs["action"] == "CLEAR"].groupby("category")["value_usd"].sum() \
            .sort_values(ascending=False).head(8)
        clear_chart = charts.hbar([(k, float(v)) for k, v in cat.items()], "money", color_var="--series-3")
        urgent = recs[recs["action"].isin(["REORDER", "STOCK"]) & (recs["urgency"] == "CRITICAL")].head(10)
        clear = recs[recs["action"] == "CLEAR"].sort_values("value_usd", ascending=False).head(10)
        bt = table("backtest_summary", run)
        val = table("stockout_validation", run)
        return render_template("overview.html", s=s, action_chart=action_chart,
                               movement_chart=movement_chart, clear_chart=clear_chart,
                               urgent=_clean(urgent), clear=_clean(clear), bt=_clean(bt),
                               val_n=len(val), val_ai=int(val["ai_had_stock"].sum()) if len(val) else 0,
                               val_lg=int(val["legacy_had_stock"].sum()) if len(val) else 0)

    @app.route("/recommendations")
    def recommendations():
        recs = table("recommendations")
        if recs.empty:
            return redirect(url_for("overview"))
        f = filter_recs(recs, request.args)
        page = max(int(request.args.get("page", 1)), 1)
        per = 50
        totals = {"n": len(f), "value": f["value_usd"].sum(), "saving": f["annual_saving_usd"].sum()}
        return render_template("recommendations.html", rows=_clean(f.iloc[(page - 1) * per: page * per]),
                               totals=totals, page=page, pages=max((len(f) - 1) // per + 1, 1),
                               args=request.args, actions=ACTIONS,
                               sites=sorted(recs["site_id"].unique()),
                               categories=sorted(recs["category"].dropna().unique()))

    @app.route("/part/<part_id>")
    def part(part_id):
        run = latest()
        if not run:
            return redirect(url_for("overview"))
        pm = wh.read("ref_part_master", where="part_id = ?", params=(part_id,))
        if pm.empty:
            abort(404)
        pol = table("policy", run, where="part_id = ?", params=(part_id,))
        recs = table("recommendations", run, where="part_id = ?", params=(part_id,))
        hist = table("monthly_demand", run, where="part_id = ?", params=(part_id,))
        fc = table("forecast", run, where="part_id = ?", params=(part_id,))
        months = pd.period_range(end=pd.Period(run["as_of_date"], "M"), periods=36, freq="M").astype(str)
        h = hist.groupby("month")["qty"].sum().reindex(months, fill_value=0)
        f = fc.groupby("month")[["unplanned_fc", "planned_qty"]].sum()
        chart = charts.demand_chart([(m, float(v)) for m, v in h.items()],
                                    [(m, float(r.unplanned_fc), float(r.planned_qty)) for m, r in f.iterrows()])
        bom = wh.read("ref_bom", where="part_id = ?", params=(part_id,))
        assets = wh.read("ref_assets")
        if len(bom):
            bom = bom.merge(assets[["asset_id", "asset_name", "site_id", "criticality", "status"]],
                            on="asset_id", how="left")
            bom["life_used"] = bom["hours_since_replacement"] / bom["expected_life_hours"]
        pos = wh.read("ref_purchase_orders", where="part_id = ?", params=(part_id,)) \
            .sort_values("order_date", ascending=False).head(12)
        pms = wh.read("ref_maintenance_plans", where="part_id = ?", params=(part_id,))
        so = wh.read("ref_stockouts", where="part_id = ?", params=(part_id,))
        return render_template("part.html", p=_clean(pm)[0], pol=_clean(pol), recs=_clean(recs),
                               chart=chart, bom=_clean(bom), pos=_clean(pos), pms=_clean(pms),
                               so=_clean(so))

    @app.route("/quality")
    def quality():
        dq = table("dq_issues")
        sev = request.args.get("severity", "")
        if sev and len(dq):
            dq = dq[dq["severity"] == sev]
        counts = dq.groupby(["severity", "check"]).size().reset_index(name="n") if len(dq) else dq
        return render_template("quality.html", rows=_clean(dq), counts=_clean(counts), sev=sev)

    @app.route("/models")
    def models():
        run = latest()
        if not run:
            return redirect(url_for("overview"))
        s = run["summary"]
        acc = sorted(s.get("forecast_holdout_wape", {}).items(), key=lambda kv: kv[1])
        pol = table("policy", run)
        mix = pol["method"].value_counts()
        mix_chart = charts.hbar([(k, float(v)) for k, v in mix.items()])
        pattern = pol["demand_pattern"].value_counts()
        pattern_chart = charts.hbar([(k.replace("_", " "), float(v)) for k, v in pattern.items()],
                                    color_var="--series-2")
        abcxyz = pd.crosstab(pol["abc_class"], pol["xyz_class"]).reindex(index=list("ABC"), columns=list("XYZ"),
                                                                         fill_value=0)
        bt = table("backtest_summary", run)
        val = table("stockout_validation", run)
        return render_template("models.html", s=s, acc=acc, mix_chart=mix_chart, pattern_chart=pattern_chart,
                               abcxyz=abcxyz, bt=_clean(bt), val=_clean(val))

    @app.route("/runs")
    def runs():
        return render_template("runs.html", runs=_clean(wh.runs()))

    # ------------------------------------------------------------ exports
    @app.route("/export/recommendations.csv")
    def export_csv():
        f = filter_recs(table("recommendations"), request.args)
        buf = io.BytesIO(f.drop(columns=["run_id"], errors="ignore").to_csv(index=False).encode("utf-8-sig"))
        return send_file(buf, mimetype="text/csv", as_attachment=True, download_name="spareai_recommendations.csv")

    @app.route("/export/report.xlsx")
    def export_xlsx():
        run = latest()
        files = (run or {}).get("summary", {}).get("reports", [])
        if not files or not Path(files[0]).exists():
            abort(404)
        return send_file(files[0], as_attachment=True)

    # ---------------------------------------------------------------- API
    @app.get("/api/health")
    def api_health():
        run = latest()
        return jsonify({"status": "ok", "latest_run": run["run_id"] if run else None,
                        "running": _run_state["running"]})

    @app.get("/api/summary")
    def api_summary():
        run = latest()
        return jsonify(run or {})

    @app.get("/api/recommendations")
    def api_recs():
        f = filter_recs(table("recommendations"), request.args)
        limit, offset = int(request.args.get("limit", 100)), int(request.args.get("offset", 0))
        return jsonify({"total": len(f), "items": _clean(f.iloc[offset:offset + limit])})

    @app.get("/api/parts/<part_id>")
    def api_part(part_id):
        pol = table("policy", where="part_id = ?", params=(part_id,))
        if pol.empty:
            return jsonify({"error": "not found"}), 404
        recs = table("recommendations", where="part_id = ?", params=(part_id,))
        fc = table("forecast", where="part_id = ?", params=(part_id,))
        return jsonify({"policy": _clean(pol), "recommendations": _clean(recs), "forecast": _clean(fc)})

    @app.get("/api/quality")
    def api_quality():
        return jsonify(_clean(table("dq_issues")))

    @app.get("/api/runs")
    def api_runs():
        return jsonify(_clean(wh.runs()))

    @app.post("/api/runs")
    @require_key
    def api_trigger():
        if not _run_lock.acquire(blocking=False):
            return jsonify({"status": "already running"}), 409

        def job():
            from ..pipeline import run_pipeline
            _run_state.update(running=True, last_error=None)
            try:
                res = run_pipeline(load_settings())
                _run_state["last_run_id"] = res["run_id"]
            except Exception as exc:          # surfaced on /runs
                _run_state["last_error"] = str(exc)
            finally:
                _run_state["running"] = False
                _run_lock.release()

        threading.Thread(target=job, daemon=True).start()
        if request.form:
            return redirect(url_for("runs"))
        return jsonify({"status": "started"}), 202

    return app
