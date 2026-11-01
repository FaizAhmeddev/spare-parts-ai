# Methodology

This document explains every number SpareAI produces. Nothing comes from a black box: each
recommendation can be traced to a formula, a configured rule and the input data.

---

## 1. Unit of decision

Each decision is made for one **part at one site** (a "part-site"), because stock is held,
counted and reordered per storeroom. The universe of part-sites is the union of:

- current inventory records
- part-sites that had consumption in the window
- part-sites needed by active PM plans

On the demo data that gives 688 part-sites.

## 2. Data quality (before any analytics)

| Check | Why it matters |
|---|---|
| Null / duplicate primary keys, orphan foreign keys, negative quantities | Basic integrity. These come from the data contract in `ingestion/schema.py` |
| **Duplicate part codes** | The same physical part under two codes splits demand history and hides stock. Matching rule: identical normalised part number, *or* same category + identical numeric tokens (sizes, ratings) + descriptions that differ only by abbreviations or typos (`Fltr`→`Filter`, `Mtr`→`Motor`). Numeric tokens stop `Bearing 6203` matching `Bearing 6204`. Distinct words (`Large`/`Small`, `NBR`/`Viton`) are treated as real variants |
| **Open POs on dead/obsolete parts** | Money about to be spent on stock nobody uses |
| Overdue POs, over-receipts, reserved > on hand, min > max, cost mismatches | Wrong inventory position means wrong decisions |
| Inactive (obsolete/superseded) parts still in stock | Candidates to clear |
| Overdue PMs | Their parts are needed now, so they count as immediate demand |
| Issues booked to retired assets, future-dated transactions | Coding errors |

When a duplicate is found, its usable stock is **counted toward the master part's inventory
position**, and the duplicate line becomes `REVIEW / MERGE_DUPLICATE`.

## 3. Segmentation

| Dimension | Rule |
|---|---|
| **ABC** | Pareto on annual usage value (last-12-month qty × unit cost) per part, network-wide. A = first 80% of value, B = next 15%, C = rest |
| **XYZ** | Coefficient of variation of monthly demand: X ≤ 0.5, Y ≤ 1.0, Z > 1.0 |
| **VED** | Criticality from the part master: Vital, Essential, Desirable |
| **Demand pattern (SBC)** | Syntetos–Boylan–Croston: ADI = periods ÷ non-zero periods, CV² of non-zero sizes. Smooth (ADI<1.32, CV²<0.49), erratic, intermittent, lumpy. Fewer than 3 demands = sparse |
| **Movement** | never_used (no issue and no planned need), dead (no issue ≥ 24 months), declining (last 12 m < 50% of prior 12 m), slow (≤ 2 issues/yr), regular. Obsolete/superseded parts that are not moving = obsolete |

## 4. Demand: three separate drivers

Spare-parts demand mixes three different processes. Forecasting them together double-counts
some demand and misses the rest, so SpareAI models each one separately.

### 4.1 Unplanned demand: forecast tournament

The demand history uses only corrective and emergency issues. Planned work is handled in §4.2.

Candidate methods (`forecasting/methods.py`, `forecasting/ml.py`):

| Method | Best for |
|---|---|
| Moving average (6, 12) | Stable, slow parts |
| Simple exponential smoothing | Smooth parts with level shifts |
| Seasonal SES (multiplicative monthly indices) | Seasonal parts (fans, filters in summer) |
| **Croston**, **SBA** (Syntetos–Boylan approximation) | Intermittent demand. SBA removes Croston's upward bias |
| **TSB** (Teunter–Syntetos–Babai) | Intermittent parts at risk of obsolescence. The forecast decays when demand stops |
| **Global gradient-boosting model** | One model across all series (lags, rolling means, seasonality, cost, criticality, installed base) with Poisson loss. Uses LightGBM when installed, otherwise scikit-learn |
| **Median ensemble** | Robust combination of all of the above |

**Selection.** Each method is backtested on rolling origins (4 origins × 3-month windows).
The score is the error of **cumulative demand over the window** (WAPE + ¼·|bias|), not
per-month error. Per-month MAE rewards forecasting zero for intermittent parts, which would
starve the stock. A single method replaces the ensemble only if it beats it by 40% on the
selection origins. The **last origin is held out** and is used only to report honest accuracy.

**Demand floor.** For parts still classed regular or slow, the rate used for policy is at
least 50% of the last-12-month unplanned average. This protects against a forecast that
collapses after a few quiet months.

**Demo result (out-of-sample WAPE, 3-month cumulative):** ensemble 0.90, ma12 0.90, TSB 0.92,
ML 0.96, Croston 1.09. On this synthetic data demand is close to random around its mean, so
simple and combined methods win. That is common for spare parts, and it is why the inventory
backtest (§8), not forecast accuracy, is the headline metric. With real data containing true
seasonality, failure trends and installed-base growth, the ML and seasonal methods have more
signal to work with.

### 4.2 Planned demand: maintenance schedules

Active PM plans on active assets are expanded into dated requirements. Overdue plans count as
due today. Not every PM consumes its listed part (inspections, condition-based swaps), so
SpareAI learns a **PM consumption ratio** per part-site:

```
ratio = (preventive issues last 12m + k·g) / (scheduled qty next 12m + k)      k = 4
```

`g` is the network-wide ratio (0.46 on the demo data). Planned demand inside the protection
period is added to the reorder point as a deterministic amount.

### 4.3 Failure demand: Weibull reliability

For each part fitted on an active asset (from the BOM):

```
η  = expected_life_hours / Γ(1 + 1/β)          β per category (config), β>1 = wear-out
dt = asset hours per day × window days
P(fail in window | survived to t) = 1 − exp(−((t+dt)/η)^β + (t/η)^β)
expected failures = Σ qty_per_asset × P
```

