#!/usr/bin/env python3
"""Venue-gated foreground supervisor for a canonical Danse installation release.

This program is deliberately on-demand. It does not install, generate, or invoke
any persistent host service. The venue owns how this foreground command is
started after power restoration, and that exact launcher must appear in the
external evidence receipt before `--run` is admitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, TextIO

try:
    from .contract import (
        ContractError,
        load_json,
        load_json_bytes,
        load_reference_contracts,
        runtime_plan,
        validate_snapshot_argument,
    )
except ImportError:  # Direct `python3 installation/runtime.py` execution.
    from contract import (  # type: ignore[no-redef]
        ContractError,
        load_json,
        load_json_bytes,
        load_reference_contracts,
        runtime_plan,
        validate_snapshot_argument,
    )

TELEMETRY_SCHEMA = "danse.installation.telemetry.v1"


def canonical_relative_path(value: Any, label: str) -> PurePosixPath:
    if not isinstance(value, str) or "\\" in value:
        raise ContractError(f"{label} must be a canonical relative path")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ContractError(f"{label} must be a canonical relative path")
    return pure


class Telemetry:
    """Append-only JSONL health events with no credentials or local paths."""

    def __init__(
        self, stream: TextIO, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.stream = stream
        self.clock = clock
        self.started = clock()
        self.sequence = 0

    def emit(self, event: str, **fields: Any) -> None:
        record = {
            "schema": TELEMETRY_SCHEMA,
            "sequence": self.sequence,
            "elapsed_seconds": round(max(0.0, self.clock() - self.started), 3),
            "event": event,
            **fields,
        }
        self.stream.write(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        )
        self.stream.flush()
        self.sequence += 1


def probe_health(url: str, timeout: float) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as response:  # noqa: S310 - numeric loopback URL is prevalidated
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


def terminate(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def open_verified_release_file(root: Path, record: dict[str, Any]) -> int:
    """Open and hash one manifest record through no-follow descriptors."""
    relative = record.get("path")
    pure = canonical_relative_path(relative, "runtime release path")
    if os.name != "posix" or not all(
        hasattr(os, name)
        for name in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    ):
        raise ContractError("descriptor-bound release access is unavailable or unsafe")
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    directories: list[int] = []
    file_fd: int | None = None
    try:
        directories.append(os.open(root, directory_flags))
        for part in pure.parts[:-1]:
            directories.append(os.open(part, directory_flags, dir_fd=directories[-1]))
        file_fd = os.open(pure.parts[-1], file_flags, dir_fd=directories[-1])
        metadata = os.fstat(file_fd)
        executable = record.get("executable")
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != record.get("bytes")
            or not isinstance(executable, bool)
            or bool(metadata.st_mode & 0o111) != executable
        ):
            raise ContractError("runtime release file identity or mode drifted")
        digest = hashlib.sha256()
        os.lseek(file_fd, 0, os.SEEK_SET)
        for block in iter(lambda: os.read(file_fd, 1024 * 1024), b""):
            digest.update(block)
        os.lseek(file_fd, 0, os.SEEK_SET)
        after = os.fstat(file_fd)
        if digest.hexdigest() != record.get("sha256") or (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
        ) != (after.st_dev, after.st_ino, after.st_size):
            raise ContractError("runtime release file bytes drifted")
        return file_fd
    except (OSError, ContractError):
        if file_fd is not None:
            os.close(file_fd)
        raise
    finally:
        for descriptor in reversed(directories):
            os.close(descriptor)


def _copy_release_file(
    source_fd: int, destination: Path, record: dict[str, Any]
) -> None:
    destination_fd: int | None = None
    try:
        mode = 0o500 if record["executable"] else 0o400
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            mode,
        )
        digest = hashlib.sha256()
        byte_count = 0
        os.lseek(source_fd, 0, os.SEEK_SET)
        for block in iter(lambda: os.read(source_fd, 1024 * 1024), b""):
            digest.update(block)
            byte_count += len(block)
            remaining = memoryview(block)
            while remaining:
                written = os.write(destination_fd, remaining)
                if written <= 0:
                    raise OSError("release snapshot write made no progress")
                remaining = remaining[written:]
        os.fsync(destination_fd)
        metadata = os.fstat(destination_fd)
        if (
            byte_count != record["bytes"]
            or metadata.st_size != record["bytes"]
            or digest.hexdigest() != record["sha256"]
        ):
            raise ContractError("runtime release changed while being snapshotted")
        os.fchmod(destination_fd, mode)
    finally:
        if destination_fd is not None:
            os.close(destination_fd)


def cleanup_release_snapshot(temporary: tempfile.TemporaryDirectory[str]) -> None:
    root = Path(temporary.name)
    if root.exists():
        for current, directories, _files in os.walk(root, topdown=True):
            try:
                Path(current).chmod(0o700)
            except OSError:
                pass
            directories.sort()
    try:
        temporary.cleanup()
    except OSError:
        pass


def snapshot_verified_release(
    root: Path, plan: dict[str, Any]
) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
    """Stream the complete admitted release into one private immutable snapshot."""
    manifest = plan.get("release_manifest")
    records = plan.get("release_files")
    if not isinstance(manifest, dict) or set(manifest) != {"path", "content"}:
        raise ContractError("runtime release manifest snapshot is malformed")
    if not isinstance(records, list) or not records:
        raise ContractError("runtime plan has no canonical release inventory")
    manifest_path = manifest["path"]
    content = manifest["content"]
    manifest_pure = canonical_relative_path(
        manifest_path, "runtime release manifest path"
    )
    if not isinstance(content, str):
        raise ContractError("runtime release manifest snapshot is malformed")
    manifest_bytes = content.encode("utf-8")
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_digest != plan.get("release_manifest_sha256"):
        raise ContractError("runtime release manifest snapshot digest drifted")
    document = load_json_bytes(manifest_bytes, "runtime release manifest")
    if set(document) != {"schema", "spec_contract_sha256", "files"}:
        raise ContractError("runtime release manifest shape drifted")
    if document["spec_contract_sha256"] != plan.get("spec_contract_sha256"):
        raise ContractError("runtime release manifest contract drifted")
    if document["files"] != records:
        raise ContractError("runtime release inventory disagrees with its manifest")

    paths: list[str] = []
    by_path: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != {
            "path",
            "bytes",
            "sha256",
            "executable",
        }:
            raise ContractError("runtime release inventory is malformed")
        pure = canonical_relative_path(
            record["path"], f"runtime release inventory[{index}].path"
        )
        relative = pure.as_posix()
        byte_count = record["bytes"]
        digest = record["sha256"]
        if (
            isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not isinstance(record["executable"], bool)
        ):
            raise ContractError("runtime release inventory is malformed")
        paths.append(relative)
        by_path[relative] = record
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ContractError("runtime release inventory must be unique and sorted")
    if manifest_pure.as_posix() in by_path:
        raise ContractError("runtime release manifest may not inventory itself")
    argv = plan.get("argv")
    launcher = plan.get("launcher")
    if (
        not isinstance(argv, list)
        or not argv
        or not isinstance(argv[0], str)
        or not isinstance(launcher, dict)
        or by_path.get(argv[0]) != launcher
        or launcher.get("executable") is not True
    ):
        raise ContractError("runtime launcher disagrees with the release inventory")
    for index, argument in enumerate(argv[1:], start=1):
        validate_snapshot_argument(argument, index)

    temporary = tempfile.TemporaryDirectory(prefix="danse-release-")
    snapshot_root = Path(temporary.name)
    try:
        for record in records:
            relative = record["path"]
            destination = snapshot_root.joinpath(
                *canonical_relative_path(relative, "runtime release path").parts
            )
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source_fd = open_verified_release_file(root, record)
            try:
                _copy_release_file(source_fd, destination, record)
            finally:
                os.close(source_fd)

        destination = snapshot_root.joinpath(*manifest_pure.parts)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        manifest_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o400,
        )
        try:
            remaining = memoryview(manifest_bytes)
            while remaining:
                written = os.write(manifest_fd, remaining)
                if written <= 0:
                    raise OSError("release manifest snapshot write made no progress")
                remaining = remaining[written:]
            os.fsync(manifest_fd)
            if os.fstat(manifest_fd).st_size != len(manifest_bytes):
                raise ContractError("runtime release manifest snapshot is incomplete")
            os.fchmod(manifest_fd, 0o400)
        finally:
            os.close(manifest_fd)

        directories = [snapshot_root]
        directories.extend(path for path in snapshot_root.rglob("*") if path.is_dir())
        for directory in sorted(
            directories, key=lambda path: len(path.parts), reverse=True
        ):
            directory.chmod(0o500)
        return snapshot_root, temporary
    except (OSError, ContractError):
        cleanup_release_snapshot(temporary)
        raise


def supervise(
    plan: dict[str, Any],
    release_root: Path,
    telemetry: Telemetry,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    popen: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
    health_probe: Callable[[str, float], bool] = probe_health,
) -> int:
    """Run one admitted launcher and recover only within its declared budget."""
    policy = plan["policy"]
    health = policy["health"]
    recovery = policy["recovery"]
    root = release_root.resolve(strict=True)
    relative_argv = plan["argv"]
    launcher = plan["launcher"]
    environment = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "TZ", "TMPDIR")
        if key in os.environ
    }
    environment.update(
        {
            "DANSE_INSTALLATION_CONTRACT_SHA256": plan["spec_contract_sha256"],
            "DANSE_INSTALLATION_EVIDENCE_ID": plan["evidence_id"],
            "DANSE_INSTALLATION_EVIDENCE_SHA256": plan["evidence_sha256"],
            "DANSE_INSTALLATION_CONFIGURATION_SHA256": plan["configuration_sha256"],
            "DANSE_INSTALLATION_RELEASE_MANIFEST_SHA256": plan[
                "release_manifest_sha256"
            ],
            "DANSE_INSTALLATION_LAUNCHER_SHA256": launcher["sha256"],
            "DANSE_INSTALLATION_OUTPUTS": ",".join(plan["outputs"]),
            "DANSE_RIVER_SEED": str(plan["river"]["seed"]),
            "DANSE_RIVER_STREAM": str(plan["river"]["stream"]),
            "DANSE_RIVER_EPOCH_MS": str(plan["river"]["epoch_ms"]),
        }
    )

    try:
        if launcher["path"] != relative_argv[0]:
            raise ContractError("runtime launcher path drifted")
        snapshot_root, temporary = snapshot_verified_release(root, plan)
    except (ContractError, OSError):
        telemetry.emit("release-integrity-failed", attempt=0)
        return 78

    snapshot_launcher = snapshot_root.joinpath(
        *canonical_relative_path(launcher["path"], "runtime launcher path").parts
    )
    argv = list(relative_argv)
    restart_times: list[float] = []
    attempt = 0
    try:
        while True:
            now = clock()
            restart_times = [
                stamp
                for stamp in restart_times
                if now - stamp <= recovery["window_seconds"]
            ]
            if attempt > 0:
                if len(restart_times) >= recovery["max_restarts"]:
                    telemetry.emit(
                        "recovery-budget-exhausted",
                        attempt=attempt,
                        restarts=len(restart_times),
                    )
                    return 75
                delay = recovery["backoff_seconds"][len(restart_times)]
                telemetry.emit(
                    "restart-admitted", attempt=attempt + 1, backoff_seconds=delay
                )
                sleep(delay)
                restart_times.append(clock())

            attempt += 1
            telemetry.emit("launcher-start", attempt=attempt)
            try:
                process = popen(
                    argv,
                    cwd=snapshot_root,
                    env=environment,
                    executable=str(snapshot_launcher),
                    shell=False,
                )
            except OSError as exc:
                telemetry.emit(
                    "launcher-error", attempt=attempt, error=type(exc).__name__
                )
                continue

            started = clock()
            ever_healthy = plan["health_url"] is None
            consecutive_failures = 0
            forced_failure: str | None = None
            try:
                while process.poll() is None:
                    if plan["health_url"] is not None:
                        ok = health_probe(
                            plan["health_url"], health["probe_timeout_seconds"]
                        )
                        if ok:
                            if not ever_healthy:
                                telemetry.emit("health-ready", attempt=attempt)
                            ever_healthy = True
                            consecutive_failures = 0
                        else:
                            consecutive_failures += 1
                            telemetry.emit(
                                "health-failed",
                                attempt=attempt,
                                consecutive=consecutive_failures,
                            )
                            elapsed = clock() - started
                            startup_failed = (
                                not ever_healthy
                                and elapsed >= health["startup_timeout_seconds"]
                            )
                            runtime_failed = (
                                ever_healthy
                                and consecutive_failures
                                >= health["max_consecutive_failures"]
                            )
                            if startup_failed or runtime_failed:
                                forced_failure = (
                                    "startup-health"
                                    if startup_failed
                                    else "runtime-health"
                                )
                                terminate(process)
                                break
                    sleep(health["probe_interval_seconds"])
            except KeyboardInterrupt:
                terminate(process)
                telemetry.emit("operator-stop", attempt=attempt)
                return 130

            returncode = process.poll()
            if returncode is None:
                try:
                    returncode = process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    terminate(process)
                    returncode = process.poll()
            duration = max(0.0, clock() - started)
            if (
                forced_failure is None
                and plan["health_url"] is not None
                and not ever_healthy
            ):
                forced_failure = "startup-exit"
            if forced_failure is not None:
                telemetry.emit(
                    "launcher-unhealthy",
                    attempt=attempt,
                    reason=forced_failure,
                    returncode=returncode,
                )
            elif returncode == 0:
                telemetry.emit("launcher-exit", attempt=attempt, returncode=0)
                return 0
            else:
                telemetry.emit("launcher-exit", attempt=attempt, returncode=returncode)
            if duration >= recovery["stable_seconds"]:
                restart_times.clear()
                telemetry.emit("recovery-window-reset", attempt=attempt)
    finally:
        cleanup_release_snapshot(temporary)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    mode = value.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="validate and print the admitted plan without launching",
    )
    mode.add_argument(
        "--run", action="store_true", help="run the admitted foreground launcher"
    )
    value.add_argument("--evidence", type=Path, required=True)
    value.add_argument("--release-root", type=Path, required=True)
    value.add_argument(
        "--telemetry", default="-", help="JSONL receipt path; - writes to stdout"
    )
    return value


def telemetry_stream(target: str) -> tuple[TextIO, bool]:
    if target == "-":
        return sys.stdout, False
    path = Path(target)
    if path.exists() or path.is_symlink():
        raise ContractError(
            "telemetry receipt path must be new and may not be a symlink"
        )
    try:
        stream = path.open("x", encoding="utf-8")
    except OSError as exc:
        raise ContractError(f"telemetry receipt cannot be created: {exc}") from exc
    return stream, True


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    stream: TextIO | None = None
    close_stream = False
    try:
        spec, _, _ = load_reference_contracts()
        evidence = load_json(args.evidence)
        plan = runtime_plan(evidence, spec, args.release_root)
        if args.check:
            print(json.dumps(plan, indent=2, sort_keys=True))
            return 0
        stream, close_stream = telemetry_stream(args.telemetry)
        telemetry = Telemetry(stream)
        telemetry.emit(
            "runtime-admitted",
            spec_contract_sha256=plan["spec_contract_sha256"],
            evidence_id=plan["evidence_id"],
            evidence_sha256=plan["evidence_sha256"],
            configuration_sha256=plan["configuration_sha256"],
            release_manifest_sha256=plan["release_manifest_sha256"],
            launcher_sha256=plan["launcher"]["sha256"],
        )

        def interrupt_foreground(_signum: int, _frame: Any) -> None:
            raise KeyboardInterrupt

        watched = [signal.SIGTERM]
        if hasattr(signal, "SIGHUP"):
            watched.append(signal.SIGHUP)
        previous = {
            signum: signal.signal(signum, interrupt_foreground) for signum in watched
        }
        try:
            return supervise(plan, args.release_root, telemetry)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    except (ContractError, OSError) as exc:
        print(f"installation runtime: {exc}", file=sys.stderr)
        return 1
    finally:
        if close_stream and stream is not None:
            stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
