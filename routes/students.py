"""
Student records and change history routes for APAAR Nexus.
Smart India Hackathon PS 26263 — Task A7.

Endpoints:
- GET /students — Central repository view
- GET /students/<id> — Student detail & history view
- GET /api/students — List & search students (masked APAAR by default, optional audit-logged reveal)
- POST /api/students — Manual entry with validation service & seat ledger integration point
- GET /api/students/<id> — Student details, change history, and linked risk case references
- PATCH /api/students/<id> — Update allowed fields with mandatory reason and history audit
- GET /api/students/export — Masked APAAR CSV export of filtered records (audit-logged)
"""
import io
import csv
from datetime import datetime, date
from typing import Optional, Dict, Any, List

from flask import Blueprint, request, jsonify, render_template, Response, abort, current_app
from flask_login import login_required, current_user
from sqlalchemy import or_, desc, select
from sqlalchemy.orm import joinedload

from extensions import db
from models.student import StudentRecord, StudentRecordHistory
from models.institution import Institution, Programme
from models.enums import Role, EnrolmentStatus, ValidationStatus, AdmissionCategory, Classification
from services.authz import roles_required, scope_query, assert_same_institution
from services.validation_service import validate_student_row, normalize_name, parse_bool_flag
from services.apaar_adapter import mask_apaar
from services.audit_service import log as audit_log
from services import seat_ledger

students_bp = Blueprint("students", __name__)


def make_error(code: str, message: str, status_code: int = 400, details: Optional[Dict[str, Any]] = None):
    """Standardized JSON error envelope per AGENTS.md §5.6."""
    return jsonify({
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "details": details or {}
        }
    }), status_code


def _trigger_identity_scan(institution_id: Optional[str] = None):
    """Guarded trigger for identity resolution scan (Member B's service)."""
    try:
        from services.identity_resolution import scan
        scan_res = scan(institution_id=institution_id)
        try:
            from services.risk_engine import evaluate_all
            evaluate_all(scan_res)
        except Exception as risk_exc:
            current_app.logger.warning("Risk engine hook failed after student change: %s", risk_exc)
            audit_log(
                actor=getattr(current_user, "username", "SYSTEM"),
                action="SCAN_RISK_ERROR",
                entity_type="SYSTEM",
                entity_id=institution_id or "all",
                details={"error": str(risk_exc)},
                institution_id=institution_id
            )
    except Exception as exc:
        current_app.logger.warning("Identity scan hook failed after student change: %s", exc)
        audit_log(
            actor=getattr(current_user, "username", "SYSTEM"),
            action="IDENTITY_SCAN_ERROR",
            entity_type="SYSTEM",
            entity_id=institution_id or "all",
            details={"error": str(exc)},
            institution_id=institution_id
        )


def _format_student(student: StudentRecord, mask_apaar_flag: bool = True) -> Dict[str, Any]:
    """Helper to serialize a StudentRecord with related metadata."""
    data = student.to_dict(mask_apaar=mask_apaar_flag)
    data["institution_code"] = student.institution.code if student.institution else ""
    data["institution_name"] = student.institution.name if student.institution else ""
    data["programme_code"] = student.programme.code if student.programme else ""
    data["programme_name"] = student.programme.name if student.programme else ""
    return data


# ---------------------------------------------------------------------------
# HTML Page Views
# ---------------------------------------------------------------------------

@students_bp.route("/students", methods=["GET"])
@login_required
@roles_required(Role.AICTE_ADMIN.value, Role.INSTITUTION_ADMIN.value, Role.AUDITOR.value)
def students_page():
    """Render the central student repository page."""
    user_role = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    is_aicte_admin = (user_role == Role.AICTE_ADMIN.value)

    # Scoped institutions for filter dropdown
    inst_query = scope_query(Institution.query, Institution)
    institutions = inst_query.order_by(Institution.name).all()

    # Available programmes for dropdown
    prog_query = scope_query(Programme.query, Programme)
    programmes = prog_query.order_by(Programme.name).all()

    return render_template(
        "students.html",
        is_aicte_admin=is_aicte_admin,
        current_institution_id=current_user.institution_id,
        institutions=institutions,
        programmes=programmes,
        categories=[c.value for c in AdmissionCategory],
        classifications=[c.value for c in Classification],
        statuses=[s.value for s in EnrolmentStatus]
    )


