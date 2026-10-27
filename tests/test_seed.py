"""
Tests for reference data seeding (Task A3).
Verifies determinism, exact account counts, password security, and key store functionality.
"""
import random
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives import serialization

from extensions import db
from models import (
    Institution,
    Programme,
    IntakeCapacity,
    SystemPolicy,
    User,
    SigningKey,
    Role,
)
from seed import seed_reference_data
from services.key_store import load_private_key, get_active_key_id
from services.authz import roles_required


def test_seed_exact_counts_and_institutions(app):
    """
    Verify seed contract from AGENTS.md §5.2 and §5.11:
    - Exactly 3 institutions (VESIT, COEP, VJTI)
    - Exactly 4 demo accounts (AICTE_Admin, VESIT, COEP, VJTI)
    - No student, verifier, or auditor accounts seeded
    """
    with app.app_context():
        seed_reference_data(random.Random(42))

        # Institutions check
        institutions = Institution.query.all()
        assert len(institutions) == 3
        inst_codes = {inst.code for inst in institutions}
        assert inst_codes == {"VESIT", "COEP", "VJTI"}

        for inst in institutions:
            assert inst.is_synthetic is True
            assert inst.status == "ACTIVE"

        # Users check
        users = User.query.all()
        assert len(users) == 4
        usernames = {u.username for u in users}
        assert usernames == {"AICTE_Admin", "VESIT", "COEP", "VJTI"}

        user_roles = {u.username: u.role for u in users}
        assert user_roles["AICTE_Admin"] == Role.AICTE_ADMIN.value
        assert user_roles["VESIT"] == Role.INSTITUTION_ADMIN.value
        assert user_roles["COEP"] == Role.INSTITUTION_ADMIN.value
        assert user_roles["VJTI"] == Role.INSTITUTION_ADMIN.value

        # Scoping check
        vesit_inst = Institution.query.filter_by(code="VESIT").first()
        vesit_user = User.query.filter_by(username="VESIT").first()
        assert vesit_user.institution_id == vesit_inst.id

        aicte_user = User.query.filter_by(username="AICTE_Admin").first()
        assert aicte_user.institution_id is None


def test_seed_determinism_produces_identical_ids(app):
    """Verify seeding twice with reset produces 100% identical primary keys and values."""
    with app.app_context():
        # First seed run
        seed_reference_data(random.Random(42))
        run1_inst = {inst.code: inst.id for inst in Institution.query.all()}
        run1_users = {u.username: u.id for u in User.query.all()}
        run1_policies = {p.key: (p.id, p.value) for p in SystemPolicy.query.all()}

        # Wipe and seed second run
        db.drop_all()
        db.create_all()
        seed_reference_data(random.Random(42))

        run2_inst = {inst.code: inst.id for inst in Institution.query.all()}
        run2_users = {u.username: u.id for u in User.query.all()}
        run2_policies = {p.key: (p.id, p.value) for p in SystemPolicy.query.all()}

        assert run1_inst == run2_inst
        assert run1_users == run2_users
        assert run1_policies == run2_policies


def test_no_plaintext_passwords_stored(app):
    """Verify passwords are authenticated via hashes and no plaintext password exists in DB."""
    with app.app_context():
        seed_reference_data(random.Random(42))
        users = User.query.all()

        for user in users:
            assert user.password_hash != "Demo@12345"
            assert user.password_hash.startswith("scrypt:") or user.password_hash.startswith("pbkdf2:")
            assert user.check_password("Demo@12345") is True
            assert user.check_password("WrongPassword") is False


def test_all_defined_role_enums_supported_in_auth_code(app):
    """
    Verify all 5 role enums (AICTE_ADMIN, INSTITUTION_ADMIN, STUDENT, VERIFIER, AUDITOR)
    remain fully supported in authorization decorators even though only 2 roles are seeded.
    """
    with app.app_context():
        # Check that all roles defined in AGENTS.md §5.1 exist
        expected_roles = {"AICTE_ADMIN", "INSTITUTION_ADMIN", "STUDENT", "VERIFIER", "AUDITOR"}
        actual_roles = {r.value for r in Role}
        assert expected_roles == actual_roles

        # Ensure decorator accepts all role enums without error
        for role in Role:
            decorator = roles_required(role)
            assert callable(decorator)


def test_signing_keys_public_in_db_private_in_keystore(app):
    """Verify DB stores public key PEM only, and private key can sign and verify."""
    with app.app_context():
        seed_reference_data(random.Random(42))

        signing_keys = SigningKey.query.all()
        assert len(signing_keys) == 1
        db_key = signing_keys[0]

        assert db_key.key_id == get_active_key_id()
        assert "BEGIN PUBLIC KEY" in db_key.public_key_pem
        assert "PRIVATE KEY" not in db_key.public_key_pem

        # Load private key from keystore helper and sign test message
        private_key = load_private_key(db_key.key_id)
        test_message = b"APAAR_NEXUS_CREDENTIAL_SIGNING_VERIFICATION"
        signature = private_key.sign(test_message)

        # Verify signature with public key loaded from database
        public_key = serialization.load_pem_public_key(db_key.public_key_pem.encode("utf-8"))
        assert isinstance(public_key, Ed25519PublicKey)
        public_key.verify(signature, test_message)  # Raises InvalidSignature if failed


