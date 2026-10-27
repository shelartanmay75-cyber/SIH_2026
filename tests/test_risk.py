"""B3 deterministic scoring, persistence, access and non-punitive behavior."""

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

from flask_login import login_user
from flask import g
import pytest

from extensions import db
from models import AdmissionEvent, AuditLog, CaseAction, Credential, RiskCase, RiskEvidence
from models import StudentRecord, StudentRecordHistory, SystemPolicy
from services import identity_resolution, risk_engine as risk
from tests.test_identity import data, accounts  # Reuse synthetic fixtures, not a second seed.


def event(key="event-a", rule="DUPLICATE_APAAR", ids=None, signal=False, **details):
    """Build a redacted, deterministic service event."""
    return {"event_key": key, "rule_key": rule, "student_ids": ids or [],
            "explanation": rule.replace("_", " ").lower(), "is_signal": signal, "evidence": details}


def result(*events):
    """Build the public ScanResult shape without requiring a persisted scan."""
    return SimpleNamespace(scan_id=str(uuid4()), events=list(events))


@pytest.mark.parametrize("rule,points", risk.DEFAULT_WEIGHTS.items())
def test_each_default_weight_applies_once(rule, points):
    score, _, rows = risk.score_events([event(rule=rule), event(rule=rule)], risk.DEFAULT_WEIGHTS)
    assert score == points
    assert len(rows) == 1 and rows[0]["points"] == points


def test_duplicate_keys_across_rules_are_order_independent():
    a, b = event(), event(rule="SIMULTANEOUS_ENROLMENT")
    assert risk.score_events([a, b], risk.DEFAULT_WEIGHTS) == risk.score_events([b, a], risk.DEFAULT_WEIGHTS)
    assert risk.score_events([a, b], risk.DEFAULT_WEIGHTS)[0] == 40


def test_redaction_preserves_numeric_uuid_segments():
    value = "00000000-0000-4000-8000-123456789012"
    assert risk._clean({"institution_id": value}) == {"institution_id": value}
    assert risk._clean("APAAR 123456789012") == "APAAR XXXXXXXX9012"


@pytest.mark.parametrize("points,severity", [(29, "LOW"), (30, "MEDIUM"), (59, "MEDIUM"),
    (60, "HIGH"), (79, "HIGH"), (80, "CRITICAL"), (100, "CRITICAL")])
def test_band_boundaries(points, severity):
    assert risk.score_events([event()], {"DUPLICATE_APAAR": points})[:2] == (points, severity)


def test_cap_and_signals_never_reach_duplicate_territory():
    assert risk.score_events([event(str(index)) for index in range(4)], risk.DEFAULT_WEIGHTS)[0] == 100
    signals = [event(str(index), "SHARED_CONTACT") for index in range(10)]
    assert risk.score_events(signals, {"SHARED_CONTACT": 1000})[:2] == (29, "LOW")
    assert risk.score_events([], {}) == (0, "LOW", [])


@pytest.mark.parametrize("weights", [{"X": -1}, {"X": True}, {"X": 0.5}, []])
def test_invalid_weights_rejected(weights):
    with pytest.raises(ValueError):
        risk.score_events([event()], weights)


def test_invalid_events_rejected():
    for broken in [{}, event(key=""), {**event(), "explanation": ""}, {**event(), "student_ids": "abc"}]:
        with pytest.raises(ValueError):
            risk.score_events([broken], risk.DEFAULT_WEIGHTS)


def test_scan_evaluate_idempotency_history_and_no_side_effects(data, caplog):
    _, _, student = data
    a, b = student(), student("Tanvi Patil", institution=1)
    db.session.flush()
    credential = Credential(reference="CRED-TEST01", student_id=a.id, institution_id=a.institution_id,
        programme_name="Synthetic programme", award_year="2026", payload_json='{"synthetic":true}',
        signature="synthetic-test-only", key_id="synthetic-key")
    db.session.add(credential)
    db.session.commit()
    before_students = {row.id: {col.name: getattr(row, col.name) for col in row.__table__.columns} for row in StudentRecord.query.all()}
    before_credential = {col.name: getattr(credential, col.name) for col in credential.__table__.columns}
    first = risk.evaluate_all(identity_resolution.scan())
    assert len(first) == 1
    case = first[0]
    assert (case.score, case.severity, case.status, case.assigned_to) == (75, "HIGH", "OPEN", None)
    assert case.reference.startswith("CASE-") and len(case.reference) == 11
    assert case.institution_id is None
    assert case.actions.one().note == "Case opened by system scan"
    assert case.recommended_action == "Request institutional clarification from both institutions and place under review"
    assert "40 duplicate APAAR + 35 simultaneous enrolment" in risk.explain(case)
    assert "risk_weights policy missing" in caplog.text
    case.status, case.assigned_to = "UNDER_REVIEW", "AICTE_Admin"
    db.session.add(CaseAction(case_id=case.id, actor_id="AICTE_Admin", action="UNDER_REVIEW",
        from_status="OPEN", to_status="UNDER_REVIEW", note="Human review"))
    db.session.commit()
    history = [item.to_dict() for item in case.actions.all()]
    ids = {item.id for item in case.evidence_items.all()}
    again = risk.evaluate_all(identity_resolution.scan())[0]
    assert again.id == case.id and RiskCase.query.count() == 1
    assert again.status == "UNDER_REVIEW" and again.assigned_to == "AICTE_Admin"
    assert history == [item.to_dict() for item in again.actions.all()]
    assert ids == {item.id for item in again.evidence_items.all()}
    assert RiskEvidence.query.count() == 2
    assert before_students == {row.id: {col.name: getattr(row, col.name) for col in row.__table__.columns} for row in StudentRecord.query.all()}
    assert before_credential == {col.name: getattr(credential, col.name) for col in credential.__table__.columns}
    assert StudentRecordHistory.query.count() == 0
    assert AuditLog.query.filter_by(action="RISK_EVALUATED").count() == 2
    metadata = again.evidence_items.first().details["_risk"]
    assert metadata["affected_student_ids"] == sorted([a.id, b.id])
    assert metadata["affected_institution_ids"] == sorted([a.institution_id, b.institution_id])


