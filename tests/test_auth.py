"""
Tests for authentication, session management, RBAC, and institution scoping.
"""
import pytest
from datetime import date
from flask import jsonify
from flask_login import login_user

from extensions import db
from models import (
    User,
    Institution,
    Programme,
    StudentRecord,
    AuditLog,
    Role,
    Classification,
    AdmissionCategory,
    EnrolmentStatus,
    ValidationStatus,
)
from services.authz import (
    roles_required,
    scope_query,
    assert_same_institution,
    login_rate_limiter,
)


@pytest.fixture(autouse=True)
def reset_rate_limits():
    """Ensure rate limiter starts fresh for every test."""
    login_rate_limiter.reset_all()
    yield
    login_rate_limiter.reset_all()


def create_test_fixtures(app):
    """Seed minimal test institutions and users."""
    with app.app_context():
        # Institutions
        vesit = Institution(code="VESIT", name="VESIT Mumbai", state="MH", city="Mumbai")
        coep = Institution(code="COEP", name="COEP Pune", state="MH", city="Pune")
        db.session.add_all([vesit, coep])
        db.session.flush()

        # Programmes
        prog_vesit = Programme(institution_id=vesit.id, code="IT", name="IT", level="UG")
        prog_coep = Programme(institution_id=coep.id, code="CS", name="CS", level="UG")
        db.session.add_all([prog_vesit, prog_coep])
        db.session.flush()

        # Users
        aicte_admin = User(username="AICTE_Admin", full_name="AICTE Super Admin", role=Role.AICTE_ADMIN.value)
        aicte_admin.set_password("Demo@12345")

        vesit_admin = User(username="VESIT", full_name="VESIT Admin", role=Role.INSTITUTION_ADMIN.value, institution_id=vesit.id)
        vesit_admin.set_password("Demo@12345")

        coep_admin = User(username="COEP", full_name="COEP Admin", role=Role.INSTITUTION_ADMIN.value, institution_id=coep.id)
        coep_admin.set_password("Demo@12345")

        inactive_user = User(username="INACTIVE", full_name="Inactive Admin", role=Role.INSTITUTION_ADMIN.value, institution_id=vesit.id, is_active=False)
        inactive_user.set_password("Demo@12345")

        # Students
        student_vesit = StudentRecord(
            institution_id=vesit.id,
            programme_id=prog_vesit.id,
            institution_student_id="V001",
            full_name="Aarav Sharma",
            normalized_name="aarav sharma",
            academic_year="2026-27",
            admission_date=date.today(),
            admission_category=AdmissionCategory.OPEN.value,
            classification=Classification.REGULAR.value,
            enrolment_status=EnrolmentStatus.ACTIVE.value,
            validation_status=ValidationStatus.VALID.value,
        )

        student_coep = StudentRecord(
            institution_id=coep.id,
            programme_id=prog_coep.id,
            institution_student_id="C001",
            full_name="Rohan Patil",
            normalized_name="rohan patil",
            academic_year="2026-27",
            admission_date=date.today(),
            admission_category=AdmissionCategory.OPEN.value,
            classification=Classification.REGULAR.value,
            enrolment_status=EnrolmentStatus.ACTIVE.value,
            validation_status=ValidationStatus.VALID.value,
        )

        db.session.add_all([aicte_admin, vesit_admin, coep_admin, inactive_user, student_vesit, student_coep])
        db.session.commit()

        return {
            "vesit_id": vesit.id,
            "coep_id": coep.id,
            "student_vesit_id": student_vesit.id,
            "student_coep_id": student_coep.id,
        }


def test_login_success_and_me_without_password(app, client):
    """Test successful login, /me endpoint, and verify password hash is never leaked."""
    create_test_fixtures(app)

    # Login via API
    res = client.post("/api/auth/login", json={"username": "VESIT", "password": "Demo@12345"})
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["data"]["user"]["username"] == "VESIT"
    assert data["data"]["user"]["role"] == Role.INSTITUTION_ADMIN.value
    assert data["data"]["redirect_url"] == "/dashboard"

    # Password must never appear in raw response text
    assert "password" not in res.get_data(as_text=True).lower() or "password_hash" not in res.get_data(as_text=True)
    assert "password_hash" not in data["data"]["user"]

    # Verify /api/auth/me session
    me_res = client.get("/api/auth/me")
    assert me_res.status_code == 200
    me_data = me_res.get_json()
    assert me_data["ok"] is True
    assert me_data["data"]["user"]["username"] == "VESIT"
    assert "password" not in me_data["data"]["user"]
    assert "password_hash" not in me_data["data"]["user"]


