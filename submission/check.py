#!/usr/bin/env python3
"""Is the ScreenDance package filable? Exit 0 ⟺ yes.

The register (`screendance-2027.yaml`) holds every fact about the call. This holds
none of them — it reads them. That separation is the point: a requirement can only
be wrong in one place, and it announces the date it was last checked.

Three kinds of check, and they fail differently on purpose:

    machine    ffprobe / PIL measure the artifact         PASS | FAIL
    attested   a human asserts it in package/attest.yaml  PASS | FAIL | MISSING
    unstated   the call never said; a phone call closes   OPEN

An OPEN blocking unknown is not a warning. It exits non-zero, because "we assumed
6:30 was fine" is exactly the failure that is only discovered after the deadline.

    ./check.py                        # register-level: deadline + open unknowns
    ./check.py --package .work/submission --phase package
    ./check.py --package .work/submission --phase uploaded
    ./check.py --package .work/submission --phase submitted
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jsonschema
import yaml

HERE = Path(__file__).resolve().parent
REGISTER = HERE / "screendance-2027.yaml"
RECEIPT_SCHEMA = HERE / "receipt.schema.json"
RIGHTS_REGISTER = HERE.parent / "rights" / "register.json"
OPPORTUNITY_CHECKER = HERE.parent / "scripts" / "check-opportunities.py"
RIGHTS_CHECKER = HERE.parent / "scripts" / "rights_contract.py"
SCORE_MOTION_CHECKER = HERE.parent / "scripts" / "score_motion_production.py"

PASS, FAIL, OPEN, SKIP = "PASS", "FAIL", "OPEN", "SKIP"
GLYPH = {PASS: "\033[32m ok \033[0m", FAIL: "\033[31mFAIL\033[0m", OPEN: "\033[33mOPEN\033[0m", SKIP: "skip"}
PHASES = ("package", "uploaded", "submitted")
OWNED_SECTIONS = ("requirements", "approvals", "terms")

VIDEO_SUFFIXES = {".mov", ".mp4", ".mxf", ".m4v"}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
GIT_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
RECEIPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{5,127}$")
DESTINATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$")
UTC_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
PHASE_RECEIPTS = {
    "package": "receipts/package.json",
    "uploaded": "receipts/uploaded.json",
    "submitted": "receipts/submitted.json",
}
PHASE_RECEIPT_SCHEMAS = {
    "package": "danse.submission.package.v1",
    "uploaded": "danse.submission.uploaded.v1",
    "submitted": "danse.submission.submitted.v1",
}
PHASE_SIGNER_ROLES = {
    "package": "package-approver",
    "uploaded": "upload-account-owner",
    "submitted": "submission-account-owner",
}
PHASE_SIGNER_GATES = {
    "package": ("final-cut-only", "bio-approved", "rights-declaration-approved"),
    "uploaded": ("link-password-protected", "link-downloadable"),
    "submitted": (
        "submitted-via-submittable",
        "accepted-film-no-withdrawal",
        "publicity-stills-free-of-rights",
        "archive-library-choice",
        "regulations-accepted",
    ),
}
DONE_RECEIPT_SCHEMA = "danse.submission.validation.v1"
DONE_RECEIPTS = {
    phase: f"receipts/validated-{phase}.json" for phase in PHASES
}
# This receipt is deliberately narrower than done.sh: it is minted by, and only
# claims, the submission validation executing in this process.  done.sh still
# gates the portable and browser batches before it invokes this final predicate.
DONE_RECEIPT_SCOPE = "submission-phase-validation"
DONE_PREDICATES = (
    "python3 submission/check.py --package <package-root> --phase {phase}",
)
MANIFEST_FIELDS = {
    "schema",
    "title",
    "seed",
    "repository_head",
    "items",
    "passage_seed",
    "passage",
    "start",
    "t0",
    "t1",
    "duration",
    "corpus_tier",
    "source_tree_sha256",
    "sound",
    "production",
}
MANIFEST_ITEM_FIELDS = {
    "name",
    "bytes",
    "sha256",
    "width",
    "height",
    "fps",
    "seconds",
    "vcodec",
    "vprofile",
    "pix_fmt",
    "acodec",
    "channels",
    "sample_rate",
    "sound",
    "source",
    "source_sha256",
    "copy_mode",
}
AUDIO_IDENTITY_HASH_FIELDS = (
    "audio_uses_sha256",
    "score_file_sha256",
    "score_contract_sha256",
    "choreography_file_sha256",
    "choreography_contract_sha256",
    "midi_sha256",
    "adaptation_sha256",
    "toolchain_sha256",
    "mix_sha256",
    "soundfont_sha256",
    "audio_render_receipt_sha256",
    "master_sha256",
)
AUDIO_SOUND_FIELDS = (
    "profile",
    *AUDIO_IDENTITY_HASH_FIELDS,
    "sources",
    "stems",
    "credit",
)


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses duplicate assertion ids."""

    def compose_node(self, parent: yaml.Node | None, index: object) -> yaml.Node:
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()
            raise yaml.constructor.ConstructorError(
                "while composing a mapping",
                getattr(event, "start_mark", None),
                "YAML aliases are not accepted in assertion contracts",
                getattr(event, "start_mark", None),
            )
        return super().compose_node(parent, index)


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    value: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            repeated = key in value
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable complex mapping key",
                key_node.start_mark,
            ) from exc
        if repeated:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        value[key] = loader.construct_object(value_node, deep=deep)
    return value


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class Report:
    """Results, and the exit code they imply."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []

    def add(self, section: str, name: str, status: str, detail: str = "") -> None:
        self.rows.append((section, name, status, detail))

    def print(self) -> None:
        section = None
        for sec, name, status, detail in self.rows:
            if sec != section:
                print(f"\n\033[1m{sec}\033[0m")
                section = sec
            print(f"  [{GLYPH[status]}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def failures(self) -> int:
        return sum(1 for _, _, s, _ in self.rows if s in (FAIL, OPEN))


# ── measurement ────────────────────────────────────────────────────────────────


def probe(path: Path) -> dict | None:
    """Video geometry and duration, or None if ffprobe is unavailable."""
    if not shutil.which("ffprobe"):
        return None
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,profile,pix_fmt,width,height,r_frame_rate,channels,sample_rate:"
            "stream_disposition=attached_pic:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        return None
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams") or []
    video = next(
        (
            stream
            for stream in streams
            if stream.get("codec_type") == "video" and not (stream.get("disposition") or {}).get("attached_pic")
        ),
        {},
    )
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
    num, _, den = (video.get("r_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    return {
        "width": video.get("width"),
        "height": video.get("height"),
        "fps": round(fps, 3),
        "seconds": float((data.get("format") or {}).get("duration") or 0.0),
        "vcodec": video.get("codec_name"),
        "vprofile": video.get("profile"),
        "pix_fmt": video.get("pix_fmt"),
        "acodec": audio.get("codec_name"),
        "channels": audio.get("channels"),
        "sample_rate": int(audio.get("sample_rate") or 0),
    }


def loudness(path: Path) -> dict | None:
    """Integrated loudness and true peak measured from the staged artifact."""
    if not shutil.which("ffmpeg"):
        return None
    try:
        out = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-af",
                "loudnorm=I=-16:TP=-1:LRA=11:print_format=json",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return None
    blocks = re.findall(r"\{\s*\"input_i\".*?\}", out.stderr, flags=re.DOTALL)
    if not blocks:
        return None
    try:
        measured = json.loads(blocks[-1])
        return {"lufs": float(measured["input_i"]), "true_peak_dbtp": float(measured["input_tp"])}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def image_size(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def words(path: Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="replace").split())


def find_one(root: Path, stem: str) -> Path | None:
    """The single file whose stem matches, of any video extension."""
    hits = [p for p in root.iterdir() if p.is_file() and p.stem == stem and p.suffix.lower() in VIDEO_SUFFIXES]
    return hits[0] if len(hits) == 1 else None


def read_manifest(root: Path) -> dict:
    path = root / "manifest.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def manifest_items(root: Path) -> dict[str, dict]:
    return {
        item["name"]: item
        for item in read_manifest(root).get("items", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def safe_contract_file(root: Path, relative: object, label: str) -> Path:
    """Resolve one regular repository/package file without following links."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"{label} contract root is missing or unsafe")
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError(f"{label} has no safe relative path")
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or relative != pure.as_posix()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ValueError(f"{label} escapes its contract root")
    current = root.resolve()
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{label} traverses a symlink")
    if not current.is_file():
        raise ValueError(f"{label} is missing")
    try:
        current.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} escapes its contract root") from exc
    return current


def read_contract_json(root: Path, relative: object, label: str) -> tuple[dict, Path]:
    path = safe_contract_file(root, relative, label)

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"{label} repeats JSON field {key!r}")
            value[key] = item
        return value

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"{label} contains non-finite JSON value {value}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith(label):
            raise
        raise ValueError(f"{label} is not readable unique-key JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not a JSON object")
    return value, path


def parse_attestation_document(document: object, label: str) -> dict[str, Any]:
    """Parse one exact, embeddable attestation snapshot without aliases."""
    if (
        not isinstance(document, str)
        or not document
        or len(document.encode("utf-8")) > 262_144
        or "\x00" in document
    ):
        raise ValueError(f"{label} has no exact UTF-8 attestation document")
    try:
        value = yaml.load(document, Loader=_UniqueKeySafeLoader)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"{label} attestation document is not unique-key YAML") from exc
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} attestation document must be a string-keyed mapping")
    if any(item is not None and type(item) is not bool and not isinstance(item, str) for item in value.values()):
        raise ValueError(f"{label} attestation values must be boolean, named choice, or null")
    return value


def read_attestations(root: Path) -> tuple[dict[str, Any], Path]:
    """Load the mutable human-assertion worksheet without accepting aliases."""
    path = safe_contract_file(root, "attest.yaml", "package attestation")
    try:
        document = path.read_text(encoding="utf-8")
        value = parse_attestation_document(document, "package")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("package attestation is not readable unique-key YAML") from exc
    return value, path


def receipt_schema_errors(value: object, label: str) -> list[str]:
    """Execute the tracked Draft 2020-12 receipt contract before semantics."""
    try:
        schema, _ = read_contract_json(
            RECEIPT_SCHEMA.parent,
            RECEIPT_SCHEMA.name,
            "receipt schema",
        )
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
        ).validate(value)
    except (ValueError, jsonschema.SchemaError, jsonschema.ValidationError) as exc:
        if isinstance(exc, jsonschema.ValidationError):
            location = ".".join(str(part) for part in exc.absolute_path)
            suffix = f" at {location}" if location else ""
            return [f"{label} does not satisfy receipt.schema.json{suffix}: {exc.message}"]
        return [f"{label} cannot use receipt.schema.json: {type(exc).__name__}"]
    return []


def canonical_json_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def receipt_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not UTC_TIMESTAMP.fullmatch(value):
        raise ValueError(f"{label} has no canonical UTC timestamp")
    rendered = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as exc:
        raise ValueError(f"{label} has no canonical UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{label} timestamp is not UTC")
    return parsed.astimezone(timezone.utc)


