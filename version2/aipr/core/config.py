"""Runtime configuration.  Paths default to version2/var (git-ignored)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # version2/


@dataclass
class Settings:
    root: Path = ROOT
    var_dir: Path = field(default_factory=lambda: Path(os.environ.get("AIPR_VAR_DIR", ROOT / "var")))
    engine: str = field(default_factory=lambda: os.environ.get("AIPR_ENGINE", "duckdb"))
    workers: int = int(os.environ.get("AIPR_WORKERS", "4"))
    max_concurrent_executions: int = int(os.environ.get("AIPR_MAX_CONCURRENT_EXECUTIONS", "4"))
    # partition sizing policy used by the planner and the checkpoint adapter
    rows_per_partition: int = int(os.environ.get("AIPR_ROWS_PER_PARTITION", "1500"))
    max_partitions: int = 8
    # anti-oscillation: minimum relative benefit before a suffix is replanned
    adaptation_min_benefit: float = 0.15
    stage_ttl_seconds: int = 3600
    cache_ttl_seconds: int = 24 * 3600
    lease_seconds: int = 120
    # optional LLM proposer (provider-neutral; off unless configured)
    llm_provider: str = field(default_factory=lambda: os.environ.get("AIPR_LLM_PROVIDER", "none"))

    @property
    def warehouse_path(self) -> Path:
        return self.var_dir / "warehouse.duckdb"

    @property
    def control_path(self) -> Path:
        return self.var_dir / "control.db"

    @property
    def artifacts_dir(self) -> Path:
        return self.var_dir / "artifacts"

    @property
    def feedback_dir(self) -> Path:
        return self.var_dir / "feedback" / "cases"

    @property
    def drafts_dir(self) -> Path:
        return self.var_dir / "recipe_drafts"

    @property
    def recipes_dir(self) -> Path:
        return self.root / "recipes"

    @property
    def context_dir(self) -> Path:
        return self.root / "context"

    def ensure_dirs(self) -> None:
        for p in (self.var_dir, self.artifacts_dir, self.feedback_dir, self.drafts_dir,
                  self.artifacts_dir / "plans", self.artifacts_dir / "receipts",
                  self.artifacts_dir / "requests"):
            p.mkdir(parents=True, exist_ok=True)