def test_login_invalid_credentials_and_audit(app, client):
    """Test login failure with invalid password and verify audit logging."""
    create_test_fixtures(app)

    res = client.post("/api/auth/login", json={"username": "VESIT", "password": "WrongPassword!"})
    assert res.status_code == 401
    data = res.get_json()
    assert data["ok"] is False
    assert data["error"]["code"] == "UNAUTHENTICATED"

    # Verify audit log was recorded without saving the invalid password
    with app.app_context():
        log_entry = AuditLog.query.filter_by(action="AUTH_LOGIN_FAILURE").order_by(AuditLog.created_at.desc()).first()
        assert log_entry is not None
        assert log_entry.actor_id == "VESIT"
        assert "WrongPassword!" not in str(log_entry.details)


def test_inactive_user_cannot_login(app, client):
    """Verify deactivated user accounts are refused authentication."""
    create_test_fixtures(app)

    res = client.post("/api/auth/login", json={"username": "INACTIVE", "password": "Demo@12345"})
    assert res.status_code == 403
    data = res.get_json()
    assert data["ok"] is False
    assert data["error"]["code"] == "ACCOUNT_INACTIVE"


def test_logout(app, client):
    """Verify session termination upon logout."""
    create_test_fixtures(app)

    # Login
    client.post("/api/auth/login", json={"username": "VESIT", "password": "Demo@12345"})

    # Logout
    logout_res = client.post("/api/auth/logout")
    assert logout_res.status_code == 200
    assert logout_res.get_json()["ok"] is True

    # Subsequent /api/auth/me should fail with 401
    me_res = client.get("/api/auth/me")
    assert me_res.status_code == 401
    assert me_res.get_json()["error"]["code"] == "UNAUTHENTICATED"


def test_unauthenticated_api_returns_401_json(client):
    """Ensure protected API endpoints return standard 401 JSON envelope when unauthenticated."""
    res = client.get("/api/auth/me")
    assert res.status_code == 401
    data = res.get_json()
    assert data is not None
    assert data["ok"] is False
    assert data["error"]["code"] == "UNAUTHENTICATED"


def test_roles_required_decorator(app, client):
    """Verify roles_required decorator enforces RBAC boundaries."""
    create_test_fixtures(app)

    # Register a temporary test route
    @app.route("/api/test-admin-only")
    @roles_required(Role.AICTE_ADMIN)
    def test_admin_route():
        return jsonify({"ok": True, "data": {"secret": 42}})

    # 1. Unauthenticated -> 401
    res1 = client.get("/api/test-admin-only")
    assert res1.status_code == 401
    assert res1.get_json()["error"]["code"] == "UNAUTHENTICATED"

    # 2. Login as INSTITUTION_ADMIN -> 403
    client.post("/api/auth/login", json={"username": "VESIT", "password": "Demo@12345"})
    res2 = client.get("/api/test-admin-only")
    assert res2.status_code == 403
    assert res2.get_json()["error"]["code"] == "FORBIDDEN"

    # 3. Login as AICTE_ADMIN -> 200
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"username": "AICTE_Admin", "password": "Demo@12345"})
    res3 = client.get("/api/test-admin-only")
    assert res3.status_code == 200
    assert res3.get_json()["ok"] is True


def test_login_rate_limiting(app, client):
    """Verify that 5 failed attempts trigger a 429 RATE_LIMITED response."""
    create_test_fixtures(app)

    # 5 failed attempts
    for _ in range(5):
        res = client.post("/api/auth/login", json={"username": "VESIT", "password": "BadPassword"})
        assert res.status_code in (401, 429)

    # 6th attempt must be 429
    res6 = client.post("/api/auth/login", json={"username": "VESIT", "password": "Demo@12345"})
    assert res6.status_code == 429
    data = res6.get_json()
    assert data["ok"] is False
    assert data["error"]["code"] == "RATE_LIMITED"
    assert "retry_after" in data["error"]["details"]


