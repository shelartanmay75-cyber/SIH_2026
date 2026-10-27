"""
APAAR Nexus Synthetic Reference Data Seeding Script (Task A3).
Smart India Hackathon Problem Statement PS 26263.

Initializes deterministic baseline reference data:
- Exactly 3 AICTE institutions: VESIT, COEP, VJTI
- Core degree programmes & multi-year intake capacities with category rules
- System policies (risk weights, APAAR regex format check, thresholds)
- Exactly 4 official demo user accounts
- Ed25519 development public signing key

Usage:
    python seed.py          # Seeds reference data if database is empty
    python seed.py --reset  # Drops, recreates, and seeds fresh database
"""
import os
import sys
import argparse
import random
import unicodedata
import uuid
from datetime import date, datetime
from typing import Dict, Any

from app import create_app
from extensions import db
from models import (
    init_db,
    Institution,
    Programme,
    IntakeCapacity,
    SystemPolicy,
    User,
    SigningKey,
    StudentRecord,
    AdmissionEvent,
    Role,
    Classification,
    AdmissionCategory,
    EnrolmentStatus,
    ValidationStatus,
    AdmissionEventResult,
)
from services.key_store import ensure_development_key, DEFAULT_KEY_ID

# Fixed namespace for deterministic UUIDs across re-seeds
SEED_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


def stable_uuid(entity: str, identifier: str) -> str:
    """Generate a stable, deterministic UUID5 string based on entity name and business key."""
    return str(uuid.uuid5(SEED_NAMESPACE, f"{entity}:{identifier}"))


def check_production_safety(is_reset: bool) -> None:
    """Refuse unsafe operations in production configuration."""
    flask_env = os.getenv("FLASK_ENV", "development").lower()
    allow_reset = os.getenv("ALLOW_DEMO_RESET", "true").lower() in ("true", "1", "yes")

    if flask_env == "production":
        if is_reset and not allow_reset:
            print("CRITICAL ERROR: Database reset is forbidden in production unless ALLOW_DEMO_RESET=true.")
            sys.exit(1)

        demo_password = os.getenv("DEMO_PASSWORD", "Demo@12345")
        if demo_password == "Demo@12345":
            print("CRITICAL ERROR: Production environment refuses to seed with default DEMO_PASSWORD.")
            sys.exit(1)


