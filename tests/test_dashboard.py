"""Governance and anomaly reporting use live synthetic database rows."""
from datetime import date

import pytest

from extensions import db
from models import AdmissionEvent, AuditLog, IdentityMatch, IntakeCapacity, RiskCase, RiskEvidence, StudentRecord
from tests.test_identity import data, accounts
from tests.test_risk import api_login


@pytest.fixture
def governance(client, data, accounts):
    institutions, programmes, student = data
    a = student(validation_status="VALID", admission_category="OPEN")
    b = student("Tanvi Patil", "100000000002", institution=1, validation_status="PENDING", admission_category="OBC")
    c = student("Rohan Gupta", "bad", institution=2, validation_status="INVALID", admission_category="SC")
    db.session.flush()
    db.session.add_all([IntakeCapacity(institution_id=inst.id, programme_id=programme.id, academic_year="2026-27",
        approved_regular_intake=1) for inst, programme in zip(institutions, programmes)])
    case = RiskCase(reference="CASE-DASH01", institution_id=institutions[0].id, severity="HIGH", score=70, summary="Synthetic conflict")
    db.session.add(case)
    db.session.flush()
    db.session.add(RiskEvidence(case_id=case.id, rule_key="DUPLICATE_APAAR", event_key="DASH-1", points=40,
        description="Same synthetic reference", related_student_ids=[a.id], details={"_risk": {"affected_institution_ids": [institutions[0].id]}}))
    db.session.add(IdentityMatch(student_a_id=a.id, student_b_id=b.id, match_type="DUPLICATE_APAAR", scan_id="DASH", event_key="DASH-1"))
    db.session.add(AdmissionEvent(institution_id=institutions[0].id, programme_id=programmes[0].id,
        academic_year="2026-27", category="OPEN", classification="REGULAR", result="REJECTED", reason_code="CAPACITY_EXCEEDED", actor_id="VESIT"))
    db.session.add(AuditLog(actor_id="AICTE_Admin", action="IDENTITY_SCAN", entity_type="SCAN", entity_id="DASH", institution_id=institutions[0].id))
    db.session.commit()
    return institutions, (a, b, c), accounts, case


def test_governance_counts_filters_and_scope(client, governance):
    institutions, students, users, case = governance
    api_login(client, users[0])
    response = client.get("/api/dashboard/governance")
    assert response.status_code == 200, response.json
    body = response.json["data"]
    totals = body["totals"]
    assert totals["institutions"] == 3 == len(institutions)
    assert totals["student_records"] == StudentRecord.query.count() == 3
    assert totals["validated"] == StudentRecord.query.filter_by(validation_status="VALID").count() == 1
    assert totals["active_enrolments"] == StudentRecord.query.filter_by(enrolment_status="ACTIVE").count() == 3
    assert totals["identity_conflicts"] == IdentityMatch.query.count() == 1
    assert totals["missing_invalid_apaar"] == 1
    assert totals["excess_admission_attempts"] == AdmissionEvent.query.filter_by(result="REJECTED").count() == 1
    assert (totals["capacity_80"], totals["capacity_90"], totals["capacity_100"]) == (3, 3, 3)
    assert body["risk_cases_by_severity"]["HIGH"] == RiskCase.query.filter_by(severity="HIGH").count() == 1
    assert len(body["credential_verification_activity"]) == 14
    assert body["category_wise_enrolment"] == {"OPEN": 1, "OBC": 1, "SC": 1, "ST": 0, "EWS": 0, "NRI": 0, "OCI": 0, "FN": 0}
    scoped = client.get("/api/dashboard/governance", query_string={"institution_id": institutions[0].id}).json["data"]
    assert scoped["totals"]["student_records"] == 1 and scoped["totals"]["institutions"] == 1
    assert client.get("/api/dashboard/governance?category=OBC").json["data"]["totals"]["student_records"] == 1
    assert client.get("/api/dashboard/governance?validation_status=PENDING").json["data"]["totals"]["student_records"] == 1
    assert client.get("/api/dashboard/governance?risk_level=HIGH").json["data"]["totals"]["student_records"] == 1
    assert client.get("/api/dashboard/governance?academic_year=2025-26").json["data"]["totals"]["student_records"] == 0
    assert client.get("/api/dashboard/governance?programme_id=" + institutions[2].programmes.first().id).json["data"]["totals"]["student_records"] == 1
    assert client.get("/api/dashboard/governance?category=INVALID").status_code == 422
    api_login(client, users[1])
    assert client.get("/api/dashboard/governance").status_code == 403
    api_login(client, users[0])
    users[0].role = "AUDITOR"
    db.session.commit()
    assert client.get("/api/dashboard/governance").status_code == 200
    assert client.post("/api/dashboard/governance").status_code == 405


def test_anomaly_export_masks_and_enforces_institution(client, governance):
    institutions, students, users, case = governance
    api_login(client, users[1])
    report = client.get("/api/reports/anomalies")
    assert report.status_code == 200 and report.json["meta"]["total"] == 1
    row = report.json["data"]["rows"][0]
    assert row["institution_code"] == "VESIT" and row["masked_apaar"] == "XXXXXXXX0001"
    assert students[0].apaar_ref not in str(report.json)
    assert client.get("/api/reports/anomalies", query_string={"institution_id": institutions[1].id}).status_code == 403
    csv_result = client.get("/api/reports/anomalies.csv")
    assert csv_result.status_code == 200 and "SYNTHETIC" in csv_result.headers["Content-Disposition"]
    assert "XXXXXXXX0001" in csv_result.text and students[0].apaar_ref not in csv_result.text
    assert "COEP" not in csv_result.text
    assert AuditLog.query.filter_by(action="EXPORT_ANOMALY_REPORT_CSV").count() == 1
    users[3].role, users[3].institution_id = "AUDITOR", None
    db.session.commit()
    api_login(client, users[3])
    assert client.get("/api/reports/anomalies").status_code == 200
    assert client.post("/api/reports/anomalies.csv").status_code == 405
    assert client.get("/dashboard").status_code == 200
    assert client.get("/reports").status_code == 200


def test_dashboard_and_reports_pages_load_scripts(client, governance):
    api_login(client, governance[2][0])
    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200 and b"js/dashboard.js" in dashboard.data and b"chart.umd.min.js" in dashboard.data
    reports = client.get("/reports")
    assert reports.status_code == 200 and b"js/reports.js" in reports.data and b"anomaly-filters" in reports.data