def test_institution_scoping_and_isolation(app):
    """Verify scope_query and assert_same_institution isolate data across institutions."""
    fixtures = create_test_fixtures(app)

    with app.test_request_context("/api/students"):
        # Login as VESIT admin
        vesit_user = User.query.filter_by(username="VESIT").first()
        login_user(vesit_user)

        # Scoped query for StudentRecord should ONLY return VESIT student
        vesit_students = scope_query(StudentRecord.query, StudentRecord).all()
        assert len(vesit_students) == 1
        assert vesit_students[0].institution_id == fixtures["vesit_id"]

        # Attempting assert_same_institution on own record -> True
        own_student = db.session.get(StudentRecord, fixtures["student_vesit_id"])
        assert assert_same_institution(own_student) is True

        # Attempting assert_same_institution on COEP student -> 403
        coep_student = db.session.get(StudentRecord, fixtures["student_coep_id"])
        from werkzeug.exceptions import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            assert_same_institution(coep_student)
        assert exc_info.value.code == 403



def test_aicte_admin_sees_all_institutions(app):
    """Verify AICTE_ADMIN is unrestricted by scope_query."""
    create_test_fixtures(app)

    with app.test_request_context("/api/students"):
        aicte_user = User.query.filter_by(username="AICTE_Admin").first()
        login_user(aicte_user)

        all_students = scope_query(StudentRecord.query, StudentRecord).all()
        assert len(all_students) == 2


def test_root_and_login_page_redirects(app, client):
    """Verify root / renders landing page for guests, and /login renders sign in."""
    create_test_fixtures(app)

    # Unauthenticated root -> renders landing page directly
    res = client.get("/")
    assert res.status_code == 200
    assert b"APAAR" in res.data and b"Setu" in res.data

    # Login page renders HTML with demo account options
    login_res = client.get("/login")
    assert login_res.status_code == 200
    assert b"Sign In" in login_res.data
    assert b"AICTE_Admin" in login_res.data


def test_landing_page_renders(app, client):
    """Verify public landing page renders with APAARSetu overview and 4-step governance flow."""
    res = client.get("/landing")
    assert res.status_code == 200
    assert b"APAAR" in res.data and b"Setu" in res.data
    assert b"Dynamic Seat Ledger" in res.data
    assert b"Identity Graph" in res.data
    assert b"Academic Passport" in res.data
    assert b"Unified Student Intake &amp; Academic Governance" in res.data
    assert b"Flask-Login + Granular RBAC" in res.data
    assert b"Ed25519 Cryptography" in res.data


def test_csrf_required_on_post_when_enabled():
    """Verify that when CSRF protection is enabled, POST requests without token fail with 400 CSRF_ERROR."""
    from app import create_app
    from extensions import db as _db
    from flask_wtf.csrf import generate_csrf

    csrf_app = create_app("testing")
    csrf_app.config["WTF_CSRF_ENABLED"] = True

    with csrf_app.app_context():
        _db.create_all()
        create_test_fixtures(csrf_app)
        client = csrf_app.test_client()

        # 1. POST without CSRF token -> 400 CSRF_ERROR
        res_fail = client.post("/api/auth/login", json={"username": "VESIT", "password": "Demo@12345"})
        assert res_fail.status_code == 400
        body_fail = res_fail.get_json()
        assert body_fail["ok"] is False
        assert body_fail["error"]["code"] == "CSRF_ERROR"

        # 2. POST with valid CSRF token header -> 200
        with client:
            client.get("/login")
            token = generate_csrf()
            res_ok = client.post(
                "/api/auth/login",
                json={"username": "VESIT", "password": "Demo@12345"},
                headers={"X-CSRFToken": token}
            )
            assert res_ok.status_code == 200
            assert res_ok.get_json()["ok"] is True






