"""Build a genuine single-simulated-day extract from a multi-year real-Synthea cohort.

Why this exists: a raw real-Synthea NDJSON export (stage1_synthea.py's `--use-container`
path) carries each patient's full birth-to-present history -- Encounter/Condition/
Observation/MedicationRequest dates span decades, not one day (confirmed directly: the
busiest single calendar day across an 1,149-patient real-Synthea run had activity for
only 17 patients, nowhere near a real health system's daily extract volume). Two
possible mistakes to avoid when building a "daily extract" test corpus from that data:

  1. Filtering to one real calendar date -- too sparse to be usable (see above).
  2. Taking N patients' *entire* multi-decade histories as a stand-in for "one day".
     That inflates volume and concentrates it in too few identities -- a real daily
     extract touches many *different* patients, each contributing typically one
     encounter that day, not a handful of patients each contributing their whole
     life's encounters. (Measured: only ~1.5% of real Synthea patient-days have more
     than one same-day encounter.)

This module does neither: for each patient in the source cohort, it draws exactly one
real encounter from their history (two for a matching ~1.5% subset), re-stamps that
encounter -- and every resource that references it -- onto one shared simulated extract
date, and keeps everything else (the real encounter class mix, the real code/demographic
content) untouched. The result is a cross-sectional, one-encounter-per-patient sample:
structurally a single day, content-wise still real Synthea output.

It also attaches one synthetic clinical note per sampled encounter, with note length
drawn per the encounter's real `class` (WELLNESS/ambulatory/outpatient/emergency vs.
inpatient) against two published word-count figures (see REPORT.md Sec 10.6) -- not a
single flat length applied to every encounter regardless of visit type. Real Synthea's
own class mix (measured on 26,803 encounters: ~99% WELLNESS/ambulatory/outpatient/
emergency, <1% inpatient) means a flat inpatient-anchored length overstates the average;
weighting by the real mix corrects that.
"""
from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

RESOURCE_TYPES_TO_FILTER = ["Condition", "Observation", "MedicationRequest"]

# Word-count anchors, both cited in REPORT.md Sec 10.6:
#  - outpatient progress notes: 642-word 2018 median (Rule et al., PMC8290305)
#  - inpatient progress notes: 886-word average (PMC8529482)
# No ED-specific figure is cited anywhere in this repo; emergency is mapped to the
# outpatient anchor as a stated, conservative placeholder (open item, not a citation).
WORDS_BY_CLASS = {
    "WELLNESS": 642,
    "ambulatory": 642,
    "outpatient": 642,
    "emergency": 642,
    "inpatient": 886,
}
DEFAULT_WORDS = 642  # class not in the map above

SAME_DAY_SECOND_ENCOUNTER_RATE = 0.015  # measured real rate, see module docstring

CLINICAL_FILLER_SENTENCES = [
    "Patient reports {symptom} for the past {duration}.",
    "Vital signs reviewed and within acceptable range for age.",
    "No acute distress noted on exam.",
    "Reviewed current medication list with patient; no changes requested today.",
    "Discussed risks, benefits, and alternatives of continuing the current plan.",
    "Patient denies fever, chills, or unintentional weight loss.",
    "Physical exam notable for {finding}.",
    "Assessment: stable, continue current management.",
    "Plan: follow up as scheduled, return sooner if symptoms worsen.",
    "Labs reviewed and discussed with patient; results largely unremarkable.",
    "Patient counseled on diet, exercise, and adherence to prescribed therapy.",
    "No new complaints at this visit.",
    "Chart reviewed prior to visit; history updated as needed.",
    "Patient tolerating current regimen well, no adverse effects reported.",
    "Recommend continued monitoring; will reassess at next scheduled visit.",
]
SYMPTOMS = ["mild fatigue", "intermittent discomfort", "occasional soreness", "general malaise", "low-grade symptoms"]
DURATIONS = ["few days", "week", "couple of weeks", "month"]
FINDINGS = ["normal heart and lung sounds", "no visible swelling", "clear breath sounds bilaterally", "stable gait"]


def _ref_id(reference: str | None) -> str | None:
    """`urn:uuid:<id>` / `Patient/<id>` -> bare `<id>`."""
    if not reference:
        return None
    return reference.split("/")[-1].split(":")[-1]


