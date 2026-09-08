"""Stage 3 support: Data Request Specification (DRS) schema + validation.

The DRS is the single JSON document that drives the whole pipeline: cohort
logic, requested fields, date range, mode/cadence, de-id parameters, and the
list of DQ checks to run on-site (stage 4 reads `checks[]` directly). See
drs/examples/DC-POC-0001.json for a concrete instance. [Req 2]
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class Authorization(BaseModel):
    permission_version: str
    irb_ref: str


class CohortRule(BaseModel):
    domain: str
    code_system: str
    codes: list[str]


class Cohort(BaseModel):
    inclusion: list[CohortRule] = Field(default_factory=list)
    exclusion: list[CohortRule] = Field(default_factory=list)


class FieldSpec(BaseModel):
    domain: str
    name: str
    required: bool = True


class DateRange(BaseModel):
    start: date
    end: date


class DateShift(BaseModel):
    per_patient: bool = True
    max_days: int = 365
    preserve_order: bool = True


class Deidentification(BaseModel):
    date_shift: DateShift


class DQCheck(BaseModel):
    id: str
    kahn_category: Literal["conformance", "completeness", "plausibility"]
    target: str
    type: Literal[
        "completeness", "uniqueness", "data_type", "date_sequence", "records_per_patient"
    ]
    params: dict = Field(default_factory=dict)
    on_fail: Literal["halt", "warn"] = "warn"


class DataRequestSpecification(BaseModel):
    request_id: str
    version: str
    authorization: Authorization
    cohort: Cohort
    source_systems: list[str]
    domains: list[str]
    fields: list[FieldSpec]
    date_range: DateRange
    extraction_mode: Literal["full", "incremental"]
    refresh_cadence: str
    deidentification: Deidentification
    checks: list[DQCheck]

    @classmethod
    def load(cls, path: str | Path) -> "DataRequestSpecification":
        return cls.model_validate(json.loads(Path(path).read_text()))
