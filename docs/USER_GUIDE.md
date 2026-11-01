# User guide (planners & maintenance managers)

## Daily routine (10 minutes)

1. Open **Overview**. Check the *Critical reorders* card. These are vital/essential parts
   that will probably run out before a new order can arrive.
2. Click **Recommendations → urgency CRITICAL**. Read the *Why* column, then raise the
   purchase requisitions. The quantity is already rounded to the supplier's MOQ. If *transfer*
   is shown, move stock from the other site first.
3. Check **Data quality → ERROR** once a week. Fixing duplicates and cancelling POs on dead
   parts gives quick wins.

## Monthly routine

| Task | Where |
|---|---|
| Clearance review with finance | Recommendations → CLEAR (or the *Clear* sheet in the Excel report). Choose a route: transfer, return to supplier, sell, scrap |
| Update min/max in the ERP | *Policy Detail* sheet: `rop_new`, `max_new` per part-site. Start with STOCK/RAISE_LEVELS and ADJUST lines |
| Review new insurance spares | Recommendations, sub-action *insurance spare* |
| Check model health | Models & backtest page: fill rate and stockouts vs current policy |

## Reading a recommendation

```
REORDER  P0197 @ S01  Emergency Stop Button   qty 15   $4,215   CRITICAL   Medium
 • 37 days of cover vs 49-day lead time
 • Inventory position 3 <= reorder point 8 (LTD 5.9 + safety 2.1 at 99% service)
 • 2.3 unit(s) expected for PM work due within lead time (46% of scheduled PM qty is consumed)
 • 100% chance of stockout before replenishment, ~$613,890 downtime per stockout
 • Order 15 (EOQ 8, MOQ 5) from SUP028, lead time ~49d
```

- **Inventory position** = usable on hand + on order (+ stock under a duplicate code).
- **LTD** = demand expected during lead time + review period. **Safety** is the buffer for
  variability at the target service level.
- **Confidence** is Low when the part has very little history. Apply judgement there.

## Actions

| Action | Meaning | What to do |
|---|---|---|
| REORDER | Below the reorder point | Raise a PR for the quantity shown |
| STOCK | Start stocking here, hold an insurance spare, or raise min/max | Create or raise the MRP parameters and buy the initial quantity |
| CLEAR | Dead, obsolete or excess stock | Follow the route. Lower the Max so the stock isn't rebought |
| ADJUST | Min/max too high, or the part should be non-stock | Update the ERP parameters |
| REVIEW | Data problem | Merge the duplicate code, or cancel the PO |
| OK | Nothing to do | |

## Tuning (with your administrator)

Edit `config/settings.yaml` and re-run:

- `service_levels`: raise these for parts where downtime is very expensive.
- `classification.dead_months`: shorten or lengthen the dead-stock definition.
- `recommendation.excess_cover_months`: how much cover counts as excess.
- `costs.holding_rate_annual`, `costs.order_cost_usd`: these drive order quantities.
