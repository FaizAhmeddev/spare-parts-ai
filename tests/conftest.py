import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from spareai.config import load_settings  # noqa: E402


@pytest.fixture(scope="session")
def settings(tmp_path_factory):
    s = load_settings(ROOT / "config" / "settings.yaml")
    tmp = tmp_path_factory.mktemp("spareai")
    s.override("warehouse.path", str(tmp / "test.db"))
    s.override("output.dir", str(tmp / "out"))
    s.override("logging.file", str(tmp / "test.log"))
    s.override("source.type", "excel")
    return s


@pytest.fixture(scope="session")
def dataset(settings):
    from spareai.ingestion.loader import load_dataset
    from spareai.ingestion.sources import build_source
    return load_dataset(build_source(settings))


@pytest.fixture(scope="session")
def run_result(settings):
    from spareai.pipeline import run_pipeline
    return run_pipeline(settings, use_ml=True, backtest=True, reports=True)