@students_bp.route("/students/<id>", methods=["GET"])
@login_required
@roles_required(Role.AICTE_ADMIN.value, Role.INSTITUTION_ADMIN.value, Role.AUDITOR.value, Role.STUDENT.value)
def student_detail_page(id: str):
    """Render the student profile, change history, and risk references view."""
    student = db.session.get(StudentRecord, id)
    if not student:
        abort(404, description="Student record not found.")

    user_role = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)

    # Enforce isolation
    if user_role == Role.INSTITUTION_ADMIN.value:
        if student.institution_id != current_user.institution_id:
            abort(403, description="Access to student records from other institutions is forbidden.")
    elif user_role == Role.STUDENT.value:
        if getattr(current_user, "student_id", None) != student.id:
            abort(403, description="Access to other students' profiles is forbidden.")

    can_edit = user_role in (Role.AICTE_ADMIN.value, Role.INSTITUTION_ADMIN.value)
    can_reveal = user_role == Role.AICTE_ADMIN.value or (
        user_role == Role.INSTITUTION_ADMIN.value and current_user.institution_id == student.institution_id
    )

    # Available programmes for editing
    prog_query = Programme.query.filter_by(institution_id=student.institution_id)
    programmes = prog_query.order_by(Programme.name).all()

    return render_template(
        "student_detail.html",
        student=student,
        can_edit=can_edit,
        can_reveal=can_reveal,
        programmes=programmes,
        categories=[c.value for c in AdmissionCategory],
        classifications=[c.value for c in Classification],
        statuses=[s.value for s in EnrolmentStatus]
    )


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@students_bp.route("/api/students", methods=["GET"])
@login_required
@roles_required(Role.AICTE_ADMIN.value, Role.INSTITUTION_ADMIN.value, Role.AUDITOR.value)
def list_students():
    """
    List students with filtering and pagination.
    Masked APAAR by default. AICTE_ADMIN and INSTITUTION_ADMIN (own) may request
    unmasked value with ?reveal=true, which is audit-logged.
    """
    user_role = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)

    # Base query scoped by authz
    query = scope_query(StudentRecord.query, StudentRecord)

    # Parse query filters
    inst_id = request.args.get("institution_id")
    if inst_id:
        if user_role == Role.INSTITUTION_ADMIN.value and inst_id != current_user.institution_id:
            return make_error("FORBIDDEN", "Cannot view students of another institution.", 403)
        query = query.filter(StudentRecord.institution_id == inst_id)

    prog_id = request.args.get("programme_id")
    if prog_id:
        query = query.filter(StudentRecord.programme_id == prog_id)

    year = request.args.get("academic_year")
    if year:
        query = query.filter(StudentRecord.academic_year == year)

    category = request.args.get("category") or request.args.get("admission_category")
    if category:
        query = query.filter(StudentRecord.admission_category == category.upper())

    classification = request.args.get("classification")
    if classification:
        query = query.filter(StudentRecord.classification == classification.upper())

    val_status = request.args.get("validation_status")
    if val_status:
        query = query.filter(StudentRecord.validation_status == val_status.upper())

    enrol_status = request.args.get("enrolment_status")
    if enrol_status:
        query = query.filter(StudentRecord.enrolment_status == enrol_status.upper())

    # Text search
    q = request.args.get("q", "").strip()
    if q:
        search_pat = f"%{q}%"
        query = query.filter(or_(
            StudentRecord.full_name.ilike(search_pat),
            StudentRecord.normalized_name.ilike(search_pat),
            StudentRecord.institution_student_id.ilike(search_pat),
            StudentRecord.apaar_ref.ilike(search_pat)
        ))

    # Pagination parameters
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(1, int(request.args.get("per_page", 25))))
    except (ValueError, TypeError):
        page = 1
        per_page = 25

    total = query.count()
    records = (
        query.options(joinedload(StudentRecord.institution), joinedload(StudentRecord.programme))
        .order_by(desc(StudentRecord.created_at), StudentRecord.id)
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    # Determine reveal privilege
    reveal_requested = request.args.get("reveal", "").lower() in ("true", "1", "yes")
    should_reveal = False

    if reveal_requested:
        if user_role == Role.AICTE_ADMIN.value:
            should_reveal = True
        elif user_role == Role.INSTITUTION_ADMIN.value:
            # Can only reveal if querying own institution
            if not inst_id or inst_id == current_user.institution_id:
                should_reveal = True

        if should_reveal:
            audit_log(
                actor=current_user.username,
                action="REVEAL_APAAR_RECORDS",
                entity_type="STUDENT_QUERY",
                entity_id="student_list",
                details={
                    "count": len(records),
                    "page": page,
                    "institution_id": inst_id or current_user.institution_id
                },
                institution_id=inst_id or current_user.institution_id
            )

    data = [_format_student(rec, mask_apaar_flag=not should_reveal) for rec in records]
    pages = max(1, (total + per_page - 1) // per_page)

    return jsonify({
        "ok": True,
        "data": data,
        "meta": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": pages
        }
    })


@students_bp.route("/api/students", methods=["POST"])
@login_required
@roles_required(Role.AICTE_ADMIN.value, Role.INSTITUTION_ADMIN.value)
def create_student():
    """
    Manual student entry validated via validation_service.
    Returns row-level errors with VALIDATION_ERROR if invalid.
    """
    user_role = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    payload = request.get_json(silent=True) or {}

    # Enforce institution scope for institution admins
    if user_role == Role.INSTITUTION_ADMIN.value:
        target_inst = payload.get("institution_id")
        if target_inst and target_inst != current_user.institution_id:
            return make_error("FORBIDDEN", "Cannot create student records for another institution.", 403)
        payload["institution_id"] = current_user.institution_id

    # Central validation service check
    validation_res = validate_student_row(payload)
    if not validation_res.valid:
        return make_error(
            "VALIDATION_ERROR",
            "Student validation failed.",
            422,
            details={"errors": validation_res.errors, "warnings": validation_res.warnings}
        )

    norm = validation_res.normalized

    # Parse admission date
    adm_date = norm["admission_date"]
    if isinstance(adm_date, str):
        try:
            adm_date = date.fromisoformat(adm_date)
        except ValueError:
            adm_date = date.today()

    try:
        admission_payload = {
            "institution_id": norm["institution_id"],
            "programme_id": norm["programme_id"],
            "institution_student_id": norm["institution_student_id"],
            "apaar_ref": norm["apaar_ref"],
            "full_name": norm["full_name"],
            "academic_year": norm["academic_year"],
            "admission_date": adm_date,
            "admission_category": norm["admission_category"],
            "classification": norm["classification"],
            "is_nri": norm.get("is_nri", False),
            "is_oci": norm.get("is_oci", False),
            "is_foreign_national": norm.get("is_fn", False),
            "enrolment_status": norm.get("enrolment_status", EnrolmentStatus.ACTIVE.value),
            "validation_status": ValidationStatus.FLAGGED.value if validation_res.warnings else ValidationStatus.VALID.value,
            "validation_notes": validation_res.warnings[0]["message"] if validation_res.warnings else None,
            "contact_ref": norm.get("contact_ref"),
            "fee_payer_ref": norm.get("fee_payer_ref"),
        }

        # Commit admission through seat ledger engine
        res = seat_ledger.commit_admission(admission_payload, actor=current_user)
        student_id = res["student"]["id"]
        student = db.session.get(StudentRecord, student_id)

        audit_log(
            actor=current_user.username,
            action="CREATE_STUDENT",
            entity_type="STUDENT",
            entity_id=student.id,
            details={
                "institution_student_id": student.institution_student_id,
                "academic_year": student.academic_year,
                "programme_id": student.programme_id,
                "warnings_count": len(validation_res.warnings)
            },
            institution_id=student.institution_id
        )

        # Trigger identity scan hook if Member B's service exists
        _trigger_identity_scan(student.institution_id)

        return jsonify({
            "ok": True,
            "data": _format_student(student, mask_apaar_flag=True),
            "warnings": validation_res.warnings
        }), 201

    except seat_ledger.CapacityExceeded as exc:
        return make_error("CAPACITY_EXCEEDED", exc.message, 409, details=exc.details)
    except seat_ledger.DuplicateAdmission as exc:
        return make_error("DUPLICATE_ADMISSION", exc.message, 409, details=exc.details)
    except seat_ledger.CategoryNotEligible as exc:
        return make_error("CATEGORY_NOT_ELIGIBLE", exc.message, 409, details=exc.details)
    except seat_ledger.CapacityNotFound as exc:
        return make_error("NOT_FOUND", exc.message, 404, details=exc.details)
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error("Failed creating student record: %s", exc)
        return make_error("SERVER_ERROR", "Failed to persist student record.", 500, details={"error": str(exc)})


@students_bp.route("/api/students/<id>", methods=["GET"])
@login_required
@roles_required(Role.AICTE_ADMIN.value, Role.INSTITUTION_ADMIN.value, Role.AUDITOR.value, Role.STUDENT.value)
def get_student(id: str):
    """
    Get student record details, change history timeline, and linked risk cases.
    """
    student = db.session.get(StudentRecord, id)
    if not student:
        return make_error("NOT_FOUND", "Student record not found.", 404)

    user_role = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)

    # Scoping check
    if user_role == Role.INSTITUTION_ADMIN.value:
        if student.institution_id != current_user.institution_id:
            return make_error("FORBIDDEN", "Cannot view students of another institution.", 403)
    elif user_role == Role.STUDENT.value:
        if getattr(current_user, "student_id", None) != student.id:
            return make_error("FORBIDDEN", "Cannot view records of other students.", 403)

    # Determine reveal privilege
    reveal_requested = request.args.get("reveal", "").lower() in ("true", "1", "yes")
    should_reveal = False

    if reveal_requested:
        if user_role == Role.AICTE_ADMIN.value:
            should_reveal = True
        elif user_role == Role.INSTITUTION_ADMIN.value and current_user.institution_id == student.institution_id:
            should_reveal = True

        if should_reveal:
            audit_log(
                actor=current_user.username,
                action="REVEAL_APAAR_RECORD",
                entity_type="STUDENT",
                entity_id=student.id,
                details={"institution_student_id": student.institution_student_id},
                institution_id=student.institution_id
            )

    student_data = _format_student(student, mask_apaar_flag=not should_reveal)

    # Change history entries
    history_entries = StudentRecordHistory.query.filter_by(student_id=student.id).order_by(desc(StudentRecordHistory.changed_at)).all()
    student_data["history"] = [h.to_dict() for h in history_entries]

    # Linked risk cases references
    linked_cases = []
    try:
        from models.risk import RiskCase, RiskEvidence
        evidence_rows = RiskEvidence.query.filter(RiskEvidence.related_student_ids.isnot(None)).all()
        case_ids = {e.case_id for e in evidence_rows if e.related_student_ids and student.id in e.related_student_ids}
        if case_ids:
            cases = RiskCase.query.filter(RiskCase.id.in_(case_ids)).order_by(desc(RiskCase.score)).all()
            for c in cases:
                if user_role == Role.INSTITUTION_ADMIN.value and c.institution_id and c.institution_id != current_user.institution_id:
                    continue
                linked_cases.append({
                    "id": c.id,
                    "reference": c.reference,
                    "severity": c.severity,
                    "score": c.score,
                    "status": c.status,
                    "summary": c.summary
                })
    except Exception as exc:
        current_app.logger.debug("Failed querying linked risk cases for student %s: %s", student.id, exc)

    student_data["linked_risk_cases"] = linked_cases

    # Linked credentials references
    linked_credentials = []
    try:
        from models.credential import Credential
        creds = Credential.query.filter_by(student_id=student.id).order_by(desc(Credential.created_at)).all()
        for cr in creds:
            if user_role == Role.INSTITUTION_ADMIN.value and cr.institution_id != current_user.institution_id:
                continue
            linked_credentials.append({
                "id": cr.id,
                "reference": cr.reference,
                "programme_name": cr.programme_name,
                "award_year": cr.award_year,
                "status_stored": cr.status_stored,
                "issue_date": cr.issue_date.isoformat() if cr.issue_date else None
            })
    except Exception as exc:
        current_app.logger.debug("Failed querying credentials for student %s: %s", student.id, exc)

    student_data["linked_credentials"] = linked_credentials

    return jsonify({
        "ok": True,
        "data": student_data
    })


