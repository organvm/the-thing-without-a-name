#!/usr/bin/env python3
"""Build and verify the deliberately small Danse GitHub Pages artifact.

The repository is not a website root. This builder names the runtime files that
may be published, derives the photographic derivatives from the public corpus
manifest, rejects symlinks and path traversal, and emits a deterministic digest
manifest that binds every delivered byte to the source commit.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import posixpath
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_MANIFEST = "pages-manifest.json"
ARTIFACT_SCHEMA = "danse.pages.v1"
REPOSITORY = "organvm/the-thing-without-a-name"
PUBLIC_TIERS = ("browse", "screen")
ENGINE_MODULES = (
    "engine/choreography.js",
    "engine/clock.js",
    "engine/corpus.js",
    "engine/engine.js",
    "engine/gl.js",
    "engine/grammar.js",
    "engine/mat4.js",
    "engine/program.js",
    "engine/renderer.js",
    "engine/rng.js",
    "engine/room.js",
    "engine/score.js",
)
INTERACTION_MODULES = (
    "interaction/adapter.js",
    "interaction/camera.js",
    "interaction/controller.js",
    "interaction/session.js",
)
VENDOR_BASE = "interaction/vendor/mediapipe"
VENDOR_MANIFEST = f"{VENDOR_BASE}/manifest.json"
RUNTIME_FILES = (
    ".nojekyll",
    "index.html",
    "arrival.js",
    *ENGINE_MODULES,
    *INTERACTION_MODULES,
    VENDOR_MANIFEST,
    "music/score.json",
    "render/program.json",
    "render/choreography.json",
    "sound/browser-midi.js",
)
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
FRAME_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
IMPORT_RE = re.compile(
    r"(?:\bfrom\s*|\bimport\s*(?:\(\s*)?)(?:\"(?P<double>\.[^\"]+)\"|'(?P<single>\.[^']+)')"
)


class ArtifactError(RuntimeError):
    """The public artifact would be incomplete or exceed its declared boundary."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_relative(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ArtifactError(f"{label} must be a non-empty relative path")
    if "\\" in value or value.startswith("/"):
        raise ArtifactError(f"{label} is not a portable relative path: {value!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ArtifactError(f"{label} contains an unsafe path component: {value!r}")
    return PurePosixPath(*parts).as_posix()


def source_file(root: Path, relative: str) -> Path:
    relative = safe_relative(relative, "allowlisted source")
    candidate = root
    for part in PurePosixPath(relative).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ArtifactError(f"allowlisted source is or crosses a symlink: {relative}")
    if not candidate.is_file():
        raise ArtifactError(f"allowlisted source is missing or not a regular file: {relative}")
    return candidate


def corpus_files(root: Path) -> set[str]:
    manifest_path = source_file(root, "corpus/manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"cannot read the public corpus manifest: {exc}") from exc

    if manifest.get("schema") != "danse.corpus.v1":
        raise ArtifactError(f"unsupported corpus schema: {manifest.get('schema')!r}")
    tiers = manifest.get("tiers")
    if not isinstance(tiers, dict) or set(tiers) != set(PUBLIC_TIERS):
        raise ArtifactError(
            "the public corpus manifest must declare exactly the browse and screen tiers"
        )

    frames = manifest.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ArtifactError("the public corpus manifest has no frames")
    frame_ids: list[str] = []
    for frame in frames:
        frame_id = frame.get("id") if isinstance(frame, dict) else None
        if not isinstance(frame_id, str) or not FRAME_ID_RE.fullmatch(frame_id):
            raise ArtifactError(f"unsafe corpus frame id: {frame_id!r}")
        frame_ids.append(frame_id)
    if len(frame_ids) != len(set(frame_ids)):
        raise ArtifactError("the public corpus manifest contains duplicate frame ids")

    room = manifest.get("room")
    room_file = safe_relative(room.get("file") if isinstance(room, dict) else None, "room file")
    if len(PurePosixPath(room_file).parts) != 1 or not room_file.endswith(".webp"):
        raise ArtifactError(f"room file must be one WebP inside corpus/: {room_file!r}")
    score_file = safe_relative(manifest.get("score"), "score file")
    if len(PurePosixPath(score_file).parts) != 1 or not score_file.endswith(".json"):
        raise ArtifactError(f"score file must be one JSON file inside corpus/: {score_file!r}")

    files = {
        "corpus/manifest.json",
        f"corpus/{room_file}",
        f"corpus/{score_file}",
    }
    for tier in PUBLIC_TIERS:
        declaration = tiers[tier]
        if not isinstance(declaration, dict) or declaration.get("local") is not False:
            raise ArtifactError(f"public tier {tier!r} must be explicitly non-local")
        for kind in ("plates", "mattes"):
            template = declaration.get(kind)
            expected = f"{kind}/{tier}/<id>.webp"
            if template != expected:
                raise ArtifactError(
                    f"public tier {tier!r} must declare {kind} as {expected!r}, got {template!r}"
                )
            for frame_id in frame_ids:
                files.add(f"corpus/{template.replace('<id>', frame_id)}")
    return files


def vendor_files(root: Path) -> set[str]:
    """Resolve and authenticate the locally served pose runtime.

    The browser may load only files named by this reviewed manifest. Digesting
    them here keeps a package update from silently expanding the public surface.
    """
    manifest_path = source_file(root, VENDOR_MANIFEST)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"cannot read the pose vendor manifest: {exc}") from exc
    if set(manifest) != {"schema", "package", "model", "patch", "files"}:
        raise ArtifactError("pose vendor manifest has an unknown shape")
    if manifest.get("schema") != "danse.vendor.v1":
        raise ArtifactError(f"unsupported pose vendor schema: {manifest.get('schema')!r}")
    package = manifest.get("package")
    model = manifest.get("model")
    if not isinstance(package, dict) or set(package) != {
        "name", "version", "source", "integrity", "sha512", "license"
    }:
        raise ArtifactError("pose vendor manifest must declare package and model custody")
    if not all(isinstance(package[key], str) and package[key] for key in package):
        raise ArtifactError("pose vendor package custody fields must be non-empty strings")
    if not package["source"].startswith("https://") or not re.fullmatch(
        r"[0-9a-f]{128}", package["sha512"]
    ) or not package["integrity"].startswith("sha512-"):
        raise ArtifactError("pose vendor package source digests are invalid")
    if not isinstance(model, dict) or set(model) != {"name", "version", "source", "license"}:
        raise ArtifactError("pose vendor manifest must declare model custody")
    if not all(isinstance(model[key], str) and model[key] for key in model):
        raise ArtifactError("pose vendor model custody fields must be non-empty strings")
    if not model["source"].startswith("https://"):
        raise ArtifactError("pose vendor model source must be HTTPS")
    patch = manifest.get("patch")
    if not isinstance(patch, dict) or set(patch) != {"reason", "transformations", "upstreamSha256"}:
        raise ArtifactError("pose vendor manifest must declare its deterministic patch")
    if not isinstance(patch["reason"], str) or not patch["reason"]:
        raise ArtifactError("pose vendor deterministic patch must state a reason")
    if not isinstance(patch["transformations"], list) or not all(
        isinstance(item, str) and item for item in patch["transformations"]
    ):
        raise ArtifactError("pose vendor deterministic patch transformations are invalid")
    if not isinstance(patch["upstreamSha256"], dict) or not all(
        isinstance(path, str) and re.fullmatch(r"[0-9a-f]{64}", str(digest))
        for path, digest in patch["upstreamSha256"].items()
    ):
        raise ArtifactError("pose vendor upstream digests are invalid")
    records = manifest.get("files")
    if not isinstance(records, list) or not records:
        raise ArtifactError("pose vendor manifest has no files")

    paths: list[str] = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "bytes", "sha256"}:
            raise ArtifactError("pose vendor manifest contains a malformed file record")
        leaf = safe_relative(record["path"], "pose vendor path")
        if leaf == "manifest.json" or Path(leaf).suffix not in {".js", ".mjs", ".wasm", ".task", ".txt"}:
            raise ArtifactError(f"unsupported pose vendor file: {leaf}")
        relative = f"{VENDOR_BASE}/{leaf}"
        path = source_file(root, relative)
        if not isinstance(record["bytes"], int) or record["bytes"] < 0:
            raise ArtifactError(f"invalid pose vendor byte count for {leaf}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(record["sha256"])):
            raise ArtifactError(f"invalid pose vendor SHA-256 for {leaf}")
        if path.stat().st_size != record["bytes"] or sha256(path) != record["sha256"]:
            raise ArtifactError(f"pose vendor digest mismatch: {leaf}")
        if path.suffix in {".js", ".mjs"}:
            text = path.read_text(encoding="utf-8", errors="strict")
            forbidden = {
                "Date.now": r"\bDate\.now\b",
                "performance.now": r"\bperformance\.now\b",
                "Math.random": r"\bMath\.random\b",
                "crypto.getRandomValues": r"\bgetRandomValues\b",
                "runtime CDN": r"(?:cdn\.jsdelivr\.net|storage\.googleapis\.com|odml\.pa\.googleapis\.com)",
            }
            hit = next((label for label, pattern in forbidden.items() if re.search(pattern, text)), None)
            if hit:
                raise ArtifactError(f"pose vendor runtime contains forbidden {hit}: {leaf}")
        paths.append(relative)
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ArtifactError("pose vendor manifest paths must be unique and sorted")
    upstream_paths = {
        f"{VENDOR_BASE}/{safe_relative(path, 'pose vendor upstream path')}"
        for path in patch["upstreamSha256"]
    }
    if not upstream_paths <= set(paths):
        raise ArtifactError("pose vendor patch names a file outside its delivered inventory")
    return {VENDOR_MANIFEST, *paths}


def _load_release_builder():
    path = ROOT / "scripts/build-release.py"
    spec = importlib.util.spec_from_file_location("danse_pages_release_builder", path)
    if spec is None or spec.loader is None:
        raise ArtifactError("cannot load the release artifact verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def public_release_files(
    release_artifact: Path,
    expected_commit: str,
) -> tuple[dict[str, tuple[Path, dict]], dict]:
    """Verify one public release artifact and select only declared public outputs."""
    release_artifact = release_artifact.absolute()
    if release_artifact.is_symlink() or not release_artifact.is_dir():
        raise ArtifactError("public release artifact is missing or symlinked")
    release_artifact = release_artifact.resolve()
    builder = _load_release_builder()
    try:
        receipt = builder.verify_artifact(release_artifact, expected_commit)
    except Exception as exc:
        raise ArtifactError(f"public release artifact failed verification: {exc}") from exc
    if receipt.get("phase") != "public":
        raise ArtifactError("Pages requires a public-phase release artifact")

    inventory_path = release_artifact / "media/release-media.json"
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"cannot read verified release-media inventory: {exc}") from exc
    if not isinstance(inventory, dict) or set(inventory) != {
        "schema",
        "release_id",
        "version",
        "phase",
        "media",
        "products",
    }:
        raise ArtifactError("verified release-media inventory has an unknown shape")
    if inventory.get("schema") != "danse.release-media.v1" or inventory.get("phase") != "public":
        raise ArtifactError("verified release-media inventory is not public")
    if (
        inventory.get("release_id") != receipt["release"]["id"]
        or inventory.get("version") != receipt["release"]["version"]
    ):
        raise ArtifactError("release-media inventory identity disagrees with its receipt")

    receipt_files = {record["path"]: record for record in receipt["files"]}
    selected: dict[str, tuple[Path, dict]] = {}

    def select(item_id: object, artifact: object, label: str) -> None:
        if not isinstance(item_id, str) or not item_id:
            raise ArtifactError(f"{label} has no stable id")
        if not isinstance(artifact, dict) or set(artifact) != {"id", "path", "bytes", "sha256"}:
            raise ArtifactError(f"{label} has no exact generated artifact identity")
        if artifact["id"] != item_id:
            raise ArtifactError(f"{label} artifact id drifted")
        relative = safe_relative(artifact["path"], f"{label} path")
        expected = receipt_files.get(relative)
        identity = {
            "path": relative,
            "bytes": artifact["bytes"],
            "sha256": artifact["sha256"],
        }
        if expected != identity:
            raise ArtifactError(f"{label} is not bound by the release receipt")
        if relative in selected:
            raise ArtifactError(f"public release destination is duplicated: {relative}")
        selected[relative] = (release_artifact / PurePosixPath(relative), identity)

    media = inventory.get("media")
    if not isinstance(media, list):
        raise ArtifactError("release-media inventory has no external media list")
    media_ids: set[str] = set()
    for row in media:
        if not isinstance(row, dict) or set(row) != {
            "id",
            "kind",
            "label",
            "required_for",
            "status",
            "clearance",
            "alt_text",
            "source",
            "released",
        }:
            raise ArtifactError("release-media inventory contains malformed external media")
        media_id = row["id"]
        if not isinstance(media_id, str) or media_id in media_ids:
            raise ArtifactError("release-media inventory repeats an external media id")
        media_ids.add(media_id)
        phases = row["required_for"]
        if not isinstance(phases, list) or not all(isinstance(item, str) for item in phases):
            raise ArtifactError(f"release media {media_id} has an invalid phase scope")
        if "public" not in phases:
            continue
        if row["status"] != "ready" or row["clearance"] != "cleared":
            raise ArtifactError(f"public release media {media_id} is not admitted")
        select(media_id, row["released"], f"public release media {media_id}")

    products = inventory.get("products")
    if not isinstance(products, list):
        raise ArtifactError("release-media inventory has no generated product list")
    product_ids: set[str] = set()
    for row in products:
        if not isinstance(row, dict) or set(row) != {
            "id",
            "kind",
            "label",
            "required_for",
            "status",
            "path",
            "artifact",
        }:
            raise ArtifactError("release-media inventory contains malformed generated products")
        product_id = row["id"]
        if not isinstance(product_id, str) or product_id in product_ids:
            raise ArtifactError("release-media inventory repeats a generated product id")
        product_ids.add(product_id)
        phases = row["required_for"]
        if not isinstance(phases, list) or not all(isinstance(item, str) for item in phases):
            raise ArtifactError(f"generated product {product_id} has an invalid phase scope")
        if "public" not in phases:
            continue
        artifact = row["artifact"]
        if (
            row["status"] != "ready"
            or not isinstance(artifact, dict)
            or row["path"] != artifact.get("path")
        ):
            raise ArtifactError(f"public generated product {product_id} is not admitted")
        select(product_id, artifact, f"public generated product {product_id}")

    if "project/index.html" not in selected:
        raise ArtifactError("public release artifact does not declare project/index.html")
    release_receipt = release_artifact / builder.ARTIFACT_MANIFEST
    binding = {
        "schema": receipt["schema"],
        "phase": receipt["phase"],
        "release_id": receipt["release"]["id"],
        "version": receipt["release"]["version"],
        "receipt_sha256": sha256(release_receipt),
    }
    return selected, binding