def register_timestamp(value: object, label: str) -> datetime:
    """Parse a canonical register instant without silently adding an offset."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} is not an offset timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} has no UTC offset")
    return parsed


def typed_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not RECEIPT_ID.fullmatch(value):
        raise ValueError(f"{label} has no typed identifier")
    return value


def recorded_by(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
        raise ValueError(f"{label} has no named recorder")
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError(f"{label} recorder is not a single public-safe name")
    if re.search(
        r"\b(?:todo|tbd|unknown|none|null|placeholder|anonymous)\b",
        value.casefold(),
    ) or value.casefold() in {
        "account owner",
        "package approver",
        "upload account owner",
        "submission account owner",
        "signer",
        "recorder",
        "test user",
    }:
        raise ValueError(f"{label} recorder is still a placeholder")
    return value


def destination_safe_https(value: object, label: str) -> str:
    """Return one unambiguous public HTTPS destination.

    Filing receipts are durable evidence, so their destination must not depend on
    browser-specific authority parsing, IDNA display, or local-network naming.
    """
    if (
        not isinstance(value, str)
        or value != value.strip()
        or "\\" in value
        or any(char.isspace() or ord(char) < 32 for char in value)
    ):
        raise ValueError(f"{label} has no HTTPS URL")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"{label} has a malformed HTTPS authority") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or "?" in value
        or "#" in value
    ):
        raise ValueError(f"{label} has no public-safe HTTPS URL")
    if parsed.netloc.endswith(":"):
        raise ValueError(f"{label} has an invalid HTTPS port")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"{label} has an invalid HTTPS port")

    hostname = hostname.lower()
    if "%" in hostname or not hostname.isascii() or any(
        part.startswith("xn--") for part in hostname.split(".")
    ):
        raise ValueError(f"{label} has a malformed or IDNA HTTPS authority")
    if hostname.endswith("."):
        raise ValueError(f"{label} has a malformed HTTPS authority")

    local_suffixes = (
        ".localhost",
        ".local",
        ".localdomain",
        ".internal",
        ".lan",
        ".home.arpa",
    )
    if hostname == "localhost" or hostname.endswith(local_suffixes):
        raise ValueError(f"{label} points to a non-public host")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        labels = hostname.split(".")
        legacy_ipv4 = len(labels) <= 4 and all(
            re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)", part) for part in labels
        )
        if parsed.netloc.startswith("[") or legacy_ipv4:
            raise ValueError(f"{label} has a malformed HTTPS authority")
        if len(labels) < 2:
            raise ValueError(f"{label} points to a non-public host")
        if len(hostname) > 253 or any(
            len(part) > 63
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", part)
            for part in labels
        ):
            raise ValueError(f"{label} has a malformed HTTPS authority")
    else:
        if not address.is_global:
            raise ValueError(f"{label} points to a non-public host")
    return value


def https_url(value: object, label: str) -> str:
    """Backward-compatible name for the destination receipt validator."""
    return destination_safe_https(value, label)


def repository_state(root: Path = HERE.parent) -> tuple[str, bool]:
    """Return the commit and source cleanliness that actually run the checker."""
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"],
            capture_output=True,
            text=True,
            check=False,
        )
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ValueError("cannot query the repository identity") from exc
    commit = (head.stdout or "").strip().lower()
    if head.returncode or not GIT_OID.fullmatch(commit):
        raise ValueError("checker has no exact repository commit")
    if status.returncode:
        raise ValueError("checker cannot verify repository cleanliness")
    return commit, not bool((status.stdout or "").strip())


def validate_package_identity(root: Path) -> tuple[dict[str, str], dict, list[str]]:
    """Authenticate the delivery manifest and every byte it inventories."""
    errors: list[str] = []
    try:
        manifest, path = read_contract_json(root, "manifest.json", "package manifest")
    except ValueError as exc:
        return {}, {}, [str(exc)]
    manifest_digest = sha256(path)
    if not {"schema", "title", "seed", "repository_head", "items"}.issubset(manifest):
        errors.append("package manifest has no complete base identity")
    if not set(manifest).issubset(MANIFEST_FIELDS):
        errors.append("package manifest has fields outside its typed contract")
    if manifest.get("schema") != "danse.delivery.manifest.v1":
        errors.append("package manifest has the wrong schema")
    repository_head = manifest.get("repository_head")
    if not isinstance(repository_head, str) or not GIT_OID.fullmatch(repository_head):
        errors.append("package manifest has no exact repository head")
        repository_head = ""
    try:
        current_head, clean = repository_state()
    except ValueError as exc:
        errors.append(str(exc))
    else:
        if repository_head != current_head:
            errors.append("package manifest repository head is not the checker HEAD")
        if not clean:
            errors.append("checker repository has uncommitted source changes")

    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        errors.append("package manifest has no item inventory")
        items = []
    seen: set[str] = set()
    census_manifest_files: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"package item {index} is not a record")
            continue
        if not {"name", "bytes", "sha256"}.issubset(item) or not set(item).issubset(
            MANIFEST_ITEM_FIELDS
        ):
            errors.append(f"package item {index} has fields outside its typed contract")
        name = item.get("name")
        if not isinstance(name, str) or name in seen:
            errors.append(f"package item {index} has a missing or duplicate name")
            continue
        seen.add(name)
        if name in {"manifest.json", "attest.yaml"} or name.startswith("receipts/"):
            errors.append(f"package item {name} crosses the out-of-band receipt boundary")
            continue
        try:
            item_path = safe_contract_file(root, name, f"package item {name}")
        except ValueError as exc:
            errors.append(str(exc))
            continue
        census_manifest_files.add(name)
        expected_sha = item.get("sha256")
        expected_bytes = item.get("bytes")
        try:
            actual_sha = sha256(item_path)
            actual_bytes = item_path.stat().st_size
        except OSError:
            errors.append(f"package item {name} changed during validation")
            continue
        if not isinstance(expected_sha, str) or not HEX64.fullmatch(expected_sha):
            errors.append(f"package item {name} has no exact digest")
        elif actual_sha != expected_sha:
            errors.append(f"package item {name} digest is stale")
        if type(expected_bytes) is not int or expected_bytes < 0:
            errors.append(f"package item {name} has no exact byte count")
        elif actual_bytes != expected_bytes:
            errors.append(f"package item {name} byte count is stale")
        try:
            changed = sha256(item_path) != actual_sha or item_path.stat().st_size != actual_bytes
        except OSError:
            changed = True
        if changed:
            errors.append(f"package item {name} changed during validation")

    out_of_band = {
        "manifest.json",
        "attest.yaml",
        *PHASE_RECEIPTS.values(),
        *DONE_RECEIPTS.values(),
    }
    receipt_digests: dict[str, str] = {}
    for receipt_phase, relative in PHASE_RECEIPTS.items():
        candidate = root / relative
        if not candidate.exists() and not candidate.is_symlink():
            continue
        label = f"out-of-band {receipt_phase} receipt"
        try:
            receipt, receipt_path = read_contract_json(root, relative, label)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        errors.extend(receipt_schema_errors(receipt, label))
        if receipt.get("schema") != PHASE_RECEIPT_SCHEMAS[receipt_phase]:
            errors.append(f"{label} has a schema that does not match its path")
        try:
            receipt_digests[relative] = sha256(receipt_path)
        except OSError:
            errors.append(f"{label} changed during validation")
    for receipt_phase, relative in DONE_RECEIPTS.items():
        candidate = root / relative
        if not candidate.exists() and not candidate.is_symlink():
            continue
        label = f"out-of-band validated-{receipt_phase} receipt"
        try:
            receipt, receipt_path = read_contract_json(root, relative, label)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        errors.extend(receipt_schema_errors(receipt, label))
        if (
            receipt.get("schema") != DONE_RECEIPT_SCHEMA
            or receipt.get("scope") != DONE_RECEIPT_SCOPE
            or receipt.get("phase") != receipt_phase
        ):
            errors.append(f"{label} has an identity that does not match its path")
        try:
            receipt_digests[relative] = sha256(receipt_path)
        except OSError:
            errors.append(f"{label} changed during validation")
    allowed_files = census_manifest_files | out_of_band
    allowed_directories = {"receipts"}
    for relative in allowed_files:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            allowed_directories.add(parent.as_posix())
            parent = parent.parent
    try:
        surface = sorted(root.rglob("*"), key=lambda candidate: candidate.as_posix())
    except OSError:
        errors.append("package file census could not be read")
        surface = []
    surface_signature: list[tuple[str, str]] = []
    for candidate in surface:
        try:
            relative = candidate.relative_to(root).as_posix()
            if candidate.is_symlink():
                surface_signature.append((relative, "symlink"))
                errors.append(f"package surface contains symlink {relative}")
            elif candidate.is_dir():
                surface_signature.append((relative, "directory"))
                if relative not in allowed_directories:
                    errors.append(f"package surface contains unknown directory {relative}")
            elif candidate.is_file():
                surface_signature.append((relative, "file"))
                if relative not in allowed_files:
                    errors.append(f"package surface contains unmanifested file {relative}")
            else:
                errors.append(f"package surface contains unsupported entry {relative}")
        except OSError:
            errors.append("package surface changed during its file census")

    try:
        manifest_changed = sha256(path) != manifest_digest
    except OSError:
        manifest_changed = True
    if manifest_changed:
        errors.append("package manifest changed during validation")
    try:
        final_surface_signature = []
        for candidate in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            relative = candidate.relative_to(root).as_posix()
            kind = (
                "symlink"
                if candidate.is_symlink()
                else "directory"
                if candidate.is_dir()
                else "file"
                if candidate.is_file()
                else "unsupported"
            )
            final_surface_signature.append((relative, kind))
    except OSError:
        final_surface_signature = []
    if final_surface_signature != surface_signature:
        errors.append("package surface changed during validation")
    for relative, digest in receipt_digests.items():
        try:
            if sha256(root / relative) != digest:
                errors.append(f"out-of-band receipt {relative} changed during validation")
        except OSError:
            errors.append(f"out-of-band receipt {relative} changed during validation")

    binding = {
        "manifest": "manifest.json",
        "manifest_sha256": manifest_digest,
        "repository_head": repository_head,
    }
    return binding, manifest, errors


def check_package_identity(root: Path, rep: Report) -> tuple[dict[str, str], dict]:
    binding, manifest, errors = validate_package_identity(root)
    rep.add(
        "package identity",
        "exact manifest and repository head",
        FAIL if errors else PASS,
        "; ".join(errors[:6])
        if errors
        else f"manifest {binding['manifest_sha256'][:16]}… · head {binding['repository_head'][:12]}",
    )
    return binding, manifest


def assertion_contracts(reg: dict, phase: str) -> dict[str, dict[str, Any]]:
    selected = PHASES.index(phase)
    contracts: dict[str, dict[str, Any]] = {}
    for section in OWNED_SECTIONS:
        for item in reg.get(section, []):
            owner = item.get("phase")
            kind = item.get("check")
            assertion_id = item.get("id")
            if (
                kind not in {"manual", "choice"}
                or owner not in PHASES
                or PHASES.index(owner) > selected
                or not isinstance(assertion_id, str)
            ):
                continue
            contracts[assertion_id] = {
                "kind": kind,
                "values": item.get("values", [True] if kind == "manual" else []),
            }
    return contracts


def validate_assertion_snapshot(
    values: object,
    contracts: dict[str, dict[str, Any]],
    label: str,
) -> list[str]:
    if not isinstance(values, dict):
        return [f"{label} has no assertion snapshot"]
    errors: list[str] = []
    if set(values) != set(contracts):
        errors.append(f"{label} assertion census is not exact")
    for key, contract in contracts.items():
        value = values.get(key)
        if contract["kind"] == "manual" and value is not True:
            errors.append(f"{label} does not affirm {key}")
        elif contract["kind"] == "choice" and value not in contract["values"]:
            errors.append(f"{label} has no registered choice for {key}")
    return errors


def package_binding_errors(
    value: object,
    expected: dict[str, str],
    label: str,
) -> list[str]:
    if not isinstance(value, dict) or set(value) != {
        "manifest",
        "manifest_sha256",
        "repository_head",
    }:
        return [f"{label} has no exact package binding"]
    errors: list[str] = []
    if value != expected:
        errors.append(f"{label} names a different package manifest or repository head")
    return errors


def opportunity_binding_errors(value: object, reg: dict, label: str) -> list[str]:
    snapshot = reg.get("opportunity_snapshot") or {}
    expected = {
        "snapshot_id": snapshot.get("snapshot_id"),
        "sha256": snapshot.get("sha256"),
    }
    if not isinstance(value, dict) or set(value) != set(expected) or value != expected:
        return [f"{label} names a different opportunity snapshot"]
    if not isinstance(expected["sha256"], str) or not HEX64.fullmatch(expected["sha256"]):
        return ["submission register has no exact opportunity digest"]
    return []


def deadline_binding(reg: dict) -> tuple[dict[str, str], datetime, datetime]:
    """Return the exact named-zone operational instants owned by the register."""
    deadline = reg.get("deadline")
    snapshot = reg.get("opportunity_snapshot")
    if not isinstance(deadline, dict) or not isinstance(snapshot, dict):
        raise ValueError("submission register has no deadline contract")
    zone_name = snapshot.get("timezone")
    if not isinstance(zone_name, str):
        raise ValueError("submission register has no named deadline timezone")
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("submission register deadline timezone is unavailable") from exc
    upload_target = register_timestamp(
        deadline.get("upload_target"),
        "submission register upload target",
    )
    hard_wall = register_timestamp(
        deadline.get("hard_wall"),
        "submission register hard wall",
    )
    for label, instant in (("upload target", upload_target), ("hard wall", hard_wall)):
        local = instant.astimezone(zone)
        if (
            local.replace(tzinfo=None) != instant.replace(tzinfo=None)
            or local.utcoffset() != instant.utcoffset()
        ):
            raise ValueError(f"submission register {label} disagrees with its named timezone")
    if upload_target >= hard_wall:
        raise ValueError("submission register upload target is not before its hard wall")
    return (
        {
            "timezone": zone_name,
            "upload_target": deadline["upload_target"],
            "hard_wall": deadline["hard_wall"],
        },
        upload_target,
        hard_wall,
    )


def canonical_phase_signer(phase: str) -> str:
    """Derive the public signer identity from the canonical redacted rights gate."""
    try:
        rights, _ = read_contract_json(
            RIGHTS_REGISTER.parent,
            RIGHTS_REGISTER.name,
            "rights authority register",
        )
    except ValueError as exc:
        raise ValueError("canonical signer authority register is unavailable") from exc
    bindings = rights.get("bindings")
    submission_binding = bindings.get("submission") if isinstance(bindings, dict) else None
    binding = submission_binding.get("source") if isinstance(submission_binding, dict) else None
    if (
        rights.get("schema") != "danse.rights.v1"
        or not isinstance(binding, dict)
        or binding.get("path") != "submission/screendance-2027.yaml"
        or binding.get("sha256") != sha256(REGISTER)
    ):
        raise ValueError("canonical signer authority register has a stale submission binding")
    gates = rights.get("human_gates")
    if not isinstance(gates, list) or not all(isinstance(gate, dict) for gate in gates):
        raise ValueError("canonical signer authority register has malformed gates")
    by_key: dict[str, str] = {}
    for gate in gates:
        attestation = gate.get("attestation")
        if not isinstance(attestation, dict) or not isinstance(attestation.get("key"), str):
            continue
        authority = gate.get("authority")
        if not isinstance(authority, str):
            continue
        key = attestation["key"]
        if key in by_key:
            raise ValueError(f"canonical signer authority repeats gate {key}")
        by_key[key] = authority
    names: set[str] = set()
    for key in PHASE_SIGNER_GATES[phase]:
        authority = by_key.get(key)
        if authority is None:
            raise ValueError(f"canonical signer authority is missing gate {key}")
        for suffix in (" as the account owner", " as the applicant"):
            if authority.endswith(suffix):
                authority = authority[: -len(suffix)]
                break
        names.add(recorded_by(authority, "canonical signer authority"))
    if len(names) != 1:
        raise ValueError("canonical signer gates do not name one exact public authority")
    return names.pop()


def signer_errors(value: object, phase: str, label: str) -> list[str]:
    if not isinstance(value, dict) or set(value) != {"name", "role"}:
        return [f"{label} has no exact signer identity"]
    errors: list[str] = []
    try:
        name = recorded_by(value.get("name"), f"{label} signer")
    except ValueError as exc:
        errors.append(str(exc))
    else:
        try:
            expected_name = canonical_phase_signer(phase)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if name != expected_name:
                errors.append(f"{label} signer is not the canonical phase authority")
    if value.get("role") != PHASE_SIGNER_ROLES[phase]:
        errors.append(f"{label} signer has the wrong phase role")
    return errors


def attestation_snapshot(
    reg: dict,
    attested: dict[str, Any],
    phase: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    contracts = assertion_contracts(reg, phase)
    values = {key: attested.get(key) for key in sorted(contracts)}
    return values, contracts


def attestation_binding_errors(
    attestation_value: object,
    assertions_value: object,
    reg: dict,
    attested: dict[str, Any],
    attestation_path: Path | None,
    phase: str,
    label: str,
    *,
    selected: bool,
) -> list[str]:
    current_values, contracts = attestation_snapshot(reg, attested, phase)
    if attestation_path is None:
        return [
            *validate_assertion_snapshot(current_values, contracts, "package attestation"),
            f"{label} cannot bind a missing or invalid attest.yaml",
        ]
    errors = validate_assertion_snapshot(current_values, contracts, "package attestation")
    embedded: dict[str, Any] = {}
    expected_attestation_fields = {"path", "sha256", "canonical_sha256", "document"}
    if not isinstance(attestation_value, dict) or set(attestation_value) != expected_attestation_fields:
        errors.append(f"{label} has no exact full-attestation binding")
    else:
        document = attestation_value.get("document")
        try:
            embedded = parse_attestation_document(document, label)
        except ValueError as exc:
            errors.append(str(exc))
        all_contracts = assertion_contracts(reg, "submitted")
        if set(embedded) != set(all_contracts):
            errors.append(f"{label} embedded attestation census is not exact")
        later_assertions = set(all_contracts) - set(contracts)
        if any(embedded.get(key) is not None for key in later_assertions):
            errors.append(f"{label} prematurely asserts a later-phase gate")
        if isinstance(document, str):
            document_sha256 = hashlib.sha256(document.encode("utf-8")).hexdigest()
            if attestation_value.get("sha256") != document_sha256:
                errors.append(f"{label} attestation document digest is stale")
        if attestation_value.get("canonical_sha256") != canonical_json_sha256(embedded):
            errors.append(f"{label} canonical attestation digest is stale")
        for field in ("sha256", "canonical_sha256"):
            candidate = attestation_value.get(field)
            if not isinstance(candidate, str) or not HEX64.fullmatch(candidate):
                errors.append(f"{label} has no exact attestation {field}")
        if attestation_value.get("path") != "attest.yaml":
            errors.append(f"{label} names a different attestation source")
        if selected:
            try:
                live_document = attestation_path.read_text(encoding="utf-8")
                live_sha256 = sha256(attestation_path)
            except (OSError, UnicodeDecodeError):
                errors.append(f"{label} attestation source changed during validation")
            else:
                if (
                    embedded != attested
                    or document != live_document
                    or attestation_value.get("sha256") != live_sha256
                ):
                    errors.append(f"{label} names a different full attestation snapshot")
    embedded_values, _ = attestation_snapshot(reg, embedded, phase)
    errors.extend(validate_assertion_snapshot(embedded_values, contracts, label))
    if embedded_values != current_values:
        errors.append(f"{label} cumulative assertions disagree with the current attestation")
    expected_assertions = {
        "sha256": canonical_json_sha256(embedded_values),
        "values": embedded_values,
    }
    if not isinstance(assertions_value, dict) or set(assertions_value) != set(expected_assertions):
        errors.append(f"{label} has no exact cumulative assertion binding")
    elif assertions_value != expected_assertions:
        errors.append(f"{label} names a different cumulative assertion snapshot")
    return errors


def prior_receipt_errors(
    value: object,
    prior_phase: str,
    prior_path: Path,
    prior_value: dict[str, Any],
    label: str,
) -> list[str]:
    expected = {
        "phase": prior_phase,
        "path": PHASE_RECEIPTS[prior_phase],
        "sha256": sha256(prior_path),
        "receipt_id": prior_value.get("receipt_id"),
    }
    if not isinstance(value, dict) or set(value) != set(expected) or value != expected:
        return [f"{label} has no exact prior-phase receipt binding"]
    return []


def phase_receipt_contract(
    value: dict,
    path: Path,
    phase: str,
    reg: dict,
    package: dict[str, str],
    attested: dict[str, Any],
    attestation_path: Path | None,
    *,
    now: datetime,
    selected: bool,
    prior_phase: str | None = None,
    prior_path: Path | None = None,
    prior_value: dict[str, Any] | None = None,
    prior_valid: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    """Validate one strict human-authored phase receipt and its chain edge."""
    label = f"{phase} receipt"
    common = {
        "schema",
        "receipt_id",
        "recorded_at",
        "signer",
        "package",
        "opportunity",
        "deadline",
        "attestation",
        "assertions",
    }
    event_key = None if phase == "package" else ("upload" if phase == "uploaded" else "submission")
    expected_keys = common | ({"prior_receipt", event_key} if event_key else set())
    errors: list[str] = receipt_schema_errors(value, label)
    if set(value) != expected_keys:
        errors.append(f"{label} has fields outside its typed contract")
    if value.get("schema") != PHASE_RECEIPT_SCHEMAS[phase]:
        errors.append(f"{label} has the wrong schema")
    try:
        receipt_id = typed_id(value.get("receipt_id"), label)
    except ValueError as exc:
        receipt_id = ""
        errors.append(str(exc))
    try:
        recorded_at = receipt_timestamp(value.get("recorded_at"), label)
    except ValueError as exc:
        recorded_at = None
        errors.append(str(exc))
    else:
        if recorded_at > now.astimezone(timezone.utc):
            errors.append(f"{label} is dated in the future")
    errors.extend(signer_errors(value.get("signer"), phase, label))
    errors.extend(package_binding_errors(value.get("package"), package, label))
    errors.extend(opportunity_binding_errors(value.get("opportunity"), reg, label))
    try:
        expected_deadline, upload_target, hard_wall = deadline_binding(reg)
    except ValueError as exc:
        expected_deadline, upload_target, hard_wall = {}, None, None
        errors.append(str(exc))
    if (
        not isinstance(value.get("deadline"), dict)
        or set(value["deadline"]) != set(expected_deadline)
        or value["deadline"] != expected_deadline
    ):
        errors.append(f"{label} names a different deadline contract")
    errors.extend(
        attestation_binding_errors(
            value.get("attestation"),
            value.get("assertions"),
            reg,
            attested,
            attestation_path,
            phase,
            label,
            selected=selected,
        )
    )

    prior_recorded_at = None
    if prior_phase is not None:
        if prior_path is None or prior_value is None:
            errors.append(f"{label} has no readable prior-phase receipt")
        else:
            errors.extend(
                prior_receipt_errors(
                    value.get("prior_receipt"),
                    prior_phase,
                    prior_path,
                    prior_value,
                    label,
                )
            )
            try:
                prior_recorded_at = receipt_timestamp(
                    prior_value.get("recorded_at"),
                    f"{prior_phase} receipt",
                )
            except ValueError:
                errors.append(f"{label} chains from an invalid prior timestamp")
        if not prior_valid:
            errors.append(f"{label} chains from an invalid prior-phase receipt")
        if recorded_at is not None and prior_recorded_at is not None and recorded_at < prior_recorded_at:
            errors.append(f"{label} predates its prior-phase receipt")

    event_at = None
    if phase == "uploaded":
        upload = value.get("upload")
        if not isinstance(upload, dict) or set(upload) != {
            "provider",
            "asset_id",
            "url",
            "uploaded_at",
            "manifest_item",
            "sha256",
        }:
            errors.append("uploaded receipt has no exact destination-safe upload evidence")
        else:
            provider = upload.get("provider")
            if not isinstance(provider, str) or not DESTINATION_ID.fullmatch(provider):
                errors.append("uploaded receipt has no typed provider")
            asset_id = upload.get("asset_id")
            if not isinstance(asset_id, str) or not DESTINATION_ID.fullmatch(asset_id):
                errors.append("uploaded receipt has no typed provider asset identifier")
            try:
                upload_url = https_url(upload.get("url"), "uploaded receipt destination")
            except ValueError as exc:
                upload_url = None
                errors.append(str(exc))
            if upload_url is not None:
                parsed_upload = urlsplit(upload_url)
                if provider != parsed_upload.hostname:
                    errors.append("uploaded receipt provider does not equal its destination host")
                if asset_id not in PurePosixPath(parsed_upload.path).parts:
                    errors.append("uploaded receipt asset identifier is not bound into its destination URL")
            item_name = upload.get("manifest_item")
            item_digest = upload.get("sha256")
            if (
                not isinstance(item_name, str)
                or PurePosixPath(item_name).name != item_name
                or not item_name.startswith("screener.")
                or Path(item_name).suffix.lower() not in VIDEO_SUFFIXES
                or not isinstance(item_digest, str)
                or not HEX64.fullmatch(item_digest)
            ):
                errors.append("uploaded receipt has no exact manifested screener identity")
            else:
                try:
                    manifest, _ = read_contract_json(
                        path.parent.parent,
                        "manifest.json",
                        "uploaded receipt package manifest",
                    )
                except ValueError as exc:
                    errors.append(str(exc))
                else:
                    matches = [
                        item
                        for item in manifest.get("items", [])
                        if isinstance(item, dict) and item.get("name") == item_name
                    ] if isinstance(manifest.get("items"), list) else []
                    if len(matches) != 1 or matches[0].get("sha256") != item_digest:
                        errors.append("uploaded receipt does not bind the exact manifested screener digest")
            try:
                event_at = receipt_timestamp(upload.get("uploaded_at"), "uploaded receipt event")
            except ValueError as exc:
                errors.append(str(exc))
            # A late upload remains truthful chain evidence.  The independent
            # deadline gate below still fails closed unless this timestamp is at
            # or before the canonical 18:00 target.
    elif phase == "submitted":
        submission = value.get("submission")
        if not isinstance(submission, dict) or set(submission) != {
            "portal",
            "confirmation_id",
            "submitted_at",
        }:
            errors.append("submitted receipt has no exact destination-safe filing evidence")
        else:
            try:
                portal = https_url(submission.get("portal"), "submitted receipt portal")
            except ValueError as exc:
                portal = None
                errors.append(str(exc))
            if portal is not None and portal != reg.get("portal"):
                errors.append("submitted receipt names a different filing portal")
            confirmation = submission.get("confirmation_id")
            if not isinstance(confirmation, str) or not DESTINATION_ID.fullmatch(confirmation):
                errors.append("submitted receipt has no typed confirmation identifier")
            try:
                event_at = receipt_timestamp(
                    submission.get("submitted_at"),
                    "submitted receipt event",
                )
            except ValueError as exc:
                errors.append(str(exc))
            # A post-wall filing remains authentic historical evidence.  The
            # independent deadline gate still fails closed unless this event is
            # at or before the conservative 22:00 filing wall.

    if event_at is not None:
        if recorded_at is not None and event_at > recorded_at:
            errors.append(f"{label} was recorded before its claimed event")
        if prior_recorded_at is not None and event_at < prior_recorded_at:
            errors.append(f"{label} event predates its prior-phase receipt")
        if event_at > now.astimezone(timezone.utc):
            errors.append(f"{label} event is dated in the future")
    return {
        "phase": phase,
        "receipt_id": receipt_id,
        "recorded_at": recorded_at,
        "event_at": event_at,
        "path": path,
        "sha256": sha256(path),
    }, errors


def check_phase_receipts(
    reg: dict,
    root: Path,
    phase: str,
    package: dict[str, str],
    attested: dict[str, Any],
    attestation_path: Path | None,
    rep: Report,
    *,
    now: datetime | None = None,
    replace_done_phase: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Require every human receipt through the selected cumulative phase."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    later_receipts: list[str] = []
    for later in PHASES[PHASES.index(phase) + 1 :]:
        for relative in (PHASE_RECEIPTS[later], DONE_RECEIPTS[later]):
            candidate = root / relative
            if candidate.exists() or candidate.is_symlink():
                later_receipts.append(relative)
    if later_receipts:
        rep.add(
            "phase receipts",
            "phase-bounded receipt surface",
            FAIL,
            "later-phase receipts are not valid at this selected phase: "
            + ", ".join(later_receipts),
        )
    try:
        current_attested, current_attestation_path = read_attestations(root)
    except ValueError as exc:
        rep.add("phase receipts", "stable attestation input", FAIL, str(exc))
        current_attested, current_attestation_path = {}, None
    if current_attested != attested or current_attestation_path != attestation_path:
        rep.add(
            "phase receipts",
            "stable attestation input",
            FAIL,
            "attest.yaml changed before receipt validation",
        )
    attested = current_attested
    attestation_path = current_attestation_path
    initial_attestation_digest = sha256(attestation_path) if attestation_path is not None else None
    records: dict[str, dict[str, Any]] = {}
    raw_values: dict[str, dict[str, Any]] = {}
    paths: dict[str, Path] = {}
    digests: dict[str, str] = {}
    valid: dict[str, bool] = {}
    seen_ids: set[str] = set()
    for index, current in enumerate(PHASES[: PHASES.index(phase) + 1]):
        label = f"{current} receipt"
        try:
            value, path = read_contract_json(root, PHASE_RECEIPTS[current], label)
        except ValueError as exc:
            rep.add("phase receipts", label, FAIL, str(exc))
            valid[current] = False
            continue
        prior = PHASES[index - 1] if index else None
        record, errors = phase_receipt_contract(
            value,
            path,
            current,
            reg,
            package,
            attested,
            attestation_path,
            now=now,
            selected=current == phase,
            prior_phase=prior,
            prior_path=paths.get(prior) if prior else None,
            prior_value=raw_values.get(prior) if prior else None,
            prior_valid=valid.get(prior, False) if prior else True,
        )
        if record["receipt_id"] in seen_ids:
            errors.append(f"{label} reuses an earlier receipt identifier")
        elif record["receipt_id"]:
            seen_ids.add(record["receipt_id"])
        raw_values[current] = value
        paths[current] = path
        digests[current] = record["sha256"]
        valid[current] = not errors
        if not errors:
            records[current] = record
        rep.add(
            "phase receipts",
            label,
            FAIL if errors else PASS,
            "; ".join(errors[:6])
            if errors
            else f"{record['receipt_id']} · {record['sha256'][:16]}…",
        )
    changed: list[str] = []
    for current, path in paths.items():
        try:
            if sha256(path) != digests[current]:
                changed.append(current)
        except OSError:
            changed.append(current)
    try:
        final_attested, final_attestation_path = read_attestations(root)
        final_attestation_digest = sha256(final_attestation_path)
    except (ValueError, OSError):
        final_attested, final_attestation_path, final_attestation_digest = None, None, None
    if (
        changed
        or final_attested != attested
        or final_attestation_path != attestation_path
        or final_attestation_digest != initial_attestation_digest
    ):
        detail = (
            f"phase receipt bytes changed: {', '.join(changed)}"
            if changed
            else "attest.yaml changed during receipt validation"
        )
        rep.add("phase receipts", "stable receipt inputs", FAIL, detail)
        records = {}
    for current in PHASES[: PHASES.index(phase) + 1]:
        if current == replace_done_phase:
            continue
        relative = DONE_RECEIPTS[current]
        candidate = root / relative
        if not candidate.exists() and not candidate.is_symlink():
            continue
        label = f"validated-{current} receipt"
        try:
            value, path = read_contract_json(root, relative, label)
        except ValueError as exc:
            local_errors = [str(exc)]
        else:
            local_errors = validate_done_receipt(
                value,
                path,
                root,
                current,
                package,
                {
                    receipt_phase: records[receipt_phase]
                    for receipt_phase in PHASES[: PHASES.index(current) + 1]
                    if receipt_phase in records
                },
                now=now,
            )
        rep.add(
            "phase receipts",
            label,
            FAIL if local_errors else PASS,
            "; ".join(local_errors[:6]) if local_errors else "exact local validation binding",
        )
    return records


def canonical_utc(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def done_receipt_rows(
    phase: str,
    records: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for current in PHASES[: PHASES.index(phase) + 1]:
        record = records.get(current)
        if not record:
            raise ValueError(f"cannot record validation without a valid {current} receipt")
        path = record.get("path")
        if not isinstance(path, Path) or not path.is_file() or path.is_symlink():
            raise ValueError(f"cannot record validation with an unsafe {current} receipt")
        digest = sha256(path)
        if digest != record.get("sha256"):
            raise ValueError(f"{current} receipt changed before validation was recorded")
        rows.append(
            {
                "phase": current,
                "path": PHASE_RECEIPTS[current],
                "sha256": digest,
                "receipt_id": str(record["receipt_id"]),
            }
        )
    return rows


def validate_done_receipt(
    value: dict,
    path: Path,
    root: Path,
    phase: str,
    package: dict[str, str],
    records: dict[str, dict[str, Any]],
    *,
    now: datetime,
) -> list[str]:
    """Validate the machine-only record emitted after done.sh's full batch."""
    label = "local validation receipt"
    errors: list[str] = receipt_schema_errors(value, label)
    if set(value) != {
        "schema",
        "scope",
        "phase",
        "validated_at",
        "repository_head",
        "package",
        "phase_receipts",
        "predicates",
    }:
        errors.append(f"{label} has fields outside its typed contract")
    if value.get("schema") != DONE_RECEIPT_SCHEMA:
        errors.append(f"{label} has the wrong schema")
    if value.get("scope") != DONE_RECEIPT_SCOPE:
        errors.append(f"{label} claims a different validation scope")
    if value.get("phase") != phase:
        errors.append(f"{label} names a different phase")
    try:
        validated_at = receipt_timestamp(value.get("validated_at"), label)
    except ValueError as exc:
        validated_at = None
        errors.append(str(exc))
    else:
        if validated_at > now.astimezone(timezone.utc) + timedelta(seconds=1):
            errors.append(f"{label} is dated in the future")
    if value.get("repository_head") != package.get("repository_head"):
        errors.append(f"{label} names a different repository head")
    errors.extend(package_binding_errors(value.get("package"), package, label))
    fresh_package, _, identity_errors = validate_package_identity(root)
    if identity_errors:
        errors.append(f"{label} package identity is no longer valid: {identity_errors[0]}")
    elif fresh_package != package:
        errors.append(f"{label} package identity changed after phase validation")
    try:
        expected_rows = done_receipt_rows(phase, records)
    except ValueError as exc:
        expected_rows = []
        errors.append(str(exc))
    if value.get("phase_receipts") != expected_rows:
        errors.append(f"{label} does not bind the exact cumulative receipt chain")
    expected_predicates = [command.format(phase=phase) for command in DONE_PREDICATES]
    if value.get("predicates") != expected_predicates:
        errors.append(f"{label} has a different predicate census")
    if path.is_symlink() or not path.is_file():
        errors.append(f"{label} destination is unsafe")
    return errors


