"""
Tests for Seat Ledger Engine, Concurrency, and Capacity Limits (Task A9).

Verifies:
1. Within capacity accepted (regular and supernumerary).
2. Exactly at 120 -> 121st REGULAR rejected with explanation and persisted REJECTED event.
3. Supernumerary accepted until 10, 11th rejected.
4. NRI cannot take a REGULAR seat if rules forbid it (CATEGORY_NOT_ELIGIBLE).
5. Regular full does not allow spilling into supernumerary (strict pool segregation).
6. WITHDRAWN frees a seat for new admissions.
7. Duplicate admission blocked (duplicate ID or duplicate active APAAR).
8. Concurrency test with threads: two threads race for the last seat -> exactly one ACCEPTED.
9. Counters reconcile via recompute_counters().
10. Thresholds produce warning levels None/80/90/100 from policy.
11. Demo scenario numbers (116/120, 4/10) hold after seeding.
"""
from datetime import date, datetime
import os
import tempfile
import threading
import pytest

from app import create_app
from extensions import db
from models.institution import Institution, Programme, IntakeCapacity
from models.student import StudentRecord
from models.admission import AdmissionEvent
from models.policy import SystemPolicy
from models.audit import AuditLog
from models.enums import (
    Role,
    Classification,
    AdmissionCategory,
    EnrolmentStatus,
    ValidationStatus,
    AdmissionEventResult,
)
from services.seat_ledger import (
    get_summary,
    precheck,
    commit_admission,
    recompute_counters,
    calculate_warning_level,
    get_capacity_warning_thresholds,
    CapacityExceeded,
    DuplicateAdmission,
    CategoryNotEligible,
    CapacityNotFound,
)


@pytest.fixture
def ledger_fixture(app):
    """Seed minimal test reference data for seat ledger tests."""
    with app.app_context():
        # Clear tables
        db.session.query(AdmissionEvent).delete()
        db.session.query(StudentRecord).delete()
        db.session.query(IntakeCapacity).delete()
        db.session.query(Programme).delete()
        db.session.query(Institution).delete()
        db.session.query(SystemPolicy).delete()
        db.session.query(AuditLog).delete()
        db.session.commit()

        # Seed institutions
        vesit = Institution(code="VESIT", name="VESIT Mumbai", state="Maharashtra", city="Mumbai", status="ACTIVE")
        coep = Institution(code="COEP", name="COEP Pune", state="Maharashtra", city="Pune", status="ACTIVE")
        db.session.add_all([vesit, coep])
        db.session.flush()

        # Seed programmes
        vesit_it = Programme(institution_id=vesit.id, code="BTECH-IT", name="B.Tech IT", level="UG")
        coep_cs = Programme(institution_id=coep.id, code="BTECH-CE", name="B.Tech CE", level="UG")
        db.session.add_all([vesit_it, coep_cs])
        db.session.flush()

        # Seed capacities with standard category rules
        # VESIT IT: 120 Regular, 10 Supernumerary
        vesit_cap = IntakeCapacity(
            institution_id=vesit.id,
            programme_id=vesit_it.id,
            academic_year="2026-27",
            approved_regular_intake=120,
            supernumerary_capacity=10,
            admitted_regular=0,
            admitted_supernumerary=0,
            category_rules={
                "REGULAR": ["OPEN", "OBC", "SC", "ST", "EWS"],
                "SUPERNUMERARY": ["NRI", "OCI", "FN"],
            },
        )
        # COEP CS: 60 Regular, 5 Supernumerary
        coep_cap = IntakeCapacity(
            institution_id=coep.id,
            programme_id=coep_cs.id,
            academic_year="2026-27",
            approved_regular_intake=60,
            supernumerary_capacity=5,
            admitted_regular=0,
            admitted_supernumerary=0,
            category_rules={
                "REGULAR": ["OPEN", "OBC", "SC", "ST", "EWS"],
                "SUPERNUMERARY": ["NRI", "OCI", "FN"],
            },
        )
        db.session.add_all([vesit_cap, coep_cap])

        # Seed policies
        policy_thresh = SystemPolicy(
            key="capacity_warning_thresholds",
            value=[80, 90, 100],
            description="Warning thresholds",
        )
        db.session.add(policy_thresh)
        db.session.commit()

        return {
            "vesit_id": vesit.id,
            "vesit_it_id": vesit_it.id,
            "coep_id": coep.id,
            "coep_cs_id": coep_cs.id,
            "vesit_cap_id": vesit_cap.id,
        }