def validate_module_closure(root: Path, files: set[str]) -> None:
    """Fail when a published module refers to a local module outside the boundary."""
    for relative in sorted(files):
        if relative != "index.html" and not relative.endswith((".js", ".mjs")):
            continue
        text = source_file(root, relative).read_text(encoding="utf-8")
        for match in IMPORT_RE.finditer(text):
            reference = match.group("double") or match.group("single")
            reference = reference.split("?", 1)[0].split("#", 1)[0]
            joined = posixpath.normpath(
                posixpath.join(PurePosixPath(relative).parent.as_posix(), reference)
            )
            dependency = safe_relative(joined, f"module dependency from {relative}")
            if dependency not in files:
                raise ArtifactError(
                    f"published module {relative} imports non-public dependency {dependency}"
                )


def source_files(root: Path) -> tuple[str, ...]:
    root = root.absolute()
    if root.is_symlink():
        raise ArtifactError(f"source root must not be a symlink: {root}")
    root = root.resolve()
    if not root.is_dir():
        raise ArtifactError(f"source root is not a regular directory: {root}")
    files = set(RUNTIME_FILES) | corpus_files(root) | vendor_files(root)
    for relative in files:
        source_file(root, relative)
    validate_module_closure(root, files)
    return tuple(sorted(files))


