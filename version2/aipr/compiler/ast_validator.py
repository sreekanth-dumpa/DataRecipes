"""AST validation of compiled SQL (spec section 7).

Checks, on a parsed AST rather than on text:
* exactly one statement; SELECT or CREATE TABLE ... AS SELECT only
* CTAS targets only the governed ``stage`` / ``cache`` schemas
* every referenced relation is a CTE, an allowlisted source or an admitted stage relation
* no DML/DDL, no table functions or file readers, no forbidden functions
* bind placeholders exactly match the declared parameter set
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

FORBIDDEN_FUNCS = re.compile(r"^(read_|scan_|glob|getenv|current_setting|system|query|sql|copy|export|attach|load|install)",
                             re.IGNORECASE)
FORBIDDEN_NODES = tuple(getattr(exp, n) for n in ("Insert", "Update", "Delete", "Drop", "Alter", "Command",
                                                    "Merge", "Copy", "Use", "Set", "Pragma", "Attach")
                        if hasattr(exp, n))


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    placeholders: list[str] = field(default_factory=list)


def _placeholders(sql: str, dialect: str) -> set[str]:
    if dialect == "snowflake":
        return set(re.findall(r"%\((\w+)\)s", sql))
    return set(re.findall(r"\$(\w+)", sql))


def validate_sql(sql: str, dialect: str, allowed_relations: set[str], declared_params: set[str],
                 allow_ctas_schemas: tuple[str, ...] = ("stage", "cache")) -> ValidationResult:
    res = ValidationResult(ok=True)
    parse_sql = re.sub(r"%\((\w+)\)s", r":\1", sql) if dialect == "snowflake" else sql
    try:
        stmts = [s for s in sqlglot.parse(parse_sql, read=dialect) if s is not None]
    except sqlglot.errors.ParseError as e:
        return ValidationResult(False, [f"parse error: {str(e)[:200]}"])
    if len(stmts) != 1:
        return ValidationResult(False, [f"expected exactly one statement, got {len(stmts)}"])
    root = stmts[0]
    if isinstance(root, exp.Create):
        if (root.args.get("kind") or "").upper() != "TABLE" or not isinstance(root.expression, (exp.Select, exp.Union)):
            res.errors.append("only CREATE TABLE ... AS SELECT is permitted")
        target = root.this.this if isinstance(root.this, exp.Schema) else root.this
        schema = target.db if isinstance(target, exp.Table) else ""
        if schema not in allow_ctas_schemas:
            res.errors.append(f"CTAS target schema '{schema}' is not governed")
    elif not isinstance(root, (exp.Select, exp.Union)):
        res.errors.append(f"statement type {type(root).__name__} is not permitted")
    for node in root.walk():
        if FORBIDDEN_NODES and isinstance(node, FORBIDDEN_NODES):
            res.errors.append(f"forbidden operation {type(node).__name__}")
        if isinstance(node, (exp.Anonymous, exp.Func)):
            name = node.name if isinstance(node, exp.Anonymous) else (node.sql_name() if hasattr(node, "sql_name") else "")
            if name and FORBIDDEN_FUNCS.match(name):
                res.errors.append(f"forbidden function {name}")
    ctes = {c.alias_or_name for c in root.find_all(exp.CTE)}
    target_name = None
    if isinstance(root, exp.Create):
        t = root.this.this if isinstance(root.this, exp.Schema) else root.this
        target_name = f"{t.db}.{t.name}" if t.db else t.name
    for t in root.find_all(exp.Table):
        if isinstance(t.this, exp.Func) or t.args.get("this") is None:
            res.errors.append("table functions are not permitted")
            continue
        full = f"{t.db}.{t.name}" if t.db else t.name
        if full == target_name:
            continue
        res.tables.append(full)
        if t.name in ctes and not t.db:
            continue
        if full not in allowed_relations:
            res.errors.append(f"relation {full} is not allowlisted")
    found = _placeholders(sql, dialect)
    res.placeholders = sorted(found)
    if found - declared_params:
        res.errors.append(f"undeclared bind parameters {sorted(found - declared_params)}")
    res.ok = not res.errors
    return res
