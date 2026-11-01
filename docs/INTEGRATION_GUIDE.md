# Integration guide: from Excel to live ERP data

The engine reads 10 logical tables (see [DATA_CONTRACT.md](DATA_CONTRACT.md)). Moving to
production means delivering those tables from your systems. Analytics, dashboard and reports
stay the same.

## Option A: SQL views on an ERP/CMMS replica (recommended)

1. Create a read-only schema with views named `spareai_<table>` that return the contract
   columns.
2. Set:
   ```
   SPAREAI_SOURCE=sql
   SPAREAI_DB_URL=<SQLAlchemy URL>
   ```
   | Database | URL example | Driver |
   |---|---|---|
   | SQL Server | `mssql+pyodbc://user:pw@host/db?driver=ODBC+Driver+18+for+SQL+Server` | `pip install pyodbc` |
   | PostgreSQL | `postgresql+psycopg2://user:pw@host:5432/db` | `pip install psycopg2-binary` |
   | Oracle | `oracle+oracledb://user:pw@host:1521/?service_name=ORCL` | `pip install oracledb` |
   | SAP HANA | `hana://user:pw@host:30015` | `pip install sqlalchemy-hana` |
3. If your view names differ, edit the queries under `source.sql.queries` in `settings.yaml`.
4. `python -m spareai validate`, then `python -m spareai run`.

### Typical field mapping: SAP PM / MM

| Contract table | SAP source |
|---|---|
| part_master | MARA, MAKT, MARC (lead time `PLIFZ`, MRP params), MBEW (price), EINA/EINE (vendor) |
| suppliers | LFA1 + vendor evaluation (on-time) |
| assets | EQUI, EQKT, ITOB (criticality `ABCKZ`) |
| bom | EQST/STPO (equipment BOM) + measuring documents IMRG for running hours |
| inventory | MARD (unrestricted `LABST`), RESB (reservations), EKPO/EKET (open PO qty), MARC (`MINBE`, `MABST`) |
| consumption | MSEG/MATDOC movement types 261/262 (goods issue to order), AUFK for order type |
| work_orders | AUFK, AFIH, QMEL |
| purchase_orders | EKKO, EKPO, EKET (promised), EKBE (GR date) |
| maintenance_plans | MPLA, MPOS, PLPO/STPO (task list components) |
| stockouts | Usually not in ERP: derive from orders with "waiting for parts" status, or log in CMMS |

Example view (SQL Server, SAP replica):
```sql
CREATE VIEW spareai_consumption AS
SELECT  m.MBLNR + '-' + m.ZEILE              AS txn_id,
        CAST(m.BUDAT_MKPF AS date)           AS txn_date,
        m.MATNR                              AS part_id,
        m.WERKS                              AS site_id,
        a.EQUNR                              AS asset_id,
        m.AUFNR                              AS wo_id,
        CASE WHEN m.BWART = '261' THEN m.MENGE ELSE -m.MENGE END AS qty_issued,
        CASE a.AUART WHEN 'PM02' THEN 'Preventive'
                     WHEN 'PM03' THEN 'Emergency' ELSE 'Corrective' END AS issue_reason
FROM    MSEG m
JOIN    AFIH a ON a.AUFNR = m.AUFNR
WHERE   m.BWART IN ('261','262');
```

### Typical field mapping: IBM Maximo

| Contract table | Maximo source |
|---|---|
| part_master | ITEM, INVENTORY (`DELIVERYTIME`, `ORDERQTY`), INVCOST |
| suppliers | COMPANIES |
| assets | ASSET (`PRIORITY` → criticality), METERREADING for hours |
| bom | SPAREPART |
| inventory | INVBALANCES, INVENTORY (`MINLEVEL`, `MAXLEVEL`, `REORDER`) |
| consumption | MATUSETRANS (`ISSUETYPE='ISSUE'`) joined to WORKORDER.WORKTYPE |
| work_orders | WORKORDER |
| purchase_orders | PO, POLINE, MATRECTRANS |
| maintenance_plans | PM, JOBPLAN, JOBMATERIAL |

## Option B: file extracts (CSV)

If a database connection isn't allowed, have the ERP job export `<table>.csv` files to a
shared folder:
```
SPAREAI_SOURCE=csv        # reads source.csv.path (default data/csv)
```
`python scripts/export_to_csv.py` shows the exact format.

## Option C: custom connector (REST APIs, SaaS CMMS)

Subclass `DataSource` and return one DataFrame per table:
```python
class FiixSource(DataSource):
    name = "fiix"
    def read(self, table):
        return pd.DataFrame(call_api(table))
```
Register it in `build_source()`.

## Scheduling

| Platform | Command |
|---|---|
| Windows Task Scheduler | Program: `C:\spareai\.venv\Scripts\python.exe`, args `-m spareai run`, start in `C:\spareai` |
| cron | `30 2 * * * cd /opt/spareai && .venv/bin/python -m spareai run >> outputs/cron.log 2>&1` |
| Airflow / API | `POST http://host:8010/api/runs` with header `X-API-Key` |

## REST API

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/health` | Status and latest run id |
| GET | `/api/summary` | KPIs of the latest run |
| GET | `/api/recommendations?action=REORDER&site=S01&urgency=CRITICAL&q=bearing&limit=100&offset=0` | Filtered actions |
| GET | `/api/parts/{part_id}` | Policy, recommendations, forecast for one part |
| GET | `/api/quality` | Data-quality issues |
| GET / POST | `/api/runs` | Run history / start a new run (202, async) |
| GET | `/export/recommendations.csv` | Same filters as the API |
| GET | `/export/report.xlsx` | Latest planner workbook |

**Writing back to the ERP** (closing the loop): read `/api/recommendations?action=REORDER` and
create purchase requisitions (SAP BAPI_PR_CREATE / Maximo PR). Use the new `rop_new`/`max_new`
from `/api/parts/{id}` to update MRP parameters. Keep a human approval step at first, and
automate low-value C-class lines once trust is built.

## Go-live checklist

- [ ] `spareai validate` passes with no ERRORs (or they are understood)
- [ ] Service-level matrix and costs agreed with maintenance and finance
- [ ] Backtest reviewed on real history: fill rate and inventory vs current policy
- [ ] Planners trained on the dashboard (see USER_GUIDE.md)
- [ ] Nightly schedule and log monitoring in place
- [ ] Read-only DB user, API key set, behind SSO/reverse proxy
