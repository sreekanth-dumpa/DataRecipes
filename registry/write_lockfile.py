#!/usr/bin/env python3
"""
Write a lockfile pinning the exact transitive dependency closure for a
recipe version (Section 2: "Publication writes a lockfile with the exact
transitive versions. Runtime execution pins the same closure.").

Usage:
    python write_lockfile.py <id>@<version> [--db registry/index.db] [--out lockfiles/]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "registry" / "index.db"
DEFAULT_LOCKFILE_DIR = REPO_ROOT / "lockfiles"


def resolve_closure(conn: sqlite3.Connection, root_key: str) -> dict[str, str]:
    """Returns {id: version} for the root plus every transitive dependency."""
    closure: dict[str, str] = {}
    stack = [root_key]
    while stack:
        key = stack.pop()
        if key in closure.values() or f"{key}" in [f"{i}@{v}" for i, v in closure.items()]:
            continue
        rid, rversion = key.split("@")
        if closure.get(rid) == rversion:
            continue
        closure[rid] = rversion
        row = conn.execute("SELECT deps FROM recipes WHERE id = ? AND version = ?", (rid, rversion)).fetchone()
        if row is None:
            sys.exit(f"not found in registry index: {key}")
        for dep_key in json.loads(row[0]):
            stack.append(dep_key)
    return closure


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("recipe_key", help="id@version, e.g. quote.bind_rate_by_young_driver@1.0")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--out", default=str(DEFAULT_LOCKFILE_DIR))
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    closure = resolve_closure(conn, args.recipe_key)
    conn.close()

    rid, rversion = args.recipe_key.split("@")
    lockfile = {
        "recipe": args.recipe_key,
        "resolved": {k: v for k, v in sorted(closure.items())},
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{rid}@{rversion}.lock.yaml"
    out_path.write_text(yaml.safe_dump(lockfile, sort_keys=False))
    print(f"wrote {out_path.relative_to(REPO_ROOT)} ({len(closure)} pinned version(s))")


if __name__ == "__main__":
    main()