def seed_reference_data(rng: random.Random) -> Dict[str, int]:
    """Seed institutions, programmes, capacities, policies, demo users, and keys."""
    counts = {}

    # 1. Institutions (Exactly three, synthetic per AGENTS.md §5.2 and §5.11)
    institutions_data = [
        {
            "code": "VESIT",
            "name": "Vivekanand Education Society Institute of Technology",
            "city": "Mumbai",
            "state": "Maharashtra",
        },
        {
            "code": "COEP",
            "name": "College of Engineering Pune",
            "city": "Pune",
            "state": "Maharashtra",
        },
        {
            "code": "VJTI",
            "name": "Veermata Jijabai Technological Institute",
            "city": "Mumbai",
            "state": "Maharashtra",
        },
    ]

    institutions = {}
    for data in institutions_data:
        inst_id = stable_uuid("institution", data["code"])
        inst = Institution(
            id=inst_id,
            code=data["code"],
            name=data["name"],
            city=data["city"],
            state=data["state"],
            is_synthetic=True,
            status="ACTIVE",
        )
        db.session.add(inst)
        institutions[data["code"]] = inst

    db.session.flush()
    counts["institutions"] = len(institutions)

    # 2. Programmes (Spread across institutions)
    programmes_catalog = [
        # VESIT
        ("VESIT", "BTECH-IT", "B.Tech Information Technology", "UG"),
        ("VESIT", "BTECH-CE", "B.Tech Computer Engineering", "UG"),
        # COEP
        ("COEP", "BTECH-CE", "B.Tech Computer Engineering", "UG"),
        ("COEP", "DIP-MECH", "Diploma Mechanical Engineering", "DIPLOMA"),
        # VJTI
        ("VJTI", "BTECH-IT", "B.Tech Information Technology", "UG"),
        ("VJTI", "BTECH-EXTC", "B.Tech Electronics & Telecommunication", "UG"),
    ]

    programmes = {}
    for inst_code, code, name, level in programmes_catalog:
        inst = institutions[inst_code]
        prog_id = stable_uuid("programme", f"{inst_code}:{code}")
        prog = Programme(
            id=prog_id,
            institution_id=inst.id,
            code=code,
            name=name,
            level=level,
        )
        db.session.add(prog)
        programmes[(inst_code, code)] = prog

    db.session.flush()
    counts["programmes"] = len(programmes)

    # 3. Intake Capacities (Academic years 2024-25, 2025-26, 2026-27)
    # Default category rules: regular seats for domestic categories; supernumerary for NRI/OCI/FN
    category_rules = {
        "REGULAR": ["OPEN", "OBC", "SC", "ST", "EWS"],
        "SUPERNUMERARY": ["SUPERNUMERARY", "NRI", "OCI", "FN"],
        "quota_breakdown": {
            "REGULAR": {
                "OPEN": 90,
                "OBC": 50,
                "SC": 24,
                "ST": 16,
            },
            "SUPERNUMERARY": {
                "NRI": 6,
                "OCI": 5,
                "FN": 4,
            }
        }
    }

    academic_years = ["2024-25", "2025-26", "2026-27"]
    capacity_count = 0

    for (inst_code, prog_code), prog in programmes.items():
        for year in academic_years:
            cap_id = stable_uuid("capacity", f"{inst_code}:{prog_code}:{year}")

            # Capacity per programme: 180 Regular + 15 Supernumerary = 195 Total Intake Capacity
            # Per college (2 programmes): 180 * 2 = 360 Regular, 15 * 2 = 30 Supernumerary = 390 Total
            reg_intake = 180
            sup_intake = 15

            cap = IntakeCapacity(
                id=cap_id,
                institution_id=prog.institution_id,
                programme_id=prog.id,
                academic_year=year,
                approved_regular_intake=reg_intake,
                supernumerary_capacity=sup_intake,
                category_rules=category_rules,
            )
            db.session.add(cap)
            capacity_count += 1

    db.session.flush()
    counts["intake_capacities"] = capacity_count

    # 4. System Policies (AGENTS.md §5.4 and §5.5)
    policies_data = [
        (
            "apaar_regex",
            r"^\d{12}$",
            "12-digit format check for synthetic APAAR IDs (not connected to live UIDAI/DigiLocker)",
        ),
        (
            "risk_weights",
            {
                "DUPLICATE_APAAR": 40,
                "SIMULTANEOUS_ENROLMENT": 35,
                "EXCESS_INTAKE_VIOLATION": 30,
                "DUPLICATE_INSTITUTE_ID": 25,
                "MISSING_OR_MALFORMED_APAAR": 20,
                "SIMILAR_NAME_CORROBORATED": 15,
                "CONFLICTING_ACADEMIC_DATA": 15,
                "SHARED_CONTACT": 10,
                "SHARED_FEE_PAYER": 10,
            },
            "Additive rule weights for explainable risk engine (capped at 100)",
        ),
        (
            "capacity_warning_thresholds",
            [80, 90, 100],
            "Dynamic Seat Ledger percentage thresholds triggering early warnings and strict blocking",
        ),
        (
            "risk_bands",
            {
                "LOW": [0, 29],
                "MEDIUM": [30, 59],
                "HIGH": [60, 79],
                "CRITICAL": [80, 100],
            },
            "Severity classification thresholds for risk investigation cases",
        ),
        ("max_upload_mb", 2, "Maximum allowed CSV file size in megabytes"),
        ("max_csv_rows", 5000, "Maximum allowed rows per CSV upload batch"),
        ("sharing_allowed_expiry_hours", [24, 72, 168], "Permitted durations for consent-based sharing tokens"),
        ("credential_default_validity_years", 4, "Standard validity period for issued student credentials"),
    ]

    for key, val, desc in policies_data:
        pol_id = stable_uuid("policy", key)
        policy = SystemPolicy(
            id=pol_id,
            key=key,
            value=val,
            description=desc,
            updated_by="SYSTEM",
        )
        db.session.add(policy)

    db.session.flush()
    counts["system_policies"] = len(policies_data)

    # 5. Users (Exactly four demo accounts per AGENTS.md §5.2 and §5.11)
    demo_password = os.getenv("DEMO_PASSWORD", "Demo@12345")
    users_data = [
        ("AICTE_Admin", "AICTE Super Administrator", Role.AICTE_ADMIN.value, None),
        ("VESIT", "VESIT Institutional Administrator", Role.INSTITUTION_ADMIN.value, institutions["VESIT"].id),
        ("COEP", "COEP Institutional Administrator", Role.INSTITUTION_ADMIN.value, institutions["COEP"].id),
        ("VJTI", "VJTI Institutional Administrator", Role.INSTITUTION_ADMIN.value, institutions["VJTI"].id),
    ]

    for username, full_name, role, inst_id in users_data:
        u_id = stable_uuid("user", username)
        user = User(
            id=u_id,
            username=username,
            full_name=full_name,
            role=role,
            institution_id=inst_id,
            is_active=True,
        )
        user.set_password(demo_password)
        db.session.add(user)

    db.session.flush()
    counts["users"] = len(users_data)

    # 6. Signing Keys (Ed25519 development public key in DB; private key in instance/keys)
    key_id, pub_pem = ensure_development_key(DEFAULT_KEY_ID)
    key_record_id = stable_uuid("signing_key", key_id)

    signing_key = SigningKey(
        id=key_record_id,
        key_id=key_id,
        public_key_pem=pub_pem,
        algorithm="Ed25519",
        is_active=True,
        is_development_key=True,
    )
    db.session.add(signing_key)
    db.session.flush()
    counts["signing_keys"] = 1

    db.session.commit()
    return counts


def normalize_name(value: str | None) -> str:
    """Normalize Unicode, lower-case, and strip non-alphanumeric characters."""
    text = unicodedata.normalize("NFKD", value or "").casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join("".join(char if char.isalnum() else " " for char in text).split())


def compute_seed_validation_status(apaar_ref: str | None) -> tuple[str, str | None]:
    """
    Central validation status evaluator for seed student records.
    Precursor to validation_service in A6.
    Returns (validation_status, validation_notes).
    """
    if not apaar_ref:
        return ValidationStatus.INVALID.value, "Missing APAAR reference"
    clean = str(apaar_ref).strip()
    if not (len(clean) == 12 and clean.isdigit()):
        return ValidationStatus.INVALID.value, f"Malformed APAAR reference: '{clean}' (must be exactly 12 digits)"
    return ValidationStatus.VALID.value, None


SYNTHETIC_FIRST_NAMES = [
    "Aarav", "Aditi", "Aditya", "Akash", "Ananya", "Aniket", "Anushka", "Arjun",
    "Deepak", "Devansh", "Diya", "Gaurav", "Isha", "Ishaan", "Kavya", "Manish",
    "Neha", "Nikhil", "Pooja", "Pranav", "Priya", "Rahul", "Riya", "Rohan",
    "Sakshi", "Sameer", "Siddharth", "Sneha", "Tanvi", "Utkarsh", "Varun",
    "Vedant", "Vikram", "Yash", "Zoya", "Aayush", "Bhavna", "Chetan", "Divya",
    "Esha", "Harsh", "Jay", "Kunal", "Meera", "Omkar", "Parth", "Ritu",
    "Saurabh", "Tarun", "Vidya", "Karan", "Rohit", "Shreya", "Amit", "Mayur",
    "Pooja", "Suresh", "Manoj", "Kiran", "Sunil", "Rajesh", "Anita", "Swati",
]

