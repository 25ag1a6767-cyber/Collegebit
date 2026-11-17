from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_step7_privacy_terms_and_no_refund_routes_and_links():
    with TestClient(app) as client:
        for route in ("/legal", "/privacy", "/terms"):
            res = client.get(route)
            assert res.status_code == 200
            body = res.text.lower()
            assert "privacy policy" in body
            assert "terms of service" in body
            assert "no-refund policy" in body or "non-refundable" in body
            assert "<script" not in body

        cust = client.get("/customer/")
        assert cust.status_code == 200
        assert 'href="/legal#privacy"' in cust.text
        assert 'href="/legal#terms"' in cust.text


def test_step7_mobile_links_legal_and_one_page_guides_exist():
    profile_tsx = (REPO_ROOT / "mobile" / "src" / "screens" / "ProfileScreen.tsx").read_text(encoding="utf-8")
    assert "/legal#privacy" in profile_tsx
    assert "/legal#terms" in profile_tsx
    assert "No-Refund" in profile_tsx

    staff_guide = (REPO_ROOT / "docs" / "STAFF-ONE-PAGE-GUIDE.md").read_text(encoding="utf-8")
    assert "Single-use" in staff_guide or "single-use" in staff_guide.lower()
    assert "Payment Incident" in staff_guide
    assert "Accept" in staff_guide and "Mark Ready" in staff_guide

    student_guide = (REPO_ROOT / "docs" / "STUDENT-ONE-PAGE-GUIDE.md").read_text(encoding="utf-8")
    assert "Add to Home Screen" in student_guide
    assert "No-Refund Policy" in student_guide
    assert "Offline" in student_guide
