"""Minimal SQL-on-FHIR v2 ViewDefinition evaluator.

The spec calls for compiling ViewDefinitions to DuckDB SQL via FlatQuack
(github.com/gotdan/flatquack). FlatQuack has no PyPI package, only a source
checkout, so vendoring it is out of scope for a `uv sync`-reproducible POC.
Instead this module evaluates the (small) subset of the ViewDefinition
grammar our `fhir_views/*.json` use — `column`/`path`, `forEach`,
`getResourceKey()`, `getReferenceKey()` — directly against parsed FHIR JSON.
The resulting flat rows are loaded into DuckDB (see stage2_ingest.py), so all
downstream querying (stages 3/4/6) is still pure DuckDB SQL over Parquet —
zero Spark/JVM either way. Swapping in real FlatQuack later is a drop-in
replacement for `evaluate_view()` since the ViewDefinition JSON is unchanged.
[Req 6]
"""
from __future__ import annotations

from typing import Any


def _get_path(obj: Any, path: str) -> Any:
    """Resolve a dotted FHIRPath-lite expression against a parsed resource/element."""
    if path == "getResourceKey()":
        return obj.get("id")
    if path.endswith(".getReferenceKey()"):
        field = path[: -len(".getReferenceKey()")]
        ref_obj = _get_path(obj, field)
        if not ref_obj:
            return None
        ref = ref_obj.get("reference") if isinstance(ref_obj, dict) else None
        return ref.split("/")[-1] if ref else None

    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            cur = cur[0] if cur else None
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _get_list(obj: Any, path: str) -> list:
    cur = obj
    for part in path.split("."):
        if cur is None:
            return []
        if isinstance(cur, list):
            cur = cur[0] if cur else None
        if not isinstance(cur, dict):
            return []
        cur = cur.get(part)
    if cur is None:
        return []
    return cur if isinstance(cur, list) else [cur]


def evaluate_view(view_definition: dict, resources: list[dict]) -> list[dict]:
    """Project `resources` through a ViewDefinition's `select` list into flat rows."""
    rows: list[dict] = []
    for resource in resources:
        base_row: dict[str, Any] = {}
        foreach_selects = []
        for select in view_definition["select"]:
            if "column" in select and "forEach" not in select:
                for col in select["column"]:
                    base_row[col["name"]] = _get_path(resource, col["path"])
            elif "forEach" in select:
                foreach_selects.append(select)

        if not foreach_selects:
            rows.append({**base_row, "__source_resource_id": resource.get("id")})
            continue

        for select in foreach_selects:
            items = _get_list(resource, select["forEach"])
            if not items:
                rows.append({**base_row, "__source_resource_id": resource.get("id")})
                continue
            for item in items:
                row = dict(base_row)
                for col in select["column"]:
                    row[col["name"]] = _get_path(item, col["path"])
                row["__source_resource_id"] = resource.get("id")
                rows.append(row)
    return rows