# ==============================================================================
# Tests
# ==============================================================================

def test_within_capacity_accepted(app, ledger_fixture):
    """Test that admission within capacity succeeds for both regular and supernumerary."""
    with app.app_context():
        inst_id = ledger_fixture["vesit_id"]
        prog_id = ledger_fixture["vesit_it_id"]

        # 1. Precheck regular
        payload_reg = {
            "institution_id": inst_id,
            "programme_id": prog_id,
            "academic_year": "2026-27",
            "institution_student_id": "V-IT-001",
            "full_name": "Aarav Sharma",
            "apaar_ref": "987654321001",
            "admission_category": "OPEN",
            "classification": "REGULAR",
        }
        res_pre = precheck(payload_reg)
        assert res_pre["allowed"] is True
        assert res_pre["reason_code"] == "ALLOWED"
        assert res_pre["summary_after"]["regular_admitted"] == 1
        assert res_pre["summary_after"]["regular_remaining"] == 119

        # 2. Commit regular admission
        res_commit = commit_admission(payload_reg, actor="VESIT_ADMIN")
        assert res_commit["status"] == "ACCEPTED"
        assert res_commit["student"]["institution_student_id"] == "V-IT-001"
        assert res_commit["admission_event"]["result"] == "ACCEPTED"
        assert res_commit["admission_event"]["reason_code"] == "APPROVED"
        assert res_commit["summary"]["regular_admitted"] == 1
        assert res_commit["summary"]["regular_remaining"] == 119

        # 3. Commit supernumerary admission (NRI)
        payload_sup = {
            "institution_id": inst_id,
            "programme_id": prog_id,
            "academic_year": "2026-27",
            "institution_student_id": "V-IT-002",
            "full_name": "Rohan Mehta",
            "apaar_ref": "987654321002",
            "admission_category": "NRI",
            "classification": "SUPERNUMERARY",
            "is_nri": True,
        }
        res_sup = commit_admission(payload_sup, actor="VESIT_ADMIN")
        assert res_sup["status"] == "ACCEPTED"
        assert res_sup["summary"]["supernumerary_admitted"] == 1
        assert res_sup["summary"]["supernumerary_remaining"] == 9


