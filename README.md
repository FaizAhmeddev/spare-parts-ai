# SpareAI: Spare Parts Inventory Intelligence

> Some spare parts sit unused for years while others run out when they're needed most.
> SpareAI looks at parts usage, maintenance plans, asset reliability and supplier performance.
> It then recommends what to **STOCK**, what to **REORDER** and what to **CLEAR**, with a
> reason and a dollar value for every line.

The prototype runs on 10 Excel files. In production the same engine reads from your ERP/CMMS
database (SAP PM, IBM Maximo, Oracle EAM, Infor, or any SQL replica). You change one line of
configuration and nothing in the analytics changes.

---

## 1. Quick start (5 minutes)

**Requirements:** Python 3.10 or newer. Windows, macOS or Linux.

### Windows
```bat
run_windows.bat
```
This sets up a virtual environment, installs dependencies, runs the analysis and opens the
dashboard at **http://localhost:8010**.

### macOS / Linux
```bash
./run.sh
```

### Manual
```bash
python -m venv .venv
.venv\Scripts\activate            # Windows   (source .venv/bin/activate on mac/linux)
pip install -e .                  # installs the `spareai` package + dependencies
python -m spareai validate        # schema + data-quality checks
python -m spareai run             # full analysis (~15 s)
python -m spareai serve           # dashboard + REST API on http://localhost:8010
```

> **Port:** the dashboard uses **8010** by default, so it won't clash with other apps on 8000.
> To change it: `python -m spareai serve --port 8020`, or set `SPAREAI_PORT=8020`, or edit
> `server.port` in `config/settings.yaml`. If the port is busy, SpareAI tells you instead of failing silently.

### Docker
```bash
docker compose up --build         # runs the analysis, then serves the dashboard on :8010
```

### Run the tests
```bash
pip install pytest
pytest -q                         # 43 tests: maths, data quality, pipeline, API
```

---

## 2. What you get

| Output | Where |
|---|---|
| Web dashboard: overview KPIs, action lists with reasons, part drill-down, data quality, model and backtest pages | `python -m spareai serve` |
| Excel workbook for planners: Summary, Reorder, Stock, Clear, Review, Policy detail, Data Quality, Backtest, Forecast accuracy | `outputs/SpareAI_Recommendations_<date>.xlsx` |
| REST API for ERP/BI integration | `/api/summary`, `/api/recommendations`, `/api/parts/<id>`, `/api/quality`, `POST /api/runs` |
| Results warehouse with full run history (auditable) | `data/warehouse/spareai.db` (SQLite) |
| CLI | `python -m spareai top --action CLEAR`, `python -m spareai part P0045` |

![Overview](docs/screenshots/overview.png)

More screenshots: [recommendations](docs/screenshots/recs.png) · [part drill-down](docs/screenshots/part.png) · [models & backtest](docs/screenshots/models.png) · [data quality](docs/screenshots/quality.png)

A sample planner workbook from the demo run is in `outputs/sample/`, so you can open it before running anything.

### Results on the demo data (as of 2026-10-31)

| | |
|---|---|
| Inventory value | **$720,687** across 306 parts, 3 sites |
| Dead / obsolete stock | **$300,208 (41.7%)** |
| Recommended to clear | **$395,763**, saving **$89k/yr** in holding cost |
| Recommended to buy | **$312,547** (84 reorders, 169 stock / raise-level lines) |
| Critical reorders | **25** vital/essential parts at risk now |
| Data errors caught | all **6 planted duplicate part codes**, **6 open POs on dead parts** |

**Backtest.** The analysis was run on data up to 2025-10-31. The real consumption of the
following 12 months was then replayed through both policies:

| Metric | SpareAI policy | Current min/max | Change |
|---|---|---|---|
| Fill rate | **92.8%** | 89.9% | +2.9 pts |
| Stockout events | **105** | 155 | −32% |
| Average inventory value | **$487k** | $629k | −23% |
| Est. downtime cost of stockouts | **$7.0M** | $12.7M | −45% |
| Purchase orders raised | **210** | 496 | −58% |

So the SpareAI policy gives better service with less capital tied up. That combination is the
whole point of the system.

---

## 3. How it works

