"""
Dashboard routes for APAAR Nexus (Task A11).
Shared between Member A (institution view/metrics) and Member B (governance view).
Provides:
- GET /dashboard — Authenticated landing portal view.
- GET /dashboard/institution-preview — Temporary JSON viewer for institution metrics.
- GET /api/dashboard/institution — Dynamic institution metrics API (Member A).
"""
from typing import Any, Dict, Optional
from flask import Blueprint, render_template, request, jsonify, current_app
from flask_login import login_required, current_user

from extensions import db
from models.institution import Institution, Programme, IntakeCapacity
from models.student import StudentRecord
from models.admission import DataSubmission, SubmissionError
from models.enums import Role, EnrolmentStatus, Classification, AdmissionCategory, ValidationStatus
from services.authz import scope_query
from services.seat_ledger import calculate_warning_level
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from models import AdmissionEvent, AuditLog, Credential, IdentityMatch, RiskCase, RiskEvidence, VerificationEvent
from services.apaar_adapter import validate_format
from services.authz import roles_required

dashboards_bp = Blueprint("dashboards", __name__)


def _make_error(code: str, message: str, status_code: int = 400, details: Optional[dict] = None):
    """Standard API error response envelope."""
    return jsonify({
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "details": details or {}
        }
    }), status_code


@dashboards_bp.route("/dashboard", methods=["GET"])
@login_required
def dashboard_page():
    """Render the dashboard portal page for authenticated users."""
    role = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    institution = None
    if current_user.institution_id:
        institution = db.session.get(Institution, current_user.institution_id)

    if role == Role.AICTE_ADMIN.value:
        stats = {
            "institutions_count": Institution.query.count(),
            "programmes_count": Programme.query.count(),
            "students_count": StudentRecord.query.count(),
            "active_enrolments": StudentRecord.query.filter_by(enrolment_status=EnrolmentStatus.ACTIVE.value).count(),
        }
    elif role == Role.INSTITUTION_ADMIN.value and institution:
        scoped = scope_query(StudentRecord.query, StudentRecord)
        stats = {
            "institution_name": institution.name,
            "institution_code": institution.code,
            "students_count": scoped.count(),
            "programmes_count": Programme.query.filter_by(institution_id=institution.id).count(),
            "active_enrolments": scoped.filter_by(enrolment_status=EnrolmentStatus.ACTIVE.value).count(),
        }
    else:
        stats = {
            "students_count": 0,
            "programmes_count": 0,
            "active_enrolments": 0,
        }

    return render_template(
        "dashboard.html",
        stats=stats,
        institution=institution,
    )


@dashboards_bp.route("/dashboard/institution-preview", methods=["GET"])
@login_required
def dashboard_institution_preview():
    """Temporary JSON viewer page for institution dashboard metrics while Member B finishes dashboard.js."""
    user_role = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    if user_role not in (Role.AICTE_ADMIN.value, Role.INSTITUTION_ADMIN.value, Role.AUDITOR.value):
        return _make_error("FORBIDDEN", "Access denied.", 403)

    return render_template(
        "institution_dashboard_preview.html",
        user_role=user_role,
        institution_id=current_user.institution_id,
    )


# ==============================================================================
# Institution Dashboard Metrics API (Task A11)
# ==============================================================================