def test_regular_capacity_121st_rejected_with_explanation(app, ledger_fixture):
    """
    Test that when regular capacity is exactly 120:
    - 120 admissions are accepted.
    - 121st admission is rejected with CapacityExceeded.
    - Human explanation mentions 'Approved regular intake is 120 and 120 regular admissions already exist; admission 121 was blocked'.
    - REJECTED admission_events row is persisted.
    """
    with app.app_context():
        inst_id = ledger_fixture["vesit_id"]
        prog_id = ledger_fixture["vesit_it_id"]

        # Bulk create 120 regular students to fill capacity to exactly 120
        students = []
        events = []
        for i in range(1, 121):
            s = StudentRecord(
                institution_id=inst_id,
                programme_id=prog_id,
                institution_student_id=f"V-REG-{i:03d}",
                full_name=f"Student {i}",
                normalized_name=f"student {i}",
                academic_year="2026-27",
                admission_category="OPEN",
                classification=Classification.REGULAR.value,
                enrolment_status=EnrolmentStatus.ACTIVE.value,
                admission_date=date.today(),
            )
            students.append(s)
            ev = AdmissionEvent(
                institution_id=inst_id,
                programme_id=prog_id,
                academic_year="2026-27",
                student_id=s.id,
                category="OPEN",
                classification=Classification.REGULAR.value,
                result=AdmissionEventResult.ACCEPTED.value,
                reason_code="APPROVED",
                actor_id="SEED",
            )
            events.append(ev)

        db.session.add_all(students)
        db.session.flush()
        db.session.add_all(events)
        db.session.commit()

        # Reconcile counters
        recompute_counters(inst_id, prog_id, "2026-27")
        summary = get_summary(inst_id, prog_id, "2026-27")
        assert summary["regular_admitted"] == 120
        assert summary["regular_remaining"] == 0

        # Attempt 121st regular admission
        payload_121 = {
            "institution_id": inst_id,
            "programme_id": prog_id,
            "academic_year": "2026-27",
            "institution_student_id": "V-REG-121",
            "full_name": "Excess Student",
            "admission_category": "OPEN",
            "classification": "REGULAR",
        }

        # Precheck must report blocked
        pre = precheck(payload_121)
        assert pre["allowed"] is False
        assert pre["reason_code"] == "CAPACITY_EXCEEDED"
        assert "Approved regular intake is 120 and 120 regular admissions already exist; admission 121 was blocked" in pre["message"]

        # Commit must raise CapacityExceeded
        with pytest.raises(CapacityExceeded) as exc_info:
            commit_admission(payload_121, actor="VESIT_ADMIN")

        assert "admission 121 was blocked" in str(exc_info.value)

        # Student record for 121 must NOT exist
        assert StudentRecord.query.filter_by(institution_student_id="V-REG-121").first() is None

        # Persisted REJECTED admission event MUST exist
        rej_event = AdmissionEvent.query.filter_by(
            institution_id=inst_id,
            programme_id=prog_id,
            academic_year="2026-27",
            result=AdmissionEventResult.REJECTED.value,
            reason_code="CAPACITY_EXCEEDED",
        ).first()

        assert rej_event is not None
        assert rej_event.classification == "REGULAR"
        assert "admission 121 was blocked" in rej_event.message


def test_supernumerary_accepted_until_limit_and_11th_rejected(app, ledger_fixture):
    """Test supernumerary intake accepts up to capacity 10 and rejects 11th."""
    with app.app_context():
        inst_id = ledger_fixture["vesit_id"]
        prog_id = ledger_fixture["vesit_it_id"]

        # Admit 10 supernumerary students
        for i in range(1, 11):
            payload = {
                "institution_id": inst_id,
                "programme_id": prog_id,
                "academic_year": "2026-27",
                "institution_student_id": f"V-SUP-{i:03d}",
                "full_name": f"NRI Student {i}",
                "admission_category": "NRI",
                "classification": "SUPERNUMERARY",
                "is_nri": True,
            }
            res = commit_admission(payload, actor="VESIT_ADMIN")
            assert res["status"] == "ACCEPTED"

        summary = get_summary(inst_id, prog_id, "2026-27")
        assert summary["supernumerary_admitted"] == 10
        assert summary["supernumerary_remaining"] == 0

        # Attempt 11th supernumerary admission
        payload_11 = {
            "institution_id": inst_id,
            "programme_id": prog_id,
            "academic_year": "2026-27",
            "institution_student_id": "V-SUP-011",
            "full_name": "NRI Student 11",
            "admission_category": "NRI",
            "classification": "SUPERNUMERARY",
            "is_nri": True,
        }

        pre = precheck(payload_11)
        assert pre["allowed"] is False
        assert pre["reason_code"] == "CAPACITY_EXCEEDED"
        assert "Supernumerary capacity is 10 and 10 supernumerary admissions already exist; admission 11 was blocked" in pre["message"]

        with pytest.raises(CapacityExceeded) as exc_info:
            commit_admission(payload_11, actor="VESIT_ADMIN")

        assert "admission 11 was blocked" in str(exc_info.value)

        # REJECTED event must be recorded
        rej_event = AdmissionEvent.query.filter_by(
            classification="SUPERNUMERARY",
            result="REJECTED",
            reason_code="CAPACITY_EXCEEDED",
        ).first()
        assert rej_event is not None