@students_bp.route("/api/students/<id>", methods=["PATCH"])
@login_required
@roles_required(Role.AICTE_ADMIN.value, Role.INSTITUTION_ADMIN.value)
def update_student(id: str):
    """
    Update student record fields.
    Mandatory 'reason' field required. Writes StudentRecordHistory rows in same transaction.
    Re-validates and re-triggers identity scan hook.
    """
    student = db.session.get(StudentRecord, id)
    if not student:
        return make_error("NOT_FOUND", "Student record not found.", 404)

    user_role = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)

    # Cross-institution check
    if user_role == Role.INSTITUTION_ADMIN.value:
        if student.institution_id != current_user.institution_id:
            return make_error("FORBIDDEN", "Cannot update students of another institution.", 403)

    payload = request.get_json(silent=True) or {}
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        return make_error("VALIDATION_ERROR", "A reason is required to update a student record.", 422, details={"field": "reason"})

    # Updatable fields
    updatable_fields = [
        "full_name", "programme_id", "academic_year", "admission_category",
        "classification", "is_nri", "is_oci", "is_foreign_national",
        "enrolment_status", "contact_ref", "fee_payer_ref", "apaar_ref"
    ]

    candidate_row = {
        "institution_id": student.institution_id,
        "institution_student_id": student.institution_student_id,
        "full_name": payload.get("full_name", student.full_name),
        "programme_id": payload.get("programme_id", student.programme_id),
        "academic_year": payload.get("academic_year", student.academic_year),
        "admission_date": student.admission_date,
        "admission_category": payload.get("admission_category", student.admission_category),
        "classification": payload.get("classification", student.classification),
        "is_nri": payload.get("is_nri", student.is_nri),
        "is_oci": payload.get("is_oci", student.is_oci),
        "is_foreign_national": payload.get("is_foreign_national", student.is_foreign_national),
        "enrolment_status": payload.get("enrolment_status", student.enrolment_status),
        "contact_ref": payload.get("contact_ref", student.contact_ref),
        "fee_payer_ref": payload.get("fee_payer_ref", student.fee_payer_ref),
        "apaar_ref": payload.get("apaar_ref", student.apaar_ref),
    }

    # Re-validate candidate row
    v_res = validate_student_row(candidate_row, context={"current_student_id": student.id, "exclude_student_id": student.id})
    if not v_res.valid:
        return make_error(
            "VALIDATION_ERROR",
            "Updated fields failed validation.",
            422,
            details={"errors": v_res.errors, "warnings": v_res.warnings}
        )

    norm = v_res.normalized
    changed_fields = {}

    try:
        # Compare and record history in the same transaction
        for field in updatable_fields:
            if field in payload:
                new_val = norm.get(field, payload[field])
                old_val = getattr(student, field)

                # Format normalization for comparison
                old_str = str(old_val) if old_val is not None else ""
                new_str = str(new_val) if new_val is not None else ""

                if old_str != new_str:
                    changed_fields[field] = {"old": old_val, "new": new_val}
                    history_row = StudentRecordHistory(
                        student_id=student.id,
                        changed_by=current_user.username,
                        field=field,
                        old_value=old_str,
                        new_value=new_str,
                        reason=reason
                    )
                    db.session.add(history_row)
                    setattr(student, field, new_val)

        # Update normalized name if full_name was updated
        if "full_name" in changed_fields:
            student.normalized_name = norm["normalized_name"]

        # Update validation status and notes
        if v_res.warnings:
            student.validation_status = ValidationStatus.FLAGGED.value
            student.validation_notes = v_res.warnings[0]["message"]
        else:
            student.validation_status = ValidationStatus.VALID.value
            student.validation_notes = None

        if changed_fields:
            db.session.commit()

            audit_log(
                actor=current_user.username,
                action="UPDATE_STUDENT",
                entity_type="STUDENT",
                entity_id=student.id,
                details={
                    "changes": {k: {"old": str(v["old"]), "new": str(v["new"])} for k, v in changed_fields.items()},
                    "reason": reason
                },
                institution_id=student.institution_id
            )

            # Re-run identity scan hook if B's service exists
            _trigger_identity_scan(student.institution_id)

        return jsonify({
            "ok": True,
            "data": _format_student(student, mask_apaar_flag=True),
            "changes_count": len(changed_fields),
            "warnings": v_res.warnings
        })

    except Exception as exc:
        db.session.rollback()
        current_app.logger.error("Failed updating student record: %s", exc)
        return make_error("SERVER_ERROR", "Failed to update student record.", 500, details={"error": str(exc)})


