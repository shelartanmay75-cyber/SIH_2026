"""
Dynamic Seat Ledger Service — The Prevent Engine (Task A9).

Smart India Hackathon Problem Statement PS 26263.

Philosophy & Architecture:
- Enforces hard AICTE intake limits for educational degree programmes.
- Strictly separates REGULAR and SUPERNUMERARY seats (never mixed, never borrowed).
- Counting Rule: Only ACTIVE and GRADUATED students count towards consumed seats;
  WITHDRAWN frees a seat, making capacity available for subsequent admissions.
- Enforces atomic conditional updates on counter columns (admitted_regular,
  admitted_supernumerary) to guarantee race-free concurrency under multithreaded loads.
- Audit logs all admission attempts (both ACCEPTED and REJECTED).
- Persists REJECTED admission events even though the student record is not created.
- Calls services.risk_engine.record_excess_intake_event(event) on capacity violations (guarded).
- Reads thresholds and category rules dynamically from database configuration.
"""
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple
import unicodedata

from flask import current_app
from extensions import db
from models.institution import Institution, Programme, IntakeCapacity
from models.student import StudentRecord
from models.admission import AdmissionEvent
from models.policy import SystemPolicy
from models.enums import Classification, AdmissionCategory, EnrolmentStatus, ValidationStatus, AdmissionEventResult
from services import audit_service
from services.apaar_adapter import mask_apaar


# ==============================================================================
# Exceptions
# ==============================================================================

