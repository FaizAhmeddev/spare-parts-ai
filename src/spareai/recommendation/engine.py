"""Decision engine: turns the policy into STOCK / REORDER / CLEAR actions
with a plain-language reason and the money at stake for every line.

Actions
  REORDER  inventory position at/below the new reorder point -> raise a PO
  STOCK    start stocking a part (new need or insurance spare) or raise
           min/max levels that are too low for the risk
  CLEAR    dead, obsolete or excess stock -> transfer, return, sell or scrap
  ADJUST   stock is fine today but min/max parameters should be lowered
  REVIEW   data problem that blocks an automatic decision (duplicate code,
           open PO on a dead part, ...)
  OK       no action needed
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..inventory.policy import round_up_to_moq

INACTIVE = {"dead", "never_used", "obsolete"}
PRIORITY = {"REORDER": 1, "STOCK": 2, "REVIEW": 3, "CLEAR": 4, "ADJUST": 5, "OK": 9}


def _fmt_money(v: float) -> str:
    return f"${v:,.0f}"


def _confidence(r) -> str:
    pts = 0
    pts += 2 if r.months_with_demand >= 12 else (1 if r.months_with_demand >= 4 else 0)
    pts += 1 if r.lt_observations >= 3 else 0
    pts += 1 if (pd.notna(r.backtest_wape) and r.backtest_wape <= 0.6) else 0
    return "High" if pts >= 3 else ("Medium" if pts >= 2 else "Low")


def _disposal_route(r) -> str:
    if r.lifecycle_status in ("Obsolete", "Superseded"):
        return "Return to supplier or sell to surplus dealer"
    if r.unit_cost_usd < 50:
        return "Scrap / write off"
    return "Sell surplus or return to supplier for credit"


def recommend(pol: pd.DataFrame, cfg, duplicates: pd.DataFrame,
              open_po_dead: pd.DataFrame) -> pd.DataFrame:
    h_rate = float(cfg.get("costs.holding_rate_annual", 0.22))
    excess_m = float(cfg.get("recommendation.excess_cover_months", 24))
    min_clear = float(cfg.get("recommendation.clear_min_value_usd", 25))
    raise_r = float(cfg.get("recommendation.raise_levels_ratio", 1.5))
    reduce_r = float(cfg.get("recommendation.reduce_levels_ratio", 0.5))
    dup_map = dict(zip(duplicates["duplicate_part_id"], duplicates["master_part_id"])) \
        if len(duplicates) else {}
    po_dead = open_po_dead.groupby("part_id")["po_id"].apply(lambda s: ", ".join(s)).to_dict() \
        if len(open_po_dead) else {}

    recs = []
    for r in pol.itertuples():
        reasons: list[str] = []
        action, sub, qty, value, saving, risk_usd = "OK", "", 0, 0.0, 0.0, 0.0
        urgency = ""
        monthly_need = r.annual_demand / 12
        keep = max(r.max_new, math.ceil(excess_m * monthly_need), 1 if r.insurance_spare else 0)
        excess_qty = max(int(r.on_hand_qty - keep), 0)

        # ---------- 1. data problems first
        if r.part_id in dup_map:
            action, sub = "REVIEW", "MERGE_DUPLICATE"
            qty, value = int(r.on_hand_qty), r.on_hand_qty * r.unit_cost_usd
            reasons.append(f"Part code looks like a duplicate of {dup_map[r.part_id]}; "
                           f"{int(r.on_hand_qty)} unit(s) here are counted toward {dup_map[r.part_id]}'s stock")
            reasons.append(f"Merge codes, move stock to {dup_map[r.part_id]} and block {r.part_id}")

        # ---------- 2. inactive parts -> clear
        elif r.movement_class in INACTIVE and r.planned_12m <= 0 and not r.insurance_spare:
            if r.movement_class == "never_used":
                reasons.append("Never issued in the analysis window and no maintenance plan needs it")
            elif r.movement_class == "obsolete":
                reasons.append(f"Part is {r.lifecycle_status} and not moving")
            else:
                reasons.append(f"No issue for {r.months_since_last_issue:.0f} months and no planned need")
            if r.n_assets == 0:
                reasons.append("Not fitted on any active asset")
            if r.on_hand_qty > 0 and r.on_hand_qty * r.unit_cost_usd >= min_clear:
                action, sub = "CLEAR", "DEAD_STOCK"
                qty = int(r.on_hand_qty)
                value = qty * r.unit_cost_usd
                saving = value * h_rate
                reasons.append(f"Free {_fmt_money(value)} of capital, save {_fmt_money(saving)}/yr holding cost")
            if r.part_id in po_dead or r.on_order_qty > 0:
                action = "REVIEW" if action == "OK" else action
                sub = sub or "CANCEL_PO"
                reasons.append(f"Cancel open PO(s) {po_dead.get(r.part_id, '')}".strip()
                               + f" for {int(r.on_order_qty)} unit(s)")
                value = value or r.on_order_qty * r.unit_cost_usd

        # ---------- 3. needs replenishment
        elif r.rop_new > 0 and r.inventory_position <= r.rop_new:
            not_stocked = r.max_level <= 0 and r.on_hand_qty <= 0
            action = "STOCK" if not_stocked else "REORDER"
            sub = "INSURANCE_SPARE" if r.insurance_spare else ("START_STOCKING" if not_stocked else "BELOW_ROP")
            qty = round_up_to_moq(r.max_new - r.inventory_position, r.min_order_qty)
            value = qty * r.unit_cost_usd
            cover = r.days_of_cover
            if not_stocked:
                # never stocked here before: urgency follows the risk, not the empty bin
                urgency = "HIGH" if (r.criticality_ved in ("V", "E") and r.p_stockout_lt >= 0.3) else "NORMAL"
                reasons.append(f"Not stocked at {r.site_id} today; {int(r.issue_events_12m)} issue(s) in last 12 months"
                               + (f", {r.planned_12m:.0f} planned by PM" if r.planned_12m > 0 else ""))
            elif r.available_qty <= 0:
                urgency = "CRITICAL" if r.criticality_ved in ("V", "E") else "HIGH"
                reasons.append("No usable stock on hand")
            elif cover < r.lt_mean_days:
                urgency = "CRITICAL" if r.criticality_ved == "V" else "HIGH"
                reasons.append(f"{cover:.0f} days of cover vs {r.lt_mean_days:.0f}-day lead time")
            else:
                urgency = "HIGH" if cover < 1.5 * r.lt_mean_days else "NORMAL"
            reasons.append(f"Inventory position {r.inventory_position:.0f} <= reorder point {r.rop_new}"
                           f" (LTD {r.ltd_unplanned + r.planned_in_pp:.1f} + safety {r.safety_stock:.1f}"
                           f" at {r.service_level:.0%} service)")
            if r.planned_in_pp > 0:
                reasons.append(f"{r.planned_in_pp:.1f} unit(s) expected for PM work due within lead time"
                               f" ({r.pm_consumption_ratio:.0%} of scheduled PM qty is actually consumed)")
            if r.rel_failures_in_pp > r.ltd_unplanned_forecast and r.rel_failures_in_pp > 0.05:
                reasons.append(f"Wear-out risk: {r.rel_failures_in_pp:.2f} expected failures of fitted parts"
                               f" (oldest at {r.max_life_used_pct:.0%} of rated life)")
            if r.insurance_spare:
                reasons.append(f"Vital part on critical asset: expected downtime risk "
                               f"{_fmt_money(r.insurance_risk_usd)}/yr far exceeds holding cost")
            risk_usd = r.p_stockout_lt * r.stockout_cost_usd
            if r.p_stockout_lt >= 0.005:
                reasons.append(f"{r.p_stockout_lt:.0%} chance of stockout before replenishment,"
                               f" ~{_fmt_money(r.stockout_cost_usd)} downtime per stockout")
            reasons.append(f"Order {qty} (EOQ {r.eoq:.0f}, MOQ {int(r.min_order_qty)}) from "
                           f"{r.preferred_supplier_id}, lead time ~{r.lt_mean_days:.0f}d")

        # ---------- 4. excess stock
        elif excess_qty > 0 and excess_qty * r.unit_cost_usd >= min_clear:
            action, sub = "CLEAR", "EXCESS"
            qty = excess_qty
            value = qty * r.unit_cost_usd
            saving = value * h_rate
            mc = r.months_of_cover
            if np.isfinite(mc):
                reasons.append(f"{int(r.on_hand_qty)} on hand = {mc:.0f} months of cover; keep {keep}, clear {qty}")
            else:
                reasons.append(f"{int(r.on_hand_qty)} on hand with no forecast demand; keep {keep}, clear {qty}")
            reasons.append(f"Free {_fmt_money(value)}, save {_fmt_money(saving)}/yr holding cost")
            if r.max_level > r.max_new:
                reasons.append(f"Lower Max {int(r.max_level)} -> {r.max_new}")

        # ---------- 5. parameter changes
        else:
            if r.rop_new == 0 and not r.stock_item and r.max_level > 0 and r.on_hand_qty <= r.max_level:
                action, sub = "ADJUST", "MAKE_NON_STOCK"
                reasons.append(f"Only {int(r.months_with_demand)} month(s) with demand in history:"
                               f" stop automatic replenishment (min/max {int(r.min_level)}/{int(r.max_level)} -> 0/0),"
                               " buy on demand and use existing stock")
            elif r.rop_new >= max(raise_r * r.reorder_point, r.reorder_point + 1) and r.rop_new > 0:
                action, sub = "STOCK", "RAISE_LEVELS"
                reasons.append(f"Reorder point too low: {int(r.reorder_point)} -> {r.rop_new}"
                               f" (Max {int(r.max_level)} -> {r.max_new})")
                reasons.append(f"Demand {monthly_need:.1f}/month, lead time {r.lt_mean_days:.0f}d,"
                               f" target service {r.service_level:.0%}")
                if r.stockout_events_12m > 0:
                    reasons.append(f"{int(r.stockout_events_12m)} stockout(s) logged in last 12 months")
            elif (r.reorder_point > 0 and r.rop_new <= reduce_r * r.reorder_point
                  and r.max_new < r.max_level):
                action, sub = "ADJUST", "LOWER_LEVELS"
                saving = max(r.max_level - r.max_new, 0) * r.unit_cost_usd * h_rate / 2
                reasons.append(f"Reorder point higher than needed: {int(r.reorder_point)} -> {r.rop_new}"
                               f" (Max {int(r.max_level)} -> {r.max_new})")
                reasons.append(f"Avoids ~{_fmt_money(saving)}/yr of future holding cost")
            else:
                reasons.append("Stock and parameters are within policy")

        recs.append({
            "part_id": r.part_id, "site_id": r.site_id, "description": r.description,
            "category": r.category, "action": action, "sub_action": sub, "urgency": urgency,
            "recommended_qty": int(qty), "value_usd": round(float(value), 2),
            "annual_saving_usd": round(float(saving), 2), "stockout_risk_usd": round(float(risk_usd), 2),
            "confidence": _confidence(r), "reason": " | ".join(reasons),
            "disposal_route": _disposal_route(r) if action == "CLEAR" else "",
        })
    out = pd.DataFrame(recs)
    out = _match_transfers(out, pol)
    urg = out["urgency"].map({"CRITICAL": 0, "HIGH": 1, "NORMAL": 2}).fillna(3)
    out["priority"] = out["action"].map(PRIORITY).fillna(9).astype(int)
    out["_u"] = urg
    out = out.sort_values(["priority", "_u", "value_usd"], ascending=[True, True, False]).drop(columns="_u")
    return out.reset_index(drop=True)


def _match_transfers(recs: pd.DataFrame, pol: pd.DataFrame) -> pd.DataFrame:
    """Use surplus at one site to cover a need at another before buying."""
    recs = recs.copy()
    recs["transfer_from"] = ""
    recs["transfer_qty"] = 0
    need = recs[recs["action"].isin(["REORDER", "STOCK"]) & (recs["recommended_qty"] > 0)]
    give = recs[(recs["action"] == "CLEAR") & (recs["recommended_qty"] > 0)]
    if need.empty or give.empty:
        return recs
    avail = give.set_index(["part_id", "site_id"])["recommended_qty"].to_dict()
    for i, r in need.iterrows():
        for (pid, sid), q in list(avail.items()):
            if pid != r.part_id or sid == r.site_id or q <= 0:
                continue
            t = int(min(q, recs.at[i, "recommended_qty"]))
            if t <= 0:
                continue
            avail[(pid, sid)] = q - t
            recs.at[i, "transfer_from"] = (recs.at[i, "transfer_from"] + f" {sid}").strip()
            recs.at[i, "transfer_qty"] += t
            recs.at[i, "reason"] += f" | Transfer {t} from {sid} surplus instead of buying"
            unit = recs.at[i, "value_usd"] / max(recs.at[i, "recommended_qty"], 1)
            recs.at[i, "recommended_qty"] -= t
            recs.at[i, "value_usd"] = round(unit * recs.at[i, "recommended_qty"], 2)
            if recs.at[i, "recommended_qty"] == 0:
                recs.at[i, "sub_action"] = "TRANSFER_ONLY"
            j = recs.index[(recs["part_id"] == pid) & (recs["site_id"] == sid)][0]
            recs.at[j, "disposal_route"] = f"Transfer {t} to {r.site_id}" + (
                f"; {recs.at[j, 'disposal_route']} for rest" if q - t > 0 else "")
            recs.at[j, "reason"] += f" | {t} unit(s) can cover the need at {r.site_id}"
    return recs
