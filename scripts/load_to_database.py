"""Load the prototype data into a SQL database and create the spareai_* views
the SQL source reads from - a rehearsal of the production ERP integration.

Usage:
  pip install sqlalchemy
  python scripts/load_to_database.py sqlite:///data/warehouse/erp_replica.db
  set SPAREAI_SOURCE=sql
  set SPAREAI_DB_URL=sqlite:///data/warehouse/erp_replica.db
  python -m spareai run
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine  # noqa: E402

from spareai.config import load_settings  # noqa: E402
from spareai.ingestion.schema import TABLES  # noqa: E402
from spareai.ingestion.sources import ExcelSource  # noqa: E402

url = sys.argv[1] if len(sys.argv) > 1 else "sqlite:///data/warehouse/erp_replica.db"
s = load_settings()
src = ExcelSource(s.path("source.excel.path"), s.get("source.excel.files"))
eng = create_engine(url)
for t in TABLES:
    df = src.read(t)
    df.to_sql(f"spareai_{t}", eng, if_exists="replace", index=False)
    print(f"loaded spareai_{t:18s} {len(df):6d} rows")
print(f"\nDone. Set SPAREAI_SOURCE=sql and SPAREAI_DB_URL={url}")
