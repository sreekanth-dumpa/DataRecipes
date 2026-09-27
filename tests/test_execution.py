"""
Real execution + independent reconciliation against the synthetic
warehouse. This is the machine-checkable version of Section 11's
acceptance criterion: "the analyst can inspect source-level edge cases
and reconcile numerator, denominator and exclusions to an independent
reference."

Cohort choice: July 2026, cutoff 2026-08-31. This is a full calendar
month plus a month's buffer before the knowledge cutoff, so every
journey's decision window (max ~21 days per the generator) is closed by
the cutoff -- the reconciliation isn't accidentally testing partial-month
truncation.

Run: pytest tests/test_execution.py -v
"""
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from compiler.sql_compiler import compile_recipe  # noqa: E402
from builder.db import duckdb_runner  # noqa: E402
from builder.context_adapter import OSIContextAdapter  # noqa: E402

COHORT_START = "2026-07-01"
COHORT_END = "2026-07-31"
KNOWLEDGE_CUTOFF = "2026-08-31"


def _independent_bind_rate_by_young_driver(conn: duckdb.DuckDBPyConnection) -> dict[str, dict]:
    """Hand-written, deliberately NOT sharing code with compiler/sql_compiler.py,
    querying the base tables directly. If this and the compiled recipe agree,
    that's real evidence the compiler's join/aggregation logic is correct --
    not just that it's internally consistent with itself."""
    sql = """
        SELECT
            d.young_driver_segment,
            COUNT(DISTINCT CASE WHEN j.is_eligible THEN j.quote_journey_id END) AS eligible_count,
            COUNT(DISTINCT CASE WHEN p.is_bound THEN p.quote_journey_id END) AS bound_count
        FROM quote_fdp.quote_journey j
        LEFT JOIN policy_fdp.policy p
            ON p.quote_journey_id = j.quote_journey_id AND p.effective_date <= $knowledge_cutoff
        LEFT JOIN quote_fdp.rated_driver d
            ON d.quote_journey_id = j.quote_journey_id
        WHERE j.product_line = $product_line
          AND j.journey_date BETWEEN $cohort_start AND $cohort_end
          AND j.effective_date <= $knowledge_cutoff
        GROUP BY d.young_driver_segment
    """
    rows = conn.execute(sql, {
        "knowledge_cutoff": KNOWLEDGE_CUTOFF,
        "product_line": "AUTO",
        "cohort_start": COHORT_START,
        "cohort_end": COHORT_END,
    }).fetchall()
    return {r[0]: {"eligible_count": r[1], "bound_count": r[2]} for r in rows}


def test_compiled_recipe_matches_independent_reconciliation(ensure_warehouse):
    sql = compile_recipe("quote.bind_rate_by_young_driver@1.0", engine="duckdb")
    params = {
        "cohort_start": COHORT_START,
        "cohort_end": COHORT_END,
        "knowledge_cutoff": KNOWLEDGE_CUTOFF,
        "product_line": "AUTO",
    }
    compiled_rows = duckdb_runner.execute(sql, params, db_path=ensure_warehouse)
    compiled_by_segment = {r["young_driver_segment"]: r for r in compiled_rows}

    conn = duckdb.connect(str(ensure_warehouse), read_only=True)
    independent = _independent_bind_rate_by_young_driver(conn)
    conn.close()

    assert set(compiled_by_segment) == set(independent), (compiled_by_segment, independent)
    assert compiled_by_segment  # cohort must be non-empty, or this test proves nothing

    for segment, expected in independent.items():
        actual = compiled_by_segment[segment]
        assert actual["eligible_count"] == expected["eligible_count"], segment
        assert actual["bound_count"] == expected["bound_count"], segment
        expected_rate = expected["bound_count"] / expected["eligible_count"]
        assert abs(actual["bind_rate"] - expected_rate) < 1e-9, segment


def test_late_bind_after_cutoff_is_really_excluded(ensure_warehouse):
    """Finds a real journey in the data that bound after COHORT-era
    activity to prove implementation.engines.duckdb's effective_date
    convention actually produces the documented behavior end-to-end, not
    just in the fixture's prose description."""
    conn = duckdb.connect(str(ensure_warehouse), read_only=True)
    row = conn.execute("""
        SELECT quote_journey_id, bind_date
        FROM policy_fdp.policy
        WHERE is_bound AND bind_date > DATE '2026-08-01'
        LIMIT 1
    """).fetchone()
    conn.close()
    assert row is not None, "expected at least one late-August-or-later bind in the synthetic data"
    journey_id, bind_date = row

    sql = compile_recipe("policy.quote_to_bound_policy@4.0", engine="duckdb")

    before = duckdb_runner.execute(sql, {"knowledge_cutoff": str(bind_date - __import__("datetime").timedelta(days=1))}, db_path=ensure_warehouse)
    after = duckdb_runner.execute(sql, {"knowledge_cutoff": str(bind_date)}, db_path=ensure_warehouse)

    before_ids = {r["quote_journey_id"] for r in before}
    after_ids = {r["quote_journey_id"] for r in after}
    assert journey_id not in before_ids, f"{journey_id} should not resolve as bound before its bind_date"
    assert journey_id in after_ids, f"{journey_id} should resolve once cutoff reaches its bind_date"
    assert next(r for r in after if r["quote_journey_id"] == journey_id)["is_bound"] is True


def test_osi_context_metric_matches_recipe_output(ensure_warehouse):
    """Cross-checks the OSI context file's own young_driver_bind_rate
    metric expression against the recipe's compiled output for the same
    cohort -- two independently-authored artifacts (a semantic-layer
    metric definition and a recipe) agreeing on the same real data."""
    conn = duckdb.connect(str(ensure_warehouse), read_only=True)
    adapter = OSIContextAdapter()
    metric = next(m for m in adapter.get_metrics() if m["name"] == "young_driver_bind_rate")
    expr = next(d["expression"] for d in metric["expression"]["dialects"] if d["dialect"] == "ANSI_SQL")

    osi_sql = f"""
        SELECT {expr} AS young_driver_bind_rate
        FROM quote_fdp.quote_journey quote_journey
        LEFT JOIN policy_fdp.policy policy ON policy.quote_journey_id = quote_journey.quote_journey_id
            AND policy.effective_date <= $knowledge_cutoff
        LEFT JOIN quote_fdp.rated_driver rated_driver ON rated_driver.quote_journey_id = quote_journey.quote_journey_id
        WHERE quote_journey.product_line = $product_line
          AND quote_journey.journey_date BETWEEN $cohort_start AND $cohort_end
          AND quote_journey.effective_date <= $knowledge_cutoff
    """
    osi_rate = conn.execute(osi_sql, {
        "knowledge_cutoff": KNOWLEDGE_CUTOFF, "product_line": "AUTO",
        "cohort_start": COHORT_START, "cohort_end": COHORT_END,
    }).fetchone()[0]
    conn.close()

    sql = compile_recipe("quote.bind_rate_by_young_driver@1.0", engine="duckdb")
    rows = duckdb_runner.execute(sql, {
        "cohort_start": COHORT_START, "cohort_end": COHORT_END,
        "knowledge_cutoff": KNOWLEDGE_CUTOFF, "product_line": "AUTO",
    }, db_path=ensure_warehouse)
    recipe_rate = next(r["bind_rate"] for r in rows if r["young_driver_segment"] == "young_driver")

    assert abs(osi_rate - recipe_rate) < 1e-9, (osi_rate, recipe_rate)
