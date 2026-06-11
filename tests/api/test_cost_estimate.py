from decimal import Decimal

from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.core import contracts as c


def _login(client: TestClient) -> None:
    response = client.post("/api/auth/login", json={"email": "admin@local.cutagent", "password": "local-admin"})
    assert response.status_code == 200, response.text


def _seed_prices(app) -> None:
    catalog = c.ProviderPriceCatalog(id="price_test_estimate", provider_id="sandbox", status="published")
    app.state.repository.price_catalogs[catalog.id] = catalog
    app.state.repository.price_items["price_test_tts"] = c.ProviderPriceItem(
        id="price_test_tts",
        catalog_id=catalog.id,
        provider_id="sandbox",
        model_id="*",
        capability_id="tts.speech",
        unit="input_token",
        unit_price=c.Money(amount=Decimal("0.001"), currency="CNY"),
    )
    app.state.repository.price_items["price_test_video"] = c.ProviderPriceItem(
        id="price_test_video",
        catalog_id=catalog.id,
        provider_id="sandbox",
        model_id="*",
        capability_id="lipsync.video",
        unit="media_second",
        unit_price=c.Money(amount=Decimal("0.05"), currency="CNY"),
    )


def test_estimate_digital_human_video_cost_prices_tts_video_and_total() -> None:
    app = create_app()
    with TestClient(app) as client:
        _login(client)
        _seed_prices(app)

        response = client.post(
            "/api/jobs/digital-human-video/estimate-cost",
            json={
                "case_id": "case_demo",
                "script": "1234567890" * 4,
                "voice": {"voice_id": "voice_sandbox"},
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["tts_characters"] == 40
        assert body["estimated_video_seconds"] == 8
        assert body["tts"]["unpriced"] is False
        assert body["video"]["unpriced"] is False
        assert body["tts"]["estimated_cost"]["amount"] == "0.040"
        assert body["video"]["estimated_cost"]["amount"] == "0.40"
        assert body["total"]["estimated_cost"]["amount"] == "0.440"


def test_estimate_digital_human_video_cost_marks_missing_video_price_unpriced() -> None:
    app = create_app()
    with TestClient(app) as client:
        _login(client)

        response = client.post(
            "/api/jobs/digital-human-video/estimate-cost",
            json={
                "case_id": "case_demo",
                "script": "短脚本",
                "voice": {"voice_id": "voice_sandbox"},
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["video"]["unpriced"] is True
        assert body["total"]["unpriced"] is True