@dashboards_bp.route("/api/dashboard/institution", methods=["GET"])
@login_required
def get_institution_dashboard():
    """
    Comprehensive institution-level compliance, capacity, risk, and intake metrics.
    Scope:
      - INSTITUTION_ADMIN: Strictly scoped to own institution (403 on mismatch).
      - AICTE_ADMIN / AUDITOR: Can specify ?institution_id=; defaults to first active institution.
    Filters:
      - academic_year: Academic session filter (e.g., '2026-27').
      - programme_id: Specific degree programme UUID.
      - category: Specific admission category (OPEN, OBC, etc.).
      - validation_status: Student validation status (VALID, FLAGGED, etc.).
      - risk_level: Risk severity band (LOW, MEDIUM, HIGH, CRITICAL).
    """
    user_role = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    if user_role not in (Role.INSTITUTION_ADMIN.value, Role.AICTE_ADMIN.value, Role.AUDITOR.value):
        return _make_error("FORBIDDEN", "Access denied.", 403)

    req_inst_id = request.args.get("institution_id")

    if user_role == Role.INSTITUTION_ADMIN.value:
        if req_inst_id and req_inst_id != current_user.institution_id:
            return _make_error("FORBIDDEN", "Cannot access metrics for another institution.", 403)
        target_inst_id = current_user.institution_id
    else:
        # AICTE_ADMIN or AUDITOR
        if req_inst_id:
            target_inst_id = req_inst_id
        else:
            first_inst = Institution.query.filter_by(status="ACTIVE").order_by(Institution.name.asc()).first()
            if not first_inst:
                first_inst = Institution.query.first()
            target_inst_id = first_inst.id if first_inst else None

    if not target_inst_id:
        return _make_error("NOT_FOUND", "No institution found.", 404)

    institution = db.session.get(Institution, target_inst_id)
    if not institution:
        return _make_error("NOT_FOUND", f"Institution '{target_inst_id}' was not found.", 404)

    # Extract query filters
    academic_year = request.args.get("academic_year") or "2026-27"
    programme_id = request.args.get("programme_id")
    category = request.args.get("category")
    validation_status = request.args.get("validation_status")
    risk_level = request.args.get("risk_level")

    # 1. Seat Capacity & Intake Calculation
    cap_query = IntakeCapacity.query.filter_by(institution_id=target_inst_id)
    if academic_year and academic_year.upper() != "ALL":
        cap_query = cap_query.filter_by(academic_year=academic_year)
    if programme_id:
        cap_query = cap_query.filter_by(programme_id=programme_id)

    capacities = cap_query.all()

    approved_regular = sum(c.approved_regular_intake for c in capacities)
    supernumerary_capacity = sum(c.supernumerary_capacity for c in capacities)

    # Query active/graduated students for admissions count
    active_statuses = [EnrolmentStatus.ACTIVE.value, EnrolmentStatus.GRADUATED.value]
    stud_query = StudentRecord.query.filter_by(institution_id=target_inst_id)
    if academic_year and academic_year.upper() != "ALL":
        stud_query = stud_query.filter_by(academic_year=academic_year)
    if programme_id:
        stud_query = stud_query.filter_by(programme_id=programme_id)
    if category:
        stud_query = stud_query.filter_by(admission_category=category.upper())
    if validation_status:
        stud_query = stud_query.filter_by(validation_status=validation_status.upper())

    regular_admitted = stud_query.filter(
        StudentRecord.classification == Classification.REGULAR.value,
        StudentRecord.enrolment_status.in_(active_statuses)
    ).count()

    supernumerary_admitted = stud_query.filter(
        StudentRecord.classification == Classification.SUPERNUMERARY.value,
        StudentRecord.enrolment_status.in_(active_statuses)
    ).count()

    regular_remaining = max(0, approved_regular - regular_admitted)
    supernumerary_remaining = max(0, supernumerary_capacity - supernumerary_admitted)

    regular_utilisation_pct = (
        round((regular_admitted / approved_regular * 100), 1) if approved_regular > 0 else 0.0
    )
    supernumerary_utilisation_pct = (
        round((supernumerary_admitted / supernumerary_capacity * 100), 1)
        if supernumerary_capacity > 0
        else 0.0
    )

    warning_level = calculate_warning_level(regular_utilisation_pct)

    # Category-wise capacity and admitted breakdown for all standard categories
    num_progs = len(capacities) if capacities else 1
    cat_quotas_per_prog = {
        "OPEN": {"capacity": 90, "name": "OPEN", "classification": "REGULAR"},
        "OBC": {"capacity": 50, "name": "OBC", "classification": "REGULAR"},
        "SC": {"capacity": 24, "name": "SC", "classification": "REGULAR"},
        "ST": {"capacity": 16, "name": "ST", "classification": "REGULAR"},
        "NRI": {"capacity": 6, "name": "NRI Category", "classification": "SUPERNUMERARY"},
        "OCI": {"capacity": 5, "name": "OCI Status", "classification": "SUPERNUMERARY"},
        "FN": {"capacity": 4, "name": "Foreign National (FN)", "classification": "SUPERNUMERARY"},
    }

    category_wise_capacity = {}
    for cat_code, cat_info in cat_quotas_per_prog.items():
        cat_cap = cat_info["capacity"] * num_progs
        cat_adm = stud_query.filter(
            StudentRecord.admission_category == cat_code,
            StudentRecord.enrolment_status.in_(active_statuses)
        ).count()
        cat_util = round((cat_adm / cat_cap * 100), 1) if cat_cap > 0 else 0.0
        category_wise_capacity[cat_code] = {
            "category": cat_info["name"],
            "classification": cat_info["classification"],
            "admitted": cat_adm,
            "capacity": cat_cap,
            "remaining": max(0, cat_cap - cat_adm),
            "utilisation_pct": cat_util
        }

    # 2. Submission History (last 10)
    submissions = (
        DataSubmission.query.filter_by(institution_id=target_inst_id)
        .order_by(DataSubmission.created_at.desc())
        .limit(10)
        .all()
    )
    submission_history = [s.to_dict() for s in submissions]

    # 3. Validation Error Counts by Code
    error_counts_query = (
        db.session.query(SubmissionError.code, db.func.count(SubmissionError.id))
        .join(DataSubmission, SubmissionError.submission_id == DataSubmission.id)
        .filter(DataSubmission.institution_id == target_inst_id)
        .group_by(SubmissionError.code)
        .all()
    )
    validation_error_counts = {code: count for code, count in error_counts_query}

    # 4. Pending Identity Checks (validation_status PENDING/FLAGGED)
    base_check_query = StudentRecord.query.filter_by(institution_id=target_inst_id)
    if academic_year and academic_year.upper() != "ALL":
        base_check_query = base_check_query.filter_by(academic_year=academic_year)
    if programme_id:
        base_check_query = base_check_query.filter_by(programme_id=programme_id)

    pending_count = base_check_query.filter_by(validation_status=ValidationStatus.PENDING.value).count()
    flagged_count = base_check_query.filter_by(validation_status=ValidationStatus.FLAGGED.value).count()

    pending_identity_checks = {
        "pending": pending_count,
        "flagged": flagged_count,
        "total": pending_count + flagged_count
    }

    # 5. Institutional Risk Cases (count by severity; tolerate empty table)
    risk_cases_by_severity = {"LOW": 0, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0, "total": 0}
    try:
        from models.risk import RiskCase
        rc_query = db.session.query(RiskCase.severity, db.func.count(RiskCase.id)).filter(
            RiskCase.institution_id == target_inst_id
        )
        if risk_level:
            rc_query = rc_query.filter(RiskCase.severity == risk_level.strip().upper())
        for sev, count in rc_query.group_by(RiskCase.severity).all():
            if sev in risk_cases_by_severity:
                risk_cases_by_severity[sev] = count
        risk_cases_by_severity["total"] = sum(v for k, v in risk_cases_by_severity.items() if k != "total")
    except Exception as exc:
        current_app.logger.warning("Could not query risk cases for institution %s: %s", target_inst_id, exc)

    # 6. Credential Issuance History (last 10; tolerate empty table)
    credential_issuance_history = []
    try:
        from models.credential import Credential
        creds = (
            Credential.query.filter_by(institution_id=target_inst_id)
            .order_by(Credential.created_at.desc())
            .limit(10)
            .all()
        )
        for c in creds:
            st = db.session.get(StudentRecord, c.student_id) if c.student_id else None
            credential_issuance_history.append({
                "id": c.id,
                "reference": c.reference,
                "student_id": c.student_id,
                "student_name": st.full_name if st else "Unknown",
                "programme_name": c.programme_name,
                "award_year": c.award_year,
                "status": c.status_stored,
                "issue_date": c.issue_date.isoformat() if hasattr(c, "issue_date") and c.issue_date else None,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            })
    except Exception as exc:
        current_app.logger.warning("Could not query credentials for institution %s: %s", target_inst_id, exc)

    payload = {
        "institution": {
            "id": institution.id,
            "name": institution.name,
            "code": institution.code,
            "status": institution.status,
        },
        "filters_applied": {
            "academic_year": academic_year,
            "programme_id": programme_id,
            "category": category,
            "validation_status": validation_status,
            "risk_level": risk_level,
        },
        "seat_summary": {
            "approved_regular_intake": approved_regular,
            "regular_admitted": regular_admitted,
            "regular_remaining": regular_remaining,
            "regular_utilisation_pct": regular_utilisation_pct,
            "supernumerary_capacity": supernumerary_capacity,
            "supernumerary_admitted": supernumerary_admitted,
            "supernumerary_remaining": supernumerary_remaining,
            "supernumerary_utilisation_pct": supernumerary_utilisation_pct,
            "total_approved_intake": approved_regular + supernumerary_capacity,
            "total_admitted": regular_admitted + supernumerary_admitted,
            "warning_level": warning_level,
            "category_capacity": category_wise_capacity,
        },
        "submission_history": submission_history,
        "validation_error_counts": validation_error_counts,
        "pending_identity_checks": pending_identity_checks,
        "risk_cases_by_severity": risk_cases_by_severity,
        "credential_issuance_history": credential_issuance_history,
    }

    return jsonify({
        "ok": True,
        "data": payload
    }), 200


