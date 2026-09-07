import pyarrow as pa
import pyarrow.parquet as pq

from data_counts.dsr import DQCheck
from data_counts.stage4_dq_kahn import run as run_checks


def _write(tmp_path, name, table):
    pq.write_table(table, tmp_path / f"{name}.parquet")


def test_completeness_pass_and_fail(tmp_path):
    _write(tmp_path, "patient", pa.table({"id": ["a", "b"], "birth_date": ["2000-01-01", None]}))
    checks = [DQCheck(id="c1", kahn_category="completeness", target="patient.birth_date", type="completeness", on_fail="warn")]
    result = run_checks(checks, tmp_path)
    assert result["checks"][0]["anomaly"] is True
    assert result["halt"] is False  # on_fail=warn


def test_uniqueness_halt(tmp_path):
    _write(tmp_path, "patient", pa.table({"id": ["a", "a"], "birth_date": ["2000-01-01", "2000-01-01"]}))
    checks = [DQCheck(id="c2", kahn_category="conformance", target="patient.id", type="uniqueness", on_fail="halt")]
    result = run_checks(checks, tmp_path)
    assert result["checks"][0]["anomaly"] is True
    assert result["halt"] is True


def test_data_type_check(tmp_path):
    _write(tmp_path, "observation", pa.table({"value_quantity": ["1.5", "not-a-number"]}))
    checks = [DQCheck(id="c3", kahn_category="conformance", target="observation.value_quantity", type="data_type",
                       params={"dtype": "double"}, on_fail="halt")]
    result = run_checks(checks, tmp_path)
    assert result["checks"][0]["detail"]["non_castable"] == 1
    assert result["halt"] is True


def test_date_sequence_check(tmp_path):
    _write(tmp_path, "encounter", pa.table({"period_start": ["2020-01-05", "2020-01-01"], "period_end": ["2020-01-01", "2020-01-10"]}))
    checks = [DQCheck(id="c4", kahn_category="plausibility", target="encounter", type="date_sequence",
                       params={"start": "period_start", "end": "period_end"}, on_fail="halt")]
    result = run_checks(checks, tmp_path)
    assert result["checks"][0]["detail"]["out_of_sequence"] == 1


def test_records_per_patient_check(tmp_path):
    _write(tmp_path, "observation", pa.table({"patient_id": ["a", "a", "a", "b"]}))
    checks = [DQCheck(id="c5", kahn_category="plausibility", target="observation", type="records_per_patient",
                       params={"min": 1, "max": 2}, on_fail="warn")]
    result = run_checks(checks, tmp_path)
    assert result["checks"][0]["detail"]["violating_patients"] == 1  # patient "a" has 3 > max 2
