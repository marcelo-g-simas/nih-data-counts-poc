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

from pydantic import BaseModel, Field, field_validator


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
    """Per-patient date-shift parameters. [Req 4]

    `preserve_seasonality` selects the offset *shape*, and it is the parameter
    that matters analytically. When set (the default), the offset is drawn from a
    52-week lattice -- `364 * max_years_draw + 7 * jitter_weeks_draw` -- so a
    shifted date keeps its day of the week exactly and lands within a couple of
    weeks of its original day of the year. A January admission stays in January,
    and weekday/weekend utilization patterns survive. `max_days` is then unused:
    it describes a uniform ±N-day draw, which is precisely the shape that destroys
    season by moving January into July.

    The jitter is measured in **weeks, not days**, and that is not cosmetic: a
    day-granular jitter would rotate the day of the week and void the guarantee
    the 52-week quantum exists to provide. Every term of the offset is a whole
    number of weeks.

    `max_days` is retained rather than removed so that an existing DRS still
    validates. It has no effect while `preserve_seasonality` is true.
    """

    per_patient: bool = True
    max_days: int = 365
    preserve_order: bool = True
    preserve_seasonality: bool = True
    max_years: int = 3
    jitter_weeks: int = 2

    @field_validator("jitter_weeks")
    @classmethod
    def _jitter_cannot_flip_season(cls, v: int) -> int:
        # A month is the smallest unit a seasonality claim can survive losing part
        # of, so the jitter is capped at four weeks. Beyond that the lattice's whole
        # purpose is defeated, and the caller should say preserve_seasonality=false
        # rather than configure a season-preserving shift that does not preserve
        # season.
        if v < 0 or v > 4:
            raise ValueError("jitter_weeks must be between 0 and 4 to preserve seasonality")
        return v


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