def write_done_receipt(
    root: Path,
    phase: str,
    package: dict[str, str],
    records: dict[str, dict[str, Any]],
    *,
    now: datetime | None = None,
) -> tuple[Path, str]:
    """Atomically persist, then re-read, the machine validation record."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("cannot write a validation receipt into an unsafe package root")
    fresh_package, _, identity_errors = validate_package_identity(root)
    if identity_errors:
        raise ValueError(f"cannot record validation for an invalid current package: {identity_errors[0]}")
    if fresh_package != package:
        raise ValueError("cannot record validation after the package identity changed")
    receipt_dir = root / "receipts"
    if receipt_dir.is_symlink() or (receipt_dir.exists() and not receipt_dir.is_dir()):
        raise ValueError("validation receipt directory is unsafe")
    receipt_dir.mkdir(parents=False, exist_ok=True)
    relative = DONE_RECEIPTS[phase]
    destination = root / relative
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise ValueError("validation receipt destination is unsafe")
    payload = {
        "schema": DONE_RECEIPT_SCHEMA,
        "scope": DONE_RECEIPT_SCOPE,
        "phase": phase,
        "validated_at": canonical_utc(now),
        "repository_head": package.get("repository_head"),
        "package": package,
        "phase_receipts": done_receipt_rows(phase, records),
        "predicates": [command.format(phase=phase) for command in DONE_PREDICATES],
    }
    rendered = (json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=receipt_dir,
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(rendered)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
        try:
            directory_fd = os.open(receipt_dir, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass
    value, path = read_contract_json(root, relative, "local validation receipt")
    errors = validate_done_receipt(value, path, root, phase, package, records, now=now)
    if errors:
        raise ValueError("; ".join(errors[:6]))
    return path, sha256(path)


def competition_audio_profile(spec: dict) -> tuple[dict, str, list[str]]:
    """Load the digest-bound usage manifest selected by the submission register."""
    errors: list[str] = []
    reference = spec.get("usage_contract")
    if not isinstance(reference, dict) or set(reference) != {
        "path",
        "sha256",
        "schema",
        "profile",
    }:
        return {}, "", ["submission audio has no typed usage contract"]
    try:
        uses, path = read_contract_json(HERE.parent, reference.get("path"), "audio usage contract")
    except ValueError as exc:
        return {}, "", [str(exc)]
    actual = sha256(path)
    if reference.get("sha256") != actual or not HEX64.fullmatch(str(reference.get("sha256", ""))):
        errors.append("audio usage contract digest is missing or stale")
    if uses.get("schema") != reference.get("schema") or reference.get("schema") != "danse.audio.uses.v1":
        errors.append("audio usage contract schema has drifted")
    profile_id = reference.get("profile")
    if profile_id != uses.get("competition_profile") or profile_id != "competition-classical":
        errors.append("submission does not select the canonical competition-classical profile")
    profiles = uses.get("profiles")
    profile = profiles.get(profile_id) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict):
        errors.append("competition-classical profile is absent")
        profile = {}
    if profile.get("package_eligible") is not True:
        errors.append("competition-classical profile is package-ineligible")
    declared = profile.get("declared_sources")
    required_stems = profile.get("required_stems")
    forbidden = profile.get("forbidden_source_kinds")
    if (
        not isinstance(declared, list)
        or not all(
            isinstance(row, dict)
            and isinstance(row.get("id"), str)
            and isinstance(row.get("kind"), str)
            for row in declared
        )
        or not isinstance(required_stems, list)
        or not all(isinstance(value, str) for value in required_stems)
        or not isinstance(forbidden, list)
        or not all(isinstance(value, str) for value in forbidden)
    ):
        errors.append("competition-classical sources, stems, or forbidden kinds are malformed")
    elif {row["kind"] for row in declared} & set(forbidden):
        errors.append("competition-classical profile admits a forbidden source kind")
    hybrid = spec.get("hybrid_apartment")
    hybrid_profile = profiles.get("hybrid-apartment") if isinstance(profiles, dict) else None
    if (
        not isinstance(hybrid, dict)
        or hybrid.get("profile") != "hybrid-apartment"
        or hybrid.get("package_eligible") is not False
        or not isinstance(hybrid_profile, dict)
        or hybrid_profile.get("package_eligible") is not False
    ):
        errors.append("hybrid-apartment must remain explicitly package-ineligible")
    return profile, actual, errors


def competition_sound_errors(
    sound: object,
    spec: dict,
    profile: dict,
    audio_uses_sha256: str,
) -> list[str]:
    """Validate the full competition sound identity without accepting aliases."""
    if not isinstance(sound, dict):
        return ["manifest has no typed competition sound identity"]
    errors: list[str] = []
    if set(sound) != set(AUDIO_SOUND_FIELDS):
        errors.append("sound identity has fields outside its typed contract")
    if sound.get("profile") != "competition-classical":
        errors.append("sound identity selects a package-ineligible or unknown profile")
    for field in AUDIO_IDENTITY_HASH_FIELDS:
        if not isinstance(sound.get(field), str) or not HEX64.fullmatch(sound[field]):
            errors.append(f"sound identity has no exact {field}")
    if sound.get("audio_uses_sha256") != audio_uses_sha256:
        errors.append("sound identity names a different audio-use contract")
    declared = profile.get("declared_sources") if isinstance(profile, dict) else None
    expected_sources = [row.get("id") for row in declared] if isinstance(declared, list) else []
    if sound.get("sources") != expected_sources:
        errors.append("sound identity does not name the declared competition sources")
    by_id = {
        row.get("id"): row
        for row in declared or []
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    if sound.get("midi_sha256") != (by_id.get("delibes-chamber-midi") or {}).get("sha256"):
        errors.append("sound identity names a different adapted MIDI")
    if sound.get("soundfont_sha256") != (by_id.get("musescore-general-sf3") or {}).get("sha256"):
        errors.append("sound identity names a different soundfont")
    stems = sound.get("stems")
    expected_stems = profile.get("required_stems") if isinstance(profile, dict) else None
    if not isinstance(stems, list) or not isinstance(expected_stems, list) or len(stems) != len(expected_stems):
        errors.append("sound identity has no exact stem census")
    else:
        for stem, expected_id in zip(stems, expected_stems, strict=True):
            if (
                not isinstance(stem, dict)
                or set(stem) != {"id", "sha256"}
                or stem.get("id") != expected_id
                or not isinstance(stem.get("sha256"), str)
                or not HEX64.fullmatch(stem["sha256"])
            ):
                errors.append("sound identity has a malformed or reordered stem")
                break
    if sound.get("credit") != spec.get("credit"):
        errors.append("sound identity does not carry the exact approved Delibes credit")
    return errors


def copied_score_receipt(root: Path, manifest: dict, spec: dict) -> tuple[dict, list[str]]:
    """Resolve the one copied v2 score receipt through production.json."""
    errors: list[str] = []
    reference = manifest.get("production")
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        return {}, ["manifest has no exact production receipt reference"]
    if reference.get("path") != spec.get("production_receipt"):
        errors.append("manifest names a noncanonical production receipt")
    try:
        production, production_path = read_contract_json(
            root,
            reference.get("path"),
            "package production receipt",
        )
    except ValueError as exc:
        return {}, [*errors, str(exc)]
    if reference.get("sha256") != sha256(production_path):
        errors.append("package production receipt digest is stale")
    repository_head = manifest.get("repository_head")
    if not isinstance(repository_head, str) or not GIT_OID.fullmatch(repository_head):
        errors.append("package manifest has no exact repository head")
    if production.get("repository_head") != repository_head:
        errors.append("package production receipt names a different repository head")
    if production.get("sound") != manifest.get("sound"):
        errors.append("package production receipt does not equal manifest.sound")
    producers = production.get("producers")
    score_rows = [
        row
        for row in producers or []
        if isinstance(row, dict) and row.get("kind") == "score"
    ]
    if not isinstance(producers, list) or len(score_rows) != 1:
        return {}, [*errors, "production receipt does not name exactly one score producer"]
    receipt_reference = score_rows[0].get("receipt")
    if not isinstance(receipt_reference, dict) or set(receipt_reference) != {"path", "sha256"}:
        return {}, [*errors, "score producer has no exact copied receipt"]
    relative = receipt_reference.get("path")
    if not isinstance(relative, str) or not relative.startswith("provenance/producer-receipts/"):
        errors.append("score producer receipt is outside its package boundary")
    try:
        receipt, receipt_path = read_contract_json(root, relative, "copied score receipt")
    except ValueError as exc:
        return {}, [*errors, str(exc)]
    if receipt_reference.get("sha256") != sha256(receipt_path):
        errors.append("copied score receipt digest is stale")
    return receipt, errors


def durable_audio_render_receipt_errors(
    root: Path,
    manifest: dict,
    spec: dict,
    items: dict[str, dict],
) -> list[str]:
    """Authenticate the package copy of the otherwise ignored render receipt."""
    errors: list[str] = []
    relative = spec.get("audio_render_receipt")
    try:
        receipt, path = read_contract_json(root, relative, "packaged audio-render receipt")
    except ValueError as exc:
        return [str(exc)]
    item = items.get(relative) if isinstance(relative, str) else None
    if not isinstance(item, dict):
        return ["audio-render receipt is absent from the manifest"]
    actual = sha256(path)
    if item.get("sha256") != actual:
        errors.append("audio-render receipt manifest digest is stale")
    if item.get("bytes") != path.stat().st_size:
        errors.append("audio-render receipt manifest byte count is stale")
    manifest_sound = manifest.get("sound")
    expected = (
        manifest_sound.get("audio_render_receipt_sha256")
        if isinstance(manifest_sound, dict)
        else None
    )
    if item.get("sha256") != expected:
        errors.append("audio-render receipt does not equal manifest.sound identity")
    if receipt.get("schema") != "danse.audio.render.v1":
        errors.append("packaged audio-render receipt has the wrong schema")
    return errors


# ── register-level checks (no package needed) ──────────────────────────────────


def check_deadline(
    reg: dict,
    phase: str,
    rep: Report,
    now: datetime | None = None,
    receipts: dict[str, dict[str, Any]] | None = None,
) -> None:
    d = reg.get("deadline")
    if not isinstance(d, dict):
        rep.add("deadline", "contract", FAIL, "canonical deadline mapping is missing")
        return
    try:
        zone = ZoneInfo(reg["opportunity_snapshot"]["timezone"])
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        rep.add("deadline", "timezone", FAIL, "canonical named timezone is missing or unavailable")
        return
    try:
        _, target, wall = deadline_binding(reg)
    except ValueError as exc:
        rep.add("deadline", "contract", FAIL, str(exc))
        return
    now = now or datetime.now(zone)
    if now.tzinfo is None or now.utcoffset() is None:
        rep.add("deadline", "clock", FAIL, "validation clock has no UTC offset")
        return
    now = now.astimezone(zone)
    receipts = receipts or {}
    submitted_at = (receipts.get("submitted") or {}).get("event_at")
    uploaded_at = (receipts.get("uploaded") or {}).get("event_at")

    wall_left = (wall - now).total_seconds() / 86400
    if wall_left < 0:
        timely = isinstance(submitted_at, datetime) and submitted_at <= wall
        rep.add(
            "deadline",
            "hard wall",
            PASS if timely else FAIL,
            f"passed {abs(wall_left):.1f} days ago; "
            + (
                f"submitted receipt proves filing at {submitted_at.astimezone(zone):%a %d %b %H:%M %Z}"
                if timely
                else "no timely submitted receipt closes the historical filing gate"
            ),
        )
    else:
        rep.add(
            "deadline",
            "hard wall",
            PASS,
            f"{wall_left:.1f} days left → {wall.astimezone(zone):%a %d %b %H:%M %Z}",
        )

    target_left = (target - now).total_seconds() / 86400
    if target_left < 0:
        timely = isinstance(uploaded_at, datetime) and uploaded_at <= target
        status = PASS if timely else FAIL
        detail = (
            f"uploaded receipt proves completion at {uploaded_at.astimezone(zone):%a %d %b %H:%M %Z}"
            if timely
            else "no timely uploaded receipt closes the elapsed target"
        )
    else:
        status = PASS
        detail = "upload early; the panel sees timestamps"
    rep.add(
        "deadline",
        "upload target",
        status,
        f"{target.astimezone(zone):%a %d %b %H:%M %Z} ({target_left:+.1f} days) — {detail}",
    )


def check_unknowns(reg: dict, rep: Report) -> None:
    """The call is silent on these. Blocking ones exit non-zero; the rest report
    what stands in for the missing answer — evidence where we found some, a bare
    assumption where we did not — so the two never read alike."""
    for item in reg.get("unstated", []):
        if item.get("blocking", False):
            rep.add("unpublished by the call", item["id"], OPEN, item["resolve"])
            continue
        detail = (
            f"de-blocked by evidence — {item['evidence']}"
            if "evidence" in item
            else f"assuming {item.get('assume', item.get('assume_master', 'default'))}"
        )
        rep.add("unpublished by the call", item["id"], SKIP, detail)


# ── package checks ─────────────────────────────────────────────────────────────


def register_structure_errors(reg: dict) -> list[str]:
    """Describe register shapes that downstream checks cannot interpret."""
    errors: list[str] = []
    for field in ("call", "presenter", "portal"):
        if not isinstance(reg.get(field), str) or not reg[field]:
            errors.append(f"{field} must be a non-empty string")
    for section in OWNED_SECTIONS:
        rows = reg.get(section)
        if not isinstance(rows, list):
            errors.append(f"{section} must be a list")
            continue
        errors.extend(
            f"{section}[{index}] must be a mapping"
            for index, item in enumerate(rows)
            if not isinstance(item, dict)
        )

    for field in ("deadline", "opportunity_snapshot", "package"):
        if not isinstance(reg.get(field), dict):
            errors.append(f"{field} must be a mapping")

    package = reg.get("package")
    if isinstance(package, dict):
        package_sections = (
            "master",
            "screener",
            "stills",
            "origin_still",
            "trailer",
            "audio",
            "text",
        )
        for field in package_sections:
            if not isinstance(package.get(field), dict):
                errors.append(f"package.{field} must be a mapping")
    unstated = reg.get("unstated")
    if not isinstance(unstated, list):
        errors.append("unstated must be a list")
    elif not all(isinstance(item, dict) for item in unstated):
        errors.append("unstated rows must be mappings")
    return errors


def check_requirement_phases(reg: dict, rep: Report) -> None:
    rep.add(
        "register",
        "schema",
        PASS if reg.get("schema") == "danse.submission.v2" else FAIL,
        str(reg.get("schema")),
    )
    structure_errors = register_structure_errors(reg)
    raw_owned = [
        item
        for section in OWNED_SECTIONS
        for item in (reg.get(section) if isinstance(reg.get(section), list) else [])
    ]
    owned = [item for item in raw_owned if isinstance(item, dict)]
    invalid = [
        str(item.get("id", "<unnamed>"))
        for item in owned
        if item.get("phase") not in PHASES
    ]
    invalid.extend("<non-record>" for item in raw_owned if not isinstance(item, dict))
    invalid.extend(structure_errors)
    rep.add(
        "register",
        "requirement phase ownership",
        PASS if not invalid else FAIL,
        f"{len(owned)} owned requirements and approvals"
        if not invalid
        else f"missing/invalid phase: {', '.join(invalid)}",
    )
    assertion_ids = [
        item.get("id")
        for item in owned
        if item.get("check") in {"manual", "choice"}
    ]
    malformed_ids = [
        value
        for value in assertion_ids
        if not isinstance(value, str) or not value or value != value.strip()
    ]
    duplicate_ids = sorted(
        {
            value
            for value in assertion_ids
            if isinstance(value, str) and assertion_ids.count(value) > 1
        }
    )
    rep.add(
        "register",
        "unique assertion identity census",
        PASS if not malformed_ids and not duplicate_ids else FAIL,
        f"{len(assertion_ids)} unique typed assertion(s)"
        if not malformed_ids and not duplicate_ids
        else "malformed or duplicate assertion ids: "
        + ", ".join([*(str(value) for value in malformed_ids[:3]), *duplicate_ids[:3]]),
    )
    terms = reg.get("terms") if isinstance(reg.get("terms"), list) else []
    term_errors = [error for error in structure_errors if error.startswith("terms")]
    for item in terms:
        if not isinstance(item, dict):
            continue
        name = item.get("id", "<unnamed>")
        source = item.get("source")
        if item.get("status") != "verified" or not item.get("checked") or not (
            isinstance(source, str) and source.startswith("https://")
        ):
            term_errors.append(f"{name}: provenance")
        check_kind = item.get("check")
        values = item.get("values")
        if check_kind == "choice":
            if not (
                isinstance(values, list)
                and len(values) >= 2
                and all(isinstance(value, str) and value for value in values)
                and len(values) == len(set(values))
            ):
                term_errors.append(f"{name}: choices")
        elif check_kind != "manual":
            term_errors.append(f"{name}: check")
    rep.add(
        "register",
        "published term provenance and choice contract",
        PASS if not term_errors else FAIL,
        f"{len(terms)} source-verified term(s)"
        if not term_errors
        else "; ".join(term_errors),
    )

    package_errors: list[str] = []
    package = reg.get("package")
    if not isinstance(package, dict):
        package_errors.append("package contract is missing")
        audio = {}
    else:
        if package.get("classification") != "internal-delivery-spec":
            package_errors.append("delivery specifications are not labelled internal")
        if package.get("published_requirement") is not False:
            package_errors.append("delivery specifications are misrepresented as published")
        note = package.get("note")
        if not isinstance(note, str) or "does not publish" not in note:
            package_errors.append("delivery specification provenance note is missing")
        audio = package.get("audio") if isinstance(package.get("audio"), dict) else {}
    _, _, audio_errors = competition_audio_profile(audio)
    package_errors.extend(audio_errors)
    rep.add(
        "register",
        "internal delivery and audio-use contract",
        PASS if not package_errors else FAIL,
        "internal delivery targets · exact competition audio-use digest"
        if not package_errors
        else "; ".join(package_errors),
    )


def check_opportunity_snapshot(register_path: Path, rep: Report) -> None:
    """Bind filing facts to the exact source-verified release snapshot.

    The opportunity checker owns the schema, source census, digest receipt, and
    issue #2/#12 consumer contract. Importing it here keeps those rules in one
    executable home while making every submission phase fail closed on drift.
    """
    try:
        spec = importlib.util.spec_from_file_location(
            "danse_opportunity_checker", OPPORTUNITY_CHECKER
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("checker module could not be loaded")
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        snapshot, receipt = checker.validate_all(consumer_path=register_path)
    except Exception as exc:
        rep.add("register", "frozen opportunity snapshot", FAIL, str(exc))
        return
    rep.add(
        "register",
        "frozen opportunity snapshot",
        PASS,
        f"{snapshot['snapshot_id']} · {receipt['snapshot']['sha256'][:16]}… · issue #2 bound / #12 pending",
    )


def check_rights(package: Path, phase: str, rep: Report) -> None:
    """Require the exact redacted issue-16 contract for every staged phase."""
    try:
        spec = importlib.util.spec_from_file_location("danse_rights_checker", RIGHTS_CHECKER)
        if spec is None or spec.loader is None:
            raise RuntimeError("checker module could not be loaded")
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        _, receipt = checker.validate_all(phase=phase, package=package)
    except Exception as exc:
        # Exceptions may carry a caller-owned package or machine-local path.
        # The detailed diagnostic remains available from the local rights CLI;
        # the submission report itself is a public-safe receipt surface.
        rep.add(
            "rights",
            "redacted exact-manifest contract",
            FAIL,
            f"rights validation failed ({type(exc).__name__}); run scripts/check-rights.py locally",
        )
        return
    blockers = receipt["blockers"]
    detail = (
        f"{receipt['inventory']['assets']} assets · register {receipt['register']['sha256'][:16]}…"
        if not blockers
        else f"{len(blockers)} blocker(s): " + "; ".join(blockers[:3])
    )
    rep.add("rights", "redacted exact-manifest contract", PASS if not blockers else FAIL, detail)


def check_attestations(
    reg: dict,
    root: Path,
    phase: str,
    rep: Report,
) -> tuple[dict[str, Any], Path | None]:
    try:
        attested, path = read_attestations(root)
    except ValueError as exc:
        rep.add("attested through " + phase, "attest.yaml contract", FAIL, str(exc))
        return {}, None
    initial_digest = sha256(path)
    selected = PHASES.index(phase)
    for req in [item for section in OWNED_SECTIONS for item in reg.get(section, [])]:
        owner = req.get("phase")
        check_kind = req.get("check")
        if check_kind not in ("manual", "choice") or owner not in PHASES or PHASES.index(owner) > selected:
            continue
        value = attested.get(req["id"])
        if check_kind == "choice" and value in req.get("values", []):
            rep.add(f"attested through {phase}", req["id"], PASS, f"{req['rule']} — {value}")
        elif check_kind == "choice":
            choices = ", ".join(req.get("values", []))
            rep.add(
                f"attested through {phase}",
                req["id"],
                FAIL,
                f"choose exactly one of [{choices}] in attest.yaml — {req['rule']}",
            )
        elif value is True:
            rep.add(f"attested through {phase}", req["id"], PASS, req["rule"])
        elif value is False:
            rep.add(f"attested through {phase}", req["id"], FAIL, req["rule"])
        else:
            rep.add(
                f"attested through {phase}",
                req["id"],
                FAIL,
                f"unattested in attest.yaml (owned by {owner}) — {req['rule']}",
            )
    try:
        final, final_path = read_attestations(root)
    except ValueError:
        final, final_path = None, None
    if final != attested or final_path != path or sha256(path) != initial_digest:
        rep.add(
            f"attested through {phase}",
            "stable attestation snapshot",
            FAIL,
            "attest.yaml changed during validation",
        )
        return {}, None
    return attested, path


def check_master(spec: dict, reg: dict, root: Path, rep: Report) -> None:
    path = find_one(root, "master")
    if not path:
        rep.add("package", "master", FAIL, "no unique master.<mov|mp4|mxf> in package")
        return
    info = probe(path)
    if not info:
        rep.add("package", "master", OPEN, f"{path.name} present; ffprobe unavailable — cannot verify")
        return

    w, h, fps, secs = info["width"], info["height"], info["fps"], info["seconds"]
    rep.add("package", "master present", PASS, f"{path.name} · {w}×{h} · {fps}fps · {secs / 60:.2f} min")

    ratio = (w / h) if h else 0
    want = 16 / 9
    ok_aspect = abs(ratio - want) < 0.01
    rep.add("package", "aspect 16:9", PASS if ok_aspect else FAIL, f"{ratio:.4f}")

    ok_fps = any(abs(fps - f) < 0.5 for f in spec["fps_allowed"])
    rep.add("package", "frame rate", PASS if ok_fps else FAIL, f"{fps} — allowed {spec['fps_allowed']}")

    ok_size = (w or 0) >= spec["min_width"] and (h or 0) >= spec["min_height"]
    rep.add(
        "package",
        "master resolution",
        PASS if ok_size else FAIL,
        f"{w}×{h} (min {spec['min_width']}×{spec['min_height']})",
    )
    ok_codec = (
        info["vcodec"] == spec["video_codec"] and str(info["vprofile"]).lower() == str(spec["video_profile"]).lower()
    )
    rep.add(
        "package",
        "master codec",
        PASS if ok_codec else FAIL,
        f"{info['vcodec']} {info['vprofile']} (want {spec['video_codec']} {spec['video_profile']})",
    )
    ok_audio = info["acodec"] == spec["audio_codec"] and info["channels"] == spec["audio_channels"]
    rep.add(
        "package",
        "master audio stream",
        PASS if ok_audio else FAIL,
        f"{info['acodec']} · {info['channels']} channels",
    )

    manifest = read_manifest(root)
    item = manifest_items(root).get(path.name) or {}
    expected_seconds = manifest.get("duration")
    duration_matches = (
        isinstance(expected_seconds, (int, float))
        and not isinstance(expected_seconds, bool)
        and fps > 0
        and abs(secs - expected_seconds) * fps <= 2
    )
    rep.add(
        "package",
        "master is one whole manifested passage",
        PASS if duration_matches else FAIL,
        f"{secs:.3f}s staged vs {expected_seconds!r}s manifested",
    )
    actual_digest = sha256(path)
    digest_matches = item.get("sha256") == actual_digest
    rep.add(
        "package",
        "master bytes match delivery manifest",
        PASS if digest_matches else FAIL,
        f"{actual_digest[:16]}…" + ("" if digest_matches else " — missing or stale manifest digest"),
    )

    cap = next((u.get("assume_max_seconds") for u in reg.get("unstated", []) if u["id"] == "runtime-cap"), None)
    if cap:
        # OPEN, not PASS: the cap is our assumption, not the festival's stated rule.
        status = OPEN if secs > cap else PASS
        rep.add(
            "package",
            "runtime vs assumed cap",
            status,
            f"{secs:.0f}s vs assumed {cap}s — cap is UNCONFIRMED, call {reg['phone']}",
        )


def check_screener(spec: dict, root: Path, rep: Report) -> None:
    path = find_one(root, "screener")
    if not path:
        rep.add("package", "screener", FAIL, "no unique screener.<mov|mp4> in package")
        return
    manifest = read_manifest(root)
    item = manifest_items(root).get(path.name) or {}
    actual_digest = sha256(path)
    digest_matches = item.get("sha256") == actual_digest
    rep.add(
        "package",
        "screener bytes match delivery manifest",
        PASS if digest_matches else FAIL,
        f"{actual_digest[:16]}…" + ("" if digest_matches else " — missing or stale manifest digest"),
    )
    info = probe(path)
    if not info:
        rep.add("package", "screener", OPEN, f"{path.name} present; ffprobe unavailable")
        return
    ok = (info["width"] or 0) >= spec["min_width"] and (info["height"] or 0) >= spec["min_height"]
    rep.add(
        "package",
        "screener",
        PASS if ok else FAIL,
        f"{path.name} · {info['width']}×{info['height']} (min {spec['min_width']}×{spec['min_height']})",
    )
    ok_codec = info["vcodec"] == spec["video_codec"]
    rep.add(
        "package",
        "screener codec",
        PASS if ok_codec else FAIL,
        f"{info['vcodec']} (want {spec['video_codec']})",
    )
    ok_audio = info["acodec"] == spec["audio_codec"] and info["channels"] == spec["audio_channels"]
    rep.add(
        "package",
        "screener audio stream",
        PASS if ok_audio else FAIL,
        f"{info['acodec']} · {info['channels']} channels",
    )
    expected_seconds = manifest.get("duration")
    duration_matches = isinstance(expected_seconds, (int, float)) and abs(info["seconds"] - expected_seconds) <= 0.1
    rep.add(
        "package",
        "screener is one whole manifested passage",
        PASS if duration_matches else FAIL,
        f"{info['seconds']:.3f}s staged vs {expected_seconds!r}s manifested",
    )


def check_stills(spec: dict, root: Path, rep: Report, exempt: set[str] = frozenset()) -> None:
    folder = root / "stills"
    if not folder.is_dir():
        rep.add("package", "stills", FAIL, "no stills/ directory")
        return

    pattern = re.compile(spec["filename_pattern"])
    files = sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith("."))
    named = [p for p in files if pattern.match(p.name)]
    # The origin photograph lives here too and is checked by name elsewhere; it is
    # not a seed still and must not read as a naming violation.
    misnamed = [p.name for p in files if not pattern.match(p.name) and p.name not in exempt]

    ok_count = len(named) >= spec["count_min"]
    rep.add(
        "package",
        "stills count",
        PASS if ok_count else FAIL,
        f"{len(named)} conforming of {len(files)} (min {spec['count_min']})"
        + (f"; misnamed: {', '.join(misnamed[:4])}" if misnamed else ""),
    )

    if spec.get("distinct_seeds"):
        seeds = {p.stem.lower() for p in named}
        ok = len(seeds) == len(named)
        rep.add("package", "stills distinct seeds", PASS if ok else FAIL, f"{len(seeds)} distinct of {len(named)}")

    manifested = manifest_items(root)
    stale = [p.name for p in named if (manifested.get(f"stills/{p.name}") or {}).get("sha256") != sha256(p)]
    rep.add(
        "package",
        "stills bytes match delivery manifest",
        FAIL if stale else PASS,
        "; ".join(stale[:4]) if stale else f"{len(named)} seed still receipt(s) match",
    )

    undersized = []
    unmeasured = 0
    for p in named:
        size = image_size(p)
        if size is None:
            unmeasured += 1
        elif size[0] < spec["min_width"] or size[1] < spec["min_height"]:
            undersized.append(f"{p.name} {size[0]}×{size[1]}")
    if unmeasured:
        rep.add("package", "stills resolution", OPEN, f"{unmeasured} unmeasurable (Pillow missing?)")
    else:
        rep.add(
            "package",
            "stills resolution",
            FAIL if undersized else PASS,
            "; ".join(undersized[:4]) if undersized else f"all ≥ {spec['min_width']}×{spec['min_height']}",
        )


def check_origin_still(spec: dict, root: Path, rep: Report) -> None:
    path = root / "stills" / spec["filename"]
    exists = (
        root.is_dir()
        and not root.is_symlink()
        and path.parent.is_dir()
        and not path.parent.is_symlink()
        and path.is_file()
        and not path.is_symlink()
    )
    rep.add(
        "package",
        "unaltered 2017 photograph",
        PASS if exists else FAIL,
        f"stills/{spec['filename']}" + ("" if exists else " — missing"),
    )
    if not exists:
        return
    item = manifest_items(root).get(f"stills/{spec['filename']}") or {}
    actual = sha256(path)
    registered = spec.get("source_sha256")
    copied = (
        isinstance(registered, str)
        and bool(re.fullmatch(r"[0-9a-fA-F]{64}", registered))
        and actual == registered.lower()
        and item.get("source") == spec["source_filename"]
        and item.get("copy_mode") == spec["copy_mode"]
        and item.get("sha256") == actual
        and item.get("source_sha256") == registered.lower()
    )
    rep.add(
        "package",
        "origin is byte-identical to its registered source",
        PASS if copied else FAIL,
        f"{item.get('source', 'unrecorded')} · {actual[:16]}…",
    )


def check_trailer(spec: dict, root: Path, rep: Report) -> None:
    path = find_one(root, "trailer")
    if not path:
        rep.add("package", "trailer", SKIP, "optional, not staged")
        return
    info = probe(path)
    if not info:
        rep.add("package", "trailer", OPEN, "present; ffprobe unavailable")
        return
    ok = info["seconds"] <= spec["max_seconds"]
    rep.add("package", "trailer", PASS if ok else FAIL, f"{info['seconds']:.0f}s (max {spec['max_seconds']}s)")


def check_audio(spec: dict, root: Path, rep: Report) -> None:
    master = find_one(root, "master")
    measured = loudness(master) if master else None
    if master is None:
        rep.add("audio", "loudness", OPEN, "master audio artifact not staged")
    elif measured is None:
        rep.add("audio", "loudness", OPEN, "ffmpeg loudnorm measurement unavailable")
    else:
        delta = abs(measured["lufs"] - spec["target_lufs"])
        rep.add(
            "audio",
            "integrated loudness",
            PASS if delta <= spec["tolerance_lu"] else FAIL,
            f"{measured['lufs']:.2f} LUFS (target {spec['target_lufs']:.1f} ± {spec['tolerance_lu']:.1f})",
        )
        rep.add(
            "audio",
            "true peak",
            PASS if measured["true_peak_dbtp"] <= spec["max_true_peak_dbtp"] else FAIL,
            f"{measured['true_peak_dbtp']:.2f} dBTP (max {spec['max_true_peak_dbtp']:.1f})",
        )

    manifest = read_manifest(root)
    profile, audio_uses_digest, profile_errors = competition_audio_profile(spec)
    expected_sources = [
        row.get("id")
        for row in profile.get("declared_sources", [])
        if isinstance(row, dict)
    ]
    rep.add(
        "audio",
        "package-eligible competition-classical usage profile",
        FAIL if profile_errors else PASS,
        "; ".join(profile_errors)
        if profile_errors
        else f"{len(expected_sources)} declared sources · {len(profile.get('required_stems', []))} required stems",
    )

    manifest_sound = manifest.get("sound")
    identity_errors = competition_sound_errors(
        manifest_sound,
        spec,
        profile,
        audio_uses_digest,
    )
    items = manifest_items(root)
    audio_paths = [
        path
        for stem in ("master", "midnight-moment", "trailer", "screener", "reel")
        if (path := find_one(root, stem))
    ]
    surface_errors: list[str] = list(identity_errors)
    if not audio_paths:
        surface_errors.append("no audio artifact staged")
    for path in audio_paths:
        item = items.get(path.name)
        if not isinstance(item, dict):
            surface_errors.append(f"{path.name} is absent from the manifest")
            continue
        if item.get("sound") != manifest_sound:
            surface_errors.append(f"{path.name} has a different sound identity")
        if item.get("sha256") != sha256(path):
            surface_errors.append(f"{path.name} digest is stale")
        if path.stem == "screener":
            info = probe(path)
            passage_seconds = manifest.get("duration")
            if not (
                info
                and isinstance(passage_seconds, (int, float))
                and not isinstance(passage_seconds, bool)
                and abs(info.get("seconds", -1) - passage_seconds) <= 0.1
            ):
                surface_errors.append(f"{path.name} passage duration is stale")

    score_relative = spec.get("score_source")
    score_item = items.get(score_relative) if isinstance(score_relative, str) else None
    try:
        score_path = safe_contract_file(root, score_relative, "manifested score source")
    except ValueError as exc:
        score_path = None
        surface_errors.append(str(exc))
    if not isinstance(score_item, dict):
        surface_errors.append("score source is absent from the manifest")
    else:
        if score_item.get("sound") != manifest_sound:
            surface_errors.append("score source has a different sound identity")
        if score_path is not None and score_item.get("sha256") != sha256(score_path):
            surface_errors.append("score source manifest digest is stale")
        if isinstance(manifest_sound, dict) and (
            score_item.get("sha256") != manifest_sound.get("master_sha256")
        ):
            surface_errors.append("score source does not equal the rendered audio master")
    rep.add(
        "audio",
        "identical timed-audio sound identity",
        FAIL if surface_errors else PASS,
        "; ".join(surface_errors[:6])
        if surface_errors
        else f"{len(audio_paths) + 1} timed artifact(s) share one full identity",
    )

    audio_render_errors = durable_audio_render_receipt_errors(
        root,
        manifest,
        spec,
        items,
    )
    rep.add(
        "audio",
        "durable audio-render receipt identity",
        FAIL if audio_render_errors else PASS,
        "; ".join(audio_render_errors[:6])
        if audio_render_errors
        else f"{spec.get('audio_render_receipt')} · exact manifested bytes",
    )

    receipt, receipt_errors = copied_score_receipt(root, manifest, spec)
    expected_receipt_fields = {
        "schema",
        "sha256",
        "t0",
        "t1",
        "duration",
        *AUDIO_SOUND_FIELDS,
    }
    if set(receipt) != expected_receipt_fields:
        receipt_errors.append("copied score receipt has fields outside its v2 contract")
    if receipt.get("schema") != spec.get("score_receipt_schema"):
        receipt_errors.append("copied score receipt has the wrong schema")
    receipt_sound = {field: receipt.get(field) for field in AUDIO_SOUND_FIELDS}
    if receipt_sound != manifest_sound:
        receipt_errors.append("copied score receipt does not equal manifest.sound")
    sound_master_sha256 = (
        manifest_sound.get("master_sha256") if isinstance(manifest_sound, dict) else None
    )
    if not isinstance(sound_master_sha256, str) or len(sound_master_sha256) != 64:
        receipt_errors.append("package manifest sound has no valid master_sha256")
    if receipt.get("sha256") != sound_master_sha256:
        receipt_errors.append("copied score receipt does not bind the rendered master WAV")
    for field in ("t0", "t1", "duration"):
        if receipt.get(field) != manifest.get(field):
            receipt_errors.append(f"copied score receipt has a different {field}")
    rep.add(
        "audio",
        "copied score receipt v2 identity",
        FAIL if receipt_errors else PASS,
        "; ".join(receipt_errors[:6])
        if receipt_errors
        else f"{spec.get('score_receipt_schema')} · master {sound_master_sha256[:16]}…",
    )


def check_score_motion(root: Path, rep: Report) -> None:
    """Require machine-bound production A/B proof without implying acceptance."""
    try:
        spec = importlib.util.spec_from_file_location(
            "danse_submission_score_motion_contract",
            SCORE_MOTION_CHECKER,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("score-to-motion contract loader is unavailable")
        contract = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(contract)
        manifest = read_manifest(root)
        errors = contract.packaged_receipt_errors(root, manifest, schema_root=HERE.parent)
    except Exception as exc:
        rep.add(
            "score-to-motion",
            "production A/B machine evidence",
            FAIL,
            f"validation failed ({type(exc).__name__}); human acceptance remains a separate attestation",
        )
        return
    rep.add(
        "score-to-motion",
        "production A/B machine evidence",
        FAIL if errors else PASS,
        "; ".join(errors[:6])
        if errors
        else "exact score, choreography, span, audio PCM, renderer, and Git HEAD · human review not attested",
    )


def check_text(spec: dict, root: Path, rep: Report) -> None:
    folder = root / "text"
    for name, rule in spec.items():
        path = folder / f"{name}.txt"
        if not path.exists():
            rep.add("text", name, FAIL if rule.get("required") else SKIP, f"text/{name}.txt missing")
            continue
        n = words(path)
        lo, hi = rule["words_min"], rule["words_max"]
        rep.add("text", name, PASS if lo <= n <= hi else FAIL, f"{n} words (want {lo}–{hi})")


# ── entry ──────────────────────────────────────────────────────────────────────


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--package", type=Path, help="staged submission directory")
    ap.add_argument("--register", type=Path, default=REGISTER)
    ap.add_argument("--phase", choices=PHASES, default="package", help="cumulative delivery phase to validate")
    ap.add_argument(
        "--write-done-receipt",
        action="store_true",
        help="after every predicate passes, atomically record this machine-only validation",
    )
    args = ap.parse_args()

    try:
        reg = yaml.load(args.register.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        print(f"submission register is not readable unique-key YAML: {type(exc).__name__}", file=sys.stderr)
        return 1
    if not isinstance(reg, dict):
        print("submission register is not a mapping", file=sys.stderr)
        return 1
    structure_errors = register_structure_errors(reg)
    if structure_errors:
        print(
            "submission register has malformed structure: "
            + "; ".join(structure_errors[:8]),
            file=sys.stderr,
        )
        return 1
    rep = Report()
    root: Path | None = None
    package_binding: dict[str, str] = {}
    receipts: dict[str, dict[str, Any]] = {}

    print(f"\033[1m{reg['call']}\033[0m — {reg['presenter']}")

    check_requirement_phases(reg, rep)
    check_opportunity_snapshot(args.register, rep)

    if args.package:
        candidate = args.package
        if candidate.is_symlink() or not candidate.is_dir():
            rep.add("package", "directory", FAIL, f"{candidate} does not exist or is unsafe")
        else:
            root = candidate
            package_binding, _ = check_package_identity(root, rep)
            attested, attestation_path = check_attestations(reg, root, args.phase, rep)
            receipts = check_phase_receipts(
                reg,
                root,
                args.phase,
                package_binding,
                attested,
                attestation_path,
                rep,
                replace_done_phase=args.phase if args.write_done_receipt else None,
            )

    check_deadline(reg, args.phase, rep, receipts=receipts)
    check_unknowns(reg, rep)

    if root is not None:
        pkg = reg["package"]
        check_master(pkg["master"], reg, root, rep)
        check_screener(pkg["screener"], root, rep)
        check_stills(pkg["stills"], root, rep, exempt={pkg["origin_still"]["filename"]})
        check_origin_still(pkg["origin_still"], root, rep)
        check_trailer(pkg["trailer"], root, rep)
        check_audio(pkg["audio"], root, rep)
        check_score_motion(root, rep)
        check_text(pkg["text"], root, rep)
        check_rights(root, args.phase, rep)
    elif not args.package:
        rep.add("package", "not staged", OPEN, "re-run with --package <dir> once the cut exists")

    if args.write_done_receipt:
        if root is None:
            rep.add("validation receipt", "durable local record", FAIL, "--package is required")
        elif rep.failures == 0:
            try:
                receipt_path, receipt_digest = write_done_receipt(
                    root,
                    args.phase,
                    package_binding,
                    receipts,
                )
            except ValueError as exc:
                rep.add("validation receipt", "durable local record", FAIL, str(exc))
            except OSError as exc:
                rep.add(
                    "validation receipt",
                    "durable local record",
                    FAIL,
                    f"local validation receipt I/O failed ({type(exc).__name__})",
                )
            else:
                rep.add(
                    "validation receipt",
                    "durable local record",
                    PASS,
                    f"{receipt_path.relative_to(root).as_posix()} {receipt_digest[:16]}… · "
                    f"{args.phase} {receipts[args.phase]['receipt_id']} "
                    f"{receipts[args.phase]['sha256'][:16]}… · manifest "
                    f"{package_binding['manifest_sha256'][:16]}… · head "
                    f"{package_binding['repository_head'][:12]}",
                )

    rep.print()

    n = rep.failures
    print()
    if n == 0:
        print(f"\033[32m{args.phase.upper()} PHASE READY — every owned requirement met, no open blockers\033[0m")
        return 0
    print(f"\033[31m{args.phase.upper()} PHASE NOT READY — {n} item(s) failing or open\033[0m")
    return 1


def main() -> int:
    """Run the CLI without turning caller-owned malformed input into a traceback."""
    try:
        return _main()
    except (AttributeError, IndexError, KeyError, OSError, TypeError, UnicodeError, ValueError) as exc:
        print(
            f"submission validation failed closed on malformed input ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
