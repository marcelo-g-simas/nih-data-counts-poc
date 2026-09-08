"""REQUIREMENTS.md and stage9_audit.RFI_MAP must not drift apart.

REQUIREMENTS.md (repo root) is the authoritative index joining the RFI's
requirement numbering to this repo's stage numbering; RFI_MAP is the runtime
half that `make demo` prints into data/work/rfi_summary.txt. The whole point of
having an index is that it stays true, so this asserts the two agree rather than
trusting that whoever edits one remembers the other.

Deliberate asymmetry: REQUIREMENTS.md is a superset. It also carries the
architecture-only requirements (7, 10, 12, 14), which by definition have no
module to name at runtime. So the test checks that RFI_MAP's requirements are
exactly REQUIREMENTS.md's non-architecture-only ones -- neither a missing row
nor an extra one passes.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from data_counts.stage9_audit import RFI_MAP

# tests/ -> site/ -> repo root
REQUIREMENTS_MD = Path(__file__).resolve().parent.parent.parent / "REQUIREMENTS.md"

VALID_STATUSES = {"implemented", "partial", "architecture-only", "not-addressed"}
RUNTIME_STATUSES = {"implemented", "partial"}


def parse_requirements_md() -> dict[str, str]:
    """Map req number -> status, from the Index table's rows only.

    The Index table's rows are the ones whose first cell is a requirement
    number (`1`, `1a`, `12a`, ...). That pattern is what distinguishes them from
    the Status-legend table earlier in the file, which shares the same pipe
    syntax -- keying on the cell content rather than on position in the file
    means inserting prose between the tables doesn't break this.
    """
    statuses: dict[str, str] = {}
    for line in REQUIREMENTS_MD.read_text().splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or not re.fullmatch(r"\d+[a-z]?", cells[0]):
            continue
        req, status = cells[0], cells[3]
        assert status in VALID_STATUSES, f"Req {req}: unknown status {status!r}"
        assert req not in statuses, f"Req {req} appears twice in REQUIREMENTS.md"
        statuses[req] = status
    return statuses


def test_requirements_md_is_present_and_parseable():
    statuses = parse_requirements_md()
    # 15 numbered requirements plus the lettered sub-requirements the RFI adds
    # (1a, 1b, 12a). A parse that silently matched nothing would otherwise make
    # every assertion below vacuously true.
    assert len(statuses) == 18, f"expected 18 indexed requirements, parsed {sorted(statuses)}"


def test_rfi_map_covers_exactly_the_runtime_requirements():
    statuses = parse_requirements_md()
    expected = {req for req, status in statuses.items() if status in RUNTIME_STATUSES}
    actual = {req for req, _status, _module, _desc in RFI_MAP}
    assert actual == expected, (
        f"RFI_MAP is missing {sorted(expected - actual)} and has extra {sorted(actual - expected)}. "
        "Update both REQUIREMENTS.md and stage9_audit.RFI_MAP."
    )


@pytest.mark.parametrize("entry", RFI_MAP, ids=[e[0] for e in RFI_MAP])
def test_each_rfi_map_status_matches_requirements_md(entry):
    req, status, module, description = entry
    assert status == parse_requirements_md()[req], (
        f"Req {req}: RFI_MAP says {status!r}, REQUIREMENTS.md says "
        f"{parse_requirements_md()[req]!r}"
    )
    assert module and description, f"Req {req}: empty module or description"


def test_architecture_only_requirements_are_absent_from_runtime_map():
    """A requirement with no code must not appear to have code at runtime.

    Guards the specific failure this index exists to prevent: rfi_summary.txt
    claiming a requirement was "exercised" when the write-up only designs it.
    """
    statuses = parse_requirements_md()
    arch_only = {req for req, status in statuses.items() if status == "architecture-only"}
    assert arch_only, "expected some architecture-only requirements; parse likely broke"
    assert not arch_only & {req for req, *_ in RFI_MAP}