# Member B: national governance view. Institution metrics above remain A-owned.
_CATEGORIES = ("OPEN", "OBC", "SC", "ST", "EWS", "NRI", "OCI", "FN")
_SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
_VALIDATIONS = ("PENDING", "VALID", "INVALID", "FLAGGED")
_FILTERS = ("academic_year", "institution_id", "programme_id", "category", "validation_status", "risk_level")


def governance_filters(args):
    """Validate every national filter before constructing an authorized cohort."""
    values = {key: (args.get(key) or None) for key in _FILTERS}
    if values["academic_year"] and values["academic_year"] != "ALL" and (len(values["academic_year"]) > 20 or not values["academic_year"].replace("-", "").isdigit()):
        raise ValueError("Invalid academic_year.")
    for key, allowed in (("category", _CATEGORIES), ("validation_status", _VALIDATIONS), ("risk_level", _SEVERITIES)):
        if values[key] and values[key] not in allowed:
            raise ValueError(f"Invalid {key}.")
    if values["institution_id"] and db.session.get(Institution, values["institution_id"]) is None:
        raise LookupError("Institution not found.")
    if values["programme_id"] and db.session.get(Programme, values["programme_id"]) is None:
        raise LookupError("Programme not found.")
    return values


def governance_cohort(filters):
    """Return filtered synthetic academic records and associated risk cases."""
    query = select(StudentRecord).where(StudentRecord.is_synthetic.is_(True))
    for key, column in (("academic_year", StudentRecord.academic_year), ("institution_id", StudentRecord.institution_id),
                        ("programme_id", StudentRecord.programme_id), ("category", StudentRecord.admission_category),
                        ("validation_status", StudentRecord.validation_status)):
        if filters[key] and filters[key] != "ALL":
            query = query.where(column == filters[key])
    records = list(db.session.execute(query).scalars())
    evidence = list(db.session.execute(select(RiskEvidence)).scalars())
    risk_ids = {sid for row in evidence if row.case.severity == filters["risk_level"] for sid in (row.related_student_ids or [])} if filters["risk_level"] else None
    if risk_ids is not None:
        records = [row for row in records if row.id in risk_ids]
    cohort_ids = {row.id for row in records}
    has_record_filter = any(filters[key] and filters[key] != "ALL" for key in _FILTERS)
    cases = []
    for case in db.session.execute(select(RiskCase)).scalars():
        if filters["risk_level"] and case.severity != filters["risk_level"]:
            continue
        parts = [item for item in evidence if item.case_id == case.id]
        related = {sid for item in parts for sid in (item.related_student_ids or [])}
        institutions = {case.institution_id} if case.institution_id else set()
        for item in parts:
            institutions.update((item.details or {}).get("_risk", {}).get("affected_institution_ids", []))
        if filters["institution_id"] and filters["institution_id"] not in institutions and not (related & cohort_ids):
            continue
        if has_record_filter and related and not related & cohort_ids:
            continue
        if has_record_filter and not related and filters["institution_id"] is None and (filters["programme_id"] or filters["category"] or filters["validation_status"] or filters["risk_level"]):
            continue
        cases.append((case, parts, institutions))
    return records, cases


