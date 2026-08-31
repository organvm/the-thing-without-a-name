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
import ipaddress
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
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
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MEDIA_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
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
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise AssetError(f"{label} must be a safe POSIX-relative path")
    pure = PurePosixPath(value)
    if not pure.parts or pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise AssetError(f"{label} must be a safe POSIX-relative path")
    if pure.parts[0] in {".asset-cache", ".git"}:
        raise AssetError(f"{label} collides with repository control data")
    return pure.as_posix()


def _https_url(value: object) -> str:
    if not isinstance(value, str):
        raise AssetError("HTTPS source URL must be a string")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AssetError("HTTPS source URL violates the source policy")
    try:
        if parsed.port not in {None, 443}:
            raise AssetError("HTTPS source URL must use the default TLS port")
    except ValueError as exc:
        raise AssetError("HTTPS source URL has an invalid port") from exc
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise AssetError("HTTPS source URL cannot target a local host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise AssetError("HTTPS source URL must use a public DNS hostname")
    return value


def _assert_public_https_host(url: str) -> None:
    host = urllib.parse.urlsplit(_https_url(url)).hostname
    assert host is not None
    try:
        addresses = {row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise AssetError("HTTPS source host cannot be resolved") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise AssetError("HTTPS source URL cannot target a non-public address")


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


def load_lock(path: Path) -> Lock:
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
    assets: list[Asset] = []
    ids: set[str] = set()
    targets: set[str] = set()
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
        _validate_production_assets(assets)
    return Lock(
        value["lock_id"],
        value["profile"],
        value["repository_commit"],
        tuple(assets),
        hashlib.sha256(raw).hexdigest(),
    )


def _production_targets() -> set[str]:
    manifest = _json_loads((ROOT / "corpus/manifest.json").read_bytes(), "corpus manifest")
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


def _canonical_production_pins() -> tuple[str, str, str]:
    toolchain = _json_loads(
        (ROOT / "music/audio-toolchain.json").read_bytes(),
        "audio toolchain",
    )
    try:
        register = yaml.safe_load(
            (ROOT / "submission/screendance-2027.yaml").read_text(encoding="utf-8")
        )
        origin_sha256 = register["package"]["origin_still"]["source_sha256"]
        soundfont = toolchain["soundfont"]
        soundfont_sha256 = soundfont["sha256"]
        soundfont_url = soundfont["source_url"]
    except (OSError, TypeError, KeyError, yaml.YAMLError) as exc:
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


def _validate_production_assets(assets: list[Asset]) -> None:
    expected = _production_targets()
    actual = {asset.target for asset in assets}
    if actual != expected or len(assets) != len(expected):
        raise AssetError("production lock does not contain the exact 487-object target census")
    by_target = {asset.target: asset for asset in assets}
    origin_sha256, soundfont_sha256, soundfont_url = _canonical_production_pins()
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
        if asset.target.startswith("pipeline/.work/"):
            if any(source["kind"] not in {"file", "github-release"} for source in asset.sources):
                raise AssetError("private production inputs cannot use public HTTPS locators")
        else:
            if asset.rights_class != "restricted":
                raise AssetError("the licensed soundfont must remain a restricted input")
            for source in asset.sources:
                if source["kind"] == "https" and source["url"] != soundfont_url:
                    raise AssetError("soundfont HTTPS source is not the canonical upstream locator")
        if not asset.asset_id.startswith("asset-"):
            raise AssetError("production input ids must be opaque")


def _sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def _sha256_stream(handle: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    for block in iter(lambda: handle.read(CHUNK), b""):
        size += len(block)
        digest.update(block)
    return size, digest.hexdigest()


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


def _repository_head(root: Path, lock: Lock) -> str:
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
    if status.returncode != 0:
        raise AssetError("Git checkout state cannot be read")
    allowed = {asset.target for asset in lock.assets}
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


def _cache_path(root: Path, asset: Asset, *, create: bool) -> Path:
    relative = f".asset-cache/sha256/{asset.sha256[:2]}/{asset.sha256}"
    return _path_under(root, relative, create_parents=create)


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
        if not stat.S_ISREG(before.st_mode) or before.st_size != asset.size:
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


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _assert_public_https_host(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            old_host = urllib.parse.urlsplit(req.full_url).hostname
            new_host = urllib.parse.urlsplit(newurl).hostname
            if old_host != new_host:
                redirected.remove_header("Authorization")
                redirected.unredirected_hdrs.pop("Authorization", None)
        return redirected


def _open_url(url: str, headers: dict[str, str], timeout: float):
    url = _https_url(url)
    _assert_public_https_host(url)
    request = urllib.request.Request(url, headers=headers)
    return urllib.request.build_opener(_SafeRedirect()).open(request, timeout=timeout)


def _github_release_asset(source: dict, timeout: float) -> tuple[str, dict[str, str]]:
    repository = source["repository"]
    tag = urllib.parse.quote(source["tag"], safe="")
    api = f"https://api.github.com/repos/{repository}/releases/tags/{tag}"
    token = os.environ.get(source.get("token_env", "GITHUB_TOKEN"))
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
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
):
    if source["kind"] == "file":
        if not allow_file or source_root is None:
            raise AssetError("file sources require --allow-file and --file-source-root")
        return _file_source(source, source_root)
    headers = {"User-Agent": USER_AGENT}
    url = source.get("url")
    if source["kind"] == "github-release":
        url, headers = _github_release_asset(source, timeout)
    try:
        return _open_url(url, headers, timeout)
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


def _cache_from_sources(
    root: Path,
    asset: Asset,
    *,
    allow_file: bool,
    source_root: Path | None,
    timeout: float,
) -> Path:
    cache = _cache_path(root, asset, create=True)
    state = _identity(cache, asset)
    if state == "verified":
        return cache
    if state != "missing":
        raise AssetError("content-addressed cache contains a corrupt object")
    errors: list[str] = []
    for index, source in enumerate(asset.sources, start=1):
        descriptor, temporary_name = tempfile.mkstemp(prefix=".asset-", dir=cache.parent)
        temporary = Path(temporary_name)
        try:
            try:
                with os.fdopen(descriptor, "wb") as output, _source_stream(
                    source,
                    allow_file=allow_file,
                    source_root=source_root,
                    timeout=timeout,
                ) as stream:
                    _write_stream_verified(stream, output, asset)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(temporary, 0o444)
                try:
                    os.link(temporary, cache)
                except FileExistsError:
                    pass
                if _identity(cache, asset) != "verified":
                    raise AssetError("cache publication did not preserve asset identity")
                return cache
            except (AssetError, OSError, urllib.error.URLError) as exc:
                errors.append(f"source {index} ({source['kind']}): {exc}")
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    detail = "; ".join(errors) if errors else "no sources declared"
    raise AssetError(f"no source satisfied asset {asset.asset_id}: {detail}")


def _publish_no_overwrite(cache: Path, target: Path, asset: Asset) -> None:
    if os.path.lexists(target):
        state = _identity(target, asset)
        if state == "verified":
            return
        raise AssetError("existing asset target disagrees with the lock; refusing to overwrite")
    try:
        os.link(cache, target)
    except FileExistsError:
        pass
    except OSError as exc:
        raise AssetError("asset target could not be published atomically") from exc
    if _identity(target, asset) != "verified":
        raise AssetError("published asset target disagrees with the lock")


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
    if _repository_head(root, lock) != lock.repository_commit:
        raise AssetError("asset lock does not bind the exact checked-out repository commit")
    rows = []
    for asset in lock.assets:
        target = _path_under(root, asset.target, create_parents=False)
        cache = _cache_path(root, asset, create=False)
        rows.append((asset, _identity(target, asset), _identity(cache, asset)))
    return _report(command, lock, rows)


def pull(
    lock: Lock,
    root: Path,
    *,
    allow_file: bool,
    source_root: Path | None,
    timeout: float,
) -> dict:
    if _repository_head(root, lock) != lock.repository_commit:
        raise AssetError("asset lock does not bind the exact checked-out repository commit")
    failures: list[str] = []
    for asset in lock.assets:
        target = _path_under(root, asset.target, create_parents=True)
        if _identity(target, asset) == "verified":
            continue
        try:
            cache = _cache_from_sources(
                root,
                asset,
                allow_file=allow_file,
                source_root=source_root,
                timeout=timeout,
            )
            _publish_no_overwrite(cache, target, asset)
        except AssetError:
            if asset.required or os.path.lexists(target):
                failures.append(asset.asset_id)
    report = inspect("pull", lock, root)
    if failures:
        report["unresolved"] = sorted(set(report["unresolved"]) | set(failures))
        report["ok"] = False
    return report


def _atomic_json(path: Path, value: dict, *, no_overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if no_overwrite:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise AssetError("output already exists; refusing to overwrite") from exc
        else:
            os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


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
    expected = _production_targets() if profile == "screendance-production" else None
    entries = sorted(root.rglob("*"))
    for path in entries:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise AssetError("inventory source contains a symlink")
        if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
            raise AssetError("inventory source contains a non-regular filesystem entry")
    actual = {
        path.relative_to(root).as_posix()
        for path in entries
        if stat.S_ISREG(path.lstat().st_mode)
    }
    if expected is not None and actual != expected:
        raise AssetError("production inventory source does not contain the exact 487-object target census")
    for path in entries:
        if not stat.S_ISREG(path.lstat().st_mode):
            continue
        relative = path.relative_to(root).as_posix()
        size, digest = _sha256(path)
        media_type = "application/octet-stream"
        suffix = path.suffix.lower()
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
        if profile == "screendance-production":
            effective_rights = "restricted" if relative == ".work/music/MuseScore_General.sf3" else "private"
        if effective_rights in {"private", "restricted"}:
            path_digest = hashlib.sha256(relative.encode()).hexdigest()
            asset_id = f"asset-{digest[:16]}-{path_digest[:12]}"
        else:
            asset_id = re.sub(r"[^a-z0-9._-]+", "-", relative.lower()).strip("-.")
            if not asset_id or len(asset_id) > 96 or asset_id in {row["id"] for row in rows}:
                path_digest = hashlib.sha256(relative.encode()).hexdigest()
                asset_id = f"asset-{digest[:16]}-{path_digest[:12]}"
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
    if profile == "screendance-production":
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
            ]
        )
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
        lock = load_lock(args.lock)
        root = _root(args.root, create=args.command == "pull")
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
        if args.receipt:
            _atomic_json(args.receipt, report, no_overwrite=True)
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
