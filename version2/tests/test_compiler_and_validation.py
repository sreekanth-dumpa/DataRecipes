"""Typed IR, compilation, AST validation and DAG checks (spec section 7)."""
import pytest

from aipr.compiler.ast_validator import validate_sql
from aipr.compiler.dialect import SOURCE_ALLOWLIST, get_dialect
from aipr.compiler.physical import CompileError, check_dag, compile_fused, compile_partitioned, compile_staged
from aipr.intent.interpreter import intent_from_form
from aipr.ir.logical import build_logical_ir, template_fingerprint

from conftest import form

PARAMS = {"state", "lob", "cohort_start", "cohort_end", "window_days", "cutoff_ts", "cutoff_date"}


@pytest.fixture(scope="module")
def ir(svc):
    r = svc.recipes.latest("quote_conversion")
    return build_logical_ir(intent_from_form(form(), r), r)


def test_logical_ir_has_grains_and_obligations(ir):
    outcome = next(n for n in ir["nodes"] if n["logical_node_id"] == "bind_outcome")
    assert outcome["join"]["relationship"] == "one_to_many"
    assert outcome["join"]["reduction"] == "one_outcome_per_quote"
    assert outcome["output_grain"] == ["quote_id"]
    assert "no_future_knowledge" in outcome["obligations"]


def test_template_fingerprint_ignores_bound_values(svc, ir):
    r = svc.recipes.latest("quote_conversion")
    other = build_logical_ir(intent_from_form(form(state="SC", window_days=14), r), r)
    assert template_fingerprint(ir) == template_fingerprint(other)
    dims = build_logical_ir(intent_from_form(form(dimensions=["channel"]), r), r)
    assert template_fingerprint(ir) != template_fingerprint(dims)


@pytest.mark.parametrize("variant", ["fused", "staged", "partitioned"])
def test_every_compiled_node_passes_ast_validation(ir, variant):
    d = get_dialect("duckdb")
    plan = {"fused": compile_fused, "staged": compile_staged}.get(variant, lambda i, d: compile_partitioned(i, d, 3))(ir, d)
    rels = {n["node_id"]: f"stage.s_test_{n['node_id']}_a1" for n in plan["nodes"]}
    for n in plan["nodes"]:
        if n["kind"] != "sql":
            continue
        sql = n["sql"]
        for k, v in rels.items():
            sql = sql.replace("{{rel:" + k + "}}", v)
        res = validate_sql(d.ctas(rels[n["node_id"]], sql), "duckdb", set(SOURCE_ALLOWLIST) | set(rels.values()), PARAMS)
        assert res.ok, (n["node_id"], res.errors)


@pytest.mark.parametrize("sql,why", [
    ("DROP TABLE src_quote.quote_iteration", "not permitted"),
    ("SELECT * FROM secret.salaries", "not allowlisted"),
    ("SELECT * FROM read_csv('/etc/passwd')", "table functions"),
    ("SELECT * FROM src_quote.quote_iteration WHERE state = $evil", "undeclared"),
    ("CREATE TABLE src_quote.x AS SELECT 1", "not governed"),
    ("SELECT 1; SELECT 2", "exactly one statement"),
])
def test_ast_validator_rejects_disallowed_sql(sql, why):
    res = validate_sql(sql, "duckdb", set(SOURCE_ALLOWLIST), {"state"})
    assert not res.ok and any(why in e for e in res.errors), res.errors


def test_snowflake_rendering_pins_source_cut_and_uses_binds(ir):
    plan = compile_staged(ir, get_dialect("snowflake"))
    sql = plan["nodes"][0]["sql"]
    assert "AT(TIMESTAMP => %(source_cut_ts)s" in sql and "DATEADD(day, %(window_days)s" in sql
    assert "'NC'" not in sql  # values are binds, never literals


def test_dag_checks_reject_cycles(ir):
    plan = compile_staged(ir, get_dialect("duckdb"))
    plan["nodes"][0]["depends_on"] = ["metric_aggregate"]
    with pytest.raises(CompileError):
        check_dag(plan)


def test_unapproved_python_operator_refused():
    from aipr.engines.python_ops import run_operator
    with pytest.raises(PermissionError):
        run_operator({"operator_id": "arbitrary_code", "version": "1", "params": {}}, [])
    with pytest.raises(PermissionError):
        run_operator({"operator_id": "wilson_interval", "version": "1.0.0", "params": {"code": "x"}}, [])