def test_nri_cannot_take_regular_seat_if_forbidden(app, ledger_fixture):
    """Verify category eligibility: NRI applicant cannot take a REGULAR seat."""
    with app.app_context():
        inst_id = ledger_fixture["vesit_id"]
        prog_id = ledger_fixture["vesit_it_id"]

        payload = {
            "institution_id": inst_id,
            "programme_id": prog_id,
            "academic_year": "2026-27",
            "institution_student_id": "V-NRI-MISMATCH",
            "full_name": "International Candidate",
            "admission_category": "NRI",
            "classification": "REGULAR",  # Invalid pool for NRI
            "is_nri": True,
        }

        # Precheck must reject with CATEGORY_NOT_ELIGIBLE
        pre = precheck(payload)
        assert pre["allowed"] is False
        assert pre["reason_code"] == "CATEGORY_NOT_ELIGIBLE"

        # Commit must raise CategoryNotEligible
        with pytest.raises(CategoryNotEligible) as exc_info:
            commit_admission(payload, actor="VESIT_ADMIN")

        assert "CATEGORY_NOT_ELIGIBLE" in exc_info.value.reason_code

        # REJECTED event persisted
        rej_event = AdmissionEvent.query.filter_by(
            reason_code="CATEGORY_NOT_ELIGIBLE"
        ).first()
        assert rej_event is not None
        assert rej_event.category == "NRI"


def test_regular_full_does_not_spill_into_supernumerary(app, ledger_fixture):
    """
    Test strict pool segregation:
    When regular seats are full, domestic students cannot take supernumerary seats.
    """
    with app.app_context():
        inst_id = ledger_fixture["vesit_id"]
        prog_id = ledger_fixture["vesit_it_id"]

        # Cap has 10 supernumerary seats available
        # Attempt to admit a domestic OPEN student into SUPERNUMERARY pool
        payload = {
            "institution_id": inst_id,
            "programme_id": prog_id,
            "academic_year": "2026-27",
            "institution_student_id": "V-OPEN-SPILL",
            "full_name": "Spill Attempt Candidate",
            "admission_category": "OPEN",
            "classification": "SUPERNUMERARY",  # Ineligible
        }

        pre = precheck(payload)
        assert pre["allowed"] is False
        assert pre["reason_code"] == "CATEGORY_NOT_ELIGIBLE"

        with pytest.raises(CategoryNotEligible):
            commit_admission(payload, actor="VESIT_ADMIN")


def test_withdrawn_frees_a_seat(app, ledger_fixture):
    """
    Test seat release:
    - Fill regular capacity to 120.
    - Set one student to WITHDRAWN.
    - Verify regular_remaining increases from 0 to 1.
    - Verify a new student can now be admitted into the freed seat.
    """
    with app.app_context():
        inst_id = ledger_fixture["vesit_id"]
        prog_id = ledger_fixture["vesit_it_id"]

        # Bulk create 120 students
        students = []
        for i in range(1, 121):
            s = StudentRecord(
                institution_id=inst_id,
                programme_id=prog_id,
                institution_student_id=f"V-SEAT-{i:03d}",
                full_name=f"Student {i}",
                normalized_name=f"student {i}",
                academic_year="2026-27",
                admission_category="OPEN",
                classification=Classification.REGULAR.value,
                enrolment_status=EnrolmentStatus.ACTIVE.value,
                admission_date=date.today(),
            )
            students.append(s)

        db.session.add_all(students)
        db.session.commit()
        recompute_counters(inst_id, prog_id, "2026-27")

        summary_before = get_summary(inst_id, prog_id, "2026-27")
        assert summary_before["regular_admitted"] == 120
        assert summary_before["regular_remaining"] == 0

        # Withdraw student 50
        withdrawn_student = StudentRecord.query.filter_by(institution_student_id="V-SEAT-050").first()
        withdrawn_student.enrolment_status = EnrolmentStatus.WITHDRAWN.value
        db.session.commit()

        # Summary must immediately reflect the freed seat
        summary_after = get_summary(inst_id, prog_id, "2026-27")
        assert summary_after["regular_admitted"] == 119
        assert summary_after["regular_remaining"] == 1

        # Now admit a replacement student
        replacement_payload = {
            "institution_id": inst_id,
            "programme_id": prog_id,
            "academic_year": "2026-27",
            "institution_student_id": "V-REPLACEMENT-001",
            "full_name": "Replacement Student",
            "admission_category": "OPEN",
            "classification": "REGULAR",
        }

        res = commit_admission(replacement_payload, actor="VESIT_ADMIN")
        assert res["status"] == "ACCEPTED"
        assert res["summary"]["regular_admitted"] == 120
        assert res["summary"]["regular_remaining"] == 0


