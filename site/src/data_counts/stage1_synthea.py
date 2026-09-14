"""Stage 1: synthetic EHR generation + bulk-export endpoint. [Req 1a]

Optional path: shell out to the smartonfhir/synthea container image
(`--use-container`) to generate a real-Synthea FHIR cohort (see the fidelity
trade-offs documented above `generate_with_container`, notably DSTU3 output,
not R4). That image is large and a 1,000-patient run can take a long time to
pull/run, so the default path (no flag needed) is a deterministic, seeded,
pure-Python FHIR R4 generator that emits the same per-resource-type NDJSON
shape Synthea's bulk-export produces (Patient/Condition/Observation/Encounter/
MedicationRequest.ndjson). Either way, the output folder is what stage 2
treats as "the bulk-export endpoint's payload" — Synthea's own docs note the
NDJSON-folder and live `$export` paths are interchangeable for a downstream
consumer, so this POC takes the simpler one by default and documents the
container as a drop-in swap.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from faker import Faker

RESOURCE_TYPES = ["Patient", "Condition", "Observation", "Encounter", "MedicationRequest"]

# A handful of real code systems/codes so downstream DQ/cohort logic has
# something real to filter on (E11.x = Type 2 diabetes, matches the example DRS).
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


def _opaque_id(rng: random.Random) -> str:
    """A resource id with no relationship to any patient identifier.

    122 bits (matching UUIDv4's entropy) rather than fewer: collision probability
    is n^2/2N (birthday approximation), so at even 1M resources of one type --
    far past this POC's scale -- p is still ~1e-19, vs. ~1 in 560 at 48 bits.
    Drawn from the seeded `rng`, not `uuid.uuid4()`, to preserve the
    same-input-same-seed reproducibility guarantee.
    """
    return f"{rng.getrandbits(122):031x}"


def generate_synthetic_fallback(out_dir: Path, population: int, seed: int) -> dict[str, int]:
    """Deterministic pure-Python FHIR R4 cohort generator (no Java or container runtime needed)."""
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
                    "id": f"cond-{_opaque_id(rng)}",
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
                # Opaque, patient-independent: FHIR resource ids are a distinct namespace
                # from the patient identifier they reference, and (per Req 4/13) nothing
                # in the de-identified/tokenized output path may recover a raw identifier
                # by inspection -- a scheme like f"{pid}-enc-..." would leak exactly that,
                # and downstream reference fields (e.g. Observation.encounter_id) would
                # carry the leak forward. Confirmed necessary by finding it leak through
                # to the packaged output before this fix.
                enc_id = f"enc-{_opaque_id(rng)}"
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
                        "id": f"obs-{_opaque_id(rng)}",
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
                    "id": f"med-{_opaque_id(rng)}",
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


# podman first: it is the reference engine for the leave-behind artifact (see
# Containerfile), and preferring it here keeps the one place this POC shells out
# to a container runtime consistent with how the pipeline itself is packaged.
# docker is kept as a fallback so a site that has standardized on it still works.
CONTAINER_ENGINES = ("podman", "docker")


def resolve_container_engine() -> str | None:
    """First available engine from CONTAINER_ENGINES, or None if neither is installed."""
    for engine in CONTAINER_ENGINES:
        if shutil.which(engine) is not None:
            return engine
    return None


# mitre/synthea (the community image this POC originally shelled out to) has
# been removed from Docker Hub entirely -- the upstream synthetichealth/synthea
# project has never published an official container image of its own, only a
# release jar. smartonfhir/synthea (https://github.com/smart-on-fhir/synthea)
# is the container image in active-enough use to be worth supporting instead,
# but it's a thin 2018 Node HTTP wrapper around Synthea's own CLI, not a
# purpose-built exporter, so three real fidelity trade-offs vs. the old path:
#   - it only exports FHIR DSTU3 ("stu=3"), not R4 (the fallback generator and
#     stage 2's schema both target R4; DSTU3 resources still round-trip through
#     stage 2's generic cumulus-fhir-support ingest, but element shapes for the
#     same resource type can differ slightly between the two FHIR versions).
#   - it has no bulk-export mode (no --exporter.fhir.bulk_data equivalent),
#     only one Bundle-per-patient file, which _bundles_to_ndjson() below splits
#     into the per-resource-type NDJSON stage 2 expects.
#   - its HTTP layer (index.js) exposes no seed parameter -- only `-p` gets
#     forwarded to run_synthea -- so we bypass it and `exec` run_synthea
#     directly inside the running container with both `-s <seed> -p
#     <population>` (the App.java entrypoint's own documented flags). This
#     pins Synthea's *population* seed, which reproduces the same cohort
#     shape and per-resource-type counts run to run (verified) -- but unlike
#     the pure-Python path it is not byte-for-byte reproducible: Synthea
#     draws each resource's id/UUID from unseeded entropy regardless of `-s`.
SMARTONFHIR_IMAGE = "smartonfhir/synthea"


def _wait_for_container(engine: str, name: str, timeout: float = 30) -> bool:
    """Poll `exec ... true` until the container's process tree is up enough to exec into."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            subprocess.run([engine, "exec", name, "true"], check=True, capture_output=True, timeout=5)
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            time.sleep(0.5)
    return False


def _bundles_to_ndjson(bundle_dir: Path, out_dir: Path) -> None:
    """Split smartonfhir/synthea's one-Bundle-per-patient output into per-resource-type NDJSON."""
    writers: dict[str, Any] = {}
    try:
        for bundle_file in sorted(bundle_dir.glob("*.json")):
            bundle = json.loads(bundle_file.read_text())
            for entry in bundle.get("entry", []):
                resource = entry.get("resource")
                rt = resource.get("resourceType") if resource else None
                if rt is None:
                    continue
                if rt not in writers:
                    writers[rt] = (out_dir / f"{rt}.ndjson").open("w")
                writers[rt].write(json.dumps(resource) + "\n")
    finally:
        for w in writers.values():
            w.close()


def generate_with_container(out_dir: Path, population: int, seed: int) -> str | None:
    """Try the smartonfhir/synthea image. Returns the engine used, or None if unavailable."""
    engine = resolve_container_engine()
    if engine is None:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = out_dir / "_smartonfhir_raw"
    if raw_dir.exists():
        shutil.rmtree(raw_dir)
    raw_dir.mkdir(parents=True)
    container_name = f"data-counts-synthea-{os.getpid()}"
    run_cmd = [
        engine, "run", "-d", "--rm", "--name", container_name,
        # :Z applies a private SELinux label (needed on the RHEL-family hosts
        # podman is native to, no-op elsewhere). Synthea writes as root inside
        # the container; under rootless podman that lands as the invoking user
        # on the host, so no --userns mapping is needed for this one-shot.
        "-v", f"{raw_dir.resolve()}:/synthea/output:Z",
        SMARTONFHIR_IMAGE,
    ]
    try:
        subprocess.run(run_cmd, check=True, capture_output=True, timeout=60)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    try:
        if not _wait_for_container(engine, container_name):
            return None
        # Blocks until run_synthea's process exits inside the container. Real
        # Synthea's single-threaded JVM generation is roughly linear in
        # population once JVM/module-load warmup is paid, so the timeout
        # scales with population (floor covers that fixed warmup cost) rather
        # than a single constant that's fine at population=5 but too tight at
        # population=100000.
        gen_timeout = max(1800, population * 2)
        subprocess.run(
            [engine, "exec", container_name, "/synthea/run_synthea", "-s", str(seed), "-p", str(population)],
            check=True, capture_output=True, timeout=gen_timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    finally:
        subprocess.run([engine, "stop", container_name], capture_output=True, timeout=30)

    fhir_dir = raw_dir / "fhir"
    if not fhir_dir.exists():
        return None
    _bundles_to_ndjson(fhir_dir, out_dir)
    shutil.rmtree(raw_dir, ignore_errors=True)
    return engine


def run(out_dir: str | Path = "data/synthea_ndjson", population: int = 1000, seed: int = 42, use_container: bool = False) -> dict[str, Any]:
    out_path = Path(out_dir)
    engine = generate_with_container(out_path, population, seed) if use_container else None
    if engine is not None:
        source = f"{SMARTONFHIR_IMAGE} (dstu3, {engine})"
        counts = {rt: sum(1 for _ in (out_path / f"{rt}.ndjson").open()) for rt in RESOURCE_TYPES if (out_path / f"{rt}.ndjson").exists()}
    else:
        counts = generate_synthetic_fallback(out_path, population, seed)
        source = f"pure-python fallback generator (seed={seed})"
    return {"out_dir": str(out_path), "source": source, "counts": counts}