```
 ┌────────────── DATA SOURCES (pluggable) ──────────────┐
 │  Excel (today)  │  CSV extract  │  ERP / CMMS via SQL │
 └──────────────────────────┬───────────────────────────┘
                            ▼
 1. INGEST & CONTRACT   normalise columns, enforce types, recompute formula fields
 2. DATA QUALITY        keys, orphans, duplicate part codes (fuzzy), POs on dead parts,
                        overdue PMs/POs, inactive parts in stock …
 3. SEGMENT             ABC (value) · XYZ (variability) · VED (criticality)
                        SBC demand pattern (smooth/erratic/intermittent/lumpy)
                        movement class (regular/slow/declining/dead/never used/obsolete)
 4. DEMAND DRIVERS      ├─ unplanned demand  → forecast tournament per part-site:
                        │   MA, SES, seasonal SES, Croston, SBA, TSB, global ML (GBM),
                        │   median ensemble; chosen by rolling-origin backtest
                        ├─ planned demand    → PM schedules × learned PM consumption ratio
                        └─ failure demand    → Weibull reliability of fitted components
 5. SUPPLY              lead time mean/std learned from PO history, shrunk to supplier stats
 6. POLICY              service-level matrix (VED×ABC) → fill-rate reorder point (Poisson /
                        negative binomial / normal LTD), EOQ with MOQ, Max, insurance spares,
                        stock vs non-stock rule
 7. DECIDE              REORDER · STOCK · CLEAR · ADJUST · REVIEW · OK
                        + reason, qty, $ value, saving, risk, confidence, transfer matching
 8. VALIDATE            12-month policy simulation vs current min/max; real stockouts check
 9. PUBLISH             SQLite warehouse (run history) → dashboard, Excel, REST API
```

The full method, with formulas and references, is in **[docs/METHODOLOGY.md](docs/METHODOLOGY.md)**.

---

## 4. Moving from Excel to real data

1. Build views or extract queries that return the columns in **[docs/DATA_CONTRACT.md](docs/DATA_CONTRACT.md)**.
   **[docs/INTEGRATION_GUIDE.md](docs/INTEGRATION_GUIDE.md)** has example SQL for SAP PM and Maximo.
2. Point SpareAI at the database:
   ```bat
   set SPAREAI_SOURCE=sql
   set SPAREAI_DB_URL=mssql+pyodbc://user:pass@server/ERP?driver=ODBC+Driver+18+for+SQL+Server
   pip install sqlalchemy pyodbc
   python -m spareai validate
   python -m spareai run
   ```
3. Schedule `python -m spareai run` nightly (Windows Task Scheduler, cron or Airflow), or
   trigger it through `POST /api/runs`.

To try it end to end without an ERP:
`python scripts/load_to_database.py sqlite:///data/warehouse/erp_replica.db` loads the demo
data into a SQL database. Then run with `SPAREAI_SOURCE=sql`.

---

## 5. Project layout

```
spare-parts-ai/
├── config/settings.yaml          all business rules & thresholds (service levels, costs, …)
├── data/raw/                     the 10 prototype Excel files
├── src/spareai/
│   ├── ingestion/                schema.py (data contract) · sources.py (Excel/CSV/SQL) · loader.py
│   ├── quality/checks.py         data-quality rules + fuzzy duplicate detection
│   ├── analytics/                demand.py · classification.py · drivers.py (PM, Weibull, lead time) · core.py
│   ├── forecasting/              methods.py (Croston/SBA/TSB/…) · ml.py (global GBM) · engine.py (tournament)
│   ├── inventory/policy.py       safety stock, fill-rate ROP, EOQ, Max
│   ├── recommendation/engine.py  STOCK / REORDER / CLEAR decisions + reasons + transfers
│   ├── evaluation/backtest.py    no-look-ahead policy simulation
│   ├── reporting/excel_report.py planner workbook
│   ├── storage/warehouse.py      run-stamped results store
│   ├── web/                      Flask dashboard + REST API (no CDN, works offline)
│   ├── pipeline.py               orchestration
│   └── cli.py                    command line
├── tests/                        pytest suite
├── scripts/                      CSV export, DB loader, data-contract generator
├── docs/                         methodology, architecture, data contract, integration, user guide
├── Dockerfile · docker-compose.yml · run_windows.bat · run.sh · pyproject.toml
```

## 6. Configuration

Every business rule lives in `config/settings.yaml`. That includes service levels per VED×ABC,
holding and ordering cost, dead-stock threshold, excess-cover months, Weibull shapes per
category and the insurance-spare policy. Planners can tune these without touching code. Any
value can be overridden with an environment variable (`SPAREAI_*`, see `.env.example`).

## 7. Documentation

* [METHODOLOGY.md](docs/METHODOLOGY.md): the analytics, formulas and validation results
* [ARCHITECTURE.md](docs/ARCHITECTURE.md): components, data flow, deployment, security, scaling
* [DATA_CONTRACT.md](docs/DATA_CONTRACT.md): the input tables and columns
* [INTEGRATION_GUIDE.md](docs/INTEGRATION_GUIDE.md): connecting SAP / Maximo / SQL, scheduling, API
* [USER_GUIDE.md](docs/USER_GUIDE.md): how planners use the dashboard and the reports