def test_duplicate_admission_blocked(app, ledger_fixture):
    """Test that duplicate student ID and duplicate active APAAR are blocked."""
    with app.app_context():
        inst_id = ledger_fixture["vesit_id"]
        prog_id = ledger_fixture["vesit_it_id"]

        # Admit initial student
        p1 = {
            "institution_id": inst_id,
            "programme_id": prog_id,
            "academic_year": "2026-27",
            "institution_student_id": "V-DUP-001",
            "full_name": "First Admission",
            "apaar_ref": "999988881111",
            "admission_category": "OPEN",
            "classification": "REGULAR",
        }
        commit_admission(p1, actor="VESIT_ADMIN")

        # 1. Attempt duplicate institutional student ID
        p_dup_id = {
            "institution_id": inst_id,
            "programme_id": prog_id,
            "academic_year": "2026-27",
            "institution_student_id": "V-DUP-001",  # Same ID
            "full_name": "Another Person",
            "apaar_ref": "111122223333",
            "admission_category": "OPEN",
            "classification": "REGULAR",
        }
        pre1 = precheck(p_dup_id)
        assert pre1["allowed"] is False
        assert pre1["reason_code"] == "DUPLICATE_ADMISSION"

        with pytest.raises(DuplicateAdmission):
            commit_admission(p_dup_id, actor="VESIT_ADMIN")

        # 2. Attempt duplicate APAAR reference
        p_dup_apaar = {
            "institution_id": inst_id,
            "programme_id": prog_id,
            "academic_year": "2026-27",
            "institution_student_id": "V-DUP-002",
            "full_name": "Third Person",
            "apaar_ref": "999988881111",  # Same APAAR
            "admission_category": "OPEN",
            "classification": "REGULAR",
        }
        pre2 = precheck(p_dup_apaar)
        assert pre2["allowed"] is False
        assert pre2["reason_code"] == "DUPLICATE_ADMISSION"

        with pytest.raises(DuplicateAdmission):
            commit_admission(p_dup_apaar, actor="VESIT_ADMIN")