class SeatLedgerError(Exception):
    """Base exception for seat ledger operations."""

    def __init__(self, message: str, reason_code: str = "SEAT_LEDGER_ERROR", details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.reason_code = reason_code
        self.details = details or {}


class CapacityExceeded(SeatLedgerError):
    """Raised when an admission attempt exceeds approved intake capacity."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message, reason_code="CAPACITY_EXCEEDED", details=details)


class DuplicateAdmission(SeatLedgerError):
    """Raised when a duplicate student ID or active APAAR admission is detected."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message, reason_code="DUPLICATE_ADMISSION", details=details)


class CategoryNotEligible(SeatLedgerError):
    """Raised when a category or demographic flag is ineligible for the requested pool."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message, reason_code="CATEGORY_NOT_ELIGIBLE", details=details)


class CapacityNotFound(SeatLedgerError):
    """Raised when no intake capacity configuration exists for the programme/year."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message, reason_code="CAPACITY_NOT_FOUND", details=details)


# ==============================================================================
# Helper Functions
# ==============================================================================

def normalize_name(value: Optional[str]) -> str:
    """Normalize Unicode, lower-case, and strip non-alphanumeric characters."""
    text = unicodedata.normalize("NFKD", value or "").casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join("".join(char if char.isalnum() else " " for char in text).split())


def get_capacity_warning_thresholds() -> List[int]:
    """
    Read capacity warning percentage thresholds from system_policies.
    Falls back to [100, 90, 80] descending if not configured.
    """
    policy = SystemPolicy.query.filter_by(key="capacity_warning_thresholds").first()
    if policy and isinstance(policy.value, list):
        try:
            return sorted([int(x) for x in policy.value], reverse=True)
        except (ValueError, TypeError):
            pass
    return [100, 90, 80]


def calculate_warning_level(utilisation_pct: float, thresholds: Optional[List[int]] = None) -> Optional[int]:
    """
    Returns the highest exceeded warning threshold (e.g. 100, 90, 80) or None.
    """
    if thresholds is None:
        thresholds = get_capacity_warning_thresholds()
    for threshold in thresholds:
        if utilisation_pct >= threshold:
            return threshold
    return None


def check_category_eligibility(
    cap: IntakeCapacity,
    category: str,
    classification: str,
    flags: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Applies explicit category eligibility and pool separation:
    - Determine target pool (REGULAR vs SUPERNUMERARY) from category_rules and NRI/OCI/FN flags.
    - Regular and supernumerary capacity are NEVER mixed or borrowed from each other.
    - Rejects if the category or international flag is not allowed in the requested classification pool.
    Returns (is_eligible, human_readable_reason).
    """
    flags = flags or {}
    is_nri = bool(flags.get("is_nri") or category == "NRI")
    is_oci = bool(flags.get("is_oci") or category == "OCI")
    is_fn = bool(flags.get("is_foreign_national") or category == "FN")
    has_international_flag = is_nri or is_oci or is_fn

    if classification not in (Classification.REGULAR.value, Classification.SUPERNUMERARY.value):
        return False, f"Invalid classification '{classification}'. Must be REGULAR or SUPERNUMERARY."

    category_rules = cap.category_rules

    # If explicit rules configured on IntakeCapacity
    if category_rules and isinstance(category_rules, dict):
        # Format 1: {"REGULAR": ["OPEN", "OBC", ...], "SUPERNUMERARY": ["NRI", ...]}
        if "REGULAR" in category_rules or "SUPERNUMERARY" in category_rules:
            allowed_pool = category_rules.get(classification, [])
            if category not in allowed_pool:
                return (
                    False,
                    f"Category '{category}' is not eligible for {classification} seats under configured intake rules.",
                )
            if classification == Classification.REGULAR.value and has_international_flag:
                # Disallow international in regular if not explicitly included
                if category not in allowed_pool:
                    return (
                        False,
                        f"International applicant (Category: {category}) cannot take a REGULAR seat under intake rules.",
                    )
            return True, None

        # Format 2: {"NRI": {"allowed_classifications": ["SUPERNUMERARY"]}, ...}
        if category in category_rules and isinstance(category_rules[category], dict):
            allowed_class = category_rules[category].get("allowed_classifications", [])
            if allowed_class and classification not in allowed_class:
                return (
                    False,
                    f"Category '{category}' is restricted to {allowed_class} seats and cannot take a {classification} seat.",
                )

        if has_international_flag and "NRI" in category_rules and isinstance(category_rules["NRI"], dict):
            allowed_class = category_rules["NRI"].get("allowed_classifications", [])
            if allowed_class and classification not in allowed_class:
                return (
                    False,
                    f"International/NRI applicants are restricted to {allowed_class} seats and cannot take a {classification} seat.",
                )

    # Default institutional / AICTE rules (when no custom rules or unhandled category)
    if classification == Classification.REGULAR.value:
        if has_international_flag:
            return (
                False,
                f"Category '{category}' is restricted to SUPERNUMERARY quota and cannot be admitted to REGULAR intake.",
            )
        # Domestic categories allowed
        if category not in ("OPEN", "OBC", "SC", "ST", "EWS", "REGULAR"):
            return (
                False,
                f"Category '{category}' is not eligible for REGULAR seats.",
            )
    elif classification == Classification.SUPERNUMERARY.value:
        # Domestic students cannot spill into supernumerary seats
        if category in ("OPEN", "OBC", "SC", "ST", "EWS"):
            return (
                False,
                f"Domestic category '{category}' is not eligible for SUPERNUMERARY quota and regular seats cannot spill over.",
            )

    return True, None


def recompute_counters(
    institution_id: Optional[str] = None,
    programme_id: Optional[str] = None,
    academic_year: Optional[str] = None,
) -> int:
    """
    Reconciles admitted_regular and admitted_supernumerary counter columns in intake_capacities
    with active/graduated student records.
    Returns the count of intake_capacities updated.
    Counting Rule: Only ACTIVE and GRADUATED students count towards seats; WITHDRAWN frees a seat.
    """
    query = IntakeCapacity.query
    if institution_id:
        query = query.filter_by(institution_id=institution_id)
    if programme_id:
        query = query.filter_by(programme_id=programme_id)
    if academic_year:
        query = query.filter_by(academic_year=academic_year)

    capacities = query.all()
    active_statuses = [EnrolmentStatus.ACTIVE.value, EnrolmentStatus.GRADUATED.value]
    updated_count = 0

    for cap in capacities:
        reg_count = (
            db.session.query(db.func.count(StudentRecord.id))
            .filter(
                StudentRecord.institution_id == cap.institution_id,
                StudentRecord.programme_id == cap.programme_id,
                StudentRecord.academic_year == cap.academic_year,
                StudentRecord.classification == Classification.REGULAR.value,
                StudentRecord.enrolment_status.in_(active_statuses),
            )
            .scalar()
            or 0
        )

        sup_count = (
            db.session.query(db.func.count(StudentRecord.id))
            .filter(
                StudentRecord.institution_id == cap.institution_id,
                StudentRecord.programme_id == cap.programme_id,
                StudentRecord.academic_year == cap.academic_year,
                StudentRecord.classification == Classification.SUPERNUMERARY.value,
                StudentRecord.enrolment_status.in_(active_statuses),
            )
            .scalar()
            or 0
        )

        if cap.admitted_regular != reg_count or cap.admitted_supernumerary != sup_count:
            cap.admitted_regular = reg_count
            cap.admitted_supernumerary = sup_count
            updated_count += 1

    if updated_count > 0:
        db.session.commit()

    return updated_count


# ==============================================================================
# Summary & Queries
# ==============================================================================

def get_summary(
    institution_id: str,
    programme_id: str,
    academic_year: str,
) -> Dict[str, Any]:
    """
    Computes real-time dynamic seat ledger summary for a specific programme and academic year.
    Returns plain dict:
    - approved_regular, regular_admitted, regular_remaining
    - supernumerary_capacity, supernumerary_admitted, supernumerary_remaining
    - utilisation_pct, supernumerary_utilisation_pct
    - warning_level (None/80/90/100 from policy)
    - category_breakdown (per category counts of active students)
    Rule: Only ACTIVE and GRADUATED students count towards seats; WITHDRAWN frees a seat.
    """
    cap = IntakeCapacity.query.filter_by(
        institution_id=institution_id,
        programme_id=programme_id,
        academic_year=academic_year,
    ).first()

    if not cap:
        raise CapacityNotFound(
            f"No intake capacity configured for programme {programme_id} in {academic_year}."
        )

    active_statuses = [EnrolmentStatus.ACTIVE.value, EnrolmentStatus.GRADUATED.value]

    # Compute actual active student counts
    reg_count = (
        db.session.query(db.func.count(StudentRecord.id))
        .filter(
            StudentRecord.institution_id == institution_id,
            StudentRecord.programme_id == programme_id,
            StudentRecord.academic_year == academic_year,
            StudentRecord.classification == Classification.REGULAR.value,
            StudentRecord.enrolment_status.in_(active_statuses),
        )
        .scalar()
        or 0
    )

    sup_count = (
        db.session.query(db.func.count(StudentRecord.id))
        .filter(
            StudentRecord.institution_id == institution_id,
            StudentRecord.programme_id == programme_id,
            StudentRecord.academic_year == academic_year,
            StudentRecord.classification == Classification.SUPERNUMERARY.value,
            StudentRecord.enrolment_status.in_(active_statuses),
        )
        .scalar()
        or 0
    )

    # Auto-heal counters if drift detected
    if cap.admitted_regular != reg_count or cap.admitted_supernumerary != sup_count:
        cap.admitted_regular = reg_count
        cap.admitted_supernumerary = sup_count
        db.session.commit()

    approved_regular = cap.approved_regular_intake
    regular_admitted = reg_count
    regular_remaining = max(0, approved_regular - regular_admitted)

    supernumerary_capacity = cap.supernumerary_capacity
    supernumerary_admitted = sup_count
    supernumerary_remaining = max(0, supernumerary_capacity - supernumerary_admitted)

    utilisation_pct = (
        round((regular_admitted / approved_regular) * 100, 1) if approved_regular > 0 else 0.0
    )
    sup_utilisation_pct = (
        round((supernumerary_admitted / supernumerary_capacity) * 100, 1)
        if supernumerary_capacity > 0
        else 0.0
    )

    warning_level = calculate_warning_level(utilisation_pct)

    # Per-category breakdown for active students
    cat_query = (
        db.session.query(
            StudentRecord.admission_category,
            db.func.count(StudentRecord.id),
        )
        .filter(
            StudentRecord.institution_id == institution_id,
            StudentRecord.programme_id == programme_id,
            StudentRecord.academic_year == academic_year,
            StudentRecord.enrolment_status.in_(active_statuses),
        )
        .group_by(StudentRecord.admission_category)
        .all()
    )
    category_breakdown = {cat: count for cat, count in cat_query}
    for cat in ("OPEN", "OBC", "SC", "ST", "NRI", "OCI", "FN"):
        if cat not in category_breakdown:
            category_breakdown[cat] = 0

    category_quotas = {
        "OPEN": 90,
        "OBC": 50,
        "SC": 24,
        "ST": 16,
        "NRI": 6,
        "OCI": 5,
        "FN": 4,
    }

    return {
        "institution_id": institution_id,
        "programme_id": programme_id,
        "academic_year": academic_year,
        "approved_regular": approved_regular,
        "regular_admitted": regular_admitted,
        "regular_remaining": regular_remaining,
        "supernumerary_capacity": supernumerary_capacity,
        "supernumerary_admitted": supernumerary_admitted,
        "supernumerary_remaining": supernumerary_remaining,
        "utilisation_pct": utilisation_pct,
        "supernumerary_utilisation_pct": sup_utilisation_pct,
        "warning_level": warning_level,
        "category_breakdown": category_breakdown,
        "category_quotas": category_quotas,
    }


# ==============================================================================
# Pre-Check (Simulation without Mutating State)
# ==============================================================================

def precheck(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Applies explicit category eligibility and capacity rules in simulation mode.
    Returns:
    {
        "allowed": bool,
        "reason_code": str,
        "message": str,
        "summary_after": dict | None
    }
    Never modifies database records.
    """
    institution_id = payload.get("institution_id")
    programme_id = payload.get("programme_id")
    academic_year = payload.get("academic_year")
    classification = payload.get("classification", Classification.REGULAR.value)
    category = payload.get("admission_category") or payload.get("category", AdmissionCategory.OPEN.value)

    if not institution_id or not programme_id or not academic_year:
        return {
            "allowed": False,
            "reason_code": "VALIDATION_ERROR",
            "message": "institution_id, programme_id, and academic_year are required.",
            "summary_after": None,
        }

    cap = IntakeCapacity.query.filter_by(
        institution_id=institution_id,
        programme_id=programme_id,
        academic_year=academic_year,
    ).first()

    if not cap:
        return {
            "allowed": False,
            "reason_code": "CAPACITY_NOT_FOUND",
            "message": f"No intake capacity found for programme in academic year {academic_year}.",
            "summary_after": None,
        }

    # 1. Duplicate checks
    inst_student_id = payload.get("institution_student_id")
    if inst_student_id:
        dup_student = StudentRecord.query.filter(
            StudentRecord.institution_id == institution_id,
            StudentRecord.academic_year == academic_year,
            StudentRecord.institution_student_id == inst_student_id,
        ).first()
        if dup_student and dup_student.id != payload.get("student_id"):
            return {
                "allowed": False,
                "reason_code": "DUPLICATE_ADMISSION",
                "message": f"Student ID '{inst_student_id}' is already registered at this institution for {academic_year}.",
                "summary_after": None,
            }

    apaar_ref = payload.get("apaar_ref")
    if apaar_ref:
        active_statuses = [EnrolmentStatus.ACTIVE.value, EnrolmentStatus.GRADUATED.value]
        dup_apaar = StudentRecord.query.filter(
            StudentRecord.institution_id == institution_id,
            StudentRecord.academic_year == academic_year,
            StudentRecord.apaar_ref == str(apaar_ref).strip(),
            StudentRecord.enrolment_status.in_(active_statuses),
        ).first()
        if dup_apaar and dup_apaar.id != payload.get("student_id"):
            return {
                "allowed": False,
                "reason_code": "DUPLICATE_ADMISSION",
                "message": f"Student with APAAR '{mask_apaar(apaar_ref)}' is already actively admitted at this institution for {academic_year}.",
                "summary_after": None,
            }

    # 2. Category eligibility
    flags = {
        "is_nri": payload.get("is_nri", False),
        "is_oci": payload.get("is_oci", False),
        "is_foreign_national": payload.get("is_foreign_national", False),
    }
    is_eligible, elig_reason = check_category_eligibility(cap, category, classification, flags)
    if not is_eligible:
        return {
            "allowed": False,
            "reason_code": "CATEGORY_NOT_ELIGIBLE",
            "message": elig_reason or "Category is not eligible for the requested classification pool.",
            "summary_after": None,
        }

    # 3. Capacity availability check
    summary = get_summary(institution_id, programme_id, academic_year)

    if classification == Classification.REGULAR.value:
        if summary["regular_remaining"] <= 0:
            msg = (
                f"Approved regular intake is {summary['approved_regular']} and "
                f"{summary['regular_admitted']} regular admissions already exist; "
                f"admission {summary['regular_admitted'] + 1} was blocked"
            )
            return {
                "allowed": False,
                "reason_code": "CAPACITY_EXCEEDED",
                "message": msg,
                "summary_after": summary,
            }
        # Projected summary
        summary_after = dict(summary)
        summary_after["regular_admitted"] = summary["regular_admitted"] + 1
        summary_after["regular_remaining"] = max(0, summary["approved_regular"] - summary_after["regular_admitted"])
        summary_after["utilisation_pct"] = (
            round((summary_after["regular_admitted"] / summary["approved_regular"]) * 100, 1)
            if summary["approved_regular"] > 0
            else 0.0
        )
        summary_after["warning_level"] = calculate_warning_level(summary_after["utilisation_pct"])

    elif classification == Classification.SUPERNUMERARY.value:
        if summary["supernumerary_remaining"] <= 0:
            msg = (
                f"Supernumerary capacity is {summary['supernumerary_capacity']} and "
                f"{summary['supernumerary_admitted']} supernumerary admissions already exist; "
                f"admission {summary['supernumerary_admitted'] + 1} was blocked"
            )
            return {
                "allowed": False,
                "reason_code": "CAPACITY_EXCEEDED",
                "message": msg,
                "summary_after": summary,
            }
        summary_after = dict(summary)
        summary_after["supernumerary_admitted"] = summary["supernumerary_admitted"] + 1
        summary_after["supernumerary_remaining"] = max(
            0, summary["supernumerary_capacity"] - summary_after["supernumerary_admitted"]
        )
        summary_after["supernumerary_utilisation_pct"] = (
            round((summary_after["supernumerary_admitted"] / summary["supernumerary_capacity"]) * 100, 1)
            if summary["supernumerary_capacity"] > 0
            else 0.0
        )

    return {
        "allowed": True,
        "reason_code": "ALLOWED",
        "message": f"Admission allowed under {classification} intake.",
        "summary_after": summary_after,
    }


# ==============================================================================
# Commit Admission (Atomic DB Transaction)
# ==============================================================================

def _create_and_persist_rejected_event(
    institution_id: str,
    programme_id: str,
    academic_year: str,
    category: str,
    classification: str,
    reason_code: str,
    message: str,
    actor_id: str,
    actor: Any,
) -> AdmissionEvent:
    """
    Persists a REJECTED admission event and audit log in a safe, isolated transaction.
    Calls Member B risk engine hook if excess intake event.
    """
    db.session.rollback()

    event = AdmissionEvent(
        institution_id=institution_id,
        programme_id=programme_id,
        academic_year=academic_year,
        student_id=None,
        category=category,
        classification=classification,
        result=AdmissionEventResult.REJECTED.value,
        reason_code=reason_code,
        message=message,
        actor_id=actor_id,
        created_at=datetime.utcnow(),
    )
    db.session.add(event)
    db.session.commit()

    # Audit log
    audit_service.log(
        actor=actor_id,
        action="ADMISSION_REJECTED",
        entity_type="admission_event",
        entity_id=event.id,
        institution_id=institution_id,
        details={
            "reason_code": reason_code,
            "classification": classification,
            "category": category,
            "programme_id": programme_id,
            "academic_year": academic_year,
            "message": message,
        },
    )

    # Hook for Member B Risk Engine on excess intake violations
    if reason_code in ("CAPACITY_EXCEEDED", "EXCESS_INTAKE_VIOLATION"):
        try:
            import services.risk_engine as risk_engine
            if hasattr(risk_engine, "record_excess_intake_event"):
                risk_engine.record_excess_intake_event(event)
        except Exception as exc:
            current_app.logger.warning(f"Error calling record_excess_intake_event hook: {exc}")

    return event


def commit_admission(payload: Dict[str, Any], actor: Any) -> Dict[str, Any]:
    """
    Commits an admission within ONE database transaction:
    - Re-checks capacity under an atomic conditional update / lock.
    - Creates or attaches the student record.
    - Creates an admission_events row (ACCEPTED).
    - Audit logs the admission.
    - On failure: creates a REJECTED admission_events row, audit-logs it, and raises
      CapacityExceeded (or DuplicateAdmission / CategoryNotEligible).
    """
    institution_id = payload.get("institution_id")
    programme_id = payload.get("programme_id")
    academic_year = payload.get("academic_year")
    classification = payload.get("classification", Classification.REGULAR.value)
    category = payload.get("admission_category") or payload.get("category", AdmissionCategory.OPEN.value)
    actor_id = getattr(actor, "username", str(actor))

    if not institution_id or not programme_id or not academic_year:
        raise SeatLedgerError("institution_id, programme_id, and academic_year are required.")

    cap = IntakeCapacity.query.filter_by(
        institution_id=institution_id,
        programme_id=programme_id,
        academic_year=academic_year,
    ).first()

    if not cap:
        raise CapacityNotFound(
            f"No intake capacity configured for programme {programme_id} in {academic_year}."
        )

    # 1. Duplicate checks
    inst_student_id = payload.get("institution_student_id")
    if inst_student_id:
        dup_student = StudentRecord.query.filter(
            StudentRecord.institution_id == institution_id,
            StudentRecord.academic_year == academic_year,
            StudentRecord.institution_student_id == inst_student_id,
        ).first()
        if dup_student and dup_student.id != payload.get("student_id"):
            msg = f"Student ID '{inst_student_id}' is already registered at this institution for {academic_year}."
            _create_and_persist_rejected_event(
                institution_id, programme_id, academic_year, category, classification,
                "DUPLICATE_ADMISSION", msg, actor_id, actor
            )
            raise DuplicateAdmission(msg, details={"institution_student_id": inst_student_id})

    apaar_ref = payload.get("apaar_ref")
    if apaar_ref:
        active_statuses = [EnrolmentStatus.ACTIVE.value, EnrolmentStatus.GRADUATED.value]
        dup_apaar = StudentRecord.query.filter(
            StudentRecord.institution_id == institution_id,
            StudentRecord.academic_year == academic_year,
            StudentRecord.apaar_ref == str(apaar_ref).strip(),
            StudentRecord.enrolment_status.in_(active_statuses),
        ).first()
        if dup_apaar and dup_apaar.id != payload.get("student_id"):
            msg = f"Student with APAAR '{mask_apaar(apaar_ref)}' is already actively admitted at this institution for {academic_year}."
            _create_and_persist_rejected_event(
                institution_id, programme_id, academic_year, category, classification,
                "DUPLICATE_ADMISSION", msg, actor_id, actor
            )
            raise DuplicateAdmission(msg, details={"apaar_ref": mask_apaar(apaar_ref)})

    # 2. Category eligibility check
    flags = {
        "is_nri": payload.get("is_nri", False),
        "is_oci": payload.get("is_oci", False),
        "is_foreign_national": payload.get("is_foreign_national", False),
    }
    is_eligible, elig_reason = check_category_eligibility(cap, category, classification, flags)
    if not is_eligible:
        msg = elig_reason or "Category is not eligible for requested classification pool."
        _create_and_persist_rejected_event(
            institution_id, programme_id, academic_year, category, classification,
            "CATEGORY_NOT_ELIGIBLE", msg, actor_id, actor
        )
        raise CategoryNotEligible(msg, details={"category": category, "classification": classification})

    # 3. Atomic conditional update on counter column
    if classification == Classification.REGULAR.value:
        stmt = (
            db.update(IntakeCapacity)
            .where(
                IntakeCapacity.id == cap.id,
                IntakeCapacity.admitted_regular < IntakeCapacity.approved_regular_intake,
            )
            .values(admitted_regular=IntakeCapacity.admitted_regular + 1)
        )
        result = db.session.execute(stmt)
        if result.rowcount == 0:
            msg = (
                f"Approved regular intake is {cap.approved_regular_intake} and "
                f"{cap.approved_regular_intake} regular admissions already exist; "
                f"admission {cap.approved_regular_intake + 1} was blocked"
            )
            _create_and_persist_rejected_event(
                institution_id, programme_id, academic_year, category, classification,
                "CAPACITY_EXCEEDED", msg, actor_id, actor
            )
            raise CapacityExceeded(
                msg,
                details={
                    "approved_regular_intake": cap.approved_regular_intake,
                    "admitted_regular": cap.approved_regular_intake,
                },
            )
        db.session.expire(cap)

    elif classification == Classification.SUPERNUMERARY.value:
        stmt = (
            db.update(IntakeCapacity)
            .where(
                IntakeCapacity.id == cap.id,
                IntakeCapacity.admitted_supernumerary < IntakeCapacity.supernumerary_capacity,
            )
            .values(admitted_supernumerary=IntakeCapacity.admitted_supernumerary + 1)
        )
        result = db.session.execute(stmt)
        if result.rowcount == 0:
            msg = (
                f"Supernumerary capacity is {cap.supernumerary_capacity} and "
                f"{cap.supernumerary_capacity} supernumerary admissions already exist; "
                f"admission {cap.supernumerary_capacity + 1} was blocked"
            )
            _create_and_persist_rejected_event(
                institution_id, programme_id, academic_year, category, classification,
                "CAPACITY_EXCEEDED", msg, actor_id, actor
            )
            raise CapacityExceeded(
                msg,
                details={
                    "supernumerary_capacity": cap.supernumerary_capacity,
                    "admitted_supernumerary": cap.supernumerary_capacity,
                },
            )
        db.session.expire(cap)

    # 5. Create or attach the student record
    student = None
    try:
        if payload.get("student_record"):
            student = payload["student_record"]
            if student not in db.session:
                db.session.add(student)
        elif payload.get("student_id"):
            student = StudentRecord.query.get(payload["student_id"])
            if not student:
                raise SeatLedgerError(f"StudentRecord '{payload['student_id']}' not found.")
        else:
            # Create new StudentRecord
            adm_date = payload.get("admission_date")
            if isinstance(adm_date, str):
                adm_date = date.fromisoformat(adm_date)
            elif not isinstance(adm_date, date):
                adm_date = date.today()

            full_name = payload.get("full_name", "")
            student = StudentRecord(
                institution_id=institution_id,
                programme_id=programme_id,
                institution_student_id=payload.get("institution_student_id", ""),
                apaar_ref=str(apaar_ref).strip() if apaar_ref else None,
                full_name=full_name,
                normalized_name=normalize_name(full_name),
                academic_year=academic_year,
                admission_date=adm_date,
                admission_category=category,
                classification=classification,
                is_nri=flags["is_nri"],
                is_oci=flags["is_oci"],
                is_foreign_national=flags["is_foreign_national"],
                contact_ref=payload.get("contact_ref"),
                fee_payer_ref=payload.get("fee_payer_ref"),
                source_submission_id=payload.get("source_submission_id"),
                enrolment_status=payload.get("enrolment_status", EnrolmentStatus.ACTIVE.value),
                validation_status=payload.get("validation_status", ValidationStatus.PENDING.value),
                validation_notes=payload.get("validation_notes"),
                is_synthetic=True,
            )
            db.session.add(student)

        db.session.flush()

        # 6. Create ACCEPTED AdmissionEvent
        event = AdmissionEvent(
            institution_id=institution_id,
            programme_id=programme_id,
            academic_year=academic_year,
            student_id=student.id,
            category=category,
            classification=classification,
            result=AdmissionEventResult.ACCEPTED.value,
            reason_code="APPROVED",
            message=f"Admitted under {classification} intake",
            actor_id=actor_id,
            created_at=datetime.utcnow(),
        )
        db.session.add(event)
        db.session.flush()

        student_dict = student.to_dict()
        event_dict = event.to_dict()

        # 7. Audit log
        audit_service.log(
            actor=actor,
            action="ADMISSION_ACCEPTED",
            entity_type="student",
            entity_id=student.id,
            institution_id=institution_id,
            details={
                "classification": classification,
                "category": category,
                "programme_id": programme_id,
                "academic_year": academic_year,
                "admission_event_id": event.id,
            },
        )

        db.session.commit()

    except Exception:
        db.session.rollback()
        # Rollback counter decrement is automatic since counter update was in the same transaction
        raise

    return {
        "status": "ACCEPTED",
        "student": student_dict,
        "admission_event": event_dict,
        "summary": get_summary(institution_id, programme_id, academic_year),
    }