def test_custom_policies_change_score_and_bands(data):
    row = data[2]()
    db.session.commit()
    incoming = result(event(ids=[row.id]))
    case = risk.evaluate_all(incoming)[0]
    db.session.add_all([SystemPolicy(key="risk_weights", value={"DUPLICATE_APAAR": 85}),
        SystemPolicy(key="risk_bands", value={"LOW": [0, 19], "MEDIUM": [20, 39], "HIGH": [40, 89], "CRITICAL": [90, 100]})])
    db.session.commit()
    updated = risk.evaluate_all(incoming)[0]
    assert updated.id == case.id and (updated.score, updated.severity) == (85, "HIGH")
    assert updated.evidence_items.one().points == 85


def test_signals_cannot_bridge_identity_components(data):
    rows = [data[2](f"Synthetic {index}", f"{index + 1:012}") for index in range(4)]
    db.session.commit()
    a, b, c, d = [row.id for row in rows]
    cases = risk.evaluate_all(result(event("ab", ids=[a, b]), event("cd", ids=[c, d]),
        event("shared", "SHARED_CONTACT", [a, c])))
    assert len(cases) == 3
    signals = [case for case in cases if case.summary.startswith("Investigation signal")]
    assert len(signals) == 1 and (signals[0].score, signals[0].severity) == (10, "LOW")
    assert RiskEvidence.query.count() == 3


def test_cluster_growth_merges_cases_and_preserves_actions(data):
    rows = [data[2](f"Synthetic {index}", f"{index + 1:012}") for index in range(4)]
    db.session.commit()
    a, b, c, d = [row.id for row in rows]
    first = risk.evaluate_all(result(event("ab", ids=[a, b]), event("cd", ids=[c, d])))
    first[0].status, first[0].assigned_to = "RESOLVED", "AICTE_Admin"
    db.session.commit()
    survivor_id = min(first, key=lambda case: (case.opened_at, case.id)).id
    case = risk.evaluate_all(result(event("bc", "SIMULTANEOUS_ENROLMENT", [b, c])))[0]
    assert case.id == survivor_id and RiskCase.query.count() == 1
    assert (case.status, case.assigned_to) == ("RESOLVED", "AICTE_Admin")
    assert case.actions.count() == 2 and case.evidence_items.count() == 3
    assert case.score == 100
    assert risk.evaluate_all(result(event("bc", "SIMULTANEOUS_ENROLMENT", [b, c])))[0].id == case.id


def test_partial_rescan_keeps_distinct_historical_evidence(data):
    row = data[2]()
    db.session.commit()
    case = risk.evaluate_all(result(event("a", ids=[row.id]), event("b", "CONFLICTING_ACADEMIC_DATA", [row.id])))[0]
    assert case.score == 55
    assert risk.evaluate_all(result(event("a", ids=[row.id])))[0].score == 55
    assert risk.evaluate_all(result(event("a", "SIMULTANEOUS_ENROLMENT", [row.id])))[0].score == 55


def admission(data, result_value="REJECTED", reason="CAPACITY_EXCEEDED", institution=0):
    """Create a committed synthetic admission attempt without touching the ledger."""
    institutions, programmes, _ = data
    row = AdmissionEvent(institution_id=institutions[institution].id, programme_id=programmes[institution].id,
        academic_year="2026-27", result=result_value, reason_code=reason, actor_id="SYSTEM")
    db.session.add(row)
    db.session.commit()
    return row