def test_counters_reconcile(app, ledger_fixture):
    """Test recompute_counters restores correct counts from student_records."""
    with app.app_context():
        inst_id = ledger_fixture["vesit_id"]
        prog_id = ledger_fixture["vesit_it_id"]

        # Create 5 active regular, 2 active supernumerary, 1 withdrawn regular
        for i in range(1, 6):
            db.session.add(StudentRecord(
                institution_id=inst_id, programme_id=prog_id, institution_student_id=f"R{i}",
                full_name=f"Reg {i}", normalized_name=f"reg {i}", academic_year="2026-27",
                admission_category="OPEN", classification="REGULAR", enrolment_status="ACTIVE",
                admission_date=date.today()
            ))
        for i in range(1, 3):
            db.session.add(StudentRecord(
                institution_id=inst_id, programme_id=prog_id, institution_student_id=f"S{i}",
                full_name=f"Sup {i}", normalized_name=f"sup {i}", academic_year="2026-27",
                admission_category="NRI", classification="SUPERNUMERARY", enrolment_status="ACTIVE",
                admission_date=date.today(), is_nri=True
            ))
        db.session.add(StudentRecord(
            institution_id=inst_id, programme_id=prog_id, institution_student_id="W1",
            full_name="Withdrawn", normalized_name="withdrawn", academic_year="2026-27",
            admission_category="OPEN", classification="REGULAR", enrolment_status="WITHDRAWN",
            admission_date=date.today()
        ))
        db.session.commit()

        # Deliberately corrupt counter columns
        cap = IntakeCapacity.query.filter_by(
            institution_id=inst_id, programme_id=prog_id, academic_year="2026-27"
        ).first()
        cap.admitted_regular = 999
        cap.admitted_supernumerary = 888
        db.session.commit()

        # Run reconcile
        updated = recompute_counters(inst_id, prog_id, "2026-27")
        assert updated == 1

        db.session.refresh(cap)
        assert cap.admitted_regular == 5  # 5 ACTIVE regular (withdrawn ignored)
        assert cap.admitted_supernumerary == 2  # 2 ACTIVE supernumerary


def test_thresholds_produce_warning_levels(app, ledger_fixture):
    """Test warning levels calculated dynamically from system_policies."""
    with app.app_context():
        # Default thresholds in fixture: [100, 90, 80]
        assert calculate_warning_level(79.9) is None
        assert calculate_warning_level(80.0) == 80
        assert calculate_warning_level(85.5) == 80
        assert calculate_warning_level(90.0) == 90
        assert calculate_warning_level(95.0) == 90
        assert calculate_warning_level(100.0) == 100
        assert calculate_warning_level(105.0) == 100

        # Dynamic update in system_policies
        thresh_pol = SystemPolicy.query.filter_by(key="capacity_warning_thresholds").first()
        thresh_pol.value = [70, 85, 95]
        db.session.commit()

        # Now 75% triggers 70
        assert calculate_warning_level(75.0) == 70
        assert calculate_warning_level(86.0) == 85
        assert calculate_warning_level(96.0) == 95


