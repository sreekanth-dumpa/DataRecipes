"""Engine dialects.  Parameter values are always connector binds; object names
come from the server-side allowlist below (spec section 7)."""
from __future__ import annotations

SOURCE_ALLOWLIST = {
    "src_quote.quote_iteration": "quote_iterations",
    "src_policy.bind_event": "bind_events",
    "ref.channel_crosswalk": "channel_crosswalk",
}
DIMENSION_COLUMNS = {"product_version", "channel", "state"}


class Dialect:
    name = "base"
    sqlglot = "duckdb"

    def param(self, name: str) -> str:
        raise NotImplementedError

    def source(self, table: str, alias: str) -> str:
        if table not in SOURCE_ALLOWLIST:
            raise ValueError(f"source {table} not in allowlist")
        return f"{table} {alias}"

    def date_add_days(self, expr: str) -> str:
        raise NotImplementedError

    def hash_bucket(self, expr: str, n: int) -> str:
        raise NotImplementedError

    def ctas(self, target: str, select_sql: str) -> str:
        return f"CREATE TABLE {target} AS\n{select_sql}"


class DuckDBDialect(Dialect):
    name = "duckdb"
    sqlglot = "duckdb"

    def param(self, name: str) -> str:
        return f"${name}"

    def date_add_days(self, expr: str) -> str:
        return f"({expr} + CAST($window_days AS INTEGER))"

    def hash_bucket(self, expr: str, n: int) -> str:
        return f"(hash({expr}) % {int(n)})"


class SnowflakeDialect(Dialect):
    """Snowflake rendering: pyformat binds and Time Travel-pinned base-table reads."""
    name = "snowflake"
    sqlglot = "snowflake"

    def param(self, name: str) -> str:
        return f"%({name})s"

    def source(self, table: str, alias: str) -> str:
        if table not in SOURCE_ALLOWLIST:
            raise ValueError(f"source {table} not in allowlist")
        return f"{table} AT(TIMESTAMP => %(source_cut_ts)s::TIMESTAMP_LTZ) {alias}"

    def date_add_days(self, expr: str) -> str:
        return f"DATEADD(day, %(window_days)s, {expr})"

    def hash_bucket(self, expr: str, n: int) -> str:
        return f"MOD(ABS(HASH({expr})), {int(n)})"

    def ctas(self, target: str, select_sql: str) -> str:
        return f"CREATE TRANSIENT TABLE {target} AS\n{select_sql}"


def get_dialect(name: str) -> Dialect:
    return {"duckdb": DuckDBDialect, "snowflake": SnowflakeDialect}[name]()
