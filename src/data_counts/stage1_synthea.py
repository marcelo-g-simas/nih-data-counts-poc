"""Stage 1: synthetic EHR generation + bulk-export endpoint. [Req 1a]

Primary path: shell out to the official MITRE Synthea Docker image
(`--use-docker`) to generate a FHIR R4 NDJSON cohort. That image is large and
a 1,000-patient run can take a long time to pull/run, so the default path
(no flag needed) is a deterministic, seeded, pure-Python FHIR R4 generator
that emits the same per-resource-type NDJSON shape Synthea's bulk-export
produces (Patient/Condition/Observation/Encounter/MedicationRequest.ndjson).
Either way, the output folder is what stage 2 treats as "the bulk-export
endpoint's payload" — Synthea's own docs note the NDJSON-folder and live
`$export` paths are interchangeable for a downstream consumer, so this POC
takes the simpler one by default and documents docker as a drop-in swap.
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from faker import Faker

RESOURCE_TYPES = ["Patient", "Condition", "Observation", "Encounter", "MedicationRequest"]

# A handful of real code systems/codes so downstream DQ/cohort logic has
# something real to filter on (E11.x = Type 2 diabetes, matches the example DSR).
CONDITION_CODES = [
    ("http://hl7.org/fhir/sid/icd-10-cm", "E11.9", "Type 2 diabetes mellitus without complications"),
    ("http://hl7.org/fhir/sid/icd-10-cm", "I10", "Essential (primary) hypertension"),
    ("http://hl7.org/fhir/sid/icd-10-cm", "J45.909", "Unspecified asthma, uncomplicated"),
    ("http://hl7.org/fhir/sid/icd-10-cm", "E78.5", "Hyperlipidemia, unspecified"),
]
OBSERVATION_CODES = [
    ("http://loinc.org", "4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood", "%"),
    ("http://loinc.org", "2339-0", "Glucose [Mass/volume] in Blood", "mg/dL"),
    ("http://loinc.org", "8480-6", "Systolic blood pressure", "mm[Hg]"),
    ("http://loinc.org", "8462-4", "Diastolic blood pressure", "mm[Hg]"),
]
MEDICATION_CODES = [
    ("http://www.nlm.nih.gov/research/umls/rxnorm", "860975", "metformin 500 MG Oral Tablet"),
    ("http://www.nlm.nih.gov/research/umls/rxnorm", "197361", "lisinopril 10 MG Oral Tablet"),
]


def _random_date(rng: random.Random, start: date, end: date) -> date:
    delta = (end - start).days
    return start + timedelta(days=rng.randint(0, max(delta, 1)))


def generate_synthetic_fallback(out_dir: Path, population: int, seed: int) -> dict[str, int]:
    """Deterministic pure-Python FHIR R4 cohort generator (no Java/Docker needed)."""
    rng = random.Random(seed)
    fake = Faker()
    Faker.seed(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    writers = {rt: (out_dir / f"{rt}.ndjson").open("w") for rt in RESOURCE_TYPES}
    counts = {rt: 0 for rt in RESOURCE_TYPES}

    try:
        for i in range(population):
            pid = f"synthetic-patient-{i:05d}"
            birth = _random_date(rng, date(1935, 1, 1), date(2015, 1, 1))
            gender = rng.choice(["male", "female"])
            patient = {
                "resourceType": "Patient",
                "id": pid,
                "identifier": [{"system": "urn:data-counts:mrn", "value": f"MRN{i:07d}"}],
                "name": [{"family": fake.last_name(), "given": [fake.first_name()]}],
                "gender": gender,
                "birthDate": birth.isoformat(),
                "address": [{"city": fake.city(), "state": fake.state_abbr(), "postalCode": fake.zipcode()}],
            }
            writers["Patient"].write(json.dumps(patient) + "\n")
            counts["Patient"] += 1

            for _ in range(rng.randint(0, 3)):
                system, code, display = rng.choice(CONDITION_CODES)
                onset = _random_date(rng, birth, date(2025, 12, 31))
                condition = {
                    "resourceType": "Condition",
                    "id": f"{pid}-cond-{rng.randint(0, 999999)}",
                    "subject": {"reference": f"Patient/{pid}"},
                    "code": {"coding": [{"system": system, "code": code, "display": display}]},
                    "onsetDateTime": onset.isoformat(),
                }
                writers["Condition"].write(json.dumps(condition) + "\n")
                counts["Condition"] += 1

            n_enc = rng.randint(1, 6)
            for _ in range(n_enc):
                start_dt = _random_date(rng, birth, date(2025, 12, 31))
                end_dt = start_dt + timedelta(days=rng.randint(0, 2))
                enc_id = f"{pid}-enc-{rng.randint(0, 999999)}"
                encounter = {
                    "resourceType": "Encounter",
                    "id": enc_id,
                    "subject": {"reference": f"Patient/{pid}"},
                    "period": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
                }
                writers["Encounter"].write(json.dumps(encounter) + "\n")
                counts["Encounter"] += 1

                for _ in range(rng.randint(0, 4)):
                    system, code, display, unit = rng.choice(OBSERVATION_CODES)
                    obs = {
                        "resourceType": "Observation",
                        "id": f"{enc_id}-obs-{rng.randint(0, 999999)}",
                        "subject": {"reference": f"Patient/{pid}"},
                        "encounter": {"reference": f"Encounter/{enc_id}"},
                        "effectiveDateTime": start_dt.isoformat(),
                        "code": {"coding": [{"system": system, "code": code, "display": display}]},
                        "valueQuantity": {"value": round(rng.uniform(60, 200), 1), "unit": unit},
                    }
                    writers["Observation"].write(json.dumps(obs) + "\n")
                    counts["Observation"] += 1

            for _ in range(rng.randint(0, 2)):
                system, code, display = rng.choice(MEDICATION_CODES)
                med = {
                    "resourceType": "MedicationRequest",
                    "id": f"{pid}-med-{rng.randint(0, 999999)}",
                    "subject": {"reference": f"Patient/{pid}"},
                    "medicationCodeableConcept": {"coding": [{"system": system, "code": code, "display": display}]},
                    "authoredOn": _random_date(rng, birth, date(2025, 12, 31)).isoformat(),
                }
                writers["MedicationRequest"].write(json.dumps(med) + "\n")
                counts["MedicationRequest"] += 1
    finally:
        for w in writers.values():
            w.close()
    return counts


def generate_with_docker(out_dir: Path, population: int) -> bool:
    """Try the real MITRE Synthea Docker image. Returns False if docker/image is unavailable."""
    if shutil.which("docker") is None:
        return False
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{out_dir.resolve()}:/output",
        "mitre/synthea",
        "-p", str(population),
        "--exporter.fhir.export", "true",
        "--exporter.fhir.bulk_data", "true",
    ]
    try:
        subprocess.run(cmd, check=True, timeout=1800)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False


def run(out_dir: str | Path = "data/synthea_ndjson", population: int = 1000, seed: int = 42, use_docker: bool = False) -> dict[str, Any]:
    out_path = Path(out_dir)
    if use_docker and generate_with_docker(out_path, population):
        source = "mitre/synthea (docker)"
        counts = {rt: sum(1 for _ in (out_path / f"{rt}.ndjson").open()) for rt in RESOURCE_TYPES if (out_path / f"{rt}.ndjson").exists()}
    else:
        counts = generate_synthetic_fallback(out_path, population, seed)
        source = f"pure-python fallback generator (seed={seed})"
    return {"out_dir": str(out_path), "source": source, "counts": counts}