SYNTHETIC_LAST_NAMES = [
    "Sharma", "Verma", "Patil", "Deshmukh", "Kulkarni", "Joshi", "Shinde", "Pawar",
    "Chavan", "More", "Gaikwad", "Jadhav", "Bhosale", "Kadam", "Sawant", "Bane",
    "Tambe", "Mane", "Rane", "Salunkhe", "Thakur", "Mehta", "Shah", "Gupta",
    "Agarwal", "Singh", "Mishra", "Pandey", "Trivedi", "Bhat", "Kapse", "Nadkarni",
]

REGULAR_CATEGORIES = [
    AdmissionCategory.OPEN.value,
    AdmissionCategory.OBC.value,
    AdmissionCategory.SC.value,
    AdmissionCategory.ST.value,
    AdmissionCategory.EWS.value,
]


def seed_students(database, rng: random.Random) -> int:
    """
    Seed synthetic student records and demo admission events (Task A4).
    Guarantees 100% determinism via stable UUIDs and fixed pseudo-random generators.
    """
    session = getattr(database, "session", database)

    institutions = {inst.code: inst for inst in Institution.query.all()}
    programmes = {(prog.institution.code, prog.code): prog for prog in Programme.query.all()}

    if not institutions or not programmes:
        raise RuntimeError("Reference data must be seeded before running seed_students.")

    students_to_add = []
    events_to_add = []

    apaar_counter = 100
    contact_counter = 100
    fee_counter = 100
    name_idx = 0

    def next_name() -> tuple[str, str]:
        nonlocal name_idx
        first = SYNTHETIC_FIRST_NAMES[name_idx % len(SYNTHETIC_FIRST_NAMES)]
        last = SYNTHETIC_LAST_NAMES[(name_idx // len(SYNTHETIC_FIRST_NAMES)) % len(SYNTHETIC_LAST_NAMES)]
        name_idx += 1
        return f"{first} {last}", normalize_name(f"{first} {last}")

    def create_student(
        inst_code: str,
        prog_code: str,
        academic_year: str,
        roll_no: int,
        full_name: str,
        apaar_ref: str | None,
        admission_category: str,
        classification: str,
        admission_date: date,
        enrolment_status: str = EnrolmentStatus.ACTIVE.value,
        contact_ref: str | None = None,
        fee_payer_ref: str | None = None,
        is_nri: bool = False,
        is_oci: bool = False,
        is_foreign_national: bool = False,
    ) -> StudentRecord:
        nonlocal apaar_counter, contact_counter, fee_counter
        inst = institutions[inst_code]
        prog = programmes[(inst_code, prog_code)]
        short_year = academic_year[-2:]
        inst_student_id = f"{inst_code}-{short_year}-{prog_code}-{roll_no:04d}"

        val_status, val_notes = compute_seed_validation_status(apaar_ref)

        if contact_ref is None:
            contact_ref = f"SYN-C-{contact_counter:04d}"
            contact_counter += 1
        if fee_payer_ref is None:
            fee_payer_ref = f"SYN-F-{fee_counter:04d}"
            fee_counter += 1

        student_id = stable_uuid("student", f"{inst_code}:{prog_code}:{academic_year}:{inst_student_id}")

        student = StudentRecord(
            id=student_id,
            institution_id=inst.id,
            programme_id=prog.id,
            institution_student_id=inst_student_id,
            apaar_ref=apaar_ref,
            full_name=full_name,
            normalized_name=normalize_name(full_name),
            academic_year=academic_year,
            admission_date=admission_date,
            admission_category=admission_category,
            classification=classification,
            is_nri=is_nri,
            is_oci=is_oci,
            is_foreign_national=is_foreign_national,
            enrolment_status=enrolment_status,
            validation_status=val_status,
            validation_notes=val_notes,
            contact_ref=contact_ref,
            fee_payer_ref=fee_payer_ref,
            is_synthetic=True,
        )

        adm_event = AdmissionEvent(
            id=stable_uuid("admission_event", f"{student_id}:accepted"),
            institution_id=inst.id,
            programme_id=prog.id,
            academic_year=academic_year,
            student_id=student.id,
            category=admission_category,
            classification=classification,
            result=AdmissionEventResult.ACCEPTED.value,
            reason_code="APPROVED",
            message=f"Admitted under {classification} intake",
            actor_id="SYSTEM_SEED",
            created_at=datetime.combine(admission_date, datetime.min.time()),
        )

        students_to_add.append(student)
        events_to_add.append(adm_event)
        return student

    def get_category_for_roll(roll: int) -> str:
        """
        Quota breakdown (50% filled for 180 approved regular seats):
        - Rolls 1 to 45  : OPEN (45 / 90 capacity = 50.0%)
        - Rolls 46 to 70 : OBC (25 / 50 capacity = 50.0%)
        - Rolls 71 to 82 : SC (12 / 24 capacity = 50.0%)
        - Rolls 83 to 90 : ST (8 / 16 capacity = 50.0%)
        """
        if roll <= 45:
            return AdmissionCategory.OPEN.value
        elif roll <= 70:
            return AdmissionCategory.OBC.value
        elif roll <= 82:
            return AdmissionCategory.SC.value
        else:
            return AdmissionCategory.ST.value

    # ==========================================================================
    # 1. SCENARIO 1: VESIT / B.Tech IT / 2026-27 (90 Regular + 8 Supernumerary)
    # Approved Regular: 180, Admitted: 90 (50.0% Half-Filled)
    # Approved Supernumerary: 15, Admitted: 8 (53.3%)
    # Total Capacity: 195, Total Admitted: 98 (50.3%)
    # ==========================================================================
    for roll in range(1, 91):
        cat = get_category_for_roll(roll)
        if roll == 1:
            # Scenario 7: Credential demo student (Roll 1)
            create_student(
                inst_code="VESIT",
                prog_code="BTECH-IT",
                academic_year="2026-27",
                roll_no=1,
                full_name="Aarav Sharma",
                apaar_ref="900000000001",
                admission_category=AdmissionCategory.OPEN.value,
                classification=Classification.REGULAR.value,
                admission_date=date(2026, 7, 1),
                contact_ref="SYN-C-0001",
                fee_payer_ref="SYN-F-0001",
            )
        elif roll == 10:
            # Scenario 4(a): Cross-institution simultaneous enrolment (Roll 10)
            create_student(
                inst_code="VESIT",
                prog_code="BTECH-IT",
                academic_year="2026-27",
                roll_no=10,
                full_name="Devansh Verma",
                apaar_ref="900000000010",
                admission_category=AdmissionCategory.OPEN.value,
                classification=Classification.REGULAR.value,
                admission_date=date(2026, 7, 5),
                contact_ref="SYN-C-0010",
                fee_payer_ref="SYN-F-0010",
            )
        elif roll == 15:
            # Scenario 4(c): Corroborated fuzzy name variation (Rolls 15 & 16)
            create_student(
                inst_code="VESIT",
                prog_code="BTECH-IT",
                academic_year="2026-27",
                roll_no=15,
                full_name="Rahul Sharma",
                apaar_ref="900000000031",
                admission_category=AdmissionCategory.OPEN.value,
                classification=Classification.REGULAR.value,
                admission_date=date(2026, 7, 15),
            )
        elif roll == 16:
            create_student(
                inst_code="VESIT",
                prog_code="BTECH-IT",
                academic_year="2026-27",
                roll_no=16,
                full_name="Rahul Sarma",
                apaar_ref="900000000032",
                admission_category=AdmissionCategory.OPEN.value,
                classification=Classification.REGULAR.value,
                admission_date=date(2026, 7, 15),
            )
        elif roll == 21:
            # Scenario 5: Legitimate shared contact - Sibling 1 (Roll 21)
            create_student(
                inst_code="VESIT",
                prog_code="BTECH-IT",
                academic_year="2026-27",
                roll_no=21,
                full_name="Aarav Patil",
                apaar_ref="900000000041",
                admission_category=AdmissionCategory.OPEN.value,
                classification=Classification.REGULAR.value,
                admission_date=date(2026, 7, 12),
                contact_ref="SYN-C-0050",
            )
        else:
            name, _ = next_name()
            day = 1 + ((roll * 3) % 25)
            apaar = f"9{apaar_counter:011d}"
            apaar_counter += 1
            create_student(
                inst_code="VESIT",
                prog_code="BTECH-IT",
                academic_year="2026-27",
                roll_no=roll,
                full_name=name,
                apaar_ref=apaar,
                admission_category=cat,
                classification=Classification.REGULAR.value,
                admission_date=date(2026, 7, day),
            )

    # Supernumerary Admissions for VESIT B.Tech IT (Branch 1: 3 NRI, 3 OCI, 2 FN = 8)
    supernumerary_vesit_it = [
        (91, "Rohan Mehta", "900000000091", AdmissionCategory.NRI.value, True, False, False),
        (92, "Zoya Agarwal", "900000000092", AdmissionCategory.NRI.value, True, False, False),
        (93, "Kabir Mehra", "900000000093", AdmissionCategory.NRI.value, True, False, False),
        (94, "Diya Patel", "900000000094", AdmissionCategory.OCI.value, False, True, False),
        (95, "Dev Kothari", "900000000095", AdmissionCategory.OCI.value, False, True, False),
        (96, "Tara Singhania", "900000000096", AdmissionCategory.OCI.value, False, True, False),
        (97, "Alexander Wright", "900000000097", AdmissionCategory.FN.value, False, False, True),
        (98, "Maya Chen", "900000000098", AdmissionCategory.FN.value, False, False, True),
    ]
    for roll, name, apaar, cat, is_nri, is_oci, is_fn in supernumerary_vesit_it:
        create_student(
            inst_code="VESIT",
            prog_code="BTECH-IT",
            academic_year="2026-27",
            roll_no=roll,
            full_name=name,
            apaar_ref=apaar,
            admission_category=cat,
            classification=Classification.SUPERNUMERARY.value,
            admission_date=date(2026, 7, 20),
            is_nri=is_nri,
            is_oci=is_oci,
            is_foreign_national=is_fn,
        )

    # ==========================================================================
    # 2. VESIT / B.Tech CE / 2026-27 (90 Regular + 7 Supernumerary = 97 Half-Filled)
    # Combined VESIT: 180 Regular (50.0%) + 15 Supernumerary (50.0%) = 195 (50.0%)
    # ==========================================================================
    for roll in range(1, 91):
        cat = get_category_for_roll(roll)
        if roll == 21:
            # Scenario 5: Sibling 2 sharing contact SYN-C-0050 (Roll 21)
            create_student(
                inst_code="VESIT",
                prog_code="BTECH-CE",
                academic_year="2026-27",
                roll_no=21,
                full_name="Ananya Patil",
                apaar_ref="900000000042",
                admission_category=AdmissionCategory.OPEN.value,
                classification=Classification.REGULAR.value,
                admission_date=date(2026, 7, 12),
                contact_ref="SYN-C-0050",
            )
        else:
            name, _ = next_name()
            day = 1 + ((roll * 2) % 25)
            apaar = f"9{apaar_counter:011d}"
            apaar_counter += 1
            create_student(
                inst_code="VESIT",
                prog_code="BTECH-CE",
                academic_year="2026-27",
                roll_no=roll,
                full_name=name,
                apaar_ref=apaar,
                admission_category=cat,
                classification=Classification.REGULAR.value,
                admission_date=date(2026, 7, day),
            )

    supernumerary_vesit_ce = [
        (91, "Arjun Trivedi", "900000000101", AdmissionCategory.NRI.value, True, False, False),
        (92, "Maya Hansen", "900000000102", AdmissionCategory.NRI.value, True, False, False),
        (93, "Aarush Bhatia", "900000000103", AdmissionCategory.NRI.value, True, False, False),
        (94, "Chloe Martin", "900000000104", AdmissionCategory.OCI.value, False, True, False),
        (95, "Lucas Dubois", "900000000105", AdmissionCategory.OCI.value, False, True, False),
        (96, "Kenji Sato", "900000000106", AdmissionCategory.FN.value, False, False, True),
        (97, "Fatima Al-Mansoor", "900000000107", AdmissionCategory.FN.value, False, False, True),
    ]
    for roll, name, apaar, cat, is_nri, is_oci, is_fn in supernumerary_vesit_ce:
        create_student(
            inst_code="VESIT",
            prog_code="BTECH-CE",
            academic_year="2026-27",
            roll_no=roll,
            full_name=name,
            apaar_ref=apaar,
            admission_category=cat,
            classification=Classification.SUPERNUMERARY.value,
            admission_date=date(2026, 7, 20),
            is_nri=is_nri,
            is_oci=is_oci,
            is_foreign_national=is_fn,
        )

    # ==========================================================================
    # 3. COEP / B.Tech CE / 2026-27 (90 Regular + 8 Supernumerary = 98 Half-Filled)
    # ==========================================================================
    for roll in range(1, 91):
        cat = get_category_for_roll(roll)
        if roll == 10:
            # Scenario 4(a): Devansh Verma simultaneous active enrolment at COEP (Roll 10)
            create_student(
                inst_code="COEP",
                prog_code="BTECH-CE",
                academic_year="2026-27",
                roll_no=10,
                full_name="Devansh Verma",
                apaar_ref="900000000010",
                admission_category=AdmissionCategory.OPEN.value,
                classification=Classification.REGULAR.value,
                admission_date=date(2026, 7, 5),
                contact_ref="SYN-C-0010",
                fee_payer_ref="SYN-F-0010",
            )
        elif roll == 25:
            # Scenario 5: Sponsor sharing fee-payer SYN-F-0099 - Student 1 (Roll 25)
            create_student(
                inst_code="COEP",
                prog_code="BTECH-CE",
                academic_year="2026-27",
                roll_no=25,
                full_name="Vikram Deshmukh",
                apaar_ref="900000000051",
                admission_category=AdmissionCategory.OPEN.value,
                classification=Classification.REGULAR.value,
                admission_date=date(2026, 7, 18),
                fee_payer_ref="SYN-F-0099",
            )
        else:
            name, _ = next_name()
            day = 1 + ((roll * 4) % 25)
            apaar = f"9{apaar_counter:011d}"
            apaar_counter += 1
            create_student(
                inst_code="COEP",
                prog_code="BTECH-CE",
                academic_year="2026-27",
                roll_no=roll,
                full_name=name,
                apaar_ref=apaar,
                admission_category=cat,
                classification=Classification.REGULAR.value,
                admission_date=date(2026, 7, day),
            )

    supernumerary_coep_ce = [
        (91, "Chetan Mehta", "900000000111", AdmissionCategory.NRI.value, True, False, False),
        (92, "Priya Shah", "900000000112", AdmissionCategory.NRI.value, True, False, False),
        (93, "Rishi Kapoor", "900000000113", AdmissionCategory.NRI.value, True, False, False),
        (94, "Sonia Gandhi", "900000000114", AdmissionCategory.OCI.value, False, True, False),
        (95, "Neil Armstrong", "900000000115", AdmissionCategory.OCI.value, False, True, False),
        (96, "Elena Rostova", "900000000116", AdmissionCategory.OCI.value, False, True, False),
        (97, "Hans Mueller", "900000000117", AdmissionCategory.FN.value, False, False, True),
        (98, "Sarah Connor", "900000000118", AdmissionCategory.FN.value, False, False, True),
    ]
    for roll, name, apaar, cat, is_nri, is_oci, is_fn in supernumerary_coep_ce:
        create_student(
            inst_code="COEP",
            prog_code="BTECH-CE",
            academic_year="2026-27",
            roll_no=roll,
            full_name=name,
            apaar_ref=apaar,
            admission_category=cat,
            classification=Classification.SUPERNUMERARY.value,
            admission_date=date(2026, 7, 20),
            is_nri=is_nri,
            is_oci=is_oci,
            is_foreign_national=is_fn,
        )

    # ==========================================================================
    # 4. COEP / Diploma Mech / 2026-27 (90 Regular + 7 Supernumerary = 97 Half-Filled)
    # Combined COEP: 180 Regular (50.0%) + 15 Supernumerary (50.0%) = 195 (50.0%)
    # ==========================================================================
    for roll in range(1, 91):
        cat = get_category_for_roll(roll)
        if roll == 25:
            # Scenario 5: Sponsor sharing fee-payer SYN-F-0099 - Student 2 (Roll 25)
            create_student(
                inst_code="COEP",
                prog_code="DIP-MECH",
                academic_year="2026-27",
                roll_no=25,
                full_name="Pooja Kulkarni",
                apaar_ref="900000000052",
                admission_category=AdmissionCategory.OPEN.value,
                classification=Classification.REGULAR.value,
                admission_date=date(2026, 7, 18),
                fee_payer_ref="SYN-F-0099",
            )
        else:
            name, _ = next_name()
            day = 1 + ((roll * 3) % 25)
            apaar = f"9{apaar_counter:011d}"
            apaar_counter += 1
            create_student(
                inst_code="COEP",
                prog_code="DIP-MECH",
                academic_year="2026-27",
                roll_no=roll,
                full_name=name,
                apaar_ref=apaar,
                admission_category=cat,
                classification=Classification.REGULAR.value,
                admission_date=date(2026, 7, day),
            )

    supernumerary_coep_mech = [
        (91, "Amit Mishra", "900000000121", AdmissionCategory.NRI.value, True, False, False),
        (92, "Neha Pandey", "900000000122", AdmissionCategory.NRI.value, True, False, False),
        (93, "Vivek Oberoi", "900000000123", AdmissionCategory.NRI.value, True, False, False),
        (94, "Olivia Smith", "900000000124", AdmissionCategory.OCI.value, False, True, False),
        (95, "Liam Johnson", "900000000125", AdmissionCategory.OCI.value, False, True, False),
        (96, "Tariq Aziz", "900000000126", AdmissionCategory.FN.value, False, False, True),
        (97, "Yuki Tanaka", "900000000127", AdmissionCategory.FN.value, False, False, True),
    ]
    for roll, name, apaar, cat, is_nri, is_oci, is_fn in supernumerary_coep_mech:
        create_student(
            inst_code="COEP",
            prog_code="DIP-MECH",
            academic_year="2026-27",
            roll_no=roll,
            full_name=name,
            apaar_ref=apaar,
            admission_category=cat,
            classification=Classification.SUPERNUMERARY.value,
            admission_date=date(2026, 7, 20),
            is_nri=is_nri,
            is_oci=is_oci,
            is_foreign_national=is_fn,
        )

    # ==========================================================================
    # 5. VJTI / B.Tech IT / 2026-27 (90 Regular + 8 Supernumerary = 98 Half-Filled)
    # ==========================================================================
    for roll in range(1, 91):
        cat = get_category_for_roll(roll)
        if roll == 20:
            # Scenario 4(b): Same APAAR within institution (Roll 20)
            create_student(
                inst_code="VJTI",
                prog_code="BTECH-IT",
                academic_year="2026-27",
                roll_no=20,
                full_name="Kunal Joshi",
                apaar_ref="900000000020",
                admission_category=AdmissionCategory.OPEN.value,
                classification=Classification.REGULAR.value,
                admission_date=date(2026, 7, 10),
                contact_ref="SYN-C-0020",
            )
        elif roll == 26:
            # Scenario 6: Malformed / Missing APAAR format violations (Rolls 26, 27, 28, 29)
            create_student(
                inst_code="VJTI",
                prog_code="BTECH-IT",
                academic_year="2026-27",
                roll_no=26,
                full_name="Sameer Kulkarni",
                apaar_ref="9123456",  # Too short (7 digits)
                admission_category=AdmissionCategory.OPEN.value,
                classification=Classification.REGULAR.value,
                admission_date=date(2026, 7, 14),
            )
        elif roll == 27:
            create_student(
                inst_code="VJTI",
                prog_code="BTECH-IT",
                academic_year="2026-27",
                roll_no=27,
                full_name="Varun Mehta",
                apaar_ref="90000000001X",  # Non-digit character
                admission_category=AdmissionCategory.OPEN.value,
                classification=Classification.REGULAR.value,
                admission_date=date(2026, 7, 14),
            )
        elif roll == 28:
            create_student(
                inst_code="VJTI",
                prog_code="BTECH-IT",
                academic_year="2026-27",
                roll_no=28,
                full_name="Tanvi Shah",
                apaar_ref="",  # Empty string
                admission_category=AdmissionCategory.OPEN.value,
                classification=Classification.REGULAR.value,
                admission_date=date(2026, 7, 14),
            )
        elif roll == 29:
            create_student(
                inst_code="VJTI",
                prog_code="BTECH-IT",
                academic_year="2026-27",
                roll_no=29,
                full_name="Bhavna Shinde",
                apaar_ref=None,  # Null
                admission_category=AdmissionCategory.OPEN.value,
                classification=Classification.REGULAR.value,
                admission_date=date(2026, 7, 14),
            )
        else:
            name, _ = next_name()
            day = 1 + ((roll * 5) % 25)
            apaar = f"9{apaar_counter:011d}"
            apaar_counter += 1
            create_student(
                inst_code="VJTI",
                prog_code="BTECH-IT",
                academic_year="2026-27",
                roll_no=roll,
                full_name=name,
                apaar_ref=apaar,
                admission_category=cat,
                classification=Classification.REGULAR.value,
                admission_date=date(2026, 7, day),
            )

    supernumerary_vjti_it = [
        (91, "Nikhil Trivedi", "900000000131", AdmissionCategory.NRI.value, True, False, False),
        (92, "Sakshi Nadkarni", "900000000132", AdmissionCategory.NRI.value, True, False, False),
        (93, "Armaan Malik", "900000000133", AdmissionCategory.NRI.value, True, False, False),
        (94, "Emma Watson", "900000000134", AdmissionCategory.OCI.value, False, True, False),
        (95, "Daniel Craig", "900000000135", AdmissionCategory.OCI.value, False, True, False),
        (96, "Natasha Romanoff", "900000000136", AdmissionCategory.OCI.value, False, True, False),
        (97, "Carlos Silva", "900000000137", AdmissionCategory.FN.value, False, False, True),
        (98, "Amina Yusuf", "900000000138", AdmissionCategory.FN.value, False, False, True),
    ]
    for roll, name, apaar, cat, is_nri, is_oci, is_fn in supernumerary_vjti_it:
        create_student(
            inst_code="VJTI",
            prog_code="BTECH-IT",
            academic_year="2026-27",
            roll_no=roll,
            full_name=name,
            apaar_ref=apaar,
            admission_category=cat,
            classification=Classification.SUPERNUMERARY.value,
            admission_date=date(2026, 7, 20),
            is_nri=is_nri,
            is_oci=is_oci,
            is_foreign_national=is_fn,
        )

    # ==========================================================================
    # 6. VJTI / B.Tech EXTC / 2026-27 (90 Regular + 7 Supernumerary = 97 Half-Filled)
    # Combined VJTI: 180 Regular (50.0%) + 15 Supernumerary (50.0%) = 195 (50.0%)
    # ==========================================================================
    for roll in range(1, 91):
        cat = get_category_for_roll(roll)
        if roll == 20:
            # Scenario 4(b): Same APAAR within institution - second record (Roll 20)
            create_student(
                inst_code="VJTI",
                prog_code="BTECH-EXTC",
                academic_year="2026-27",
                roll_no=20,
                full_name="Kunal Joshi",
                apaar_ref="900000000020",
                admission_category=AdmissionCategory.OPEN.value,
                classification=Classification.REGULAR.value,
                admission_date=date(2026, 7, 10),
                contact_ref="SYN-C-0020",
            )
        else:
            name, _ = next_name()
            day = 1 + ((roll * 2) % 25)
            apaar = f"9{apaar_counter:011d}"
            apaar_counter += 1
            create_student(
                inst_code="VJTI",
                prog_code="BTECH-EXTC",
                academic_year="2026-27",
                roll_no=roll,
                full_name=name,
                apaar_ref=apaar,
                admission_category=cat,
                classification=Classification.REGULAR.value,
                admission_date=date(2026, 7, day),
            )

    supernumerary_vjti_extc = [
        (91, "Pranav Kapse", "900000000141", AdmissionCategory.NRI.value, True, False, False),
        (92, "Divya Bhosale", "900000000142", AdmissionCategory.NRI.value, True, False, False),
        (93, "Sameera Reddy", "900000000143", AdmissionCategory.NRI.value, True, False, False),
        (94, "Ethan Hunt", "900000000144", AdmissionCategory.OCI.value, False, True, False),
        (95, "Sophie Marceau", "900000000145", AdmissionCategory.OCI.value, False, True, False),
        (96, "Lars Mikkelsen", "900000000146", AdmissionCategory.FN.value, False, False, True),
        (97, "Zhang Wei", "900000000147", AdmissionCategory.FN.value, False, False, True),
    ]
    for roll, name, apaar, cat, is_nri, is_oci, is_fn in supernumerary_vjti_extc:
        create_student(
            inst_code="VJTI",
            prog_code="BTECH-EXTC",
            academic_year="2026-27",
            roll_no=roll,
            full_name=name,
            apaar_ref=apaar,
            admission_category=cat,
            classification=Classification.SUPERNUMERARY.value,
            admission_date=date(2026, 7, 20),
            is_nri=is_nri,
            is_oci=is_oci,
            is_foreign_national=is_fn,
        )

    # ==========================================================================
    # 7. HISTORICAL RECORDS (2024-25 and 2025-26)
    # ==========================================================================
    all_prog_keys = [
        ("VESIT", "BTECH-IT"),
        ("VESIT", "BTECH-CE"),
        ("COEP", "BTECH-CE"),
        ("COEP", "DIP-MECH"),
        ("VJTI", "BTECH-IT"),
        ("VJTI", "BTECH-EXTC"),
    ]

    # Academic Year 2024-25: 12 students (Graduated / Active)
    for inst_code, prog_code in all_prog_keys:
        for roll in (1, 2):
            name, _ = next_name()
            apaar = f"9{apaar_counter:011d}"
            apaar_counter += 1
            status = EnrolmentStatus.GRADUATED.value if roll == 1 else EnrolmentStatus.ACTIVE.value
            create_student(
                inst_code=inst_code,
                prog_code=prog_code,
                academic_year="2024-25",
                roll_no=roll,
                full_name=name,
                apaar_ref=apaar,
                admission_category=AdmissionCategory.OPEN.value,
                classification=Classification.REGULAR.value,
                admission_date=date(2024, 7, 15),
                enrolment_status=status,
            )

    # Academic Year 2025-26: 12 students (Active / Withdrawn)
    for inst_code, prog_code in all_prog_keys:
        for roll in (1, 2):
            name, _ = next_name()
            apaar = f"9{apaar_counter:011d}"
            apaar_counter += 1
            status = EnrolmentStatus.WITHDRAWN.value if roll == 2 else EnrolmentStatus.ACTIVE.value
            create_student(
                inst_code=inst_code,
                prog_code=prog_code,
                academic_year="2025-26",
                roll_no=roll,
                full_name=name,
                apaar_ref=apaar,
                admission_category=AdmissionCategory.OPEN.value,
                classification=Classification.REGULAR.value,
                admission_date=date(2025, 7, 15),
                enrolment_status=status,
            )

    # ==========================================================================
    # 8. SCENARIO 3: Past Rejected Excess Admissions (AdmissionEvent records)
    # ==========================================================================
    coep = institutions["COEP"]
    coep_dip_mech = programmes[("COEP", "DIP-MECH")]
    vesit = institutions["VESIT"]
    vesit_btech_it = programmes[("VESIT", "BTECH-IT")]

    rejected_1 = AdmissionEvent(
        id=stable_uuid("admission_event", "rejected:coep:dip_mech:2026-27:excess"),
        institution_id=coep.id,
        programme_id=coep_dip_mech.id,
        academic_year="2026-27",
        student_id=None,
        category=AdmissionCategory.OPEN.value,
        classification=Classification.REGULAR.value,
        result=AdmissionEventResult.REJECTED.value,
        reason_code="CAPACITY_EXCEEDED",
        message="Admission attempt rejected: Approved regular intake capacity (180/180) reached for Diploma Mechanical Engineering",
        actor_id="COEP",
        created_at=datetime(2026, 7, 28, 14, 30),
    )
    rejected_2 = AdmissionEvent(
        id=stable_uuid("admission_event", "rejected:vesit:btech_it:2026-27:excess"),
        institution_id=vesit.id,
        programme_id=vesit_btech_it.id,
        academic_year="2026-27",
        student_id=None,
        category=AdmissionCategory.OPEN.value,
        classification=Classification.REGULAR.value,
        result=AdmissionEventResult.REJECTED.value,
        reason_code="CAPACITY_EXCEEDED",
        message="Admission attempt rejected: Dynamic Seat Ledger limit enforcement check failed",
        actor_id="VESIT",
        created_at=datetime(2026, 7, 29, 11, 15),
    )
    events_to_add.extend([rejected_1, rejected_2])

    session.add_all(students_to_add)
    session.flush()
    session.add_all(events_to_add)
    session.commit()

    return len(students_to_add)


def run_seeding(reset: bool = False, app: Any = None) -> None:
    """Main execution orchestrator for database seeding."""
    check_production_safety(reset)
    if app is None:
        app = create_app()

    with app.app_context():
        db.session.remove()
        if reset:
            print("Reset flag detected: Dropping all database tables...")
            db.drop_all()
            print("Recreating database schema...")
            db.create_all()
        else:
            init_db(app)
            if Institution.query.count() > 0:
                print("Database already contains records. Run with --reset to wipe and re-seed.")
                return

        rng = random.Random(42)
        print("Seeding reference data (Institutions, Programmes, Capacities, Policies, Users, Keys)...")
        counts = seed_reference_data(rng)

        # Hook for A4 student data generation
        print("Executing student scenarios hook (seed_students)...")
        student_count = seed_students(db, rng)
        counts["student_records"] = student_count
        counts["admission_events"] = AdmissionEvent.query.count()

        # Reconcile intake capacity counters (Task A9)
        from services.seat_ledger import recompute_counters
        recomputed = recompute_counters()
        print(f"Reconciled intake capacity counters for {recomputed} programmes.")

        # Hook for Member B (Prove & Detect scenarios)
        try:
            import seed_b
            if hasattr(seed_b, "seed_prove_and_detect"):
                print("Executing Member B hook (seed_b.seed_prove_and_detect)...")
                seed_b.seed_prove_and_detect(db)
                print("Member B seeding hook executed successfully.")
        except ImportError:
            print("Note: seed_b module not found (Member B will implement this in a future task).")
        except Exception as exc:
            print(f"Warning: Member B hook encountered an error: {exc}")

        # Print formatted summary table
        print("\n" + "=" * 60)
        print("APAARSetu Seed Complete — Entity Summary")
        print("=" * 60)
        for entity, count in sorted(counts.items()):
            print(f"  {entity.replace('_', ' ').capitalize():<28}: {count}")
        print("=" * 60)
        print("2026-27 Dynamic Seat Ledger Utilisation Summary (Half-Filled at ~50%):")
        print("  - Per programme  : 180 Regular + 15 Supernumerary (195 Total seats)")
        print("  - Per college    : 360 Regular + 30 Supernumerary (390 Total seats) across 2 programmes")
        print("  - Admitted       : 90 Regular + 8/7 Supernumerary per programme (50.0% utilised)")
        print("  - Regular Quotas : OPEN (45/90), OBC (25/50), SC (12/24), ST (8/16) — all 50.0% filled")
        print("  - Supernumerary  : NRI (6/12), OCI (5/10), FN (4/8) per college — all 50.0% filled")
        print("  - Total College  : 195 Admitted of 390 Approved Intake (50.0% utilised)")
        print("=" * 60)
        print("Pre-configured Demo Scenarios:")
        print("  [x] Half-Filled Intake: 180/360 Reg + 15/30 Sup (50.0%) across all 3 institutions")
        print("  [x] Identity conflicts: Devansh Verma (simultaneous enrolment), Kunal Joshi (duplicate APAAR)")
        print("  [x] Corroborated signals: Rahul Sharma / Rahul Sarma (fuzzy names)")
        print("  [x] Shared signals: Sibling contact SYN-C-0050 & Sponsor fee-payer SYN-F-0099")
        print("  [x] Format violations: 4 malformed/missing APAAR format violation records")
        print("  [x] Credential demo: Aarav Sharma (VESIT-26-BTECH-IT-0001)")
        print("=" * 60)
        print("Demo Accounts Available (Password: Demo@12345):")
        print("  - AICTE_Admin (AICTE Super Administrator)")
        print("  - VESIT       (Institution Administrator - VESIT)")
        print("  - COEP        (Institution Administrator - COEP)")
        print("  - VJTI        (Institution Administrator - VJTI)")
        print("=" * 60 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deterministic database seeder for APAAR Nexus.")
    parser.add_argument("--reset", action="store_true", help="Drop all tables and re-seed database.")
    args = parser.parse_args()

    run_seeding(reset=args.reset)

