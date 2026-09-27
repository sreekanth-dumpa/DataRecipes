#!/usr/bin/env python3
"""
Build the SQLite registry projection from recipes/**/*.yaml (Section 6.3).

Git is authoritative; this index is a rebuildable, queryable projection:
id, version, kind, status, owner, deps (JSON list of id@version), digest
(sha256 of the recipe file content, standing in for the implementation
digest), path.

Run after validate.py passes. Refuses to build over recipes with
unresolved dependencies or cycles — always runs the validator first.

Usage:
    python build_index.py [recipes_dir] [--out registry/index.db]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from validator.validate import validate, DEFAULT_RECIPES_DIR  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "registry" / "index.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id TEXT NOT NULL,
    version TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    owner TEXT NOT NULL,
    grain TEXT,
    deps TEXT NOT NULL,       -- JSON list of "id@version"
    digest TEXT NOT NULL,     -- sha256 of the recipe YAML content
    path TEXT NOT NULL,
    PRIMARY KEY (id, version)
);

CREATE TABLE IF NOT EXISTS impact_graph (
    dep_id TEXT NOT NULL,
    dep_version TEXT NOT NULL,
    dependent_id TEXT NOT NULL,
    dependent_version TEXT NOT NULL,
    PRIMARY KEY (dep_id, dep_version, dependent_id, dependent_version)
);
"""


def digest_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(recipes_dir: Path, db_path: Path) -> None:
    findings = validate(recipes_dir)
    errors = [f for f in findings if f.level == "ERROR"]
    if errors:
        for f in errors:
            print(f"[ERROR] {f.recipe}: {f.message}", file=sys.stderr)
        sys.exit(f"refusing to build index: {len(errors)} validation error(s)")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    for path in sorted(recipes_dir.rglob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        deps = [d["ref"] for d in (data.get("uses") or [])]
        conn.execute(
            "INSERT INTO recipes (id, version, kind, status, owner, grain, deps, digest, path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data["id"], data["version"], data["kind"], data["status"], data["owner"],
                data.get("grain", ""), json.dumps(deps), digest_of(path), str(path.relative_to(REPO_ROOT)),
            ),
        )
        for dep_ref in deps:
            dep_id, dep_version = dep_ref.split("@")
            conn.execute(
                "INSERT INTO impact_graph (dep_id, dep_version, dependent_id, dependent_version) VALUES (?, ?, ?, ?)",
                (dep_id, dep_version, data["id"], data["version"]),
            )

    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
    conn.close()
    print(f"built {db_path.relative_to(REPO_ROOT)}: {count} recipe(s) indexed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("recipes_dir", nargs="?", default=str(DEFAULT_RECIPES_DIR))
    parser.add_argument("--out", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args()
    build(Path(args.recipes_dir), Path(args.out))


if __name__ == "__main__":
    main()
