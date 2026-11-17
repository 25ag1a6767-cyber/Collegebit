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


def test_ios_pwa_meta_tags_opaque_apple_icon_splashes_and_safe_areas():
    import struct
    with TestClient(app) as client:
        html = client.get("/customer/").text
        assert "viewport-fit=cover" in html
        assert '<meta name="apple-mobile-web-app-capable" content="yes">' in html
        assert '<meta name="mobile-web-app-capable" content="yes">' in html
        assert '<meta name="apple-mobile-web-app-status-bar-style" content="default">' in html
        assert '<meta name="apple-mobile-web-app-title" content="CollegeBits">' in html
        assert '<link rel="apple-touch-icon" sizes="180x180" href="/assets/apple-touch-icon.png">' in html
        assert 'rel="apple-touch-startup-image"' in html

        icon = client.get("/assets/apple-touch-icon.png")
        assert icon.status_code == 200
        w, h, _depth, ctype = struct.unpack(">IIBB", icon.content[16:26])
        assert (w, h) == (180, 180)
        assert ctype == 2  # Opaque RGB (no alpha transparency)

        splash = client.get("/assets/splash/apple-splash-1179-2556.png")
        assert splash.status_code == 200

        css = client.get("/css/style.css").text + client.get("/css/v1-platform.css").text
        assert "env(safe-area-inset-top" in css
        assert "env(safe-area-inset-bottom" in css