def _load_ndjson(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_ndjson(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _reshift_datetime(value: str | None, day_delta: int) -> str | None:
    """Shift a FHIR date/dateTime string by `day_delta` days, preserving time-of-day."""
    if not value:
        return value
    try:
        if "T" in value:
            dt = datetime.fromisoformat(value)
            return (dt + timedelta(days=day_delta)).isoformat()
        d = date.fromisoformat(value)
        return (d + timedelta(days=day_delta)).isoformat()
    except ValueError:
        return value  # not a recognizable date string -- leave untouched


def _generate_note(rng: random.Random, patient: dict, encounter: dict) -> str:
    name = patient.get("name", [{}])[0]
    full_name = " ".join(name.get("given", ["Unknown"]) + [name.get("family", "Unknown")])
    dob = patient.get("birthDate", "unknown")
    phone = next((t.get("value") for t in patient.get("telecom", []) if t.get("system") == "phone"), "unknown")
    enc_type = (encounter.get("type", [{}])[0].get("text") or "office visit")

    header = f"Patient: {full_name}  DOB: {dob}  Phone: {phone}\nVisit type: {enc_type}\n\n"

    cls = encounter.get("class", {}).get("code", "")
    target_words = WORDS_BY_CLASS.get(cls, DEFAULT_WORDS)
    target_words = max(100, int(rng.gauss(target_words, target_words * 0.2)))

    body_words = len(header.split())
    sentences = []
    while body_words < target_words:
        template = rng.choice(CLINICAL_FILLER_SENTENCES)
        sentence = template.format(
            symptom=rng.choice(SYMPTOMS), duration=rng.choice(DURATIONS), finding=rng.choice(FINDINGS),
        )
        sentences.append(sentence)
        body_words += len(sentence.split())
    return header + " ".join(sentences)


def build(
    ndjson_dir: str | Path,
    out_dir: str | Path,
    notes_path: str | Path | None = None,
    seed: int = 42,
    target_date: date | None = None,
    target_patients: int | None = None,
) -> dict:
    """Build a one-encounter-per-patient, single-simulated-day sample from `ndjson_dir`.

    Returns counts + the class-weighted average note length actually produced, so
    callers can compare it against a flat assumption.
    """
    rng = random.Random(seed)
    ndjson_dir = Path(ndjson_dir)
    out_dir = Path(out_dir)
    target_date = target_date or date.today()

    patients = {p["id"]: p for p in _load_ndjson(ndjson_dir / "Patient.ndjson")}
    encounters_by_patient: dict[str, list[dict]] = {}
    for enc in _load_ndjson(ndjson_dir / "Encounter.ndjson"):
        pid = _ref_id(enc.get("subject", {}).get("reference"))
        if pid in patients:
            encounters_by_patient.setdefault(pid, []).append(enc)

    if target_patients is not None and target_patients < len(encounters_by_patient):
        keep_ids = rng.sample(sorted(encounters_by_patient), k=target_patients)
        encounters_by_patient = {pid: encounters_by_patient[pid] for pid in keep_ids}

    sampled_encounters: list[dict] = []
    sampled_patient_ids: set[str] = set()
    day_delta_by_encounter: dict[str, int] = {}

    for pid, patient_encounters in encounters_by_patient.items():
        n_pick = 2 if (len(patient_encounters) >= 2 and rng.random() < SAME_DAY_SECOND_ENCOUNTER_RATE) else 1
        picks = rng.sample(patient_encounters, k=min(n_pick, len(patient_encounters)))
        for enc in picks:
            original_start = enc.get("period", {}).get("start")
            if not original_start:
                continue
            orig_date = datetime.fromisoformat(original_start).date() if "T" in original_start else date.fromisoformat(original_start)
            day_delta = (target_date - orig_date).days
            enc = dict(enc)  # don't mutate the loaded record in place
            period = dict(enc.get("period", {}))
            period["start"] = _reshift_datetime(period.get("start"), day_delta)
            period["end"] = _reshift_datetime(period.get("end"), day_delta)
            enc["period"] = period
            sampled_encounters.append(enc)
            day_delta_by_encounter[enc["id"]] = day_delta
        sampled_patient_ids.add(pid)

    sampled_encounter_ids = set(day_delta_by_encounter)

    # Secondary resources: keep only those referencing a sampled encounter (DSTU3 uses
    # `context.reference`; R4 fallback resources use `encounter.reference` -- check both),
    # re-dated by the same day_delta as their parent encounter so onset/order dates stay
    # consistent with the encounter they're attached to, not frozen on the original date.
    filtered: dict[str, list[dict]] = {rt: [] for rt in RESOURCE_TYPES_TO_FILTER}
    date_fields = {
        "Condition": ["onsetDateTime", "assertedDate", "recordedDate"],
        "Observation": ["effectiveDateTime", "issued"],
        "MedicationRequest": ["authoredOn"],
    }
    for rt in RESOURCE_TYPES_TO_FILTER:
        for rec in _load_ndjson(ndjson_dir / f"{rt}.ndjson"):
            link = rec.get("context", {}).get("reference") or rec.get("encounter", {}).get("reference")
            enc_id = _ref_id(link)
            if enc_id not in sampled_encounter_ids:
                continue
            rec = dict(rec)
            delta = day_delta_by_encounter[enc_id]
            for field in date_fields[rt]:
                if field in rec:
                    rec[field] = _reshift_datetime(rec[field], delta)
            filtered[rt].append(rec)

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_ndjson(out_dir / "Patient.ndjson", [patients[pid] for pid in sampled_patient_ids])
    _write_ndjson(out_dir / "Encounter.ndjson", sampled_encounters)
    for rt in RESOURCE_TYPES_TO_FILTER:
        _write_ndjson(out_dir / f"{rt}.ndjson", filtered[rt])

    notes = []
    for enc in sampled_encounters:
        pid = _ref_id(enc.get("subject", {}).get("reference"))
        patient = patients.get(pid, {})
        notes.append({"id": f"note-{enc['id']}", "encounter_id": enc["id"], "note_text": _generate_note(rng, patient, enc)})

    notes_path = Path(notes_path) if notes_path else out_dir / "notes.ndjson"
    _write_ndjson(notes_path, notes)

    total_words = sum(len(n["note_text"].split()) for n in notes)
    class_counts: dict[str, int] = {}
    for enc in sampled_encounters:
        cls = enc.get("class", {}).get("code", "unknown")
        class_counts[cls] = class_counts.get(cls, 0) + 1

    return {
        "out_dir": str(out_dir),
        "notes_path": str(notes_path),
        "target_date": target_date.isoformat(),
        "patients": len(sampled_patient_ids),
        "encounters": len(sampled_encounters),
        "notes": len(notes),
        "avg_words_per_note": round(total_words / len(notes), 1) if notes else 0,
        "encounter_class_counts": class_counts,
        "resource_counts": {rt: len(filtered[rt]) for rt in RESOURCE_TYPES_TO_FILTER},
    }
