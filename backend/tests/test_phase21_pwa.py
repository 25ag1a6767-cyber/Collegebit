from fastapi.testclient import TestClient
from app.main import app


def test_manifest_is_scoped_to_customer_app():
    with TestClient(app) as client:
        response = client.get("/manifest.webmanifest")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/manifest+json")
        payload = response.json()
        assert payload["id"] == "/customer/"
        assert payload["lang"] == "en"
        assert payload["name"] == "CollegeBits Customer"
        assert payload["short_name"] == "CollegeBits"
        assert payload["start_url"] == "/customer/"
        assert payload["scope"] == "/customer/"
        assert payload["display"] == "standalone"
        assert payload["orientation"] == "portrait"
        assert {icon["sizes"] for icon in payload["icons"]} == {"192x192", "512x512"}
        assert any(icon["sizes"] == "512x512" and icon.get("purpose") == "maskable" for icon in payload["icons"])
        assert client.get("/assets/icon-maskable-512.png").status_code == 200


def test_service_worker_is_customer_scoped_and_does_not_cache_api():
    with TestClient(app) as client:
        response = client.get("/sw.js")
        assert response.status_code == 200
        assert response.headers.get("service-worker-allowed") == "/customer/"
        assert 'url.pathname.startsWith("/api/")' in response.text
        assert 'url.pathname !== "/customer"' in response.text
        assert 'url.pathname !== "/customer/"' in response.text
