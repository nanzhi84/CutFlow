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


def test_estimate_prices_against_requested_dotted_lipsync_profile() -> None:
    app = create_app()
    with TestClient(app) as client:
        _login(client)
        catalog = c.ProviderPriceCatalog(id="price_test_estimate", provider_id="sandbox", status="published")
        app.state.repository.price_catalogs[catalog.id] = catalog
        # Two providers publish lipsync.video; the dotted profile id
        # "runninghub.heygem.prod" must resolve to provider_id "runninghub.heygem"
        # (not "runninghub"), so its 0.05 price wins over the first-inserted other.
        app.state.repository.price_items["pi_other"] = c.ProviderPriceItem(
            id="pi_other", catalog_id=catalog.id, provider_id="other.lipsync",
            model_id="*", capability_id="lipsync.video", unit="media_second",
            unit_price=c.Money(amount=Decimal("0.01"), currency="CNY"),
        )
        app.state.repository.price_items["pi_heygem"] = c.ProviderPriceItem(
            id="pi_heygem", catalog_id=catalog.id, provider_id="runninghub.heygem",
            model_id="*", capability_id="lipsync.video", unit="media_second",
            unit_price=c.Money(amount=Decimal("0.05"), currency="CNY"),
        )

        response = client.post(
            "/api/jobs/digital-human-video/estimate-cost",
            json={
                "case_id": "case_demo",
                "script": "12345",  # 5 chars -> 1 estimated video second
                "voice": {"voice_id": "voice_sandbox"},
                "lipsync": {"provider_profile_id": "runninghub.heygem.prod"},
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        # 1s * 0.05 (runninghub.heygem) -- NOT 0.01 (other.lipsync, first inserted).
        assert body["video"]["estimated_cost"]["amount"] == "0.05"


def test_estimate_resolves_sandbox_profile_via_stored_provider_id() -> None:
    # The sandbox-family seed profiles use id="sandbox.tts.default" but
    # provider_id="sandbox" (a DIFFERENT convention than the dotted .prod profiles).
    # The estimator must resolve via the stored profile.provider_id, not by string-
    # splitting the id, so the requested profile's price is selected for BOTH
    # conventions (rsplit alone would mis-resolve this to "sandbox.tts").
    app = create_app()
    with TestClient(app) as client:
        _login(client)
        # Start from a clean catalog so only the two competing rows below exist.
        app.state.repository.price_catalogs.clear()
        app.state.repository.price_items.clear()
        catalog = c.ProviderPriceCatalog(id="price_test_estimate", provider_id="sandbox", status="published")
        app.state.repository.price_catalogs[catalog.id] = catalog
        # other.tts inserted first (candidates[0] on a mis-resolve); sandbox second.
        app.state.repository.price_items["pi_other_tts"] = c.ProviderPriceItem(
            id="pi_other_tts", catalog_id=catalog.id, provider_id="other.tts",
            model_id="*", capability_id="tts.speech", unit="input_token",
            unit_price=c.Money(amount=Decimal("0.002"), currency="CNY"),
        )
        app.state.repository.price_items["pi_sandbox_tts"] = c.ProviderPriceItem(
            id="pi_sandbox_tts", catalog_id=catalog.id, provider_id="sandbox",
            model_id="*", capability_id="tts.speech", unit="input_token",
            unit_price=c.Money(amount=Decimal("0.001"), currency="CNY"),
        )

        response = client.post(
            "/api/jobs/digital-human-video/estimate-cost",
            json={
                "case_id": "case_demo",
                "script": "12345",  # 5 chars
                "voice": {"voice_id": "voice_sandbox", "provider_profile_id": "sandbox.tts.default"},
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        # 5 chars * 0.001 (sandbox) = 0.005 -- NOT 0.002 (other.tts, first inserted).
        assert body["tts"]["estimated_cost"]["amount"] == "0.005"


def test_jobs_provider_id_resolves_via_gateway_reader_not_provider_repository() -> None:
    # Same guard as the TTS endpoint, for the jobs estimator: the DB-backed
    # provider_repository has no get_profile; resolution must go through the gateway
    # reader + in-memory fallback (default "sandbox"), never provider_repository.
    from types import SimpleNamespace

    from apps.api.services.jobs_runs import _provider_id_from_profile

    profile = c.ProviderProfile(
        id="reader.lipsync.prod", provider_id="reader.lipsync", model_id="x",
        capability="lipsync.video", display_name="r", environment="prod",
        options_schema_ref=c.ProviderOptionsSchemaRef(schema_id="provider.lipsync.options"),
    )

    class _Reader:
        def get_profile(self, profile_id):
            return profile if profile_id == "reader.lipsync.prod" else None

    class _SqlProviderRepoNoGetProfile:
        pass

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                provider_gateway=SimpleNamespace(provider_reader=_Reader()),
                repository=SimpleNamespace(provider_profiles={}),
                sqlalchemy_provider_repository=_SqlProviderRepoNoGetProfile(),
            )
        )
    )

    assert _provider_id_from_profile(request, "reader.lipsync.prod") == "reader.lipsync"
    assert _provider_id_from_profile(request, "foo.bar.prod") == "foo.bar"
    assert _provider_id_from_profile(request, None) == "sandbox"