@dashboards_bp.get("/api/dashboard/governance")
@login_required
@roles_required("AICTE_ADMIN", "AUDITOR")
def governance_dashboard():
    """Compute national governance indicators from current database rows."""
    try:
        filters = governance_filters(request.args)
    except (ValueError, LookupError) as exc:
        code, status = ("NOT_FOUND", 404) if isinstance(exc, LookupError) else ("VALIDATION_ERROR", 422)
        return _make_error(code, str(exc), status)
    records, cases = governance_cohort(filters)
    ids = {row.id for row in records}
    inst_ids = {row.institution_id for row in records}
    if filters["institution_id"]:
        inst_ids.add(filters["institution_id"])
    if not any(filters.values()):
        inst_ids = set(db.session.execute(select(Institution.id)).scalars())
    institutions = {row.id: row for row in db.session.execute(select(Institution).where(Institution.id.in_(inst_ids))).scalars()}
    capacity_query = select(IntakeCapacity).where(IntakeCapacity.institution_id.in_(inst_ids))
    for key, column in (("academic_year", IntakeCapacity.academic_year), ("programme_id", IntakeCapacity.programme_id)):
        if filters[key] and filters[key] != "ALL":
            capacity_query = capacity_query.where(column == filters[key])
    capacities = list(db.session.execute(capacity_query).scalars())
    approved = Counter()
    for row in capacities:
        approved[row.institution_id] += row.approved_regular_intake
    admitted = Counter(row.institution_id for row in records if row.classification == "REGULAR" and row.enrolment_status in ("ACTIVE", "GRADUATED"))
    utilization = [{"institution_id": id_, "code": inst.code, "name": inst.name,
        "approved": approved[id_], "admitted": admitted[id_],
        "percent": round(100 * admitted[id_] / approved[id_], 1) if approved[id_] else 0}
        for id_, inst in institutions.items() if approved[id_]]
    utilization.sort(key=lambda item: (-item["percent"], item["code"]))
    severity = Counter(case.severity for case, _, _ in cases)
    matches = db.session.execute(select(IdentityMatch)).scalars().all()
    conflict_keys = {row.event_key for row in matches if row.student_a_id in ids or row.student_b_id in ids}
    rejected_query = select(AdmissionEvent).where(AdmissionEvent.result == "REJECTED")
    for key, column in (("academic_year", AdmissionEvent.academic_year), ("institution_id", AdmissionEvent.institution_id),
                        ("programme_id", AdmissionEvent.programme_id), ("category", AdmissionEvent.category)):
        if filters[key] and filters[key] != "ALL":
            rejected_query = rejected_query.where(column == filters[key])
    rejected = list(db.session.execute(rejected_query).scalars())
    if filters["validation_status"] or filters["risk_level"]:
        rejected = [event for event in rejected if event.student_id in ids]
    trend = Counter((row.academic_year, row.institution_id) for row in records if row.enrolment_status in ("ACTIVE", "GRADUATED"))
    trend_rows = [{"academic_year": year, "institution_id": inst, "institution": institutions[inst].code, "admissions": count}
        for (year, inst), count in sorted(trend.items()) if inst in institutions]
    now = datetime.now(timezone.utc).date()
    days = [(now - timedelta(days=offset)).isoformat() for offset in reversed(range(14))]
    activity = {day: {result: 0 for result in ("VERIFIED", "INVALID", "REVOKED", "EXPIRED")} for day in days}
    for event in db.session.execute(select(VerificationEvent).where(VerificationEvent.created_at >= datetime.combine(now - timedelta(days=13), datetime.min.time()))).scalars():
        credential = db.session.get(Credential, event.credential_id) if event.credential_id else None
        if credential and credential.student_id in ids and event.result in activity.get(event.created_at.date().isoformat(), {}):
            activity[event.created_at.date().isoformat()][event.result] += 1
    audit_query = select(AuditLog)
    if filters["institution_id"]:
        audit_query = audit_query.where(AuditLog.institution_id == filters["institution_id"])
    recent_audit = [{"time": row.created_at.isoformat(), "actor": row.actor_id, "action": row.action}
        for row in db.session.execute(audit_query.order_by(AuditLog.created_at.desc()).limit(10)).scalars()]
    recent_high = [{"id": case.id, "reference": case.reference, "severity": case.severity, "score": case.score,
                    "summary": case.summary or "Investigation case"} for case, _, _ in cases if case.severity in ("HIGH", "CRITICAL")]
    recent_high.sort(key=lambda item: (-item["score"], item["reference"]))
    totals = {"institutions": len(institutions), "student_records": len(records),
        "validated": sum(row.validation_status == "VALID" for row in records),
        "pending_validations": sum(row.validation_status == "PENDING" for row in records),
        "active_enrolments": sum(row.enrolment_status == "ACTIVE" for row in records),
        "identity_conflicts": len(conflict_keys),
        "missing_invalid_apaar": sum(not validate_format(row.apaar_ref, detailed=False) for row in records),
        "capacity_80": sum(row["percent"] >= 80 for row in utilization),
        "capacity_90": sum(row["percent"] >= 90 for row in utilization),
        "capacity_100": sum(row["percent"] >= 100 for row in utilization),
        "excess_admission_attempts": len(rejected)}
    return jsonify(ok=True, data={"filters": filters, "totals": totals,
        "risk_cases_by_severity": {key: severity[key] for key in _SEVERITIES},
        "category_wise_enrolment": {cat: sum(row.admission_category == cat and row.enrolment_status in ("ACTIVE", "GRADUATED") for row in records) for cat in ("OPEN", "OBC", "SC", "ST", "EWS", "NRI", "OCI", "FN")},
        "institution_admission_trend": trend_rows, "capacity_utilisation": utilization,
        "credential_verification_activity": activity,
        "recent_audit_events": recent_audit, "recent_high_risk_cases": recent_high[:10]})
