import json
import time
from pathlib import Path

import pytest

from aipr.core.config import Settings
from aipr.data import fixture
from aipr.engines.duckdb_engine import DuckDBEngine
from aipr.service import AIPRService

ROOT = Path(__file__).resolve().parents[1]
EDGE = ROOT / "fixtures" / "edge_cases.json"
QUESTION = ("What was the 30 day quote conversion for NC personal auto quotes issued in August 2026 "
            "by product version and channel, as known by Sept 15 2026?")


def make_service(tmp_path, rows=None, engine_factory=None, **settings):
    s = Settings(var_dir=tmp_path / "var")
    for k, v in settings.items():
        setattr(s, k, v)
    s.ensure_dirs()
    if engine_factory is not None:
        fixture.build_warehouse(s.warehouse_path, rows)
        return AIPRService(s, engine=engine_factory(s.warehouse_path))
    return AIPRService(s, warehouse_rows=rows)


@pytest.fixture(scope="module")
def svc(tmp_path_factory):
    service = make_service(tmp_path_factory.mktemp("svc"))
    yield service
    service.close()


@pytest.fixture
def fresh_svc(tmp_path):
    service = make_service(tmp_path)
    yield service
    service.close()


@pytest.fixture(scope="module")
def edge_svc(tmp_path_factory):
    service = make_service(tmp_path_factory.mktemp("edge"), rows=fixture.edge_case_rows(EDGE))
    yield service
    service.close()


def form(**kw):
    base = {"state": "NC", "cohort_start": "2026-08-01", "cohort_end": "2026-08-31",
            "knowledge_cutoff": "2026-09-15T23:59:59Z", "window_days": 30, "dimensions": ["product_version", "channel"]}
    base.update(kw)
    return base


def run(svc, subject="analyst_nc", prefer=None, answers=None, timeout=60, **f):
    d = svc.interpret(form=form(**f), subject=subject)
    svc.confirm(d["request_id"], "tester", answers)
    p = svc.prepare(d["request_id"], prefer_variant=prefer)
    if p["status"] not in ("ready", "cache_hit"):
        return d["request_id"], p, None
    r = svc.execute(d["request_id"])
    assert r["admitted"], r
    return d["request_id"], p, svc.wait(r["execution_id"], timeout)


def key(rows, dims):
    return sorted(tuple([str(r[d]) for d in dims] + [int(r["eligible"]), int(r["converted"]), int(r["immature_excluded"])])
                  for r in rows)


class SlowEngine(DuckDBEngine):
    """DuckDB engine with an interruptible per-statement delay (for cancellation / shared-producer tests)."""

    def __init__(self, path, delay=1.0):
        super().__init__(path)
        self.delay = delay
        self.flags = set()

    def execute(self, sql, params, query_id):
        with self._lock:
            self._running[query_id] = None
        end = time.time() + self.delay
        try:
            while time.time() < end:
                if query_id in self.flags:
                    raise RuntimeError("interrupted by coordinator")
                time.sleep(0.01)
        finally:
            with self._lock:
                self._running.pop(query_id, None)
        return super().execute(sql, params, query_id)

    def interrupt(self, query_id):
        self.flags.add(query_id)
        return True