def test_excess_intake_is_per_institution_and_idempotent(data):
    rejected = admission(data)
    case = risk.record_excess_intake_event(rejected)
    assert (case.score, case.severity, case.institution_id) == (30, "MEDIUM", rejected.institution_id)
    assert case.evidence_items.one().event_key == rejected.id
    assert risk.record_excess_intake_event(rejected).id == case.id
    assert case.score == 30 and case.actions.count() == 1
    assert risk.record_excess_intake_event(admission(data)).id == case.id
    assert case.score == 60 and case.evidence_items.count() == 2
    assert risk.record_excess_intake_event(admission(data, "ACCEPTED")) is None
    assert risk.record_excess_intake_event(admission(data, reason="VALIDATION_ERROR")) is None
    assert RiskCase.query.count() == 1


def test_authentication_and_institution_boundaries(app, data, accounts):
    own, foreign = data[2](), data[2]("Tanvi Patil", institution=1)
    db.session.commit()
    with app.test_request_context():
        with pytest.raises(PermissionError):
            risk.evaluate_all(result(event(ids=[own.id])))
    with app.test_request_context():
        login_user(accounts[1])
        assert risk.evaluate_all(result(event(ids=[own.id])))[0].institution_id == own.institution_id
        with pytest.raises(PermissionError):
            risk.evaluate_all(result(event(ids=[own.id, foreign.id])))
        with pytest.raises(PermissionError):
            risk.record_excess_intake_event(admission(data, institution=1))
    assert RiskCase.query.count() == 1


def test_audit_failure_rolls_back_and_pending_work_rejected(data, monkeypatch):
    row = data[2]()
    with pytest.raises(ValueError, match="Commit pending"):
        risk.evaluate_all(result())
    db.session.commit()
    monkeypatch.setattr(risk.audit_service, "log", lambda *args, **kwargs: {"id": None})
    with pytest.raises(RuntimeError, match="audit"):
        risk.evaluate_all(result(event(ids=[row.id])))
    assert RiskCase.query.count() == RiskEvidence.query.count() == CaseAction.query.count() == 0


def test_unknown_students_invalid_policy_and_redaction(data):
    row = data[2]()
    db.session.commit()
    with pytest.raises(PermissionError):
        risk.evaluate_all(result(event(ids=[str(uuid4())])))
    case = risk.evaluate_all(result(event(ids=[row.id], apaar_ref="123456789012", token="private-token")))[0]
    assert case.evidence_items.one().details["apaar_ref"] == "[REDACTED]"
    assert case.evidence_items.one().details["token"] == "[REDACTED]"
    db.session.add(SystemPolicy(key="risk_bands", value={"LOW": [0, 100]}))
    db.session.commit()
    with pytest.raises(ValueError, match="Risk bands"):
        risk.evaluate_all(result(event(ids=[row.id])))


