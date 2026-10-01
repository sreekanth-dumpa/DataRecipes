"""SQL fragments for the quote_conversion algebra, generated from the logical IR.

Every physical variant (fused, staged, partitioned) is assembled from the same
fragments; only the placement of materialisation boundaries and the
partitioning of the outcome aggregation differ.  ``{{rel:<node>}}`` tokens are
resolved at dispatch time to governed stage relation names (or CTE names when
fused).
"""
from __future__ import annotations

from .dialect import DIMENSION_COLUMNS, Dialect


def _dims(dims: list[str], alias: str | None = None) -> list[str]:
    for d in dims:
        if d not in DIMENSION_COLUMNS:
            raise ValueError(f"dimension {d} not in allowlist")
    return [f"{alias}.{d}" if alias else d for d in dims]


def canonical_cohort(d: Dialect) -> str:
    p = d.param
    return f"""SELECT s.quote_id, s.issue_date, s.product_version, s.state, s.channel, s.known_at,
       CASE WHEN {d.date_add_days('s.issue_date')} <= {p('cutoff_date')} THEN 1 ELSE 0 END AS mature
FROM (
  SELECT q.quote_id, q.issue_date, q.product_version, q.state,
         COALESCE(c.channel, 'unknown') AS channel, q.recorded_at AS known_at,
         ROW_NUMBER() OVER (PARTITION BY q.quote_id ORDER BY q.issue_date, q.iteration_no) AS rn
  FROM {d.source('src_quote.quote_iteration', 'q')}
  LEFT JOIN {d.source('ref.channel_crosswalk', 'c')} ON c.channel_code = q.channel_code
  WHERE q.status = 'ISSUED' AND q.eligible AND q.lob = {p('lob')} AND q.state = {p('state')}
    AND q.recorded_at <= {p('cutoff_ts')}
) s
WHERE s.rn = 1 AND s.issue_date BETWEEN {p('cohort_start')} AND {p('cohort_end')}"""


def quote_keys() -> str:
    return "SELECT quote_id, issue_date FROM {{rel:canonical_cohort}} WHERE mature = 1"


def bind_outcome(d: Dialect) -> str:
    p = d.param
    return f"""SELECT k.quote_id, COUNT(*) AS bind_events_in_window
FROM {d.source('src_policy.bind_event', 'b')}
JOIN {{{{rel:quote_keys}}}} k ON b.quote_id = k.quote_id
WHERE b.event_type = 'BIND' AND b.recorded_at <= {p('cutoff_ts')}
  AND b.bind_date >= k.issue_date AND b.bind_date <= {d.date_add_days('k.issue_date')}
GROUP BY k.quote_id"""


def quote_outcomes() -> str:
    return """SELECT c.quote_id, c.product_version, c.channel, c.state, c.mature,
       CASE WHEN c.mature = 1 AND r.quote_id IS NOT NULL THEN 1 ELSE 0 END AS converted
FROM {{rel:canonical_cohort}} c
LEFT JOIN {{rel:bind_outcome}} r ON r.quote_id = c.quote_id"""


def _agg_select(dims: list[str], alias: str, from_sql: str, where: str = "") -> str:
    cols = _dims(dims, alias)
    sel = ", ".join(cols + [f"SUM({alias}.mature) AS eligible", f"SUM({alias}.converted) AS converted",
                            f"SUM(1 - {alias}.mature) AS immature_excluded", "COUNT(*) AS cohort_quotes"])
    group = f"\nGROUP BY {', '.join(cols)}" if cols else ""
    return f"SELECT {sel}\nFROM {from_sql}{where}{group}"


def metric_aggregate(dims: list[str]) -> str:
    return _agg_select(dims, "o", "{{rel:quote_outcomes}} o")


def outcome_partition(d: Dialect, dims: list[str], part: int, n: int) -> str:
    """Partition i of the outcome aggregation: canonical quotes hashed by quote_id."""
    from_sql = ("(SELECT c.quote_id, c.product_version, c.channel, c.state, c.mature,\n"
                "        CASE WHEN c.mature = 1 AND r.quote_id IS NOT NULL THEN 1 ELSE 0 END AS converted\n"
                "   FROM {{rel:canonical_cohort}} c\n"
                "   LEFT JOIN {{rel:bind_outcome}} r ON r.quote_id = c.quote_id\n"
                f"  WHERE {d.hash_bucket('c.quote_id', n)} = {int(part)}) o")
    return _agg_select(dims, "o", from_sql)


def merge_partitions(dims: list[str], parts: list[str]) -> str:
    union = "\n  UNION ALL\n  ".join(f"SELECT * FROM {{{{rel:{p}}}}}" for p in parts)
    cols = _dims(dims)
    sel = ", ".join(cols + ["SUM(eligible) AS eligible", "SUM(converted) AS converted",
                            "SUM(immature_excluded) AS immature_excluded", "SUM(cohort_quotes) AS cohort_quotes"])
    group = f"\nGROUP BY {', '.join(cols)}" if cols else ""
    return f"SELECT {sel}\nFROM (\n  {union}\n) u{group}"


def raw_join_cardinality_sql(d: Dialect) -> str:
    """The *rejected* naive route: cohort joined to every bind event (no reduction)."""
    return canonical_cohort(d) + "\n-- JOIN src_policy.bind_event ON quote_id (no reduction: violates quote grain)"


def fused(d: Dialect, dims: list[str]) -> str:
    ctes = [("canonical_cohort", canonical_cohort(d)), ("quote_keys", quote_keys()),
            ("bind_outcome", bind_outcome(d)), ("quote_outcomes", quote_outcomes())]
    body = ",\n".join(f"{name} AS (\n{sql}\n)" for name, sql in ctes)
    sql = f"WITH {body}\n{metric_aggregate(dims)}"
    for name, _ in ctes:
        sql = sql.replace("{{rel:" + name + "}}", name)
    return sql