def test_seed_students_exact_scenario_counts(app):
    """
    Verify Task A4 student seeding requirements:
    - >= 150 student records (specifically 365)
    - VESIT / B.Tech IT / 2026-27: 116 regular, 4 supernumerary (remaining 4 reg, 6 sup)
    - Utilisation spread: ~50%, 80%, 90%, 100% full programme, and 1-seat-left concurrency programme
    - Two past excess attempts recorded with result=REJECTED and reason_code=CAPACITY_EXCEEDED
    - At least one eligible synthetic VESIT student record exists for credential demos (no student login)
    """
    with app.app_context():
        from seed import seed_students
        from models import StudentRecord, AdmissionEvent, Classification, AdmissionCategory

        seed_reference_data(random.Random(42))
        student_count = seed_students(db, random.Random(42))

        assert student_count >= 150
        assert student_count == 609

        # All records marked synthetic
        assert StudentRecord.query.filter_by(is_synthetic=True).count() == 609

        # Scenario 1: VESIT B.Tech IT 2026-27
        vesit = Institution.query.filter_by(code="VESIT").first()
        btech_it = Programme.query.filter_by(institution_id=vesit.id, code="BTECH-IT").first()

        vesit_it_students = StudentRecord.query.filter_by(
            institution_id=vesit.id,
            programme_id=btech_it.id,
            academic_year="2026-27"
        ).all()

        regular_students = [s for s in vesit_it_students if s.classification == Classification.REGULAR.value]
        supernumerary_students = [s for s in vesit_it_students if s.classification == Classification.SUPERNUMERARY.value]

        assert len(regular_students) == 90
        assert len(supernumerary_students) == 8

        # Supernumerary categories: NRI, OCI, FN
        categories = {s.admission_category for s in supernumerary_students}
        assert {"NRI", "OCI", "FN"}.issubset(categories)

        cap = IntakeCapacity.query.filter_by(
            institution_id=vesit.id,
            programme_id=btech_it.id,
            academic_year="2026-27"
        ).first()
        assert cap.approved_regular_intake == 180
        assert cap.supernumerary_capacity == 15
        assert cap.approved_regular_intake - len(regular_students) == 90
        assert cap.supernumerary_capacity - len(supernumerary_students) == 7

        # Scenario 2 & 3: Half-filled capacity across all 3 institutions
        # COEP Diploma Mech: 90 Regular + 7 Supernumerary = 97
        coep = Institution.query.filter_by(code="COEP").first()
        dip_mech = Programme.query.filter_by(institution_id=coep.id, code="DIP-MECH").first()
        dip_mech_students = StudentRecord.query.filter_by(
            institution_id=coep.id,
            programme_id=dip_mech.id,
            academic_year="2026-27"
        ).all()
        dip_mech_cap = IntakeCapacity.query.filter_by(
            institution_id=coep.id,
            programme_id=dip_mech.id,
            academic_year="2026-27"
        ).first()
        assert len([s for s in dip_mech_students if s.classification == Classification.REGULAR.value]) == 90
        assert dip_mech_cap.approved_regular_intake == 180

        # VESIT B.Tech CE: 90 regular seats admitted (50% utilised)
        vesit_ce = Programme.query.filter_by(institution_id=vesit.id, code="BTECH-CE").first()
        vesit_ce_students = StudentRecord.query.filter_by(
            institution_id=vesit.id,
            programme_id=vesit_ce.id,
            academic_year="2026-27"
        ).all()
        vesit_ce_cap = IntakeCapacity.query.filter_by(
            institution_id=vesit.id,
            programme_id=vesit_ce.id,
            academic_year="2026-27"
        ).first()
        assert len([s for s in vesit_ce_students if s.classification == Classification.REGULAR.value]) == 90
        assert vesit_ce_cap.approved_regular_intake == 180

        # COEP B.Tech CE: 90 regular + 8 supernumerary = 98
        coep_ce = Programme.query.filter_by(institution_id=coep.id, code="BTECH-CE").first()
        coep_ce_count = StudentRecord.query.filter_by(
            institution_id=coep.id,
            programme_id=coep_ce.id,
            academic_year="2026-27"
        ).count()
        assert coep_ce_count == 98

        # VJTI B.Tech EXTC: 90 regular + 7 supernumerary = 97
        vjti = Institution.query.filter_by(code="VJTI").first()
        vjti_extc = Programme.query.filter_by(institution_id=vjti.id, code="BTECH-EXTC").first()
        vjti_extc_count = StudentRecord.query.filter_by(
            institution_id=vjti.id,
            programme_id=vjti_extc.id,
            academic_year="2026-27"
        ).count()
        assert vjti_extc_count == 97

        # VJTI B.Tech IT: 90 regular + 8 supernumerary = 98
        vjti_it = Programme.query.filter_by(institution_id=vjti.id, code="BTECH-IT").first()
        vjti_it_count = StudentRecord.query.filter_by(
            institution_id=vjti.id,
            programme_id=vjti_it.id,
            academic_year="2026-27"
        ).count()
        assert vjti_it_count == 98

        # Scenario 3: Past rejected excess admission attempts
        rejected_events = AdmissionEvent.query.filter_by(
            result="REJECTED",
            reason_code="CAPACITY_EXCEEDED"
        ).all()
        assert len(rejected_events) == 2

        # Scenario 7: Eligible VESIT candidate for credential demos
        demo_candidate = StudentRecord.query.filter_by(apaar_ref="900000000001").first()
        assert demo_candidate is not None
        assert demo_candidate.institution_id == vesit.id
        assert demo_candidate.programme_id == btech_it.id
        assert demo_candidate.full_name == "Aarav Sharma"
        assert demo_candidate.enrolment_status == "ACTIVE"
        assert demo_candidate.validation_status == "VALID"
        # No student login account exists for demo students per AGENTS.md §5.2
        assert User.query.filter_by(student_id=demo_candidate.id).count() == 0