Expected failures inside the protection period act as a **floor** on unplanned lead-time
demand. Old components push stock up even when recent history is quiet.

## 5. Lead time

Received POs give the actual lead time per part (last 12 receipts). This is shrunk toward the
preferred supplier's published average and spread:

```
mean = (n·mean_PO + k·mean_supplier) / (n + k)          k = 3
std  = sqrt((n·sd_PO² + k·sd_supplier²) / (n + k))
```

The late-delivery rate and on-time % are reported alongside.

## 6. Inventory policy

| Step | Formula |
|---|---|
| Service level | Target **fill rate** from the VED × ABC matrix (V: 98–99.5%, E: 95–97%, D: 85–90%). Cheap vital parts get the highest service because holding them costs little |
| Protection period | P = lead time + review period (7 days) |
| Unplanned LTD | μ = max(rate × P, Weibull expected failures in P); σ² = P·σ_d² + rate²·σ_L² |
| Distribution | Normal if μ ≥ 10; negative binomial if σ² > μ (lumpy); Poisson otherwise |
| Order qty | EOQ = √(2·D·S / (h·c)), capped at 12 months of demand, rounded up to MOQ |
| **Reorder point** | Smallest s with fill rate = 1 − E[(LTD − s)⁺] / Q ≥ target, then + planned PM qty in P |
| Max | ROP + Q |
| Stock / non-stock | Stock only if PM needs the part, or ≥ 3 months with demand, or ≥ 1 for V/E parts (recent). Otherwise the part is bought on demand |
| Insurance spare | Vital, active part fitted on an A-critical asset with no demand: hold 1 if P(failure in a year) × cost of a stockout > annual holding cost |
| Stockout cost | Median downtime cost/hour of the assets using the part × median downtime hours per stockout for that VED class (learned from the stockout log) |

Using a fill-rate reorder point instead of a cycle-service quantile matters for lumpy parts. A
quantile often says "ROP = 0" because most lead times see no demand. But when demand comes it
is large, and the fill-rate formula accounts for that.

## 7. Decision rules (`recommendation/engine.py`)

The rules are evaluated in order. The first rule that matches wins:

1. **REVIEW / MERGE_DUPLICATE**: the part code is a duplicate.
2. **CLEAR / DEAD_STOCK**: dead, never used or obsolete, with no PM need and not an insurance
   spare. The disposal route depends on lifecycle and cost. Any open PO adds **REVIEW /
   CANCEL_PO**.
3. **REORDER / BELOW_ROP**: inventory position (available + on order + stock held under a
   duplicate code) ≤ new ROP. Qty = Max − position, rounded to MOQ. Urgency: CRITICAL when V/E
   has no usable stock or cover is shorter than lead time. **STOCK / START_STOCKING** or
   **INSURANCE_SPARE** applies when the part is not stocked at that site yet.
4. **CLEAR / EXCESS**: on hand above max(new Max, 24 months of need). The extra quantity is
   cleared.
5. **STOCK / RAISE_LEVELS**: stock is fine today but the current ROP is too low (new ROP ≥ 1.5 ×
   current). **ADJUST / LOWER_LEVELS** applies when both ROP and Max can come down.
   **ADJUST / MAKE_NON_STOCK** applies to rare parts that should not be auto-replenished.
6. **OK**.

**Transfer matching.** Before anything is bought, surplus at another site (CLEAR lines) is
matched to needs elsewhere, and the purchase quantity is reduced.

**Confidence** (High / Medium / Low) is based on how many months had demand, how many
lead-time observations exist, and the backtest accuracy.

## 8. Validation: policy backtest

`evaluation/backtest.py`:

1. Cut all data at as_of − 12 months (2025-10-31). POs received after the cutoff are
   un-received, so there is no look-ahead.
2. Re-run the full analytics on the cut data.
3. Replay the **actual daily consumption** of the next 12 months through a daily (s, S)
   simulation for every inventory part-site. Run it once with the AI policy and once with the
   current min/max from the inventory file.
4. Check every **real logged stockout** in that year: did the simulated AI policy have stock on
   that day?

| Metric | SpareAI | Current min/max |
|---|---|---|
| Fill rate | 92.8% | 89.9% |
| Stockout events | 105 | 155 |
| Avg inventory value | $487k | $629k |
| Est. downtime cost | $7.0M | $12.7M |
| POs raised | 210 | 496 |
| Real stockouts with stock available (of 53) | 31 | 32 (simulated) |

**Simplifications** (stated for reviewers):

- Both policies start at their own Max.
- Lead times are deterministic at the learned mean.
- Unmet demand is counted as lost.
- Historical PM schedules can't be reconstructed, so the backtest forecasts total demand.
- The reliability floor is off in the backtest.

The live system uses all drivers, so its decisions should be at least as good as the backtest.

## 9. References

- Croston, J.D. (1972). Forecasting and stock control for intermittent demands. *ORQ* 23(3).
- Syntetos, A.A., Boylan, J.E. (2005). The accuracy of intermittent demand estimates. *IJF* 21(2).
- Syntetos, A.A., Boylan, J.E., Croston, J.D. (2005). On the categorization of demand patterns. *JORS* 56.
- Teunter, R., Syntetos, A., Babai, Z. (2011). Intermittent demand: linking forecasting to inventory obsolescence. *EJOR* 214(3).
- Silver, E.A., Pyke, D.F., Thomas, D.J. (2017). *Inventory and Production Management in Supply Chains*, 4th ed. (fill-rate ROP, loss functions).
- Kennedy, W.J., Patterson, J.W., Fredendall, L.D. (2002). An overview of recent literature on spare parts inventories. *IJPE* 76.
