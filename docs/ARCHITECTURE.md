# Architecture

## Components

```
                 ┌──────────────────────────────────────────────────────────┐
  Sources        │ Excel files │ CSV extracts │ ERP/CMMS DB (SQLAlchemy)   │
                 └──────┬───────────────────────────────────────────────────┘
                        │  DataSource.read(table)   (ingestion/sources.py)
                        ▼
  Contract       loader.conform()  → normalised names, types, derived fields
                        ▼
  Quality        quality/checks.py → dq_issues, duplicate map
                        ▼
  Analytics      analytics/core.analyze()
                   ├ demand.py          monthly matrices (total / unplanned)
                   ├ classification.py  ABC / XYZ / VED / SBC / movement
                   ├ drivers.py         PM schedule · Weibull · lead time
                   ├ forecasting/       tournament (stat + ML + ensemble)
                   └ inventory/policy   ROP · SS · EOQ · Max · insurance
                        ▼
  Decisions      recommendation/engine.py → actions + reasons + transfers
                        ▼
  Validation     evaluation/backtest.py  (truncate → re-analyse → simulate)
                        ▼
  Persistence    storage/warehouse.py   SQLite, every row stamped with run_id
                        ▼
  Delivery       web/app.py (Flask: dashboard + REST)  ·  reporting/excel_report.py  ·  cli.py
```

## Design principles

| Principle | How it is implemented |
|---|---|
| **Source-agnostic** | The analytics only see the logical tables in the data contract. Excel, CSV and SQL are interchangeable `DataSource` classes, and a new ERP is one subclass or one set of SQL views |
| **Configuration over code** | Service levels, costs, thresholds and Weibull shapes live in `settings.yaml`. Secrets come from environment variables (`${VAR}` interpolation) |
| **Explainable** | Every recommendation carries a reason, quantities, money and confidence. All formulas are in plain NumPy/SciPy |
| **Auditable** | Every run is stored with its source, row counts, summary and all result tables, with 20 runs retained. Failed runs keep the stack trace |
| **Validated** | The no-look-ahead backtest runs on every run, and 43 automated tests cover the maths, rules, pipeline and API |
| **Light footprint** | The core needs only pandas/NumPy/SciPy/scikit-learn/Flask. The dashboard uses no CDN, so it works on air-gapped plant networks |
| **Fail loudly on bad schema** | Missing required columns raise `SchemaError` naming the table and columns. `spareai validate` returns exit code 1 for schema failures and 2 for data errors (CI-friendly) |

## Runtime

| | Demo data (688 part-sites, 36 months) |
|---|---|
| Full run incl. ML + backtest | ~15 s on a laptop |
| Without ML | ~6 s |

Complexity is linear in part-sites × months. For 50k+ part-sites:

1. Parallelise the per-series statistical methods (`joblib`).
2. Use LightGBM for the global model (`pip install lightgbm`; picked up automatically).
3. Move the warehouse to PostgreSQL (`Warehouse` uses plain SQL).

## Deployment options

| Option | Use |
|---|---|
| Laptop / VM | `run_windows.bat`, or `pip install -e . && python -m spareai serve` |
| Docker | `docker compose up`. The image runs the analysis on start, then serves on :8010. Mount `data/` and `config/` |
| Scheduled batch | Windows Task Scheduler / cron: `python -m spareai run` nightly after the ERP extract |
| Cloud | Container on AWS ECS / Azure Container Apps. Warehouse on RDS / Azure SQL. Trigger via `POST /api/runs` from an orchestrator (Airflow, Step Functions) |

## Security

- Database credentials come only from environment variables. Logs strip the credential part
  of URLs.
- `POST /api/runs` requires the `X-API-Key` header when `SPAREAI_API_KEY` is set.
- For internet-facing deployments, put SpareAI behind your reverse proxy/SSO (Azure AD,
  Okta). The app is read-only except for triggering runs.
- The SQL source executes only the configured SELECT queries. Use a read-only DB user.

## Extending

| Want to… | Do this |
|---|---|
| Add a forecasting method | Add a function to `STAT_METHODS` in `forecasting/methods.py`. The tournament picks it up |
| Add a data-quality rule | Add a block in `quality/checks.py` using `_issue(...)` |
| Add an action / rule | Extend `recommendation/engine.py` and add a test |
| Add an ERP | Write SQL views matching `DATA_CONTRACT.md`, or subclass `DataSource` for an API |
| Add an LLM assistant | Have it read `/api/parts/<id>` and turn the stored `reason` into a conversation. The numbers still come from the engine, not the LLM |