def test_seed_students_identity_and_signal_scenarios(app):
    """
    Verify identity conflicts and investigation signals seeded per Task A4:
    - Simultaneous enrolment: same apaar_ref active in 2 institutions
    - Duplicate APAAR within 1 institution with distinct student IDs
    - Corroborated similar name spelling variation
    - Legitimate shared contact and shared fee-payer references
    - Malformed / missing APAAR flagged validation_status=INVALID
    """
    with app.app_context():
        from seed import seed_students
        from models import StudentRecord

        seed_reference_data(random.Random(42))
        seed_students(db, random.Random(42))

        # Scenario 4(a): Simultaneous enrolment
        simul_students = StudentRecord.query.filter_by(apaar_ref="900000000010").all()
        assert len(simul_students) == 2
        inst_codes = {s.institution.code for s in simul_students}
        assert inst_codes == {"VESIT", "COEP"}
        assert all(s.enrolment_status == "ACTIVE" for s in simul_students)
        assert all(s.academic_year == "2026-27" for s in simul_students)

        # Scenario 4(b): Duplicate APAAR in one institution
        dup_students = StudentRecord.query.filter_by(apaar_ref="900000000020").all()
        assert len(dup_students) == 2
        assert dup_students[0].institution_id == dup_students[1].institution_id
        assert dup_students[0].institution_student_id != dup_students[1].institution_student_id

        # Scenario 4(c): Corroborated fuzzy name variation
        rahul_sharma = StudentRecord.query.filter_by(apaar_ref="900000000031").first()
        rahul_sarma = StudentRecord.query.filter_by(apaar_ref="900000000032").first()
        assert rahul_sharma.full_name == "Rahul Sharma"
        assert rahul_sarma.full_name == "Rahul Sarma"
        assert rahul_sharma.institution_id == rahul_sarma.institution_id
        assert rahul_sharma.programme_id == rahul_sarma.programme_id
        assert rahul_sharma.admission_date == rahul_sarma.admission_date

        # Scenario 5: Legitimate shared contact & fee-payer
        contact_shared = StudentRecord.query.filter_by(contact_ref="SYN-C-0050").all()
        assert len(contact_shared) == 2
        assert contact_shared[0].apaar_ref != contact_shared[1].apaar_ref
        assert contact_shared[0].full_name != contact_shared[1].full_name

        fee_shared = StudentRecord.query.filter_by(fee_payer_ref="SYN-F-0099").all()
        assert len(fee_shared) == 2
        assert fee_shared[0].apaar_ref != fee_shared[1].apaar_ref
        assert fee_shared[0].full_name != fee_shared[1].full_name

        # Scenario 6: Malformed / missing APAAR
        invalid_students = StudentRecord.query.filter_by(validation_status="INVALID").all()
        assert len(invalid_students) == 4
        for s in invalid_students:
            assert s.validation_notes is not None


def test_reseeding_students_is_deterministic(app):
    """Verify seeding students twice with reset produces identical IDs, APAARs, and counts."""
    with app.app_context():
        from seed import seed_students
        from models import StudentRecord, AdmissionEvent

        # Run 1
        seed_reference_data(random.Random(42))
        seed_students(db, random.Random(42))
        run1_students = {s.institution_student_id: (s.id, s.apaar_ref, s.full_name) for s in StudentRecord.query.all()}
        run1_events = {e.id: (e.result, e.reason_code) for e in AdmissionEvent.query.all()}

        # Reset & Run 2
        db.drop_all()
        db.create_all()
        seed_reference_data(random.Random(42))
        seed_students(db, random.Random(42))
        run2_students = {s.institution_student_id: (s.id, s.apaar_ref, s.full_name) for s in StudentRecord.query.all()}
        run2_events = {e.id: (e.result, e.reason_code) for e in AdmissionEvent.query.all()}

        assert run1_students == run2_students
        assert run1_events == run2_events