def source_commit(root: Path, explicit: str | None = None) -> str:
    commit = explicit
    if commit is None:
        done = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode != 0:
            raise ArtifactError(f"cannot resolve source commit: {done.stderr.strip()}")
        commit = done.stdout.strip()
    commit = commit.lower()
    if not COMMIT_RE.fullmatch(commit):
        raise ArtifactError(f"source commit must be a full 40-character Git SHA: {commit!r}")
    return commit


def validate_git_source(root: Path, expected_commit: str) -> None:
    """Bind a production CLI build to one clean, exact Git worktree."""
    root = root.absolute().resolve()
    expected_commit = source_commit(root, expected_commit)
    identity = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = identity.stdout.splitlines()
    if identity.returncode != 0 or len(lines) != 2:
        detail = identity.stderr.strip() or "source root is not a Git worktree"
        raise ArtifactError(f"cannot authenticate source checkout: {detail}")
    if Path(lines[0]).resolve() != root:
        raise ArtifactError("source root must be the Git worktree top level")
    actual_commit = lines[1].strip().lower()
    if actual_commit != expected_commit:
        raise ArtifactError(
            f"source commit {expected_commit} does not match checkout HEAD {actual_commit}"
        )
    status = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
            "--ignore-submodules=none",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0:
        raise ArtifactError(f"cannot inspect source checkout: {status.stderr.strip()}")
    if status.stdout:
        raise ArtifactError("source checkout has tracked changes")


