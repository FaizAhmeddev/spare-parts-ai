import pytest


@pytest.fixture(scope="module")
def client(run_result, settings):
    from spareai.web.app import create_app
    return create_app(settings).test_client()


@pytest.mark.parametrize("url", ["/", "/recommendations", "/recommendations?action=CLEAR",
                                 "/part/P0045", "/quality", "/models", "/runs",
                                 "/export/recommendations.csv", "/export/report.xlsx"])
def test_pages_render(client, url):
    assert client.get(url).status_code == 200


def test_unknown_part_404(client):
    assert client.get("/part/DOES-NOT-EXIST").status_code == 404


def test_api_recommendations_filter(client):
    data = client.get("/api/recommendations?action=REORDER&limit=5").get_json()
    assert data["total"] > 0 and len(data["items"]) <= 5
    assert all(i["action"] == "REORDER" for i in data["items"])


def test_api_health_and_part(client):
    assert client.get("/api/health").get_json()["status"] == "ok"
    assert "policy" in client.get("/api/parts/P0001").get_json()


def test_trigger_requires_key_when_configured(settings, run_result):
    from spareai.web.app import create_app
    settings.override("security.api_key", "secret")
    try:
        c = create_app(settings).test_client()
        assert c.post("/api/runs").status_code == 401
    finally:
        settings.override("security.api_key", "")




def test_simple_pages_render(client):
    assert client.get("/").status_code == 200
    assert client.get("/?status=ORDER&q=bearing").status_code == 200
    assert client.get("/product/P0002").status_code == 200
    assert client.get("/product/NOPE").status_code == 404


def test_used_numbers_match_raw_issue_history(dataset, run_result, settings):
    """Product page 'used' figures must equal a plain sum of the raw issues."""
    from spareai.storage.warehouse import Warehouse
    from spareai.web import simple
    wh = Warehouse(settings.path("warehouse.path"))
    run = wh.latest_run()
    cons = dataset["consumption"]
    for pid in ["P0002", "P0045", "P0197"]:
        d = simple.product_detail(pid, wh.read("monthly_demand", run["run_id"]),
                                  wh.read("forecast", run["run_id"]), run["as_of_date"])
        c = cons[cons["part_id"] == pid]
        assert d["last_month"] == c[c["txn_date"] >= "2026-10-01"]["qty_issued"].sum()
        assert d["last_year"] == c[c["txn_date"] >= "2025-11-01"]["qty_issued"].sum()
        assert d["last_3_years"] == c[c["txn_date"] >= "2023-11-01"]["qty_issued"].sum()
        assert d["required"] == int(d["by_site"]["required_next_year"].sum())
