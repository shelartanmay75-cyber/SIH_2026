"""B0 rendering and isolation checks. Fixtures are template-local, never DB users."""

import hashlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import flash, render_template_string

from app import create_app

ROOT = Path(__file__).resolve().parents[1]
BANNER = "APAARSetu prototype &mdash; synthetic data only. Not connected to AICTE, APAAR, UIDAI or DigiLocker."


@pytest.fixture
def preview_app():
    application = create_app("development")
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = True
    return application


def test_ui_kit_renders_without_database_or_user_loader(preview_app):
    client = preview_app.test_client()
    response = client.get("/ui-kit")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert BANNER in html
    assert '<aside class="sidebar"' in html
    assert 'aria-current="page">UI kit</a>' in html
    assert 'name="csrf-token" content="' in html
    assert '<script src="/static/vendor/chart.umd.min.js"' in html
    assert '<script src="/static/vendor/cytoscape.min.js"' in html
    assert "https://" not in html
    with client.session_transaction() as session:
        assert "_user_id" not in session
    assert client.get("/ui-kit").status_code == 200
    with client.session_transaction() as session:
        assert "_user_id" not in session


@pytest.mark.parametrize("config_name", ["testing", "production"])
def test_preview_disabled_outside_debug(config_name):
    application = create_app(config_name)
    response = application.test_client().get("/ui-kit")
    assert response.status_code == 404
    assert response.json["error"]["code"] == "NOT_FOUND"
    assert "ui_kit.index" not in application.view_functions
    # A production entrypoint cannot expose the gallery by changing DEBUG later.
    application.debug = True
    assert application.test_client().get("/ui-kit").status_code == 404


def test_guard_checks_debug_on_every_request(preview_app):
    client = preview_app.test_client()
    assert client.get("/ui-kit").status_code == 200
    preview_app.debug = False
    assert client.get("/ui-kit").status_code == 404


@pytest.mark.parametrize(("role", "labels"), [
    ("AICTE_ADMIN", ["Dashboard", "Institutions", "Students", "Seat Ledger", "Identity Graph", "Risk Cases", "Credentials", "Reports", "Audit Logs", "Policies"]),
    ("INSTITUTION_ADMIN", ["Dashboard", "Students", "Upload CSV", "Seat Ledger", "Risk Cases (own)", "Issue Credential", "Reports"]),
    ("STUDENT", ["My Passport", "Sharing"]),
    ("VERIFIER", ["Verify Credential"]),
    ("AUDITOR", ["Reports", "Risk Cases (read-only)", "Audit Logs"]),
])
def test_role_menus_exactly_match_contract(preview_app, role, labels):
    response = preview_app.test_client().get("/ui-kit", query_string={"role": role})
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    nav = html.split('<nav id="sidebar-menu" aria-label="Main navigation">', 1)[1].split("</nav>", 1)[0]
    assert re.findall(r'title="Available when this module is implemented">([^<]+)</span>', nav) == labels
    # Unbuilt modules must not produce dead anchors.
    assert re.findall(r'href="([^"]+)"', nav) == ["/ui-kit"]


@pytest.mark.parametrize("query", [{"role": "ANONYMOUS"}, {"shell": "public"}])
def test_anonymous_and_public_layout_have_no_sidebar(preview_app, query):
    response = preview_app.test_client().get("/ui-kit", query_string=query)
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '<body class="public-shell">' in html
    assert '<aside class="sidebar"' not in html
    assert BANNER in html


@pytest.mark.parametrize("role", ["SUPER_ADMIN", "", "<script>alert(1)</script>"])
def test_invalid_preview_roles_return_validation_envelope(preview_app, role):
    response = preview_app.test_client().get("/ui-kit", query_string={"role": role})
    assert response.status_code == 422
    assert response.json["ok"] is False
    assert response.json["error"]["code"] == "VALIDATION_ERROR"
    assert "<script>" not in response.get_data(as_text=True)


def test_plain_child_renders_with_real_anonymous_user_and_no_vendor_scripts(preview_app):
    # A2 will supply this loader; no test/login account is created here.
    preview_app.login_manager.user_loader(lambda user_id: None)
    with preview_app.test_request_context("/login"):
        html = render_template_string("{% extends 'base.html' %}{% block content %}<h1>Sign in</h1>{% endblock %}")
    assert '<body class="public-shell">' in html
    assert '<aside class="sidebar"' not in html
    assert "Sign in" in html
    assert "/static/vendor/" not in html


def test_active_link_and_public_override_for_authenticated_template(preview_app):
    @preview_app.get("/dashboard")
    def dashboard():
        return "Test endpoint only"

    user = SimpleNamespace(is_authenticated=True, role="AICTE_ADMIN", username="Synthetic preview")
    with preview_app.test_request_context("/dashboard"):
        html = render_template_string("{% extends 'base.html' %}", current_user=user)
        public = render_template_string("{% extends 'base_public.html' %}", current_user=user)
    assert 'href="/dashboard" aria-current="page">Dashboard</a>' in html
    assert '<aside class="sidebar"' not in public


def test_flash_content_is_escaped_and_logout_is_post_with_csrf(preview_app):
    if "auth.logout" not in preview_app.view_functions:
        @preview_app.post("/logout", endpoint="auth.logout")
        def logout():
            return "Test endpoint only"


    user = SimpleNamespace(is_authenticated=True, role="STUDENT", username="<script>name</script>")
    with preview_app.test_request_context("/passport"):
        flash("<script>alert(1)</script>", "error")
        html = render_template_string("{% extends 'base.html' %}", current_user=user)
    assert '<form method="post" action="/logout">' in html
    assert 'name="csrf_token" value="' in html
    assert '<div class="toast toast-danger" role="status">&lt;script&gt;' in html
    assert "&lt;script&gt;name&lt;/script&gt;" in html
    assert "<script>" not in html
    assert preview_app.test_client().post("/logout").status_code == 400


@pytest.mark.parametrize(("filename", "digest"), [
    ("chart.umd.min.js", "e4cf4d144b222634f2e64ff707cd57b953f8de4b65b231cd5ac6bde114648e4d"),
    ("cytoscape.min.js", "1bb5340e549511e111b31e5684872c949ad33d40ea5dba0ad8e7d90c62c7b3b9"),
])
def test_pinned_vendor_assets_are_served_locally(preview_app, filename, digest):
    response = preview_app.test_client().get(f"/static/vendor/{filename}")
    assert response.status_code == 200
    # Git may check out text as CRLF on Windows; validate the same upstream code.
    assert hashlib.sha256(response.data.replace(b"\r\n", b"\n")).hexdigest() == digest
    assert (ROOT / "static" / "vendor" / filename).stat().st_size > 100_000


def test_badge_and_base_text_contrast_meets_aa():
    css = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
    colours = dict(re.findall(r"(--[a-z-]+):\s*(#[0-9a-f]{6});", css))

    def luminance(hex_colour):
        channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return sum(c * weight for c, weight in zip(linear, [0.2126, 0.7152, 0.0722]))

    for foreground_key, background_key in [
        ("--ok-ink", "--ok-bg"), ("--warn-ink", "--warn-bg"),
        ("--danger", "--danger-bg"), ("--info", "--info-bg"),
        ("--muted", "--muted-bg"), ("--muted", "--bg"),
        ("--surface", "--navy"), ("--surface", "--danger"),
        ("--text", "--surface"),
    ]:
        foreground, background = colours[foreground_key], colours[background_key]
        light, dark = sorted([luminance(foreground), luminance(background)], reverse=True)
        assert (light + 0.05) / (dark + 0.05) >= 4.5, (foreground, background)
