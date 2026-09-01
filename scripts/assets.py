#!/usr/bin/env python3
"""Inventory, pull, audit, and verify locked production assets.

The lock is the portable contract.  Asset targets are repository-relative paths
under an explicit root; sources are never copied into receipts.  Pulls enter a
content-addressed cache only after exact byte-count and SHA-256 verification and
are then published to their target with an atomic, no-overwrite hard link.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

import yaml

LOCK_SCHEMA = "danse.assets.lock.v1"
RECEIPT_SCHEMA = "danse.assets.receipt.v1"
ROOT = Path(__file__).resolve().parents[1]
PROFILES = ("generic", "screendance-production")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
OPAQUE_ASSET_ID = re.compile(r"^asset-[0-9a-f]{16}-[0-9a-f]{12}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
MEDIA_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
HTTP_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
RIGHTS_CLASSES = ("public", "restricted", "private")
CHUNK = 1 << 20
USER_AGENT = "danse-asset-parity/1"


class AssetError(RuntimeError):
    """The asset contract, source policy, or byte identity failed."""


@dataclass(frozen=True)
class Asset:
    asset_id: str
    target: str
    sha256: str
    size: int
    media_type: str
    rights_class: str
    required: bool
    sources: tuple[dict, ...]


@dataclass(frozen=True)
class Lock:
    lock_id: str
    profile: str
    repository_commit: str
    assets: tuple[Asset, ...]
    sha256: str


@dataclass(frozen=True)
class InventoryEntry:
    relative: str
    kind: str
    identity: tuple[int, int, int, int, int, int]
    size: int | None = None
    sha256: str | None = None


def _opaque_asset_id(digest: str, target: str) -> str:
    path_digest = hashlib.sha256(target.encode("utf-8")).hexdigest()
    return f"asset-{digest[:16]}-{path_digest[:12]}"


def _json_loads(raw: bytes, label: str) -> dict:
    def unique(pairs: list[tuple[str, object]]) -> dict:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise AssetError(f"{label} contains duplicate JSON keys")
            value[key] = item
        return value

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AssetError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise AssetError(f"{label} must be a JSON object")
    return value


def _safe_relative(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "\x00" in value
        or any(ord(character) < 0x20 or ord(character) > 0x7E for character in value)
    ):
        raise AssetError(f"{label} must be a safe POSIX-relative path")
    pure = PurePosixPath(value)
    if not pure.parts or pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise AssetError(f"{label} must be a safe POSIX-relative path")
    if pure.parts[0].casefold() in {".asset-cache", ".git"}:
        raise AssetError(f"{label} collides with repository control data")
    return pure.as_posix()


def _portable_path_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _https_url(value: object, *, allow_query: bool = False) -> str:
    if not isinstance(value, str):
        raise AssetError("HTTPS source URL must be a string")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (parsed.query and not allow_query)
        or parsed.fragment
    ):
        raise AssetError("HTTPS source URL violates the source policy")
    try:
        if parsed.port not in {None, 443}:
            raise AssetError("HTTPS source URL must use the default TLS port")
    except ValueError as exc:
        raise AssetError("HTTPS source URL has an invalid port") from exc
    host = parsed.hostname.lower()
    if not HOSTNAME.fullmatch(host):
        raise AssetError("HTTPS source URL must use a canonical DNS hostname")
    if re.fullmatch(r"[0-9.]+", host):
        raise AssetError("HTTPS source URL must use a canonical DNS hostname")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise AssetError("HTTPS source URL cannot target a local host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise AssetError("HTTPS source URL must use a public DNS hostname")
    return value


def _assert_public_https_host(url: str, *, allow_query: bool = False) -> None:
    host = urllib.parse.urlsplit(_https_url(url, allow_query=allow_query)).hostname
    assert host is not None
    try:
        addresses = {row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise AssetError("HTTPS source host cannot be resolved") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise AssetError("HTTPS source URL cannot target a non-public address")


def _validate_http_headers(headers: dict[str, str]) -> None:
    for name, value in headers.items():
        if not isinstance(name, str) or not HTTP_HEADER_NAME.fullmatch(name):
            raise AssetError("HTTPS request contains an invalid header name")
        if (
            not isinstance(value, str)
            or not value
            or any(ord(character) < 0x20 or ord(character) > 0x7E for character in value)
        ):
            raise AssetError("HTTPS request contains an invalid header value")


def _github_token(source: dict) -> str | None:
    explicit = "token_env" in source
    environment_name = source.get("token_env", "GITHUB_TOKEN")
    token = os.environ.get(environment_name)
    if explicit and not token:
        raise AssetError("GitHub release credential is unavailable")
    if token is not None and (
        not token or any(ord(character) < 0x21 or ord(character) > 0x7E for character in token)
    ):
        raise AssetError("GitHub release credential contains invalid characters")
    return token


def _validate_source(source: object) -> dict:
    if not isinstance(source, dict) or not isinstance(source.get("kind"), str):
        raise AssetError("asset source must be an object with a kind")
    kind = source["kind"]
    if kind == "https":
        if set(source) != {"kind", "url"}:
            raise AssetError("HTTPS source has an unknown or incomplete shape")
        _https_url(source["url"])
    elif kind == "file":
        if set(source) != {"kind", "path"}:
            raise AssetError("file source has an unknown or incomplete shape")
        _safe_relative(source["path"], "file source path")
    elif kind == "github-release":
        if not {"kind", "repository", "tag", "asset"} <= set(source) <= {
            "kind",
            "repository",
            "tag",
            "asset",
            "token_env",
        }:
            raise AssetError("GitHub release source has an unknown or incomplete shape")
        if not isinstance(source["repository"], str) or not REPOSITORY.fullmatch(
            source["repository"]
        ):
            raise AssetError("GitHub release repository is invalid")
        for key in ("tag", "asset"):
            value = source[key]
            if (
                not isinstance(value, str)
                or not value
                or any(char in value for char in "\r\n\x00")
            ):
                raise AssetError(f"GitHub release {key} is invalid")
        if "/" in source["asset"] or "\\" in source["asset"]:
            raise AssetError("GitHub release asset must be a basename")
        token_env = source.get("token_env", "GITHUB_TOKEN")
        if not isinstance(token_env, str) or not ENV_NAME.fullmatch(token_env):
            raise AssetError("GitHub release token environment name is invalid")
    else:
        raise AssetError(f"unsupported asset source kind: {kind!r}")
    return dict(source)


def load_lock(path: Path, *, repository_root: Path | None = None) -> Lock:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AssetError("asset lock is missing or unreadable") from exc
    value = _json_loads(raw, "asset lock")
    if set(value) != {"schema", "lock_id", "profile", "repository_commit", "assets"}:
        raise AssetError("asset lock has an unknown or incomplete top-level shape")
    if value["schema"] != LOCK_SCHEMA:
        raise AssetError("asset lock has the wrong schema")
    if not isinstance(value["lock_id"], str) or not IDENTIFIER.fullmatch(value["lock_id"]):
        raise AssetError("asset lock id is invalid")
    if value["profile"] not in PROFILES:
        raise AssetError("asset lock profile is invalid")
    if not isinstance(value["repository_commit"], str) or not GIT_SHA.fullmatch(
        value["repository_commit"]
    ):
        raise AssetError("asset lock repository commit is invalid")
    if not isinstance(value["assets"], list):
        raise AssetError("asset lock assets must be an array")
    if not value["assets"]:
        raise AssetError("asset lock must declare at least one asset")
    assets: list[Asset] = []
    ids: set[str] = set()
    targets: set[str] = set()
    casefolded_targets: set[str] = set()
    for row in value["assets"]:
        required_keys = {
            "id",
            "target",
            "sha256",
            "bytes",
            "media_type",
            "rights_class",
            "required",
            "sources",
        }
        if not isinstance(row, dict) or set(row) != required_keys:
            raise AssetError("asset row has an unknown or incomplete shape")
        asset_id = row["id"]
        if not isinstance(asset_id, str) or not IDENTIFIER.fullmatch(asset_id):
            raise AssetError("asset id is invalid")
        if asset_id in ids:
            raise AssetError("asset lock repeats an asset id")
        ids.add(asset_id)
        target = _safe_relative(row["target"], "asset target")
        if target in targets:
            raise AssetError("asset lock repeats an asset target")
        targets.add(target)
        casefolded_target = _portable_path_key(target)
        if casefolded_target in casefolded_targets:
            raise AssetError("asset lock contains case-colliding asset targets")
        casefolded_targets.add(casefolded_target)
        digest = row["sha256"]
        if not isinstance(digest, str) or not HEX64.fullmatch(digest):
            raise AssetError("asset SHA-256 is invalid")
        size = row["bytes"]
        if type(size) is not int or size < 0:
            raise AssetError("asset byte count is invalid")
        media_type = row["media_type"]
        if not isinstance(media_type, str) or not MEDIA_TYPE.fullmatch(media_type):
            raise AssetError("asset media type is invalid")
        rights_class = row["rights_class"]
        if rights_class not in RIGHTS_CLASSES:
            raise AssetError("asset rights class is invalid")
        if type(row["required"]) is not bool or not isinstance(row["sources"], list):
            raise AssetError("asset required/sources fields are invalid")
        sources = tuple(_validate_source(source) for source in row["sources"])
        if row["required"] and not sources:
            raise AssetError("required asset must declare at least one source")
        assets.append(
            Asset(
                asset_id,
                target,
                digest,
                size,
                media_type,
                rights_class,
                row["required"],
                sources,
            )
        )
    if value["profile"] == "screendance-production":
        _validate_production_assets(assets, repository_root=repository_root)
    return Lock(
        value["lock_id"],
        value["profile"],
        value["repository_commit"],
        tuple(assets),
        hashlib.sha256(raw).hexdigest(),
    )


def _tracked_bytes(root: Path, commit: str, relative: str) -> bytes:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "cat-file", "blob", f"{commit}:{relative}"],
            capture_output=True,
            check=False,
            env=environment,
        )
    except OSError as exc:
        raise AssetError("git is required to read committed production authority") from exc
    if result.returncode != 0:
        raise AssetError("committed production authority is missing or unreadable")
    return result.stdout


def _production_targets(
    repository_root: Path | None = None,
    repository_commit: str | None = None,
) -> set[str]:
    root = ROOT if repository_root is None else repository_root
    try:
        raw = (
            _tracked_bytes(root, repository_commit, "corpus/manifest.json")
            if repository_commit is not None
            else (root / "corpus/manifest.json").read_bytes()
        )
    except OSError as exc:
        raise AssetError("tracked corpus manifest is missing or unreadable") from exc
    manifest = _json_loads(raw, "corpus manifest")
    frames = manifest.get("frames")
    if not isinstance(frames, list) or len(frames) != 162:
        raise AssetError("tracked corpus manifest does not declare the 162-frame production set")
    targets = {".work/music/MuseScore_General.sf3"}
    for frame in frames:
        if not isinstance(frame, dict) or not isinstance(frame.get("source"), str):
            raise AssetError("tracked corpus frame source is invalid")
        source = _safe_relative(frame["source"], "corpus frame source")
        if "/" in source:
            raise AssetError("tracked corpus frame source must be a basename")
        stem = Path(source).stem
        targets.add(f"pipeline/.work/raw/{source}")
        targets.add(f"pipeline/.work/vision/mask/{stem}.png")
        targets.add(f"pipeline/.work/vision/pose/{stem}.json")
    if len(targets) != 487:
        raise AssetError("production target census is not exactly 487 objects")
    return targets


def _canonical_production_pins(
    repository_root: Path | None = None,
    repository_commit: str | None = None,
) -> tuple[str, str, str]:
    root = ROOT if repository_root is None else repository_root
    try:
        toolchain_raw = (
            _tracked_bytes(root, repository_commit, "music/audio-toolchain.json")
            if repository_commit is not None
            else (root / "music/audio-toolchain.json").read_bytes()
        )
        register_raw = (
            _tracked_bytes(root, repository_commit, "submission/screendance-2027.yaml")
            if repository_commit is not None
            else (root / "submission/screendance-2027.yaml").read_bytes()
        )
        toolchain = _json_loads(
            toolchain_raw,
            "audio toolchain",
        )
        register = yaml.safe_load(register_raw.decode("utf-8"))
        origin_sha256 = register["package"]["origin_still"]["source_sha256"]
        soundfont = toolchain["soundfont"]
        soundfont_sha256 = soundfont["sha256"]
        soundfont_url = soundfont["source_url"]
    except (OSError, UnicodeError, TypeError, KeyError, yaml.YAMLError) as exc:
        raise AssetError("canonical production pin records are missing or malformed") from exc
    if (
        not isinstance(origin_sha256, str)
        or not HEX64.fullmatch(origin_sha256)
        or not isinstance(soundfont_sha256, str)
        or not HEX64.fullmatch(soundfont_sha256)
        or not isinstance(soundfont_url, str)
    ):
        raise AssetError("canonical production pin records have invalid identities")
    _https_url(soundfont_url)
    return origin_sha256, soundfont_sha256, soundfont_url


def _validate_production_assets(
    assets: list[Asset],
    *,
    repository_root: Path | None = None,
    repository_commit: str | None = None,
) -> None:
    expected = _production_targets(repository_root, repository_commit)
    actual = {asset.target for asset in assets}
    if actual != expected or len(assets) != len(expected):
        raise AssetError("production lock does not contain the exact 487-object target census")
    by_target = {asset.target: asset for asset in assets}
    origin_sha256, soundfont_sha256, soundfont_url = _canonical_production_pins(
        repository_root,
        repository_commit,
    )
    if by_target["pipeline/.work/raw/IMG_1594.JPG"].sha256 != origin_sha256:
        raise AssetError("production lock origin still disagrees with the canonical digest")
    if by_target[".work/music/MuseScore_General.sf3"].sha256 != soundfont_sha256:
        raise AssetError("production lock soundfont disagrees with the audio toolchain")
    for asset in assets:
        if not asset.required:
            raise AssetError("every production input must be required")
        if asset.size <= 0:
            raise AssetError("every production input must have a positive byte count")
        if asset.target.startswith("pipeline/.work/") and asset.rights_class != "private":
            raise AssetError("private photographic and Vision inputs must remain private")
        if asset.target.startswith("pipeline/.work/raw/"):
            expected_type = "image/jpeg" if asset.target.lower().endswith((".jpg", ".jpeg")) else "image/png"
        elif asset.target.startswith("pipeline/.work/vision/mask/"):
            expected_type = "image/png"
        elif asset.target.startswith("pipeline/.work/vision/pose/"):
            expected_type = "application/json"
        else:
            expected_type = "audio/x-soundfont"
        if asset.media_type != expected_type:
            raise AssetError("production input media type disagrees with its target class")
        for source in asset.sources:
            if source["kind"] == "file" and source["path"] != asset.target:
                raise AssetError("production file source path must equal its canonical target")
        if asset.target.startswith("pipeline/.work/"):
            for source in asset.sources:
                if source["kind"] not in {"file", "github-release"}:
                    raise AssetError("private production inputs cannot use public HTTPS locators")
                if source["kind"] == "github-release" and "token_env" not in source:
                    raise AssetError(
                        "private production GitHub releases require an explicit credential"
                    )
        else:
            if asset.rights_class != "restricted":
                raise AssetError("the licensed soundfont must remain a restricted input")
            for source in asset.sources:
                if source["kind"] == "https" and source["url"] != soundfont_url:
                    raise AssetError("soundfont HTTPS source is not the canonical upstream locator")
        expected_id = _opaque_asset_id(asset.sha256, asset.target)
        if not OPAQUE_ASSET_ID.fullmatch(asset.asset_id) or asset.asset_id != expected_id:
            raise AssetError("production input ids must use the deterministic opaque form")


def _sha256_stream(handle: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    for block in iter(lambda: handle.read(CHUNK), b""):
        size += len(block)
        digest.update(block)
    return size, digest.hexdigest()


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _inventory_directory(descriptor: int, prefix: str) -> list[InventoryEntry]:
    try:
        names = sorted(os.listdir(descriptor))
    except OSError as exc:
        raise AssetError("inventory source census could not be read") from exc
    snapshot: list[InventoryEntry] = []
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    for name in names:
        relative = f"{prefix}/{name}" if prefix else name
        try:
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as exc:
            raise AssetError("inventory source changed during its census") from exc
        if stat.S_ISLNK(before.st_mode):
            raise AssetError("inventory source contains a symlink")
        if stat.S_ISDIR(before.st_mode):
            try:
                child = os.open(name, directory_flags, dir_fd=descriptor)
            except OSError as exc:
                raise AssetError("inventory source directory could not be opened safely") from exc
            try:
                opened = os.fstat(child)
                if _stat_identity(before) != _stat_identity(opened):
                    raise AssetError("inventory source changed before traversal")
                descendants = _inventory_directory(child, relative)
                traversed = os.fstat(child)
                try:
                    final = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                except OSError as exc:
                    raise AssetError("inventory source changed during traversal") from exc
                identity = _stat_identity(traversed)
                if identity != _stat_identity(opened) or identity != _stat_identity(final):
                    raise AssetError("inventory source changed during traversal")
                snapshot.append(InventoryEntry(relative, "directory", identity))
                snapshot.extend(descendants)
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(before.st_mode):
            raise AssetError("inventory source contains a non-regular filesystem entry")
        try:
            file_descriptor = os.open(name, file_flags, dir_fd=descriptor)
        except OSError as exc:
            raise AssetError("inventory source file could not be opened safely") from exc
        try:
            opened = os.fstat(file_descriptor)
            if not stat.S_ISREG(opened.st_mode) or _stat_identity(before) != _stat_identity(opened):
                raise AssetError("inventory source changed before it could be hashed")
            with os.fdopen(file_descriptor, "rb", closefd=False) as handle:
                size, digest = _sha256_stream(handle)
            hashed = os.fstat(file_descriptor)
            try:
                final = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except OSError as exc:
                raise AssetError("inventory source changed while it was being hashed") from exc
            identity = _stat_identity(hashed)
            if identity != _stat_identity(opened) or identity != _stat_identity(final):
                raise AssetError("inventory source changed while it was being hashed")
            snapshot.append(InventoryEntry(relative, "file", identity, size, digest))
        finally:
            os.close(file_descriptor)
    return snapshot


def _inventory_snapshot(root: Path) -> tuple[InventoryEntry, ...]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(root, flags)
    except OSError as exc:
        raise AssetError("inventory source root could not be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        snapshot = _inventory_directory(descriptor, "")
        traversed = os.fstat(descriptor)
        try:
            current = os.open(root, flags)
        except OSError as exc:
            raise AssetError("inventory source root changed during traversal") from exc
        try:
            final = os.fstat(current)
        finally:
            os.close(current)
        if _stat_identity(opened) != _stat_identity(traversed) or _stat_identity(
            traversed
        ) != _stat_identity(final):
            raise AssetError("inventory source root changed during traversal")
        return tuple(sorted(snapshot, key=lambda entry: entry.relative))
    finally:
        os.close(descriptor)


def _root(path: Path, *, create: bool) -> Path:
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.is_symlink() or not path.is_dir():
        raise AssetError("asset root must be an existing regular directory")
    return path.resolve(strict=True)


def _path_under(root: Path, relative: str, *, create_parents: bool) -> Path:
    current = root
    parts = PurePosixPath(relative).parts
    missing_ancestor = False
    for part in parts[:-1]:
        current = current / part
        if missing_ancestor:
            continue
        if os.path.lexists(current):
            if current.is_symlink() or not current.is_dir():
                raise AssetError("asset target traverses a symlink or non-directory")
        elif create_parents:
            current.mkdir(mode=0o700)
        else:
            missing_ancestor = True
    final = current / parts[-1]
    if not missing_ancestor:
        try:
            current.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise AssetError("asset target escapes the explicit root") from exc
    if final.is_symlink():
        raise AssetError("asset target is a symlink")
    return final


def _parent_descriptor_under(
    root: Path,
    relative: str,
    *,
    create_parents: bool,
    durable_parents: bool = False,
) -> tuple[int, str] | None:
    """Open the target parent one component at a time without following links."""

    parts = PurePosixPath(relative).parts
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        current = os.open(root, directory_flags)
    except OSError as exc:
        raise AssetError("asset root cannot be opened safely") from exc
    try:
        for part in parts[:-1]:
            created = False
            try:
                child = os.open(part, directory_flags, dir_fd=current)
            except FileNotFoundError:
                if not create_parents:
                    os.close(current)
                    return None
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current)
                    created = True
                except FileExistsError:
                    pass
                child = os.open(part, directory_flags, dir_fd=current)
            if create_parents or durable_parents:
                try:
                    _fsync_asset_directory(current, "asset ancestor parent")
                    linked = os.stat(part, dir_fd=current, follow_symlinks=False)
                    opened = os.fstat(child)
                    if (
                        not stat.S_ISDIR(linked.st_mode)
                        or (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino)
                    ):
                        raise AssetError("asset ancestor changed during durable creation")
                except (AssetError, OSError) as exc:
                    os.close(child)
                    if created:
                        try:
                            os.rmdir(part, dir_fd=current)
                            os.fsync(current)
                        except OSError:
                            pass
                    if isinstance(exc, AssetError):
                        raise
                    raise AssetError("asset ancestor could not be durably synchronized") from exc
            os.close(current)
            current = child
        return current, parts[-1]
    except AssetError:
        os.close(current)
        raise
    except OSError as exc:
        os.close(current)
        raise AssetError("asset target traverses a symlink or non-directory") from exc


def _temporary_file_at(parent: int, *, prefix: str = ".asset-") -> tuple[int, str]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for _ in range(128):
        name = f"{prefix}{secrets.token_hex(16)}"
        try:
            return os.open(name, flags, 0o600, dir_fd=parent), name
        except FileExistsError:
            continue
        except OSError as exc:
            raise AssetError("temporary output object could not be created safely") from exc
    raise AssetError("temporary output object name space is exhausted")


def _parent_descriptor_matches(root: Path, relative: str, expected: int) -> bool:
    try:
        current = _parent_descriptor_under(root, relative, create_parents=False)
    except AssetError:
        return False
    if current is None:
        return False
    descriptor, _ = current
    try:
        expected_stat = os.fstat(expected)
        current_stat = os.fstat(descriptor)
        return (expected_stat.st_dev, expected_stat.st_ino) == (
            current_stat.st_dev,
            current_stat.st_ino,
        )
    finally:
        os.close(descriptor)


def _repository_head(root: Path, lock: Lock | None = None) -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        identity = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            capture_output=True,
            check=False,
            env=environment,
        )
        index_flags = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-v", "-z"],
            capture_output=True,
            check=False,
            env=environment,
        )
    except OSError as exc:
        raise AssetError("git is required to verify the repository commit") from exc
    lines = identity.stdout.splitlines()
    if identity.returncode != 0 or len(lines) != 2 or not GIT_SHA.fullmatch(lines[1]):
        raise AssetError("asset root is not a readable Git checkout")
    try:
        top = Path(lines[0]).resolve(strict=True)
    except OSError as exc:
        raise AssetError("Git checkout root cannot be resolved") from exc
    if top != root:
        raise AssetError("asset root must be the exact Git checkout root")
    if status.returncode != 0 or index_flags.returncode != 0:
        raise AssetError("Git checkout state cannot be read")
    for record in index_flags.stdout.split(b"\0"):
        if not record:
            continue
        if len(record) < 3 or record[1:2] != b" ":
            raise AssetError("Git index flag inventory is malformed")
        marker = record[:1]
        if marker == b"S" or b"a" <= marker <= b"z":
            raise AssetError("Git checkout uses skip-worktree or assume-unchanged flags")
    allowed = set() if lock is None else {asset.target for asset in lock.assets}
    for record in status.stdout.split(b"\0"):
        if not record:
            continue
        if not record.startswith(b"?? "):
            raise AssetError("Git checkout has tracked or staged changes")
        path = os.fsdecode(record[3:])
        if path == ".asset-cache" or path.startswith(".asset-cache/") or path in allowed:
            continue
        raise AssetError("Git checkout has untracked files outside locked asset targets")
    return lines[1]


def _assert_repository_binding(root: Path, lock: Lock) -> None:
    if _repository_head(root, lock) != lock.repository_commit:
        raise AssetError("asset lock does not bind the exact checked-out repository commit")
    if lock.profile != "screendance-production":
        return
    _validate_production_assets(
        list(lock.assets),
        repository_root=root,
        repository_commit=lock.repository_commit,
    )
    if _repository_head(root, lock) != lock.repository_commit:
        raise AssetError("asset lock does not bind a stable exact checked-out repository commit")


def _assert_locked_tree(root: Path, lock: Lock) -> None:
    if lock.profile != "screendance-production":
        return
    expected_files = {asset.target for asset in lock.assets}
    expected_directories: set[str] = set()
    for target in expected_files:
        parts = PurePosixPath(target).parts
        for length in range(1, len(parts)):
            expected_directories.add(PurePosixPath(*parts[:length]).as_posix())
    for relative_root in (".work", "pipeline/.work"):
        scan_root = root / relative_root
        if not os.path.lexists(scan_root):
            continue
        if scan_root.is_symlink() or not scan_root.is_dir():
            raise AssetError("production input root is a symlink or non-directory")
        for path in scan_root.rglob("*"):
            relative = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise AssetError("production input tree contains an unsafe filesystem entry")
            if stat.S_ISDIR(mode) and relative not in expected_directories:
                raise AssetError("production input tree contains an undeclared directory")
            if stat.S_ISREG(mode) and relative not in expected_files:
                raise AssetError("production input tree contains an undeclared file")


def _cache_path(root: Path, asset: Asset, *, create: bool) -> Path:
    return _path_under(root, _cache_relative(asset), create_parents=create)


def _cache_relative(asset: Asset) -> str:
    return f".asset-cache/sha256/{asset.sha256[:2]}/{asset.sha256}"


def _identity(path: Path, asset: Asset) -> str:
    if not os.path.lexists(path):
        return "missing"
    if path.is_symlink() or not path.is_file():
        return "unsafe"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return "unsafe"
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size != asset.size
            or before.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        ):
            return "mismatch"
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            size, digest = _sha256_stream(handle)
        after = os.fstat(descriptor)
        stable = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        return "verified" if stable and size == asset.size and digest == asset.sha256 else "mismatch"
    finally:
        os.close(descriptor)


def _identity_proof_at(
    parent: int,
    name: str,
    asset: Asset,
) -> tuple[str, tuple[int, int, int, int, int, int] | None]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unsafe", None
    try:
        before = os.fstat(descriptor)
        before_identity = _stat_identity(before)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size != asset.size
            or before.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        ):
            return "mismatch", before_identity
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            size, digest = _sha256_stream(handle)
        after = os.fstat(descriptor)
        after_identity = _stat_identity(after)
        try:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError:
            return "mismatch", None
        current_identity = _stat_identity(current)
        stable = before_identity == after_identity == current_identity
        state = (
            "verified"
            if stable and size == asset.size and digest == asset.sha256
            else "mismatch"
        )
        return state, after_identity if stable else None
    finally:
        os.close(descriptor)


def _identity_at(parent: int, name: str, asset: Asset) -> str:
    return _identity_proof_at(parent, name, asset)[0]


def _fsync_verified_asset_at(parent: int, name: str, asset: Asset) -> None:
    state, proof = _identity_proof_at(parent, name, asset)
    if state != "verified" or proof is None:
        raise AssetError("asset changed before durable synchronization")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
    except OSError as exc:
        raise AssetError("asset could not be opened for durable synchronization") from exc
    try:
        opened = _stat_identity(os.fstat(descriptor))
        if opened != proof:
            raise AssetError("asset changed before durable synchronization")
        try:
            os.fsync(descriptor)
        except OSError as exc:
            raise AssetError("asset file could not be durably synchronized") from exc
        synchronized = _stat_identity(os.fstat(descriptor))
        try:
            linked = _stat_identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
        except OSError as exc:
            raise AssetError("asset changed during durable synchronization") from exc
        if synchronized != opened or linked != synchronized:
            raise AssetError("asset changed during durable synchronization")
    finally:
        os.close(descriptor)


def _identity_proof_under(
    root: Path,
    relative: str,
    asset: Asset,
) -> tuple[str, tuple[int, int, int, int, int, int] | None]:
    try:
        parent = _parent_descriptor_under(root, relative, create_parents=False)
    except AssetError:
        return "unsafe", None
    if parent is None:
        return "missing", None
    descriptor, name = parent
    try:
        proof = _identity_proof_at(descriptor, name, asset)
        if not _parent_descriptor_matches(root, relative, descriptor):
            return "unsafe", None
        return proof
    finally:
        os.close(descriptor)


def _identity_under(root: Path, relative: str, asset: Asset) -> str:
    return _identity_proof_under(root, relative, asset)[0]


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, *, allow_github_asset_redirect: bool = False) -> None:
        super().__init__()
        self.allow_github_asset_redirect = allow_github_asset_redirect

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old_host = urllib.parse.urlsplit(req.full_url).hostname
        new = urllib.parse.urlsplit(newurl)
        allow_query = bool(
            self.allow_github_asset_redirect
            and old_host == "api.github.com"
            and new.hostname
            in {
                "github-releases.githubusercontent.com",
                "objects.githubusercontent.com",
                "release-assets.githubusercontent.com",
            }
        )
        _assert_public_https_host(newurl, allow_query=allow_query)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            new_host = new.hostname
            if old_host != new_host:
                redirected.remove_header("Authorization")
                redirected.unredirected_hdrs.pop("Authorization", None)
        return redirected


def _open_url(
    url: str,
    headers: dict[str, str],
    timeout: float,
    *,
    allow_github_asset_redirect: bool = False,
):
    url = _https_url(url)
    _assert_public_https_host(url)
    _validate_http_headers(headers)
    try:
        request = urllib.request.Request(url, headers=headers)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise AssetError("HTTPS request headers could not be constructed") from exc
    handler = _SafeRedirect(allow_github_asset_redirect=allow_github_asset_redirect)
    try:
        return urllib.request.build_opener(handler).open(request, timeout=timeout)
    except (TypeError, ValueError, UnicodeError, http.client.HTTPException) as exc:
        raise AssetError("HTTPS request headers could not be constructed") from exc


def _github_release_asset(
    source: dict,
    timeout: float,
    *,
    require_private_repository: bool = False,
) -> tuple[str, dict[str, str]]:
    repository = source["repository"]
    tag = urllib.parse.quote(source["tag"], safe="")
    token = _github_token(source)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if require_private_repository:
        if "token_env" not in source:
            raise AssetError("private GitHub repository credential is unavailable")
        repository_api = f"https://api.github.com/repos/{repository}"
        try:
            with _open_url(repository_api, headers, timeout) as response:
                repository_raw = response.read(8 << 20)
                if response.read(1):
                    raise AssetError("GitHub repository metadata is too large")
        except (OSError, urllib.error.URLError) as exc:
            raise AssetError("private GitHub repository metadata could not be read") from exc
        repository_metadata = _json_loads(
            repository_raw,
            "GitHub repository metadata",
        )
        if repository_metadata.get("private") is not True:
            raise AssetError(
                "private production source must use a private GitHub repository"
            )
    api = f"https://api.github.com/repos/{repository}/releases/tags/{tag}"
    try:
        with _open_url(api, headers, timeout) as response:
            raw = response.read(8 << 20)
            if response.read(1):
                raise AssetError("GitHub release metadata is too large")
    except (OSError, urllib.error.URLError) as exc:
        raise AssetError("GitHub release metadata could not be read") from exc
    metadata = _json_loads(raw, "GitHub release metadata")
    assets = metadata.get("assets")
    if not isinstance(assets, list):
        raise AssetError("GitHub release metadata has no asset inventory")
    matches = [row for row in assets if isinstance(row, dict) and row.get("name") == source["asset"]]
    if len(matches) != 1 or not isinstance(matches[0].get("url"), str):
        raise AssetError("GitHub release does not contain the exact locked asset")
    download_headers = dict(headers)
    download_headers["Accept"] = "application/octet-stream"
    return _https_url(matches[0]["url"]), download_headers


def _open_regular_under(root: Path, relative: str) -> BinaryIO:
    parts = PurePosixPath(relative).parts
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    try:
        current = os.open(root, directory_flags)
        descriptors.append(current)
        for part in parts[:-1]:
            current = os.open(part, directory_flags, dir_fd=current)
            descriptors.append(current)
        descriptor = os.open(parts[-1], file_flags, dir_fd=current)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise AssetError("file source is not a regular file")
        return os.fdopen(descriptor, "rb")
    except OSError as exc:
        raise AssetError("file source cannot be opened without following symlinks") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _file_source(source: dict, source_root: Path) -> BinaryIO:
    root = _root(source_root, create=False)
    relative = _safe_relative(source["path"], "file source path")
    return _open_regular_under(root, relative)


def _source_stream(
    source: dict,
    *,
    allow_file: bool,
    source_root: Path | None,
    timeout: float,
    require_private_repository: bool = False,
):
    if source["kind"] == "file":
        if not allow_file or source_root is None:
            raise AssetError("file sources require --allow-file and --file-source-root")
        return _file_source(source, source_root)
    headers = {"User-Agent": USER_AGENT}
    url = source.get("url")
    if source["kind"] == "github-release":
        url, headers = _github_release_asset(
            source,
            timeout,
            require_private_repository=require_private_repository,
        )
    try:
        return _open_url(
            url,
            headers,
            timeout,
            allow_github_asset_redirect=source["kind"] == "github-release",
        )
    except (OSError, urllib.error.URLError) as exc:
        raise AssetError("HTTPS asset source could not be read") from exc


def _write_stream_verified(stream, output: BinaryIO, asset: Asset) -> None:
    digest = hashlib.sha256()
    size = 0
    while True:
        block = stream.read(CHUNK)
        if not block:
            break
        size += len(block)
        if size > asset.size:
            raise AssetError("asset source exceeds its locked byte count")
        digest.update(block)
        output.write(block)
    if size != asset.size or digest.hexdigest() != asset.sha256:
        raise AssetError("asset source disagrees with its locked identity")


def _fsync_asset_directory(descriptor: int, label: str) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise AssetError(f"{label} could not be durably synchronized") from exc


def _remove_published_link(descriptor: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=descriptor)
    except FileNotFoundError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # The caller still fails closed and therefore cannot publish a receipt.
        pass


def _fsync_verified_under(root: Path, relative: str, asset: Asset, label: str) -> None:
    parent = _parent_descriptor_under(
        root,
        relative,
        create_parents=False,
        durable_parents=True,
    )
    if parent is None:
        raise AssetError(f"{label} disappeared before durable synchronization")
    descriptor, name = parent
    try:
        _fsync_verified_asset_at(descriptor, name, asset)
        _fsync_asset_directory(descriptor, f"{label} parent")
        if not _parent_descriptor_matches(root, relative, descriptor):
            raise AssetError(f"{label} parent changed during durable synchronization")
    finally:
        os.close(descriptor)


def _cache_from_sources(
    root: Path,
    asset: Asset,
    *,
    allow_file: bool,
    source_root: Path | None,
    timeout: float,
    require_private_repository: bool = False,
) -> Path:
    relative = _cache_relative(asset)
    parent = _parent_descriptor_under(root, relative, create_parents=True)
    if parent is None:
        raise AssetError("content-addressed cache parent is missing")
    cache_descriptor, cache_name = parent
    try:
        state = _identity_at(cache_descriptor, cache_name, asset)
        if state == "verified":
            _fsync_verified_asset_at(cache_descriptor, cache_name, asset)
            _fsync_asset_directory(cache_descriptor, "content-addressed cache parent")
            if not _parent_descriptor_matches(root, relative, cache_descriptor):
                raise AssetError("content-addressed cache parent changed during hydration")
            return root / relative
        if state != "missing":
            raise AssetError("content-addressed cache contains a corrupt object")
        errors: list[str] = []
        for index, source in enumerate(asset.sources, start=1):
            temporary_descriptor = -1
            temporary_name: str | None = None
            published = False
            try:
                try:
                    temporary_descriptor, temporary_name = _temporary_file_at(cache_descriptor)
                    output = os.fdopen(temporary_descriptor, "wb")
                    temporary_descriptor = -1
                    with output, _source_stream(
                        source,
                        allow_file=allow_file,
                        source_root=source_root,
                        timeout=timeout,
                        require_private_repository=require_private_repository,
                    ) as stream:
                        _write_stream_verified(stream, output, asset)
                        output.flush()
                        os.fsync(output.fileno())
                        os.fchmod(output.fileno(), 0o444)
                        os.fsync(output.fileno())
                    try:
                        os.link(
                            temporary_name,
                            cache_name,
                            src_dir_fd=cache_descriptor,
                            dst_dir_fd=cache_descriptor,
                            follow_symlinks=False,
                        )
                        published = True
                    except FileExistsError:
                        pass
                except (
                    AssetError,
                    OSError,
                    urllib.error.URLError,
                    http.client.HTTPException,
                ) as exc:
                    errors.append(f"source {index} ({source['kind']}): {exc}")
                    continue
                if _identity_at(cache_descriptor, cache_name, asset) != "verified":
                    errors.append(
                        f"source {index} ({source['kind']}): "
                        "cache publication did not preserve asset identity"
                    )
                    continue
                _fsync_verified_asset_at(cache_descriptor, cache_name, asset)
                if not _parent_descriptor_matches(root, relative, cache_descriptor):
                    if published:
                        _remove_published_link(cache_descriptor, cache_name)
                    raise AssetError("content-addressed cache parent changed during hydration")
                try:
                    _fsync_asset_directory(
                        cache_descriptor,
                        "content-addressed cache parent",
                    )
                except AssetError:
                    if published:
                        _remove_published_link(cache_descriptor, cache_name)
                    raise
                if not _parent_descriptor_matches(root, relative, cache_descriptor):
                    if published:
                        _remove_published_link(cache_descriptor, cache_name)
                    raise AssetError("content-addressed cache parent changed during hydration")
                return root / relative
            finally:
                if temporary_descriptor >= 0:
                    os.close(temporary_descriptor)
                if temporary_name is not None:
                    try:
                        os.unlink(temporary_name, dir_fd=cache_descriptor)
                    except FileNotFoundError:
                        pass
        detail = "; ".join(errors) if errors else "no sources declared"
        raise AssetError(f"no source satisfied asset {asset.asset_id}: {detail}")
    finally:
        os.close(cache_descriptor)


def _publish_no_overwrite(root: Path, target_relative: str, asset: Asset) -> None:
    cache_parent = _parent_descriptor_under(
        root,
        _cache_relative(asset),
        create_parents=False,
    )
    if cache_parent is None:
        raise AssetError("asset publication parent is missing")
    cache_descriptor, cache_name = cache_parent
    try:
        target_parent = _parent_descriptor_under(
            root,
            target_relative,
            create_parents=True,
        )
        if target_parent is None:
            raise AssetError("asset publication parent is missing")
        target_descriptor, target_name = target_parent
        try:
            if _identity_at(cache_descriptor, cache_name, asset) != "verified":
                raise AssetError("content-addressed cache contains a corrupt object")
            if not _parent_descriptor_matches(root, target_relative, target_descriptor):
                raise AssetError("asset target parent changed before publication")
            target_state = _identity_at(target_descriptor, target_name, asset)
            if target_state == "verified":
                _fsync_verified_asset_at(target_descriptor, target_name, asset)
                _fsync_asset_directory(target_descriptor, "asset target parent")
                if not _parent_descriptor_matches(root, target_relative, target_descriptor):
                    raise AssetError("asset target parent changed during verification")
                return
            if target_state != "missing":
                raise AssetError(
                    "existing asset target disagrees with the lock; refusing to overwrite"
                )
            published = False
            try:
                os.link(
                    cache_name,
                    target_name,
                    src_dir_fd=cache_descriptor,
                    dst_dir_fd=target_descriptor,
                    follow_symlinks=False,
                )
                published = True
            except FileExistsError:
                pass
            except OSError as exc:
                raise AssetError("asset target could not be published atomically") from exc
            if not _parent_descriptor_matches(root, target_relative, target_descriptor):
                if published:
                    os.unlink(target_name, dir_fd=target_descriptor)
                raise AssetError("asset target parent changed during publication")
            final_state = _identity_at(target_descriptor, target_name, asset)
            if not _parent_descriptor_matches(root, target_relative, target_descriptor):
                if published:
                    try:
                        os.unlink(target_name, dir_fd=target_descriptor)
                    except FileNotFoundError:
                        pass
                raise AssetError("asset target parent changed during final verification")
            if final_state != "verified":
                if published:
                    _remove_published_link(target_descriptor, target_name)
                raise AssetError("published asset target disagrees with the lock")
            _fsync_verified_asset_at(target_descriptor, target_name, asset)
            try:
                _fsync_asset_directory(target_descriptor, "asset target parent")
            except AssetError:
                if published:
                    _remove_published_link(target_descriptor, target_name)
                raise
            if not _parent_descriptor_matches(root, target_relative, target_descriptor):
                if published:
                    _remove_published_link(target_descriptor, target_name)
                raise AssetError("asset target parent changed during durable publication")
        finally:
            os.close(target_descriptor)
    finally:
        os.close(cache_descriptor)


def _report(command: str, lock: Lock, rows: list[tuple[Asset, str, str]]) -> dict:
    states: dict[str, int] = {}
    by_rights = {
        name: {"declared": 0, "required": 0, "verified": 0, "unresolved": 0}
        for name in RIGHTS_CLASSES
    }
    unresolved: list[str] = []
    for asset, target_state, cache_state in rows:
        states[target_state] = states.get(target_state, 0) + 1
        bucket = by_rights[asset.rights_class]
        bucket["declared"] += 1
        bucket["required"] += int(asset.required)
        bucket["verified"] += int(target_state == "verified")
        bad = target_state != "verified" and asset.required or target_state in {"unsafe", "mismatch"}
        if bad:
            bucket["unresolved"] += 1
            unresolved.append(asset.asset_id)
    return {
        "schema": RECEIPT_SCHEMA,
        "command": command,
        "lock_id": lock.lock_id,
        "profile": lock.profile,
        "lock_sha256": lock.sha256,
        "repository_commit": lock.repository_commit,
        "ok": not unresolved,
        "counts": {
            "declared": len(lock.assets),
            "required": sum(asset.required for asset in lock.assets),
            "verified": states.get("verified", 0),
            "cache_verified": sum(cache_state == "verified" for _, _, cache_state in rows),
            "missing": states.get("missing", 0),
            "mismatch": states.get("mismatch", 0),
            "unsafe": states.get("unsafe", 0),
        },
        "by_rights_class": by_rights,
        "unresolved": sorted(unresolved),
    }


def inspect(command: str, lock: Lock, root: Path) -> dict:
    _assert_repository_binding(root, lock)
    _assert_locked_tree(root, lock)
    first_targets = [
        _identity_proof_under(root, asset.target, asset) for asset in lock.assets
    ]
    cache_states = [
        _identity_under(root, _cache_relative(asset), asset) for asset in lock.assets
    ]
    _assert_repository_binding(root, lock)
    _assert_locked_tree(root, lock)
    final_targets = [
        _identity_proof_under(root, asset.target, asset) for asset in lock.assets
    ]
    _assert_repository_binding(root, lock)
    _assert_locked_tree(root, lock)
    if final_targets != first_targets:
        raise AssetError("asset target tree changed during completed verification")
    rows = [
        (asset, target[0], cache_state)
        for asset, target, cache_state in zip(
            lock.assets,
            final_targets,
            cache_states,
            strict=True,
        )
    ]
    return _report(command, lock, rows)


def pull(
    lock: Lock,
    root: Path,
    *,
    allow_file: bool,
    source_root: Path | None,
    timeout: float,
) -> dict:
    _assert_repository_binding(root, lock)
    _assert_locked_tree(root, lock)
    failures: list[str] = []
    for asset in lock.assets:
        if _identity_under(root, asset.target, asset) == "verified":
            cache_relative = _cache_relative(asset)
            if _identity_under(root, cache_relative, asset) == "verified":
                _fsync_verified_under(
                    root,
                    cache_relative,
                    asset,
                    "content-addressed cache",
                )
            _fsync_verified_under(root, asset.target, asset, "asset target")
            continue
        try:
            _cache_from_sources(
                root,
                asset,
                allow_file=allow_file,
                source_root=source_root,
                timeout=timeout,
                require_private_repository=(
                    lock.profile == "screendance-production"
                    and asset.rights_class == "private"
                ),
            )
            _publish_no_overwrite(root, asset.target, asset)
        except AssetError:
            if asset.required or _identity_under(root, asset.target, asset) != "missing":
                failures.append(asset.asset_id)
    report = inspect("pull", lock, root)
    if failures:
        report["unresolved"] = sorted(set(report["unresolved"]) | set(failures))
        report["ok"] = False
    return report


def _directory_descriptor_matches(path: Path, expected: int) -> bool:
    try:
        current = os.stat(path, follow_symlinks=False)
        opened = os.fstat(expected)
    except OSError:
        return False
    return (
        stat.S_ISDIR(current.st_mode)
        and stat.S_ISDIR(opened.st_mode)
        and (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)
    )


def _fsync_output_parent(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise AssetError("output parent could not be durably synchronized") from exc


def _atomic_json(
    path: Path,
    value: dict,
    *,
    no_overwrite: bool,
    forbidden_root: Path | None = None,
) -> None:
    try:
        parent_path = path.parent.resolve(strict=False)
        parent_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AssetError("output parent could not be prepared safely") from exc
    path = parent_path / path.name
    if forbidden_root is not None:
        _receipt_outside_checkout(path, forbidden_root)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        parent_descriptor = os.open(parent_path, directory_flags)
    except OSError as exc:
        raise AssetError("output parent could not be opened safely") from exc
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    temporary_descriptor = -1
    temporary_name: str | None = None
    published = False
    directory_changed = False

    def parent_is_valid() -> bool:
        if not _directory_descriptor_matches(parent_path, parent_descriptor):
            return False
        if forbidden_root is not None:
            try:
                _receipt_outside_checkout(path, forbidden_root)
            except AssetError:
                return False
        return True

    try:
        if not parent_is_valid():
            raise AssetError("output parent changed before publication")
        temporary_descriptor, temporary_name = _temporary_file_at(
            parent_descriptor,
            prefix=".receipt-",
        )
        if not parent_is_valid():
            raise AssetError("output parent changed before publication")
        with os.fdopen(temporary_descriptor, "wb") as handle:
            temporary_descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if not parent_is_valid():
            raise AssetError("output parent changed before publication")
        if no_overwrite:
            try:
                os.link(
                    temporary_name,
                    path.name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                published = True
                directory_changed = True
            except FileExistsError as exc:
                raise AssetError("output already exists; refusing to overwrite") from exc
            except OSError as exc:
                raise AssetError("output could not be published atomically") from exc
        else:
            try:
                os.replace(
                    temporary_name,
                    path.name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
            except OSError as exc:
                raise AssetError("output could not be published atomically") from exc
            temporary_name = None
            published = True
            directory_changed = True
        if not parent_is_valid():
            try:
                os.unlink(path.name, dir_fd=parent_descriptor)
                published = False
                directory_changed = True
            except FileNotFoundError:
                pass
            raise AssetError("output parent changed during publication")
        if temporary_name is not None:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
            temporary_name = None
            directory_changed = True
        try:
            _fsync_output_parent(parent_descriptor)
        except AssetError:
            if no_overwrite and published:
                try:
                    os.unlink(path.name, dir_fd=parent_descriptor)
                    published = False
                    directory_changed = True
                except FileNotFoundError:
                    pass
            raise
        directory_changed = False
        if not parent_is_valid():
            try:
                os.unlink(path.name, dir_fd=parent_descriptor)
                published = False
                directory_changed = True
                _fsync_output_parent(parent_descriptor)
                directory_changed = False
            except FileNotFoundError:
                pass
            raise AssetError("output parent changed during durable publication")
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
                directory_changed = True
            except FileNotFoundError:
                pass
        if directory_changed:
            try:
                _fsync_output_parent(parent_descriptor)
            except AssetError:
                if published:
                    raise
        os.close(parent_descriptor)


def _receipt_outside_checkout(path: Path, root: Path) -> Path:
    try:
        resolved = path.resolve(strict=False)
    except OSError as exc:
        raise AssetError("receipt path cannot be resolved safely") from exc
    try:
        resolved.relative_to(root)
    except ValueError:
        return resolved
    raise AssetError("receipt output must remain outside the verified checkout")


def inventory(
    source_root: Path,
    output: Path,
    *,
    lock_id: str,
    profile: str,
    repository_commit: str,
    rights_class: str,
) -> dict:
    root = _root(source_root, create=False)
    if not IDENTIFIER.fullmatch(lock_id) or not GIT_SHA.fullmatch(repository_commit):
        raise AssetError("inventory lock id or repository commit is invalid")
    if profile not in PROFILES:
        raise AssetError("inventory profile is invalid")
    if rights_class not in RIGHTS_CLASSES:
        raise AssetError("inventory rights class is invalid")
    rows = []
    production = profile == "screendance-production"
    if production and _repository_head(ROOT) != repository_commit:
        raise AssetError("production inventory does not bind the exact clean script checkout")
    expected = _production_targets(ROOT, repository_commit) if production else None
    snapshot = _inventory_snapshot(root)
    files = [entry for entry in snapshot if entry.kind == "file"]
    targets: set[str] = set()
    casefolded_targets: set[str] = set()
    for entry in files:
        target = _safe_relative(entry.relative, "inventory target")
        if target in targets:
            raise AssetError("inventory repeats an asset target")
        targets.add(target)
        casefolded_target = _portable_path_key(target)
        if casefolded_target in casefolded_targets:
            raise AssetError("inventory contains case-colliding asset targets")
        casefolded_targets.add(casefolded_target)
    actual = targets
    if not actual:
        raise AssetError("inventory source must contain at least one regular file")
    if expected is not None and actual != expected:
        raise AssetError("production inventory source does not contain the exact 487-object target census")
    for entry in files:
        relative = _safe_relative(entry.relative, "inventory target")
        if entry.size is None or entry.sha256 is None:
            raise AssetError("inventory source proof is incomplete")
        size, digest = entry.size, entry.sha256
        media_type = "application/octet-stream"
        suffix = Path(relative).suffix.lower()
        media_type = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".wav": "audio/wav",
            ".mid": "audio/midi",
            ".midi": "audio/midi",
            ".mov": "video/quicktime",
            ".mp4": "video/mp4",
            ".json": "application/json",
            ".sf3": "audio/x-soundfont",
        }.get(suffix, media_type)
        effective_rights = rights_class
        if production:
            effective_rights = "restricted" if relative == ".work/music/MuseScore_General.sf3" else "private"
        if effective_rights in {"private", "restricted"}:
            asset_id = _opaque_asset_id(digest, relative)
        else:
            asset_id = re.sub(r"[^a-z0-9._-]+", "-", relative.lower()).strip("-.")
            if not asset_id or len(asset_id) > 96 or asset_id in {row["id"] for row in rows}:
                asset_id = _opaque_asset_id(digest, relative)
        rows.append(
            {
                "id": asset_id,
                "target": relative,
                "sha256": digest,
                "bytes": size,
                "media_type": media_type,
                "rights_class": effective_rights,
                "required": True,
                "sources": [{"kind": "file", "path": relative}],
            }
        )
    value = {
        "schema": LOCK_SCHEMA,
        "lock_id": lock_id,
        "profile": profile,
        "repository_commit": repository_commit,
        "assets": rows,
    }
    if production:
        _validate_production_assets(
            [
                Asset(
                    row["id"],
                    row["target"],
                    row["sha256"],
                    row["bytes"],
                    row["media_type"],
                    row["rights_class"],
                    row["required"],
                    tuple(row["sources"]),
                )
                for row in rows
            ],
            repository_root=ROOT,
            repository_commit=repository_commit,
        )
    if _inventory_snapshot(root) != snapshot:
        raise AssetError("inventory source changed during its completed scan")
    if production and _repository_head(ROOT) != repository_commit:
        raise AssetError("production inventory lost its exact clean script checkout binding")
    _atomic_json(output, value, no_overwrite=True)
    return value


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--receipt", type=Path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("audit", "verify"):
        _common(commands.add_parser(name))
    pull_parser = commands.add_parser("pull")
    _common(pull_parser)
    pull_parser.add_argument("--allow-file", action="store_true")
    pull_parser.add_argument("--file-source-root", type=Path)
    pull_parser.add_argument("--timeout", type=float, default=60.0)
    inventory_parser = commands.add_parser("inventory")
    inventory_parser.add_argument("--source-root", type=Path, required=True)
    inventory_parser.add_argument("--output", type=Path, required=True)
    inventory_parser.add_argument("--lock-id", required=True)
    inventory_parser.add_argument("--profile", choices=PROFILES, default="generic")
    inventory_parser.add_argument("--repository-commit", required=True)
    inventory_parser.add_argument("--rights-class", choices=RIGHTS_CLASSES, default="private")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inventory":
            value = inventory(
                args.source_root,
                args.output,
                lock_id=args.lock_id,
                profile=args.profile,
                repository_commit=args.repository_commit,
                rights_class=args.rights_class,
            )
            print(f"assets: inventoried {len(value['assets'])} assets", flush=True)
            return 0
        root = _root(args.root, create=args.command == "pull")
        lock = load_lock(args.lock, repository_root=root)
        receipt = _receipt_outside_checkout(args.receipt, root) if args.receipt else None
        if args.command == "pull":
            if args.timeout <= 0:
                raise AssetError("timeout must be positive")
            report = pull(
                lock,
                root,
                allow_file=args.allow_file,
                source_root=args.file_source_root,
                timeout=args.timeout,
            )
        else:
            report = inspect(args.command, lock, root)
        if receipt:
            _atomic_json(
                _receipt_outside_checkout(receipt, root),
                report,
                no_overwrite=True,
                forbidden_root=root,
            )
        print(
            f"assets: {args.command} {'OK' if report['ok'] else 'BLOCKED'} "
            f"({report['counts']['verified']}/{report['counts']['declared']} verified)",
            flush=True,
        )
        return 0 if report["ok"] else 1
    except AssetError as exc:
        print(f"assets: BLOCKED — {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
