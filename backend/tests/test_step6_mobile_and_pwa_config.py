from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_pwa_runtime_config_js_reads_env_and_wires_csp(monkeypatch):
    monkeypatch.setenv("API_BASE_URL", "https://api.canteen.example.edu/")
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            cfg = client.get("/config.js")
            assert cfg.status_code == 200
            assert cfg.headers["content-type"].startswith("application/javascript")
            assert "https://api.canteen.example.edu" in cfg.text
            assert "collegebitsApiUrl" in cfg.text
            assert "collegebitsWsUrl" in cfg.text

            page = client.get("/customer/")
            assert page.status_code == 200
            assert '<script src="/config.js" defer></script>' in page.text
            assert '<link rel="icon"' in page.text
            csp = page.headers.get("content-security-policy", "")
            assert "https://api.canteen.example.edu" in csp
            assert "wss://api.canteen.example.edu" in csp
            assert client.get("/favicon.ico").status_code == 200
    finally:
        get_settings.cache_clear()


def test_mobile_app_uses_config_not_hardcoded_urls_and_frame_ws_auth():
    client_ts = (REPO_ROOT / "mobile" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
    eas_json = (REPO_ROOT / "mobile" / "eas.json").read_text(encoding="utf-8")
    app_config = (REPO_ROOT / "mobile" / "app.config.js").read_text(encoding="utf-8")
    env_example = (REPO_ROOT / "mobile" / ".env.example").read_text(encoding="utf-8")

    assert "app-sai-e3e1.vercel.app" not in client_ts
    assert "app-sai-e3e1.vercel.app" not in eas_json
    assert "www.collegebits.com" not in client_ts
    assert "EXPO_PUBLIC_API_URL" in client_ts
    assert "expo-constants" in client_ts
    assert 'type: "auth"' in client_ts
    assert "?token=" not in client_ts
    assert "EXPO_PUBLIC_API_URL" in app_config
    assert "EXPO_PUBLIC_API_URL" in env_example

    # PWA default /config.js leaves apiBaseUrl blank unless configured
    static_cfg = (REPO_ROOT / "frontend" / "config.js").read_text(encoding="utf-8")
    assert "collegebitsApiUrl" in static_cfg
    assert "app-sai-e3e1.vercel.app" not in static_cfg


def test_display_board_css_and_customer_razorpay_csp():
    with TestClient(app) as client:
        disp_css = client.get("/css/display.css")
        assert disp_css.status_code == 200
        assert ".board-column" in disp_css.text
        assert ".token-card.ready" in disp_css.text
        csp = client.get("/customer/").headers.get("content-security-policy", "")
        assert "'unsafe-inline'" not in csp
        assert "sha256-twhjKut0sH54Pd9MJk5NfNRG9SjsPfvZZkpOZsN4xDo=" in csp
