#!/usr/bin/env python3
"""Check repository-local diagnostic artifact writability."""

from __future__ import annotations

import argparse
import grp
import json
import pwd
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.artifact_writability import ArtifactWriteError, artifact_target_diagnostics, check_atomic_writability  # noqa: E402


def _paths(data_dir: Path, day: str, user: str) -> list[tuple[str, Path]]:
    safe_user = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user or "default")) or "default"
    return [
        ("research-bars-status", data_dir / "research" / "bars_status"),
        ("research-bars-consistency", data_dir / "research" / "bars_consistency"),
        ("signal-expectancy-report", data_dir / "research_metrics" / day),
        ("day-review-report", PROJECT_ROOT / "reports" / "day_review"),
        ("research-bars-status-json", data_dir / "research" / "bars_status" / f"{day}_{safe_user}.json"),
        ("research-bars-consistency-json", data_dir / "research" / "bars_consistency" / f"{day}_{safe_user}.json"),
        ("signal-expectancy-json", data_dir / "research_metrics" / day / "signal_expectancy_report.json"),
    ]


def _repo_identity() -> tuple[str, str]:
    st = PROJECT_ROOT.stat()
    return pwd.getpwuid(st.st_uid).pw_name, grp.getgrgid(st.st_gid).gr_name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Trading date YYYY-MM-DD.")
    parser.add_argument("--user", required=True, help="User id, e.g. live_bot.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    repo_user, repo_group = _repo_identity()
    parser.add_argument("--runtime-user", default=repo_user, help=argparse.SUPPRESS)
    parser.add_argument("--runtime-group", default=repo_group, help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    checks: list[dict[str, Any]] = []
    ok = True
    for name, path in _paths(args.data_dir, args.date, args.user):
        directory = path if path.suffix == "" else path.parent
        diag = artifact_target_diagnostics(path, runtime_user=args.runtime_user, runtime_group=args.runtime_group)
        row = {"name": name, "path": str(path), "directory": str(directory), "diagnostics": diag, "atomic": None}
        if path.suffix == "":
            try:
                row["atomic"] = check_atomic_writability(
                    directory,
                    filename=f".{args.date}_{args.user}_{name}.check",
                    runtime_user=args.runtime_user,
                    runtime_group=args.runtime_group,
                )
            except ArtifactWriteError as exc:
                row["atomic"] = exc.as_dict()
                ok = False
        if not diag.get("target_user_writable"):
            ok = False
        checks.append(row)

    payload = {
        "report": "artifact_writability_check",
        "research_only": True,
        "date": args.date,
        "user": args.user,
        "consistent": ok,
        "ok": ok,
        "configured_systemd_user": "algosphere",
        "configured_systemd_group": "algosphere",
        "effective_artifact_user": args.runtime_user,
        "effective_artifact_group": args.runtime_group,
        "checks": checks,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(f"Artifact Writability Check - {args.date} user={args.user}")
        print("Read-only diagnostics: no trading behavior, orders, fills, positions, thresholds, sizing, or exits changed.")
        print(f"- ok: {ok}")
        print(f"- configured_systemd_user: algosphere")
        print(f"- effective_artifact_user: {args.runtime_user}")
        for row in checks:
            diag = row["diagnostics"]
            print(
                "- {name}: dir={directory} owner={owner} group={group} mode={mode} target_user_writable={writable} world_writable={world}".format(
                    name=row["name"],
                    directory=row["directory"],
                    owner=diag.get("owner") or diag.get("nearest_parent_owner") or "missing",
                    group=diag.get("group") or diag.get("nearest_parent_group") or "missing",
                    mode=diag.get("mode") or diag.get("nearest_parent_mode") or "n/a",
                    writable=diag.get("target_user_writable"),
                    world=diag.get("world_writable"),
                )
            )
            if row.get("atomic"):
                atomic = row["atomic"]
                print(f"  atomic_create={atomic.get('atomic_create')} atomic_rename={atomic.get('atomic_rename')} cleanup={atomic.get('cleanup')}")
                if atomic.get("error_type"):
                    print(f"  atomic_error={atomic.get('reason')} path={atomic.get('path')}")
            if diag.get("error"):
                print(f"  error={diag.get('error')}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
