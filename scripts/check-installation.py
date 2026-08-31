#!/usr/bin/env python3
"""Validate the Danse installation reference contract or external physical evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from installation.contract import (  # noqa: E402
    ContractError,
    calibration_plan,
    frame_ticket,
    installation_workbook,
    load_json,
    load_reference_contracts,
    runtime_plan,
    validate_evidence,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--phase", choices=("reference", "runtime", "complete"), default="reference"
    )
    value.add_argument("--evidence", type=Path)
    value.add_argument("--release-root", type=Path)
    value.add_argument(
        "--emit",
        choices=(
            "status",
            "calibration",
            "frame",
            "workbook",
            "simulation",
            "runtime-plan",
        ),
        default="status",
    )
    value.add_argument("--seed", type=int, default=20170620)
    value.add_argument("--stream", type=int, default=0)
    value.add_argument("--frame", type=int, default=0)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        spec, gates, archive = load_reference_contracts()
        evidence = None
        if args.phase != "reference":
            if args.evidence is None or args.release_root is None:
                raise ContractError(
                    f"BLOCKED: physical phase {args.phase} requires --evidence and --release-root"
                )
            evidence = load_json(args.evidence)
            validate_evidence(
                evidence, spec, phase=args.phase, release_root=args.release_root
            )

        if args.emit == "calibration":
            result = calibration_plan(spec)
        elif args.emit == "frame":
            result = frame_ticket(spec, args.seed, args.stream, args.frame)
        elif args.emit == "workbook":
            result = installation_workbook(spec, gates)
        elif args.emit == "simulation":
            from installation.simulation import run_portable_simulation

            result = run_portable_simulation(spec)
        elif args.emit == "runtime-plan":
            if evidence is None or args.release_root is None:
                raise ContractError(
                    "BLOCKED: runtime-plan emission requires admitted runtime evidence"
                )
            result = runtime_plan(evidence, spec, args.release_root)
        else:
            result = {
                "ok": True,
                "phase": args.phase,
                "spec_contract_sha256": spec["identity"]["contract_sha256"],
                "reference_status": spec["identity"]["status"],
                "gate_status": gates["status"],
                "blocked_gates": [
                    gate["id"] for gate in gates["gates"] if gate["status"] == "blocked"
                ],
                "archive_status": archive["result"]["status"],
                "physical_predicates_satisfied": args.phase == "complete",
                "issue_14_can_close": gates["issue_14_can_close"],
            }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (ContractError, OSError) as exc:
        print(f"installation: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
