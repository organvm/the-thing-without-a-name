#!/usr/bin/env python3
"""Deterministic portable simulations for the Danse installation control plane.

These scenarios execute the real foreground supervisor against an ephemeral,
manifest-bound fixture release. They verify logical synchronization, health,
bounded recovery, and release-integrity behavior. They are deliberately not a
projector, speaker, venue, power-cycle, or restore rehearsal.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from .contract import (
        ContractError,
        calibration_plan,
        canonical_sha256,
        frame_ticket,
        validate_digital_twin,
    )
    from .runtime import Telemetry, supervise
except ImportError:  # Direct ``python3 installation/simulation.py`` execution.
    from contract import (  # type: ignore[no-redef]
        ContractError,
        calibration_plan,
        canonical_sha256,
        frame_ticket,
        validate_digital_twin,
    )
    from runtime import Telemetry, supervise  # type: ignore[no-redef]


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class _Process:
    def __init__(self, returncode: int | None) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture_release(root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    (root / "bin").mkdir()
    (root / "config").mkdir()
    configuration_sha256 = canonical_sha256(
        {
            "schema": "danse.installation.portable-configuration.v1",
            "status": "not-physical-evidence",
            "spec_contract_sha256": spec["identity"]["contract_sha256"],
        }
    )
    launcher = root / "bin/danse-launcher"
    launcher.write_text(
        "#!/bin/sh\n"
        'test "$#" -eq 0 || exit 64\n'
        f'test "$DANSE_INSTALLATION_CONTRACT_SHA256" = "{spec["identity"]["contract_sha256"]}" || exit 65\n'
        f'test "$DANSE_INSTALLATION_CONFIGURATION_SHA256" = "{configuration_sha256}" || exit 66\n'
        "test -r release-manifest.json || exit 67\n"
        "test -r config/installation.json || exit 68\n"
        "exit 0\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    configuration = root / "config/installation.json"
    configuration.write_text(
        json.dumps(
            {
                "schema": "danse.installation.portable-fixture.v1",
                "spec_contract_sha256": spec["identity"]["contract_sha256"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    records = [
        {
            "path": "bin/danse-launcher",
            "bytes": launcher.stat().st_size,
            "sha256": _sha256(launcher),
            "executable": True,
        },
        {
            "path": "config/installation.json",
            "bytes": configuration.stat().st_size,
            "sha256": _sha256(configuration),
            "executable": False,
        },
    ]
    manifest = {
        "schema": spec["release"]["manifest_schema"],
        "spec_contract_sha256": spec["identity"]["contract_sha256"],
        "files": records,
    }
    manifest_content = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    manifest_path = root / spec["release"]["manifest_name"]
    manifest_path.write_text(manifest_content, encoding="utf-8")
    identity = {
        "schema": "danse.installation.portable-simulation-identity.v1",
        "status": "not-physical-evidence",
        "spec_contract_sha256": spec["identity"]["contract_sha256"],
    }
    return {
        "schema": "danse.installation.runtime-plan.v2",
        "spec_contract_sha256": spec["identity"]["contract_sha256"],
        "evidence_id": "portable-simulation-not-evidence",
        "evidence_sha256": canonical_sha256(identity),
        "configuration_sha256": configuration_sha256,
        "release_manifest_sha256": hashlib.sha256(
            manifest_content.encode("utf-8")
        ).hexdigest(),
        "release_manifest": {
            "path": spec["release"]["manifest_name"],
            "content": manifest_content,
        },
        "release_files": records,
        "launcher": records[0],
        "argv": ["bin/danse-launcher"],
        "health_url": None,
        "river": {"seed": 20170620, "stream": 0, "epoch_ms": 0},
        "outputs": [
            output["id"]
            for output in sorted(
                spec["projection_outputs"], key=lambda item: item["channel"]
            )
        ],
        "policy": copy.deepcopy(spec["runtime"]),
    }


def _summary(exit_code: int, output: io.StringIO) -> dict[str, Any]:
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    if not records:
        raise ContractError("portable simulation emitted no telemetry")
    if [record["sequence"] for record in records] != list(range(len(records))):
        raise ContractError("portable simulation telemetry sequence drifted")
    counts = Counter(record["event"] for record in records)
    return {
        "exit_code": exit_code,
        "terminal_event": records[-1]["event"],
        "launcher_starts": counts["launcher-start"],
        "health_failures": counts["health-failed"],
        "event_counts": dict(sorted(counts.items())),
        "elapsed_seconds": records[-1]["elapsed_seconds"],
        "telemetry_sha256": hashlib.sha256(
            output.getvalue().encode("utf-8")
        ).hexdigest(),
    }


def _scenario(
    plan: dict[str, Any],
    release: Path,
    *,
    process_code: int | None,
    health: bool | None = None,
    execute_launcher: bool = False,
) -> dict[str, Any]:
    scenario_plan = copy.deepcopy(plan)
    if health is not None:
        scenario_plan["health_url"] = "http://127.0.0.1:8787/health"
    clock = _Clock()
    output = io.StringIO()

    def execute_and_wait(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        process = subprocess.Popen(*args, **kwargs)
        process.wait(timeout=5)
        return process

    popen = (
        execute_and_wait
        if execute_launcher
        else (lambda *_args, **_kwargs: _Process(process_code))
    )
    result = supervise(
        scenario_plan,
        release,
        Telemetry(output, clock=clock),
        clock=clock,
        sleep=clock.sleep,
        popen=popen,
        health_probe=lambda _url, _timeout: bool(health),
    )
    summary = _summary(result, output)
    summary["launcher_execution"] = (
        "real-subprocess" if execute_launcher else "deterministic-process-double"
    )
    return summary


def run_portable_simulation(spec: dict[str, Any]) -> dict[str, Any]:
    """Execute and receipt deterministic non-physical installation scenarios."""
    validate_digital_twin(spec)
    with tempfile.TemporaryDirectory(prefix="danse-installation-simulation-") as name:
        release = Path(name)
        plan = _fixture_release(release, spec)
        scenarios = {
            "clean-exit": _scenario(
                plan, release, process_code=0, execute_launcher=True
            ),
            "crash-storm": _scenario(plan, release, process_code=1),
            "startup-health-failure": _scenario(
                plan, release, process_code=None, health=False
            ),
        }
        (release / "config/installation.json").write_text(
            '{"drifted":true}\n', encoding="utf-8"
        )
        scenarios["release-integrity-failure"] = _scenario(
            plan, release, process_code=0
        )

    expected_attempts = spec["runtime"]["recovery"]["max_restarts"] + 1
    expected = {
        "clean-exit": (0, "launcher-exit", 1),
        "crash-storm": (75, "recovery-budget-exhausted", expected_attempts),
        "startup-health-failure": (
            75,
            "recovery-budget-exhausted",
            expected_attempts,
        ),
        "release-integrity-failure": (78, "release-integrity-failed", 0),
    }
    for name, (exit_code, terminal_event, launcher_starts) in expected.items():
        observed = scenarios[name]
        if (
            observed["exit_code"],
            observed["terminal_event"],
            observed["launcher_starts"],
        ) != (exit_code, terminal_event, launcher_starts):
            raise ContractError(f"portable simulation scenario {name} drifted")

    frames = [0, 1, 120, 3600]
    tickets = [frame_ticket(spec, 20170620, 0, frame) for frame in frames]
    if frame_ticket(spec, 20170620, 0, 120) != tickets[2]:
        raise ContractError("portable frame tickets are not seek-stable")
    output_ids = [
        output["id"]
        for output in sorted(
            spec["projection_outputs"], key=lambda item: item["channel"]
        )
    ]
    if any(
        [row["id"] for row in ticket["outputs"]] != output_ids for ticket in tickets
    ):
        raise ContractError("portable frame tickets do not cover every output")

    receipt = {
        "schema": "danse.installation.portable-simulation.v1",
        "status": "passed-not-physical-evidence",
        "spec_contract_sha256": spec["identity"]["contract_sha256"],
        "logical_sync": {
            "passed": True,
            "hardware_sync_measured": False,
            "outputs": output_ids,
            "frame_ticket_sha256": [ticket["ticket_sha256"] for ticket in tickets],
        },
        "calibration_plan_sha256": calibration_plan(spec)["plan_sha256"],
        "health_contract": copy.deepcopy(spec["runtime"]["health"]),
        "recovery_contract": copy.deepcopy(spec["runtime"]["recovery"]),
        "safety_contract": {
            "audio": {
                "latency_budget_ms": spec["audio"]["latency_budget_ms"],
                "limiter_ceiling_dbfs": spec["audio"]["limiter_ceiling_dbfs"],
            },
            "calibration_thresholds": copy.deepcopy(spec["calibration"]["thresholds"]),
            "persistent_host_service": spec["runtime"]["persistent_host_service"],
        },
        "scenarios": scenarios,
        "physical_claims": {
            "venue_approved": False,
            "hardware_measured": False,
            "projectors_calibrated": False,
            "speakers_calibrated": False,
            "power_cycles_observed": 0,
            "restore_rehearsal_observed": False,
            "issue_14_can_close": False,
        },
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    return receipt


def main() -> int:
    try:
        from .contract import load_reference_contracts
    except ImportError:
        from contract import load_reference_contracts  # type: ignore[no-redef]

    try:
        spec, _, _ = load_reference_contracts()
        print(json.dumps(run_portable_simulation(spec), indent=2, sort_keys=True))
        return 0
    except (ContractError, OSError) as exc:
        print(f"installation simulation: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