def test_five_synthetic_engine_cases_cover_all_bands(data):
    """Representative fixture, not a claim that Member A's absent seed was run."""
    _, _, student = data
    student("Arjun Mehta", "100000000001")
    student("Tanvi Patil", "100000000001", institution=1)  # HIGH: 40 + 35
    student("Zoya Kale", "100000000002")
    student("Dev Shah", "100000000002", institution=1, admission_date=date(2026, 7, 1))  # CRITICAL: +15
    student("Mira Iyer", "bad")  # LOW: 20
    student("Anika Rao", "100000000003", contact_ref="shared-demo")
    student("Zubin Singh", "100000000004", contact_ref="shared-demo", admission_date=date(2026, 8, 1))
    db.session.commit()
    risk.evaluate_all(identity_resolution.scan())
    risk.record_excess_intake_event(admission(data))  # MEDIUM: 30
    assert RiskCase.query.count() == 5
    assert {case.severity for case in RiskCase.query.all()} == {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


def api_login(client, user):
    """Use the existing four fixtures; changing a test role creates no demo account."""
    from flask_login.utils import _create_identifier
    environment = client.get("/api/health").request.environ
    with client.application.request_context(environment):
        identifier = _create_identifier()
    with client.session_transaction() as session:
        session["_user_id"] = user.id
        session["_fresh"] = True
        session["_id"] = identifier
    g.pop("_login_user", None)


def local_case(data):
    """Create one institution-local case with a student/credential to protect."""
    row = data[2](apaar="bad")
    db.session.flush()
    db.session.add(Credential(reference="CRED-API001", student_id=row.id, institution_id=row.institution_id,
        programme_name="Synthetic", award_year="2026", payload_json="{}", signature="test-only", key_id="synthetic"))
    db.session.commit()
    return row, risk.evaluate_all(identity_resolution.scan(institution_id=row.institution_id))[0]


def test_api_full_workflow_assignment_history_and_no_student_side_effects(client, data, accounts):
    student, case = local_case(data)
    before = (student.to_dict(), Credential.query.one().to_dict())
    api_login(client, accounts[0])
    base = f"/api/risk/cases/{case.id}"
    assigned = client.patch(base, json={"assigned_to": "AICTE_Admin"})
    assert assigned.status_code == 200 and assigned.json["data"]["assigned_to"] == "AICTE_Admin"
    actions = [("UNDER_REVIEW", "UNDER_REVIEW"), ("REQUEST_CLARIFICATION", "CLARIFICATION_REQUESTED"),
        ("CONFIRM_DATA_ERROR", "DATA_ERROR_CONFIRMED"), ("RESOLVE", "RESOLVED"),
        ("UNDER_REVIEW", "UNDER_REVIEW"), ("DISMISS_FALSE_POSITIVE", "FALSE_POSITIVE"), ("RESOLVE", "RESOLVED")]
    initial_actions = case.actions.count()
    initial_audits = AuditLog.query.filter_by(action="RISK_CASE_ACTION").count()
    for action, target in actions:
        response = client.post(base + "/actions", json={"action": action, "note": "Synthetic review note"})
        assert response.status_code == 200, response.json
        assert response.json["data"]["status"] == target
    assert case.actions.count() == initial_actions + len(actions)
    assert AuditLog.query.filter_by(action="RISK_CASE_ACTION").count() == initial_audits + len(actions)
    assert AuditLog.query.filter_by(action="RISK_CASE_ASSIGNED").count() == 1
    assert before == (student.to_dict(), Credential.query.one().to_dict())
    detail = client.get(base).json["data"]
    assert detail["affected_records"][0]["apaar_masked"] == "XXXXXXXX"
    assert detail["affected_institutions"][0]["id"] == student.institution_id
    assert len(detail["history"]) == 8 and detail["breakdown"][0]["points"] == 20


def test_api_transition_conflict_notes_and_invalid_reviewer(client, data, accounts):
    _, case = local_case(data)
    api_login(client, accounts[0])
    base = f"/api/risk/cases/{case.id}"
    invalid = client.post(base + "/actions", json={"action": "RESOLVE", "note": "Reason"})
    assert invalid.status_code == 409 and invalid.json["error"]["code"] == "CONFLICT"
    assert invalid.json["error"]["details"]["allowed_next_actions"] == ["DISMISS_FALSE_POSITIVE", "REQUEST_CLARIFICATION", "UNDER_REVIEW"]
    assert client.post(base + "/actions", json={"action": "DISMISS_FALSE_POSITIVE", "note": " "}).status_code == 422
    assert client.post(base + "/actions", json={"action": "UNDER_REVIEW"}).status_code == 200
    for action in ("CONFIRM_DATA_ERROR", "RESOLVE", "DISMISS_FALSE_POSITIVE"):
        assert client.post(base + "/actions", json={"action": action}).status_code == 422
    for reviewer in ("VESIT", "unknown", 123):
        assert client.patch(base, json={"assigned_to": reviewer}).status_code == 422
    assert client.patch(base, json={"assigned_to": None}).status_code == 200
    assert client.patch(base, json={}).status_code == 422
    assert client.post(base + "/actions", json={"action": "INVALID"}).status_code == 422
    assert case.actions.count() == 2


def test_api_institution_response_is_note_only_and_isolated(client, data, accounts):
    _, case = local_case(data)
    api_login(client, accounts[0])
    base = f"/api/risk/cases/{case.id}"
    assert client.post(base + "/actions", json={"action": "REQUEST_CLARIFICATION"}).status_code == 200
    api_login(client, accounts[1])
    assert client.get(base).status_code == 200
    assert client.post(base + "/actions", json={"action": "REQUEST_CLARIFICATION"}).status_code == 422
    response = client.post(base + "/actions", json={"action": "REQUEST_CLARIFICATION", "note": "Institution response: corrected data supplied"})
    assert response.status_code == 200 and response.json["data"]["status"] == "CLARIFICATION_REQUESTED"
    assert response.json["data"]["history"][0]["from_status"] == "CLARIFICATION_REQUESTED"
    assert response.json["data"]["history"][0]["to_status"] == "CLARIFICATION_REQUESTED"
    assert AuditLog.query.filter_by(action="RISK_CASE_RESPONSE").count() == 1
    for action in ("UNDER_REVIEW", "RESOLVE"):
        assert client.post(base + "/actions", json={"action": action, "note": "response"}).status_code == 403
    assert client.patch(base, json={"assigned_to": "AICTE_Admin"}).status_code == 403
    assert client.post(base + "/actions", json={"action": "REQUEST_CLARIFICATION", "note": "response", "assigned_to": None}).status_code == 403
    api_login(client, accounts[2])
    assert client.get(base).status_code == 404
    assert client.post(base + "/actions", json={"action": "REQUEST_CLARIFICATION", "note": "response"}).status_code == 404
    assert client.get("/api/risk/cases").json["data"] == []


def test_api_auditor_is_read_only_and_anonymous_denied(client, data, accounts):
    _, case = local_case(data)
    assert client.get("/api/risk/cases").status_code == 401
    assert client.post("/api/identity/scan", json={}).status_code == 401
    accounts[0].role = "AUDITOR"
    db.session.commit()
    api_login(client, accounts[0])
    base = f"/api/risk/cases/{case.id}"
    assert client.get(base).status_code == client.get("/api/risk/cases").status_code == 200
    assert client.get("/api/identity/graph").status_code == 200
    assert client.post(base + "/actions", json={"action": "UNDER_REVIEW"}).status_code == 403
    assert client.patch(base, json={"assigned_to": None}).status_code == 403
    assert client.post("/api/identity/scan", json={}).status_code == 403


def test_api_scan_idempotent_scoping_and_graph_privacy(client, data, accounts):
    own = data[2]()
    foreign = data[2]("Tanvi Patil", institution=1)
    db.session.commit()
    api_login(client, accounts[0])
    first = client.post("/api/identity/scan", json={"academic_year": "2026-27"})
    assert first.status_code == 200 and first.json["data"]["cases_created"] == 1
    again = client.post("/api/identity/scan", json={})
    assert again.status_code == 200 and again.json["data"]["cases_updated"] == 1
    assert again.json["data"]["cases_created"] == 0 and RiskCase.query.count() == 1
    graph = client.get("/api/identity/graph").json["data"]
    assert {node["type"] for node in graph["nodes"]} == {"STUDENT", "APAAR", "INSTITUTION"}
    assert own.apaar_ref not in str(graph)
    assert next(node for node in graph["nodes"] if node["type"] == "APAAR")["label"] == "XXXXXXXX0001"
    assert all(edge["explanation"] for edge in graph["edges"])
    assert len({edge["id"] for edge in graph["edges"]}) == len(graph["edges"])
    for node in graph["nodes"]:
        panel = client.get("/api/identity/node/" + node["id"])
        assert panel.status_code == 200, panel.json
        assert own.apaar_ref not in str(panel.json)
    limited = client.get("/api/identity/graph?limit=2").json["data"]
    assert len(limited["nodes"]) == 2 and limited["truncated"]
    api_login(client, accounts[1])
    local = client.get("/api/identity/graph").json["data"]
    assert foreign.id not in str(local)
    assert [node["id"] for node in local["nodes"] if node["type"] == "STUDENT"] == ["student:" + own.id]
    assert client.get("/api/identity/node/student:" + foreign.id).status_code == 404
    assert client.get("/api/risk/cases").json["data"] == []  # National conflict stays national.
    assert client.post("/api/identity/scan", json={"institution_id": foreign.institution_id}).status_code == 403
    assert client.get("/api/identity/graph?institution_id=" + foreign.institution_id).status_code == 403
    assert client.post("/api/identity/scan", json={}).json["data"]["stats"]["records_read"] == 1


def test_api_graph_filters_and_signals(client, data, accounts):
    a = data[2]("Anika Rao", contact_ref="synthetic-contact", fee_payer_ref="synthetic-payer")
    b = data[2]("Zubin Singh", "100000000002", contact_ref="synthetic-contact", fee_payer_ref="synthetic-payer", admission_date=date(2026, 7, 1))
    db.session.commit()
    api_login(client, accounts[0])
    assert client.post("/api/identity/scan", json={}).status_code == 200
    view = client.get("/api/identity/graph").json["data"]
    assert {node["type"] for node in view["nodes"]} >= {"CONTACT", "FEE_PAYER"}
    assert "synthetic-contact" not in str(view) and "synthetic-payer" not in str(view)
    hidden = client.get("/api/identity/graph?include_signals=false").json["data"]
    assert not {node["type"] for node in hidden["nodes"]}.intersection({"CONTACT", "FEE_PAYER"})
    assert client.get("/api/identity/graph?min_severity=HIGH").json["data"]["nodes"] == []
    assert client.get("/api/identity/graph?academic_year=2025-26").json["data"]["nodes"] == []
    masked = client.get("/api/identity/graph?apaar_masked=XXXXXXXX0002").json["data"]
    assert [node["id"] for node in masked["nodes"] if node["type"] == "STUDENT"] == ["student:" + b.id]
    case = RiskCase.query.one()
    assert len(client.get("/api/identity/graph?case_id=" + case.id).json["data"]["nodes"]) == len(view["nodes"])


def test_api_case_list_filters_pagination_and_validation(client, data, accounts):
    a, case = local_case(data)
    api_login(client, accounts[0])
    url = "/api/risk/cases"
    response = client.get(url + "?severity=LOW&status=OPEN&per_page=1&institution_id=" + a.institution_id)
    assert response.status_code == 200 and response.json["meta"] == {"page": 1, "per_page": 1, "total": 1, "pages": 1}
    assert client.get(url + "?q=" + case.reference).json["data"][0]["id"] == case.id
    assert client.get(url + "?q=%25").json["data"] == []
    assert client.get(url + "?assigned_to=AICTE_Admin").json["data"] == []
    assert len(client.get(url + "?assigned_to=").json["data"]) == 1
    for query in ("page=0", "per_page=101", "status=BAD", "severity=BAD", "page=x"):
        assert client.get(url + "?" + query).status_code == 422
    for query in ("include_signals=maybe", "limit=0", "limit=1001", "min_severity=BAD", "apaar_masked=123456789012"):
        assert client.get("/api/identity/graph?" + query).status_code == 422
    for body in ([], {"unknown": 1}, {"academic_year": ""}, {"institution_id": []}):
        assert client.post("/api/identity/scan", json=body).status_code == 422


def test_api_workflow_audit_failure_rolls_back_everything(client, data, accounts, monkeypatch):
    _, case = local_case(data)
    api_login(client, accounts[0])
    count = case.actions.count()
    monkeypatch.setattr(risk.audit_service, "log", lambda *args, **kwargs: {"id": None})
    response = client.post(f"/api/risk/cases/{case.id}/actions", json={"action": "UNDER_REVIEW"})
    assert response.status_code == 500 and response.json["error"]["code"] == "SERVER_ERROR"
    assert db.session.get(RiskCase, case.id).status == "OPEN" and case.actions.count() == count


def test_api_mutations_enforce_csrf(app, client, data, accounts):
    _, case = local_case(data)
    api_login(client, accounts[0])
    app.config["WTF_CSRF_ENABLED"] = True
    for method, path, body in [("post", "/api/identity/scan", {}),
        ("post", f"/api/risk/cases/{case.id}/actions", {"action": "UNDER_REVIEW"}),
        ("patch", f"/api/risk/cases/{case.id}", {"assigned_to": None})]:
        response = getattr(client, method)(path, json=body)
        assert response.status_code == 400 and response.json["error"]["code"] == "CSRF_ERROR"


@pytest.mark.parametrize("state,action,target", [
    ("OPEN", "UNDER_REVIEW", "UNDER_REVIEW"), ("OPEN", "REQUEST_CLARIFICATION", "CLARIFICATION_REQUESTED"),
    ("OPEN", "DISMISS_FALSE_POSITIVE", "FALSE_POSITIVE"),
    ("UNDER_REVIEW", "REQUEST_CLARIFICATION", "CLARIFICATION_REQUESTED"),
    ("UNDER_REVIEW", "CONFIRM_DATA_ERROR", "DATA_ERROR_CONFIRMED"),
    ("UNDER_REVIEW", "DISMISS_FALSE_POSITIVE", "FALSE_POSITIVE"), ("UNDER_REVIEW", "RESOLVE", "RESOLVED"),
    ("CLARIFICATION_REQUESTED", "UNDER_REVIEW", "UNDER_REVIEW"),
    ("CLARIFICATION_REQUESTED", "CONFIRM_DATA_ERROR", "DATA_ERROR_CONFIRMED"),
    ("CLARIFICATION_REQUESTED", "DISMISS_FALSE_POSITIVE", "FALSE_POSITIVE"),
    ("CLARIFICATION_REQUESTED", "RESOLVE", "RESOLVED"),
    ("DATA_ERROR_CONFIRMED", "RESOLVE", "RESOLVED"), ("FALSE_POSITIVE", "UNDER_REVIEW", "UNDER_REVIEW"),
    ("FALSE_POSITIVE", "RESOLVE", "RESOLVED"), ("RESOLVED", "UNDER_REVIEW", "UNDER_REVIEW"),
    ("OPEN", "CONFIRM_DATA_ERROR", None), ("UNDER_REVIEW", "UNDER_REVIEW", None),
    ("DATA_ERROR_CONFIRMED", "UNDER_REVIEW", None), ("RESOLVED", "RESOLVE", None),
])
def test_api_transition_matrix(client, data, accounts, state, action, target):
    _, case = local_case(data)
    case.status = state
    db.session.commit()
    api_login(client, accounts[0])
    before = case.actions.count()
    response = client.post(f"/api/risk/cases/{case.id}/actions", json={"action": action, "note": "Human decision"})
    assert response.status_code == (200 if target else 409)
    assert db.session.get(RiskCase, case.id).status == (target or state)
    assert case.actions.count() == before + bool(target)


def test_api_action_assignment_is_validated_atomically(client, data, accounts):
    _, case = local_case(data)
    api_login(client, accounts[0])
    url = f"/api/risk/cases/{case.id}/actions"
    rejected = client.post(url, json={"action": "UNDER_REVIEW", "assigned_to": "VESIT"})
    assert rejected.status_code == 422 and case.status == "OPEN" and case.actions.count() == 1
    accepted = client.post(url, json={"action": "UNDER_REVIEW", "assigned_to": "AICTE_Admin"})
    assert accepted.status_code == 200 and accepted.json["data"]["assigned_to"] == "AICTE_Admin"


def test_api_case_membership_filter_includes_national_case_for_aicte(client, data, accounts):
    own = data[2]()
    data[2]("Tanvi Patil", institution=1)
    db.session.commit()
    api_login(client, accounts[0])
    client.post("/api/identity/scan", json={})
    response = client.get("/api/risk/cases?institution_id=" + own.institution_id)
    assert response.status_code == 200 and response.json["meta"]["total"] == 1


def test_graph_get_does_not_mutate_and_focus_survives_small_limit(client, data, accounts):
    a, case = local_case(data)
    api_login(client, accounts[0])
    before = AuditLog.query.count()
    response = client.get("/api/identity/node/student:" + a.id + "?limit=1")
    assert response.status_code == 200 and response.json["data"]["truncated"]
    assert AuditLog.query.count() == before
    assert client.get("/api/identity/node/not-a-node").status_code == 422
    assert client.get("/api/identity/graph?case_id=" + str(uuid4())).status_code == 404


def test_graph_explorer_page_permissions_and_offline_assets(client, data, accounts):
    own = data[2]()
    data[2]("Foreign student", institution=1, academic_year="2027-28")
    db.session.commit()
    assert client.get("/identity-graph").status_code == 302
    api_login(client, accounts[0])
    page = client.get("/identity-graph")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'id="graph-institution"' in html and 'id="graph-scan"' in html
    assert '/static/vendor/cytoscape.min.js' in html and '/static/js/identity-graph.js' in html
    assert "Records and evidence" in html and "not proof of duplicate identity" in html
    assert "<details" not in html
    assert own.apaar_ref not in html
    api_login(client, accounts[1])
    html = client.get("/identity-graph").get_data(as_text=True)
    assert 'id="graph-institution"' not in html and 'id="graph-scan"' in html
    assert "2027-28" not in html
    accounts[0].role = "AUDITOR"
    db.session.commit()
    api_login(client, accounts[0])
    assert 'id="graph-scan"' not in client.get("/identity-graph").get_data(as_text=True)
    accounts[0].role = "VERIFIER"
    db.session.commit()
    assert client.get("/identity-graph").status_code == 403


def test_graph_name_search_edge_evidence_and_programme_labels(client, data, accounts):
    a = data[2]("Devansh Verma")
    b = data[2]("Devansh Verma", institution=1)
    data[2]("Unrelated student", "100000000002")
    db.session.commit()
    api_login(client, accounts[0])
    assert client.post("/api/identity/scan", json={}).status_code == 200
    graph = client.get("/api/identity/graph?q=Devansh%20Verma&include_signals=false").json["data"]
    assert [node["type"] for node in graph["nodes"]].count("STUDENT") == 2
    assert [node["type"] for node in graph["nodes"]].count("INSTITUTION") == 2
    assert [node["type"] for node in graph["nodes"]].count("APAAR") == 1
    assert all(isinstance(edge["evidence"], dict) and "identity_scope" not in edge["evidence"] for edge in graph["edges"])
    assert next(edge for edge in graph["edges"] if edge["type"] == "SIMULTANEOUS_ENROLMENT")["evidence"]["academic_year"] == "2026-27"
    panel = client.get("/api/identity/node/student:" + a.id).json["data"]
    assert all(record["programme_name"] == "Computer Engineering" for record in panel["linked_records"])
    assert a.apaar_ref not in str(graph) + str(panel)
    assert client.get("/api/identity/graph?q=Nonexistent").json["data"]["nodes"] == []
    assert client.get("/api/identity/graph?q=123456789012").status_code == 422
    assert client.get("/api/identity/graph?q=___").status_code == 422
    api_login(client, accounts[1])
    graph = client.get("/api/identity/graph?q=Devansh").json["data"]
    assert b.id not in str(graph)


def test_graph_explorer_deep_link_checks_case_scope(client, data, accounts):
    _, case = local_case(data)
    api_login(client, accounts[0])
    page = client.get("/identity-graph?case_id=" + case.id)
    assert page.status_code == 200 and f'data-case-id="{case.id}"' in page.get_data(as_text=True)
    api_login(client, accounts[2])
    assert client.get("/identity-graph?case_id=" + case.id).status_code == 404


def test_truncated_graph_keeps_relationship_context(client, data, accounts):
    for index in range(12):
        data[2](f"Synthetic record {index}", f"{index + 1:012}", admission_date=date(2026, 7, index + 1))
    db.session.commit()
    identity_resolution.scan()
    api_login(client, accounts[0])
    view = client.get("/api/identity/graph?limit=10").json["data"]
    assert view["truncated"] and len(view["nodes"]) <= 10
    assert view["edges"] and {node["type"] for node in view["nodes"]} >= {"STUDENT", "APAAR", "INSTITUTION"}
    ids = {node["id"] for node in view["nodes"]}
    assert all(edge["source"] in ids and edge["target"] in ids for edge in view["edges"])


def test_risk_pages_reviewer_options_and_read_only_role_rendering(client, data, accounts):
    _, case = local_case(data)
    assert client.get("/risk-cases").status_code == 302
    api_login(client, accounts[0])
    listing = client.get("/risk-cases")
    assert listing.status_code == 200 and '/static/js/risk.js' in listing.get_data(as_text=True)
    assert '<details' not in listing.get_data(as_text=True)
    html = client.get("/risk-cases/" + case.id).get_data(as_text=True)
    assert 'data-action="RESOLVE"' in html and 'id="risk-reviewer"' in html
    reviewers = client.get("/api/risk/reviewers")
    assert reviewers.status_code == 200 and [row["username"] for row in reviewers.json["data"]] == ["AICTE_Admin"]
    assert len(accounts) == 4 and "password_hash" not in str(reviewers.json)
    api_login(client, accounts[1])
    html = client.get("/risk-cases/" + case.id).get_data(as_text=True)
    assert 'data-action=' not in html and 'id="risk-reviewer"' not in html
    assert 'id="risk-response-form"' in html
    api_login(client, accounts[2])
    assert client.get("/risk-cases/" + case.id).status_code == 404
    accounts[3].role = "AUDITOR"
    db.session.commit()
    api_login(client, accounts[3])
    html = client.get("/risk-cases/" + case.id).get_data(as_text=True)
    assert 'data-action=' not in html and 'id="risk-response-form"' not in html and 'id="risk-assign-form"' not in html
    assert client.get("/risk-cases").status_code == 200
    assert client.post(f"/api/risk/cases/{case.id}/actions", json={"action": "INSTITUTION_RESPONSE", "note": "No"}).status_code == 403


def test_risk_summary_counts_full_scope_and_student_name_search(client, data, accounts):
    own, case = local_case(data)
    foreign = data[2]("Foreign Example", "bad", institution=1)
    db.session.commit()
    risk.evaluate_all(identity_resolution.scan(institution_id=foreign.institution_id))
    case.status, case.assigned_to = "UNDER_REVIEW", "AICTE_Admin"
    db.session.commit()
    api_login(client, accounts[0])
    response = client.get("/api/risk/summary?severity=CRITICAL&status=RESOLVED")
    assert response.status_code == 200
    assert response.json["data"]["total"] == 2
    assert response.json["data"]["severity"]["LOW"] == 2
    assert response.json["data"]["status"]["OPEN"] == response.json["data"]["status"]["UNDER_REVIEW"] == 1
    assert client.get("/api/risk/cases?per_page=1").json["meta"]["total"] == 2
    found = client.get("/api/risk/cases?q=Arjun%20Mehta")
    assert found.status_code == 200 and [row["id"] for row in found.json["data"]] == [case.id]
    assert found.json["data"][0]["institutions"][0]["code"] == "VESIT"
    assert client.get("/api/risk/summary?q=Arjun%20Mehta").json["data"]["total"] == 1
    assert client.get("/api/risk/summary?assigned_to=__unassigned__").json["data"]["total"] == 1
    api_login(client, accounts[1])
    assert client.get("/api/risk/summary").json["data"]["total"] == 1
    assert client.get("/api/risk/cases?q=Foreign%20Example").json["data"] == []
    assert client.get("/api/risk/summary?q=Foreign%20Example").json["data"]["total"] == 0
    assert client.get("/api/risk/summary?institution_id=" + foreign.institution_id).status_code == 403


def test_institution_response_has_explicit_history_type_and_no_status_change(client, data, accounts):
    student, case = local_case(data)
    case.status = "CLARIFICATION_REQUESTED"
    db.session.commit()
    before = (student.to_dict(), Credential.query.one().to_dict())
    api_login(client, accounts[1])
    url = f"/api/risk/cases/{case.id}"
    assert client.get(url).json["data"]["allowed_actions"] == ["INSTITUTION_RESPONSE"]
    assert client.post(url + "/actions", json={"action": "INSTITUTION_RESPONSE", "note": " "}).status_code == 422
    response = client.post(url + "/actions", json={"action": "INSTITUTION_RESPONSE", "note": "Synthetic clarification"})
    assert response.status_code == 200 and response.json["data"]["status"] == "CLARIFICATION_REQUESTED"
    action = response.json["data"]["history"][0]
    assert action["action"] == "INSTITUTION_RESPONSE" and action["from_status"] == action["to_status"]
    assert AuditLog.query.filter_by(action="RISK_CASE_RESPONSE", entity_id=case.id).count() == 1
    assert before == (student.to_dict(), Credential.query.one().to_dict())
    api_login(client, accounts[0])
    assert client.post(url + "/actions", json={"action": "INSTITUTION_RESPONSE", "note": "Wrong role"}).status_code == 403


def test_reviewer_options_exclude_inactive_and_unsupported_roles(client, data, accounts):
    accounts[2].role, accounts[2].is_active = "AICTE_ADMIN", False
    db.session.commit()
    api_login(client, accounts[0])
    assert [row["username"] for row in client.get("/api/risk/reviewers").json["data"]] == ["AICTE_Admin"]
    accounts[1].role = "VERIFIER"
    db.session.commit()
    api_login(client, accounts[1])
    assert client.get("/api/risk/reviewers").status_code == client.get("/api/risk/summary").status_code == 403