@students_bp.route("/api/students/export", methods=["GET"])
@login_required
@roles_required(Role.AICTE_ADMIN.value, Role.INSTITUTION_ADMIN.value, Role.AUDITOR.value)
def export_students_csv():
    """
    CSV export of the filtered student list.
    Masked APAAR always; same scope rules; action is audit-logged.
    """
    user_role = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)

    # Scoped query
    query = scope_query(StudentRecord.query, StudentRecord)

    inst_id = request.args.get("institution_id")
    if inst_id:
        if user_role == Role.INSTITUTION_ADMIN.value and inst_id != current_user.institution_id:
            return make_error("FORBIDDEN", "Cannot export students of another institution.", 403)
        query = query.filter(StudentRecord.institution_id == inst_id)

    prog_id = request.args.get("programme_id")
    if prog_id:
        query = query.filter(StudentRecord.programme_id == prog_id)

    year = request.args.get("academic_year")
    if year:
        query = query.filter(StudentRecord.academic_year == year)

    category = request.args.get("category") or request.args.get("admission_category")
    if category:
        query = query.filter(StudentRecord.admission_category == category.upper())

    classification = request.args.get("classification")
    if classification:
        query = query.filter(StudentRecord.classification == classification.upper())

    val_status = request.args.get("validation_status")
    if val_status:
        query = query.filter(StudentRecord.validation_status == val_status.upper())

    enrol_status = request.args.get("enrolment_status")
    if enrol_status:
        query = query.filter(StudentRecord.enrolment_status == enrol_status.upper())

    q = request.args.get("q", "").strip()
    if q:
        search_pat = f"%{q}%"
        query = query.filter(or_(
            StudentRecord.full_name.ilike(search_pat),
            StudentRecord.normalized_name.ilike(search_pat),
            StudentRecord.institution_student_id.ilike(search_pat),
            StudentRecord.apaar_ref.ilike(search_pat)
        ))

    records = (
        query.options(joinedload(StudentRecord.institution), joinedload(StudentRecord.programme))
        .order_by(StudentRecord.academic_year.desc(), StudentRecord.institution_student_id)
        .all()
    )

    # Generate CSV in-memory
    output = io.StringIO()
    writer = csv.writer(output)

    # CSV Header
    writer.writerow([
        "Institutional Student ID",
        "Full Name",
        "Masked APAAR",
        "Institution Code",
        "Institution Name",
        "Programme Code",
        "Programme Name",
        "Academic Year",
        "Admission Date",
        "Admission Category",
        "Classification",
        "Enrolment Status",
        "Validation Status"
    ])

    for rec in records:
        writer.writerow([
            rec.institution_student_id,
            rec.full_name,
            mask_apaar(rec.apaar_ref),
            rec.institution.code if rec.institution else "",
            rec.institution.name if rec.institution else "",
            rec.programme.code if rec.programme else "",
            rec.programme.name if rec.programme else "",
            rec.academic_year,
            rec.admission_date.isoformat() if rec.admission_date else "",
            rec.admission_category,
            rec.classification,
            rec.enrolment_status,
            rec.validation_status
        ])

    csv_data = output.getvalue()
    output.close()

    # Audit log the export
    audit_log(
        actor=current_user.username,
        action="EXPORT_STUDENTS_CSV",
        entity_type="STUDENT",
        entity_id="export",
        details={
            "records_exported": len(records),
            "filters": {
                "institution_id": inst_id,
                "programme_id": prog_id,
                "academic_year": year
            }
        },
        institution_id=inst_id or current_user.institution_id
    )

    filename = f"students_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Content-Type": "text/csv; charset=utf-8"
        }
    )