def artifact_inventory(root: Path) -> set[str]:
    if root.is_symlink() or not root.is_dir():
        raise ArtifactError(f"artifact root is not a regular directory: {root}")
    files: set[str] = set()
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            path = current_path / name
            if path.is_symlink():
                raise ArtifactError(f"artifact contains a symlinked directory: {path.relative_to(root)}")
        for name in names:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.is_file():
                raise ArtifactError(f"artifact contains a non-regular file: {relative}")
            files.add(relative)
    return files


def verify_artifact(output: Path, expected_commit: str | None = None) -> dict:
    output = output.absolute()
    if output.is_symlink():
        raise ArtifactError(f"artifact root must not be a symlink: {output}")
    output = output.resolve()
    manifest_path = output / ARTIFACT_MANIFEST
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ArtifactError(f"artifact is missing {ARTIFACT_MANIFEST}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"cannot read artifact manifest: {exc}") from exc

    if set(manifest) != {"schema", "source", "release", "files"} or manifest.get("schema") != ARTIFACT_SCHEMA:
        raise ArtifactError("artifact manifest has an unknown shape or schema")
    source = manifest.get("source")
    if not isinstance(source, dict) or set(source) != {"repository", "commit"}:
        raise ArtifactError("artifact manifest has an invalid source receipt")
    if source.get("repository") != REPOSITORY or not COMMIT_RE.fullmatch(str(source.get("commit", ""))):
        raise ArtifactError("artifact manifest source receipt is invalid")
    if expected_commit is not None and source["commit"] != source_commit(output, expected_commit):
        raise ArtifactError(
            f"artifact source commit {source['commit']} does not match expected {expected_commit}"
        )
    release = manifest["release"]
    if release is not None:
        if not isinstance(release, dict) or set(release) != {
            "schema",
            "phase",
            "release_id",
            "version",
            "receipt_sha256",
        }:
            raise ArtifactError("artifact release binding has an unknown shape")
        if (
            release["schema"] != "danse.release-build.v1"
            or release["phase"] != "public"
            or not isinstance(release["release_id"], str)
            or not release["release_id"]
            or not isinstance(release["version"], str)
            or not release["version"]
            or not re.fullmatch(r"[0-9a-f]{64}", str(release["receipt_sha256"]))
        ):
            raise ArtifactError("artifact release binding is invalid")

    records = manifest.get("files")
    if not isinstance(records, list):
        raise ArtifactError("artifact manifest files must be a list")
    paths: list[str] = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "bytes", "sha256"}:
            raise ArtifactError("artifact manifest contains a malformed file record")
        relative = safe_relative(record["path"], "artifact manifest path")
        if relative == ARTIFACT_MANIFEST:
            raise ArtifactError("artifact manifest cannot digest itself")
        if not isinstance(record["bytes"], int) or record["bytes"] < 0:
            raise ArtifactError(f"invalid byte count for {relative}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(record["sha256"])):
            raise ArtifactError(f"invalid SHA-256 for {relative}")
        path = output / PurePosixPath(relative)
        if path.is_symlink() or not path.is_file():
            raise ArtifactError(f"manifest names a missing or non-regular file: {relative}")
        if path.stat().st_size != record["bytes"] or sha256(path) != record["sha256"]:
            raise ArtifactError(f"artifact digest mismatch: {relative}")
        paths.append(relative)

    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ArtifactError("artifact manifest paths must be unique and sorted")
    inventory = artifact_inventory(output)
    expected = set(paths) | {ARTIFACT_MANIFEST}
    if inventory != expected:
        extra = sorted(inventory - expected)
        missing = sorted(expected - inventory)
        raise ArtifactError(f"artifact inventory mismatch; extra={extra}, missing={missing}")
    has_project = "project/index.html" in paths
    if has_project != (release is not None):
        raise ArtifactError("artifact project route disagrees with its public release binding")
    if has_project:
        builder = _load_release_builder()
        try:
            builder.verify_project_links(
                output,
                set(paths),
                require_artwork_root=True,
            )
        except Exception as exc:
            raise ArtifactError(f"artifact project links failed verification: {exc}") from exc
        try:
            project = (output / "project/index.html").read_text(encoding="utf-8")
            builder.verify_project_security(project)
        except Exception as exc:
            raise ArtifactError(f"artifact project security failed verification: {exc}") from exc
    return manifest


def build(
    root: Path,
    output: Path,
    commit: str,
    release_artifact: Path | None = None,
    *,
    require_git_source: bool = False,
) -> dict:
    root = root.absolute()
    if root.is_symlink():
        raise ArtifactError(f"source root must not be a symlink: {root}")
    root = root.resolve()
    output = output.absolute()
    if output.is_symlink():
        raise ArtifactError(f"refusing symlinked artifact output: {output}")
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise ArtifactError(f"artifact output must be absent or empty: {output}")
    output_resolved = output.resolve()
    if output_resolved == root or root in output_resolved.parents:
        raise ArtifactError("artifact output must be outside the source repository")

    commit = source_commit(root, commit)
    if require_git_source:
        validate_git_source(root, commit)
    files = source_files(root)
    release_files: dict[str, tuple[Path, dict]] = {}
    release_binding = None
    if release_artifact is not None:
        release_files, release_binding = public_release_files(release_artifact, commit)
    collisions = set(files) & set(release_files)
    if collisions:
        raise ArtifactError(f"public release outputs collide with the artwork: {sorted(collisions)}")
    output.mkdir(parents=True, exist_ok=True)
    records = []
    for relative in files:
        source = source_file(root, relative)
        target = output / PurePosixPath(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target, follow_symlinks=False)
        target.chmod(0o644)
        os.utime(target, (0, 0), follow_symlinks=False)
        records.append(
            {"path": relative, "bytes": target.stat().st_size, "sha256": sha256(target)}
        )
    for relative in sorted(release_files):
        source, expected = release_files[relative]
        target = output / PurePosixPath(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target, follow_symlinks=False)
        if target.stat().st_size != expected["bytes"] or sha256(target) != expected["sha256"]:
            raise ArtifactError(f"public release output changed while copying: {relative}")
        target.chmod(0o644)
        os.utime(target, (0, 0), follow_symlinks=False)
        records.append(dict(expected))
    records.sort(key=lambda record: record["path"])

    manifest = {
        "schema": ARTIFACT_SCHEMA,
        "source": {"repository": REPOSITORY, "commit": commit},
        "release": release_binding,
        "files": records,
    }
    if require_git_source:
        validate_git_source(root, commit)
    manifest_path = output / ARTIFACT_MANIFEST
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    manifest_path.chmod(0o644)
    os.utime(manifest_path, (0, 0), follow_symlinks=False)
    return verify_artifact(output, commit)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--output", type=Path, help="build a new artifact at this path")
    action.add_argument("--verify", type=Path, help="verify an existing artifact")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    parser.add_argument("--source-commit", help="expected full source commit SHA")
    parser.add_argument(
        "--release-artifact",
        type=Path,
        help="verified public release artifact whose declared outputs may enter Pages",
    )
    args = parser.parse_args()

    try:
        if args.output:
            commit = source_commit(args.root, args.source_commit)
            manifest = build(
                args.root,
                args.output,
                commit,
                release_artifact=args.release_artifact,
                require_git_source=True,
            )
        else:
            manifest = verify_artifact(args.verify, args.source_commit)
    except ArtifactError as exc:
        parser.exit(1, f"pages artifact: {exc}\n")
    print(
        f"pages artifact: {len(manifest['files'])} files from "
        f"{manifest['source']['commit']} verified"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