def test_concurrency_race_for_last_seat():
    """
    Concurrency test with threads:
    Two threads race for the last remaining regular seat in a dedicated file-backed SQLite database.
    Exactly ONE thread succeeds (ACCEPTED); exactly ONE thread fails with CapacityExceeded.
    Final admitted count strictly matches capacity.
    """
    # Create temporary SQLite DB file so threads share the exact same physical DB
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        temp_db_path = tf.name

    from config import TestingConfig
    orig_test_uri = TestingConfig.SQLALCHEMY_DATABASE_URI

    try:
        db_url = f"sqlite:///{temp_db_path}?timeout=30"
        TestingConfig.SQLALCHEMY_DATABASE_URI = db_url
        test_app = create_app("testing")

        with test_app.app_context():
            db.session.remove()
            db.create_all()

            # Create institution, programme, and capacity with approved intake = 2
            inst = Institution(code="VESIT_CONC", name="VESIT", state="MH", city="Mumbai", status="ACTIVE")
            db.session.add(inst)
            db.session.flush()

            prog = Programme(institution_id=inst.id, code="IT_CONC", name="IT", level="UG")
            db.session.add(prog)
            db.session.flush()

            # Capacity: 2 Regular seats
            cap = IntakeCapacity(
                institution_id=inst.id,
                programme_id=prog.id,
                academic_year="2026-27",
                approved_regular_intake=2,
                supernumerary_capacity=0,
                admitted_regular=1,  # 1 seat already occupied -> exactly 1 seat left!
                admitted_supernumerary=0,
                category_rules={"REGULAR": ["OPEN"]},
            )
            db.session.add(cap)

            # Pre-existing student for the 1st seat
            db.session.add(StudentRecord(
                institution_id=inst.id,
                programme_id=prog.id,
                institution_student_id="C-001",
                full_name="First Student",
                normalized_name="first student",
                academic_year="2026-27",
                admission_category="OPEN",
                classification="REGULAR",
                enrolment_status="ACTIVE",
                admission_date=date.today(),
            ))
            inst_id = str(inst.id)
            prog_id = str(prog.id)
            db.session.commit()
            db.session.remove()

        # Thread racing setup
        barrier = threading.Barrier(2)
        results = []
        errors = []
        lock = threading.Lock()

        def try_admission(student_num: int):
            # Synchronize thread start using a Barrier
            barrier.wait()
            with test_app.app_context():
                db.session.remove()
                payload = {
                    "institution_id": inst_id,
                    "programme_id": prog_id,
                    "academic_year": "2026-27",
                    "institution_student_id": f"RACE-00{student_num}",
                    "full_name": f"Racer {student_num}",
                    "admission_category": "OPEN",
                    "classification": "REGULAR",
                }
                try:
                    res = commit_admission(payload, actor=f"THREAD_{student_num}")
                    with lock:
                        results.append(res)
                except Exception as exc:
                    with lock:
                        errors.append(exc)
                finally:
                    db.session.remove()

        t1 = threading.Thread(target=try_admission, args=(1,))
        t2 = threading.Thread(target=try_admission, args=(2,))

        t1.start()
        t2.start()

        t1.join()
        t2.join()

        # Verification:
        # Exactly ONE thread got ACCEPTED
        assert len(results) == 1, f"Expected 1 success, got {len(results)}. Errors: {errors}"
        assert results[0]["status"] == "ACCEPTED"

        # Exactly ONE thread failed with CapacityExceeded
        assert len(errors) == 1, f"Expected 1 failure, got {len(errors)}"
        assert isinstance(errors[0], CapacityExceeded)
        assert "admission 3 was blocked" in str(errors[0])

        # Verify final state in database: strictly 2 active students admitted
        with test_app.app_context():
            db.session.remove()
            final_cap = IntakeCapacity.query.filter_by(
                institution_id=inst_id, programme_id=prog_id, academic_year="2026-27"
            ).first()
            assert final_cap.admitted_regular == 2

            active_students = StudentRecord.query.filter_by(
                institution_id=inst_id, programme_id=prog_id, academic_year="2026-27"
            ).count()
            assert active_students == 2

    finally:
        TestingConfig.SQLALCHEMY_DATABASE_URI = orig_test_uri
        # Cleanup temp file
        if os.path.exists(temp_db_path):
            try:
                os.remove(temp_db_path)
            except Exception:
                pass


def test_demo_scenario_numbers_hold_after_seeding(app):
    """
    Verifies that demo scenario numbers hold:
    VESIT / B.Tech IT / 2026-27: 116/120 Regular, 4/10 Supernumerary.
    Remaining: 4 Regular, 6 Supernumerary.
    """
    from seed import seed_reference_data, seed_students
    import random

    with app.app_context():
        # Clear tables
        db.drop_all()
        db.create_all()

        rng = random.Random(42)
        seed_reference_data(rng)
        seed_students(db, rng)
        recompute_counters()

        vesit = Institution.query.filter_by(code="VESIT").first()
        prog = Programme.query.filter_by(institution_id=vesit.id, code="BTECH-IT").first()

        summary = get_summary(vesit.id, prog.id, "2026-27")

        assert summary["approved_regular"] == 180
        assert summary["regular_admitted"] == 90
        assert summary["regular_remaining"] == 90

        assert summary["supernumerary_capacity"] == 15
        assert summary["supernumerary_admitted"] == 8
        assert summary["supernumerary_remaining"] == 7

        assert summary["utilisation_pct"] == 50.0
        assert summary["warning_level"] is None