# ---------------------------------------------------------------------------
# CSV Upload Wizard & Ingestion Pipeline (Task A8)
# ---------------------------------------------------------------------------

@students_bp.route("/upload", methods=["GET"])
@login_required
@roles_required(Role.AICTE_ADMIN.value, Role.INSTITUTION_ADMIN.value)
def upload_page():
    """Render the 3-step CSV upload wizard and submission history."""
    user_role = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    is_aicte_admin = (user_role == Role.AICTE_ADMIN.value)

    inst_query = scope_query(Institution.query, Institution)
    institutions = inst_query.order_by(Institution.name).all()

    return render_template(
        "upload.html",
        is_aicte_admin=is_aicte_admin,
        current_institution_id=current_user.institution_id,
        institutions=institutions
    )


@students_bp.route("/api/students/import/template", methods=["GET"])
@login_required
@roles_required(Role.AICTE_ADMIN.value, Role.INSTITUTION_ADMIN.value)
def download_template():
    """Download the canonical CSV template."""
    from services.csv_service import generate_csv_template
    csv_content = generate_csv_template()
    return Response(
        csv_content,
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=students_template.csv",
            "Content-Type": "text/csv; charset=utf-8"
        }
    )


@students_bp.route("/api/students/import", methods=["POST"])
@login_required
@roles_required(Role.AICTE_ADMIN.value, Role.INSTITUTION_ADMIN.value)
def import_students_csv():
    """
    Handle CSV upload for preview or commit.
    Fields: file (multipart), mode ('preview' or 'commit'), submission_id, update_existing.
    """
    from services.csv_service import preview_csv, commit_submission
    from models.admission import DataSubmission

    user_role = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    json_data = request.get_json(silent=True) or {}

    mode = (request.form.get("mode") or json_data.get("mode") or "preview").lower()
    target_inst = request.form.get("institution_id") or json_data.get("institution_id")

    # Enforce institution isolation
    if user_role == Role.INSTITUTION_ADMIN.value:
        if target_inst and target_inst != current_user.institution_id:
            return make_error("FORBIDDEN", "Cannot import records into another institution.", 403)
        institution_id = current_user.institution_id
    else:
        if not target_inst:
            return make_error("VALIDATION_ERROR", "An institution selection is required.", 422)
        institution_id = target_inst

    if mode == "preview":
        if "file" not in request.files:
            return make_error("VALIDATION_ERROR", "A CSV file is required for preview.", 422)

        file_obj = request.files["file"]
        if not file_obj.filename:
            return make_error("VALIDATION_ERROR", "No file selected.", 422)

        file_bytes = file_obj.read()
        try:
            res = preview_csv(
                file_bytes=file_bytes,
                institution_id=institution_id,
                filename=file_obj.filename,
                uploaded_by=current_user.username
            )
            return jsonify({"ok": True, "data": res})
        except ValueError as val_err:
            return make_error("VALIDATION_ERROR", str(val_err), 422)
        except Exception as exc:
            current_app.logger.error("Preview failed: %s", exc)
            return make_error("SERVER_ERROR", "Failed to parse CSV preview.", 500, details={"error": str(exc)})

    elif mode == "commit":
        submission_id = request.form.get("submission_id") or json_data.get("submission_id")
        update_flag = parse_bool_flag(request.form.get("update_existing") or json_data.get("update_existing", False))

        if not submission_id:
            # Check if file was supplied to preview and commit in one go
            if "file" in request.files:
                file_obj = request.files["file"]
                try:
                    p_res = preview_csv(file_obj.read(), institution_id, file_obj.filename, current_user.username)
                    if p_res.get("already_imported"):
                        return jsonify({"ok": True, "data": p_res})
                    submission_id = p_res["submission_id"]
                except Exception as exc:
                    return make_error("VALIDATION_ERROR", str(exc), 422)
            else:
                return make_error("VALIDATION_ERROR", "submission_id is required for commit.", 422)

        try:
            commit_res = commit_submission(
                submission_id=submission_id,
                institution_id=institution_id,
                update_existing=update_flag,
                committed_by=current_user.username
            )
            return jsonify({"ok": True, "data": commit_res})
        except PermissionError:
            return make_error("FORBIDDEN", "Cross-institution commit denied.", 403)
        except ValueError as val_err:
            return make_error("VALIDATION_ERROR", str(val_err), 422)
        except Exception as exc:
            current_app.logger.error("Commit failed: %s", exc)
            return make_error("SERVER_ERROR", "Failed to commit submission.", 500, details={"error": str(exc)})

    return make_error("VALIDATION_ERROR", f"Invalid mode '{mode}'. Must be 'preview' or 'commit'.", 422)


