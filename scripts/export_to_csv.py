"""Export the Excel prototype data to one CSV per logical table.

Usage:  python scripts/export_to_csv.py            -> data/csv/*.csv
Then:   python -m spareai run --source csv
Useful as the hand-off format for an ERP extract job.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from spareai.config import load_settings  # noqa: E402
from spareai.ingestion.sources import ExcelSource  # noqa: E402
from spareai.ingestion.schema import TABLES  # noqa: E402

s = load_settings()
src = ExcelSource(s.path("source.excel.path"), s.get("source.excel.files"))
out = s.path("source.csv.path")
out.mkdir(parents=True, exist_ok=True)
for t in TABLES:
    df = src.read(t)
    df.to_csv(out / f"{t}.csv", index=False)
    print(f"{t:20s} {len(df):6d} rows -> {out / (t + '.csv')}")
