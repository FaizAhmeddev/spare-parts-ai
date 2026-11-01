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
