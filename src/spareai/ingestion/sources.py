"""Pluggable data sources. Add a new ERP/CMMS by subclassing DataSource."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

import pandas as pd

from ..config import Settings

log = logging.getLogger(__name__)


class DataSource(ABC):
    """Returns one raw DataFrame per logical table name."""

    name = "base"

    @abstractmethod
    def read(self, table: str) -> pd.DataFrame: ...

    def describe(self) -> str:
        return self.name


class ExcelSource(DataSource):
    name = "excel"

    def __init__(self, folder: Path, files: dict[str, str]):
        self.folder = Path(folder)
        self.files = files

    def read(self, table: str) -> pd.DataFrame:
        fname = self.files.get(table)
        if not fname:
            raise KeyError(f"No Excel file mapped for table '{table}'")
        path = self.folder / fname
        if not path.exists():
            raise FileNotFoundError(path)
        # First sheet holds the data; README sheets are ignored.
        # openpyxl returns cached values for formula cells.
        df = pd.read_excel(path, sheet_name=0, engine="openpyxl")
        log.debug("Read %s rows from %s", len(df), path.name)
        return df

    def describe(self) -> str:
        return f"excel:{self.folder}"


class CsvSource(DataSource):
    name = "csv"

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def read(self, table: str) -> pd.DataFrame:
        return pd.read_csv(self.folder / f"{table}.csv")

    def describe(self) -> str:
        return f"csv:{self.folder}"


class SqlSource(DataSource):
    """Reads from any SQLAlchemy-supported database (ERP replica, data lake).

    Requires `pip install sqlalchemy` plus the DB driver. Queries are defined
    per table in settings.yaml (source.sql.queries).
    """
    name = "sql"

    def __init__(self, url: str, queries: dict[str, str]):
        if not url:
            raise ValueError("source.sql.url (SPAREAI_DB_URL) is not set")
        try:
            from sqlalchemy import create_engine, text  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise ImportError("SQL source needs: pip install sqlalchemy") from exc
        self._engine = create_engine(url)
        self.queries = queries
        self.url = url

    def read(self, table: str) -> pd.DataFrame:
        from sqlalchemy import text
        with self._engine.connect() as conn:
            return pd.read_sql(text(self.queries[table]), conn)

    def describe(self) -> str:
        return f"sql:{self.url.split('@')[-1]}"   # never log credentials


def build_source(settings: Settings) -> DataSource:
    kind = settings.get("source.type", "excel").lower()
    if kind == "excel":
        return ExcelSource(settings.path("source.excel.path"),
                           settings.get("source.excel.files"))
    if kind == "csv":
        return CsvSource(settings.path("source.csv.path"))
    if kind == "sql":
        return SqlSource(settings.get("source.sql.url"),
                         settings.get("source.sql.queries"))
    raise ValueError(f"Unknown source.type '{kind}'")