@students_bp.route("/api/students/import/history", methods=["GET"])
@login_required
@roles_required(Role.AICTE_ADMIN.value, Role.INSTITUTION_ADMIN.value, Role.AUDITOR.value)
def import_history():
    """List historical CSV data submissions scoped to the active institution."""
    from models.admission import DataSubmission
    user_role = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)

    query = scope_query(DataSubmission.query, DataSubmission)

    target_inst = request.args.get("institution_id")
    if target_inst:
        if user_role == Role.INSTITUTION_ADMIN.value and target_inst != current_user.institution_id:
            return make_error("FORBIDDEN", "Cannot view submission history of another institution.", 403)
        query = query.filter(DataSubmission.institution_id == target_inst)

    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(1, int(request.args.get("per_page", 25))))
    except (ValueError, TypeError):
        page = 1
        per_page = 25

    total = query.count()
    submissions = query.order_by(desc(DataSubmission.created_at)).offset((page - 1) * per_page).limit(per_page).all()

    data = []
    for s in submissions:
        item = s.to_dict()
        item["institution_code"] = s.institution.code if s.institution else ""
        item["institution_name"] = s.institution.name if s.institution else ""
        data.append(item)

    pages = max(1, (total + per_page - 1) // per_page)
    return jsonify({
        "ok": True,
        "data": data,
        "meta": {"page": page, "per_page": per_page, "total": total, "pages": pages}
    })


@students_bp.route("/api/students/import/<id>", methods=["GET"])
@login_required
@roles_required(Role.AICTE_ADMIN.value, Role.INSTITUTION_ADMIN.value, Role.AUDITOR.value)
def get_submission_detail(id: str):
    """Get metadata, error count, and status for a specific data submission."""
    from models.admission import DataSubmission
    submission = db.session.get(DataSubmission, id)
    if not submission:
        return make_error("NOT_FOUND", "Submission record not found.", 404)

    user_role = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    if user_role == Role.INSTITUTION_ADMIN.value and submission.institution_id != current_user.institution_id:
        return make_error("FORBIDDEN", "Cannot access submission from another institution.", 403)

    data = submission.to_dict()
    data["institution_code"] = submission.institution.code if submission.institution else ""
    data["institution_name"] = submission.institution.name if submission.institution else ""
    return jsonify({"ok": True, "data": data})


@students_bp.route("/api/students/import/<submission_id>/errors.csv", methods=["GET"])
@login_required
@roles_required(Role.AICTE_ADMIN.value, Role.INSTITUTION_ADMIN.value, Role.AUDITOR.value)
def download_submission_errors_csv(submission_id: str):
    """Download error report CSV for a submission with formula injection neutralized."""
    from services.csv_service import generate_error_report_csv
    user_role = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    inst_scope = current_user.institution_id if user_role == Role.INSTITUTION_ADMIN.value else None

    try:
        csv_text, filename = generate_error_report_csv(submission_id, institution_id=inst_scope)

        audit_log(
            actor=current_user.username,
            action="EXPORT_SUBMISSION_ERRORS_CSV",
            entity_type="DATA_SUBMISSION",
            entity_id=submission_id,
            details={"filename": filename},
            institution_id=inst_scope
        )

        return Response(
            csv_text,
            mimetype="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename={filename}",
                "Content-Type": "text/csv; charset=utf-8"
            }
        )
    except PermissionError:
        return make_error("FORBIDDEN", "Access to error report for another institution is forbidden.", 403)
    except ValueError as val_err:
        return make_error("NOT_FOUND", str(val_err), 404)
    except Exception as exc:
        current_app.logger.error("Failed downloading error report: %s", exc)
        return make_error("SERVER_ERROR", "Failed to generate error report.", 500)

