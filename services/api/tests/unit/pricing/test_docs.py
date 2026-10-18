"""The documentation must agree with the code: generated tables and parameter coverage."""

from pathlib import Path

from app.pricing.params import SPECS, PricingParams
from app.pricing.trace import fleet_rows, format_table, run_day, trace_rows

DOC = Path(__file__).resolve().parents[5] / "docs" / "pricing-model.md"


def test_the_trace_table_in_the_docs_is_exactly_what_the_engine_produces() -> None:
    p = PricingParams()
    table = format_table(trace_rows(run_day(p), p))
    assert DOC.exists(), "docs/pricing-model.md is missing"
    assert table in DOC.read_text(encoding="utf-8"), (
        "docs/pricing-model.md is out of date; regenerate with `uv run python -m app.pricing.trace`"
    )


def test_every_parameter_is_documented() -> None:
    text = DOC.read_text(encoding="utf-8")
    missing = [k for k in SPECS if f"`{k}`" not in text]
    assert not missing, f"undocumented parameters: {missing}"


def test_the_calibration_table_in_the_docs_is_exactly_what_the_engine_produces() -> None:
    table = format_table(fleet_rows(PricingParams()))
    assert table in DOC.read_text(encoding="utf-8"), (
        "the fleet calibration table is out of date; regenerate with `uv run python -m app.pricing.trace`"
    )
