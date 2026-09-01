#!/usr/bin/env python3
"""Portable checks for the Danse public artifact and its hidden control surface."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]


def load_pages_builder():
    spec = importlib.util.spec_from_file_location(
        "danse_pages_artifact_test", ROOT / "scripts/build-pages.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PAGES = load_pages_builder()
TEST_COMMIT = "a" * 40


def load_browser_runner():
    spec = importlib.util.spec_from_file_location(
        "danse_browser_runner_test", ROOT / "render/browser.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BROWSER = load_browser_runner()


def load_release_support():
    spec = importlib.util.spec_from_file_location(
        "danse_pages_release_test_support",
        ROOT / "scripts/tests/release-manifest.test.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RELEASE_SUPPORT = load_release_support()
RELEASE_BUILD = RELEASE_SUPPORT.BUILD


def add_release_validation_fixture(root: Path) -> None:
    """Add the complete release data contract without replacing the tiny corpus."""
    for relative in RELEASE_SUPPORT.FIXTURE_FILES:
        if relative == "corpus/manifest.json":
            continue
        source = ROOT / relative
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    manifest = RELEASE_SUPPORT.read_manifest(root)
    corpus_digest = PAGES.sha256(root / "corpus/manifest.json")
    corpus_claim = next(
        claim for claim in manifest["claims"] if claim["id"] == "corpus-session"
    )
    corpus_claim["evidence"]["sha256"] = corpus_digest
    RELEASE_SUPPORT.write_manifest(root, manifest)


class Markup(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.by_id: dict[str, tuple[str, dict[str, str | None]]] = {}
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.scripts: list[str] = []
        self._script: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        self.tags.append((tag, values))
        if "id" in values and values["id"] is not None:
            self.by_id[values["id"]] = (tag, values)
        if tag == "script":
            self._script = []

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script is not None:
            self.scripts.append("".join(self._script))
            self._script = None


def write(path: Path, data: bytes = b"fixture\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def public_fixture(root: Path) -> None:
    for relative in PAGES.RUNTIME_FILES:
        write(root / relative)
    write(root / PAGES.PROJECT_MAP, (ROOT / PAGES.PROJECT_MAP).read_bytes())
    vendor_leaf = "vision_bundle.mjs"
    vendor_data = b"export const localFixture = true;\n"
    write(root / PAGES.VENDOR_BASE / vendor_leaf, vendor_data)
    vendor_manifest = {
        "schema": "danse.vendor.v1",
        "package": {
            "name": "fixture",
            "version": "1",
            "source": "https://example.invalid/fixture.tgz",
            "integrity": "sha512-fixture",
            "sha512": "0" * 128,
            "license": "Apache-2.0",
        },
        "model": {
            "name": "fixture",
            "version": "1",
            "source": "https://example.invalid/fixture.task",
            "license": "Apache-2.0",
        },
        "patch": {
            "reason": "fixture is deterministic",
            "transformations": ["fixture"],
            "upstreamSha256": {vendor_leaf: hashlib.sha256(vendor_data).hexdigest()},
        },
        "files": [{
            "path": vendor_leaf,
            "bytes": len(vendor_data),
            "sha256": hashlib.sha256(vendor_data).hexdigest(),
        }],
    }
    write(
        root / PAGES.VENDOR_MANIFEST,
        (json.dumps(vendor_manifest, sort_keys=True) + "\n").encode(),
    )
    manifest = {
        "schema": "danse.corpus.v1",
        "room": {"file": "room.webp"},
        "tiers": {
            tier: {
                "local": False,
                "plates": f"plates/{tier}/<id>.webp",
                "mattes": f"mattes/{tier}/<id>.webp",
            }
            for tier in PAGES.PUBLIC_TIERS
        },
        "score": "score-2017.json",
        "frames": [{"id": "FRAME"}],
    }
    write(root / "corpus/manifest.json", (json.dumps(manifest) + "\n").encode())
    write(root / "corpus/room.webp")
    write(root / "corpus/score-2017.json")
    for tier in PAGES.PUBLIC_TIERS:
        for kind in ("plates", "mattes"):
            write(root / f"corpus/{kind}/{tier}/FRAME.webp")
    write(root / "submission/text/stale.md", b"must stay private\n")
    write(root / "pipeline/private.py", b"must stay private\n")
    write(root / "installation/digital-twin.json", b"internal reference contract\n")
    write(root / "installation/OPERATIONS.md", b"venue operations stay private\n")
    write(root / "README.md", b"repository documentation\n")
    write(root / "corpus/tier-receipts/browse.json", b"internal receipt\n")
    release_manifest = {
        "schema": "danse.release.v1",
        "release_id": "pages-boundary-fixture",
        "status": "draft",
        "media": [],
        "credits": [],
        "gates": [],
    }
    write(
        root / "release/manifest.json",
        (json.dumps(release_manifest, sort_keys=True) + "\n").encode(),
    )
    write(root / "project/index.html", b"unapproved project route\n")


def release_artifact_fixture(base: Path, phase: str) -> tuple[Path, Path]:
    source = RELEASE_SUPPORT.fixture_root(base / f"{phase}-release-source")
    if phase == "public":
        manifest = RELEASE_SUPPORT.complete_manifest(source)
        manifest["status"] = "public-approved"
        RELEASE_SUPPORT.write_manifest(source, manifest)
        RELEASE_SUPPORT.rebind_progressive_receipt(source, manifest)
    output = base / f"{phase}-release-artifact"
    RELEASE_BUILD.build(source, output, phase, TEST_COMMIT)
    return output, source


def authenticated_public_pages_fixture(base: Path) -> tuple[Path, Path, str]:
    """Build public release + Pages outputs from one exact synthetic Git checkout."""
    root = RELEASE_SUPPORT.fixture_root(base / "combined-source")
    manifest = RELEASE_SUPPORT.complete_manifest(root)
    manifest["status"] = "public-approved"
    public_fixture(root)
    contract_bound_runtime = (
        set(RELEASE_SUPPORT.FIXTURE_FILES) & set(PAGES.RUNTIME_FILES)
    ) | {"installation/digital-twin.json"}
    for relative in contract_bound_runtime:
        shutil.copyfile(ROOT / relative, root / relative)
    RELEASE_SUPPORT.write_manifest(root, manifest)
    RELEASE_SUPPORT.rebind_progressive_receipt(root, manifest)
    commit = RELEASE_SUPPORT.initialize_git_fixture(root)
    release_artifact = base / "public-release"
    RELEASE_BUILD.build(
        root,
        release_artifact,
        "public",
        commit,
        require_git_source=True,
    )
    pages_artifact = base / "public-pages"
    PAGES.build(
        root,
        pages_artifact,
        commit,
        release_artifact=release_artifact,
        require_git_source=True,
    )
    return root, pages_artifact, commit


class ProductionArtifactTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        supplied = os.environ.get("DANSE_PAGES_ARTIFACT")
        expected = os.environ.get("DANSE_PAGES_SOURCE_SHA")
        cls._temporary = None
        if supplied:
            cls.output = Path(supplied)
            cls.manifest = PAGES.verify_artifact(
                cls.output,
                expected,
                require_source_manifest=True,
                source_root=ROOT,
            )
        else:
            cls._temporary = tempfile.TemporaryDirectory()
            cls.output = Path(cls._temporary.name) / "pages"
            cls.manifest = PAGES.build(ROOT, cls.output, TEST_COMMIT)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._temporary is not None:
            cls._temporary.cleanup()

    def test_artifact_inventory_is_exactly_the_allowlist_and_digest_manifest(self) -> None:
        allowed = set(PAGES.source_files(ROOT))
        supplied_release = os.environ.get("DANSE_PAGES_RELEASE_ARTIFACT")
        if supplied_release:
            selected, _ = PAGES.public_release_files(
                Path(supplied_release),
                os.environ["DANSE_PAGES_SOURCE_SHA"],
            )
            allowed.update(selected)
        recorded = {record["path"] for record in self.manifest["files"]}
        actual = PAGES.artifact_inventory(self.output)
        self.assertEqual(recorded, allowed)
        self.assertEqual(actual, allowed | {PAGES.ARTIFACT_MANIFEST})
        self.assertEqual(
            self.manifest["source"]["repository"], "organvm/the-thing-without-a-name"
        )

    def test_only_declared_browse_and_screen_derivatives_are_public(self) -> None:
        corpus = json.loads((ROOT / "corpus/manifest.json").read_text())
        frame_count = len(corpus["frames"])
        paths = {record["path"] for record in self.manifest["files"]}
        for tier in PAGES.PUBLIC_TIERS:
            for kind in ("plates", "mattes"):
                prefix = f"corpus/{kind}/{tier}/"
                self.assertEqual(sum(path.startswith(prefix) for path in paths), frame_count)
        self.assertEqual(
            {path for path in paths if path.startswith("engine/")}, set(PAGES.ENGINE_MODULES)
        )
        self.assertIn("engine/choreography.js", paths)
        self.assertNotIn("engine/query.js", paths)
        self.assertNotIn("engine/tier.js", paths)
        self.assertNotIn("corpus/tier-receipts/browse.json", paths)
        self.assertNotIn("corpus/tier-receipts/screen.json", paths)

    def test_pose_runtime_is_local_and_bound_to_its_vendor_manifest(self) -> None:
        paths = {record["path"] for record in self.manifest["files"]}
        vendor = json.loads((ROOT / PAGES.VENDOR_MANIFEST).read_text())
        declared = {
            f"{PAGES.VENDOR_BASE}/{record['path']}" for record in vendor["files"]
        }
        self.assertEqual(
            {path for path in paths if path.startswith(f"{PAGES.VENDOR_BASE}/")},
            declared | {PAGES.VENDOR_MANIFEST},
        )
        camera = (self.output / "interaction/camera.js").read_text(encoding="utf-8")
        bundle = (self.output / PAGES.VENDOR_BASE / "vision_bundle.mjs").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("cdn.jsdelivr", camera)
        self.assertNotIn("storage.googleapis.com", camera)
        self.assertNotIn("odml.pa.googleapis.com", bundle)
        self.assertIn('./vendor/mediapipe/vision_bundle.mjs', camera)

    def test_repository_docs_and_harnesses_are_absent(self) -> None:
        paths = PAGES.artifact_inventory(self.output)
        forbidden = {
            ".github/workflows/pages.yml",
            "AGENTS.md",
            "LINEAGE.json",
            "README.md",
            "done.sh",
            "film.html",
            "interaction-test.html",
            "installation/OPERATIONS.md",
            "installation/digital-twin.json",
            "join.html",
            "probe.html",
            "pyproject.toml",
            "reference/T-2017-full.png",
            "scripts/check-danse.py",
            "submission/screendance-2027.yaml",
            "verify.html",
        }
        self.assertTrue(paths.isdisjoint(forbidden))
        self.assertFalse(any(path.startswith("pipeline/") for path in paths))
        self.assertFalse(any(path.startswith("release/") for path in paths))
        self.assertFalse(any(path.startswith("installation/") for path in paths))
        self.assertFalse(any(path.startswith("submission/") for path in paths))
        self.assertEqual({path for path in paths if path.startswith("music/")}, {"music/score.json"})
        self.assertEqual({path for path in paths if path.startswith("sound/")}, {"sound/browser-midi.js"})
        self.assertEqual(
            {path for path in paths if path.startswith("render/")},
            {"render/program.json", "render/choreography.json"},
        )
        if self.manifest["release"] is None:
            self.assertFalse(any(path.startswith("project/") for path in paths))
        else:
            self.assertIn("project/index.html", paths)
        self.assertFalse(any(path.startswith("rights/") for path in paths))

    def test_every_recorded_sha256_and_byte_count_verifies(self) -> None:
        supplied = bool(os.environ.get("DANSE_PAGES_ARTIFACT"))
        verified = PAGES.verify_artifact(
            self.output,
            os.environ.get("DANSE_PAGES_SOURCE_SHA") or TEST_COMMIT,
            require_source_manifest=supplied,
            source_root=ROOT,
        )
        self.assertEqual(verified, self.manifest)

    def test_deployment_requires_public_rights_before_artifact_upload(self) -> None:
        workflow = (ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8")
        rights = workflow.index("scripts/check-rights.py")
        release_build = workflow.index("scripts/build-release.py")
        release_verify = workflow.index("--verify")
        pages_build = workflow.index("scripts/build-pages.py")
        upload = workflow.index("actions/upload-pages-artifact")
        deploy = workflow.index("actions/deploy-pages")
        self.assertLess(rights, release_build)
        self.assertLess(release_build, release_verify)
        self.assertLess(release_verify, pages_build)
        self.assertLess(pages_build, upload)
        self.assertLess(upload, deploy)
        self.assertIn("--phase public", workflow)
        self.assertIn("--release-manifest release/manifest.json", workflow)
        self.assertIn("--release-artifact", workflow)

    def test_current_pending_release_fails_before_a_public_artifact_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "public-release"
            with self.assertRaisesRegex(RELEASE_SUPPORT.CONTRACT.ReleaseError, "public phase blocked"):
                RELEASE_BUILD.build(ROOT, output, "public", TEST_COMMIT)
            self.assertFalse(output.exists())


class ArtifactBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.base = Path(self._temporary.name)
        self.root = self.base / "repo"
        self.output = self.base / "pages"
        public_fixture(self.root)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def rehash_project_manifest(self) -> None:
        project = self.output / "project/index.html"
        manifest_path = self.output / PAGES.ARTIFACT_MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        record = next(
            record for record in manifest["files"] if record["path"] == "project/index.html"
        )
        record["bytes"] = project.stat().st_size
        record["sha256"] = PAGES.sha256(project)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def test_unlisted_files_are_not_copied(self) -> None:
        PAGES.build(self.root, self.output, TEST_COMMIT)
        inventory = PAGES.artifact_inventory(self.output)
        self.assertNotIn("README.md", inventory)
        self.assertFalse(any(path.startswith("submission/") for path in inventory))
        self.assertFalse(any(path.startswith("pipeline/") for path in inventory))
        self.assertFalse(any(path.startswith("release/") for path in inventory))
        self.assertFalse(any(path.startswith("project/") for path in inventory))
        self.assertFalse(any(path.startswith("installation/") for path in inventory))
        self.assertFalse(any(path.startswith("rights/") for path in inventory))
        self.assertFalse(any(path.startswith("corpus/tier-receipts/") for path in inventory))
        project_map = json.loads((self.output / PAGES.PROJECT_MAP).read_text(encoding="utf-8"))
        self.assertEqual(project_map["schema"], "danse.map.v1")
        self.assertTrue(all(node["href"] is None for node in project_map["nodes"] if node["product_id"]))

    def test_live_project_map_node_is_exactly_the_local_artwork_route(self) -> None:
        map_path = self.root / PAGES.PROJECT_MAP
        original = json.loads(map_path.read_text(encoding="utf-8"))
        mutations = (
            {"route": "javascript:alert(1)", "href": "javascript:alert(1)"},
            {"route": "https://example.invalid/", "href": "https://example.invalid/"},
            {"route": "../private/", "href": "../private/"},
            {"route": "//example.invalid/live", "href": "//example.invalid/live"},
            {"product_id": "project-page-copy"},
            {"fragment": "live"},
            {"availability": "unavailable"},
            {"route": 1, "href": 1},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                project_map = copy.deepcopy(original)
                project_map["nodes"][0].update(mutation)
                map_path.write_text(
                    json.dumps(project_map, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    PAGES.ArtifactError,
                    "canonical local artwork route|malformed node field",
                ):
                    PAGES.project_map(self.root)

    def test_project_map_metadata_and_source_node_identities_are_canonical(self) -> None:
        map_path = self.root / PAGES.PROJECT_MAP
        original = json.loads(map_path.read_text(encoding="utf-8"))

        def swap_fragments(project_map: dict) -> None:
            project_map["nodes"][2]["fragment"], project_map["nodes"][3]["fragment"] = (
                project_map["nodes"][3]["fragment"],
                project_map["nodes"][2]["fragment"],
            )

        mutations = (
            ("title", lambda project_map: project_map.update(title="Rights cleared")),
            ("version", lambda project_map: project_map.update(version=999)),
            (
                "study label",
                lambda project_map: project_map["nodes"][1].update(label="Rights cleared"),
            ),
            ("fragment swap", swap_fragments),
            (
                "evidence label",
                lambda project_map: project_map["nodes"][5].update(label="Publication evidence"),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                project_map = copy.deepcopy(original)
                mutate(project_map)
                map_path.write_text(
                    json.dumps(project_map, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    PAGES.ArtifactError,
                    "metadata or schema is not canonical|source-node identity drifted",
                ):
                    PAGES.project_map(self.root)

    def test_project_map_accepts_only_the_declared_admission_projection(self) -> None:
        map_path = self.root / PAGES.PROJECT_MAP
        original = json.loads(map_path.read_text(encoding="utf-8"))
        source = original["nodes"][1]
        canonical_href = source["route"]
        mutations = (
            {"status": "admitted", "availability": "available", "href": None},
            {"status": "admitted", "availability": "available", "href": "javascript:alert(1)"},
            {"status": "admitted", "availability": "available now", "href": canonical_href},
            {"status": "gated", "availability": "available", "href": canonical_href},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                project_map = copy.deepcopy(original)
                project_map["nodes"][1].update(mutation)
                map_path.write_text(
                    json.dumps(project_map, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    PAGES.ArtifactError, "node resolution is not canonical"
                ):
                    PAGES.project_map(self.root, allow_resolved=True)

    def test_cli_accepts_only_the_clean_exact_git_checkout(self) -> None:
        add_release_validation_fixture(self.root)
        (self.root / "tracked-sentinel.txt").write_text("clean\n", encoding="utf-8")
        commit = RELEASE_SUPPORT.initialize_git_fixture(self.root)
        command = [
            sys.executable,
            str(ROOT / "scripts/build-pages.py"),
            "--root",
            str(self.root),
            "--source-commit",
            commit,
        ]

        clean = subprocess.run(
            [*command, "--output", str(self.output)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertTrue((self.output / PAGES.ARTIFACT_MANIFEST).is_file())

        wrong_output = self.base / "wrong-pages"
        wrong = subprocess.run(
            [
                *command[:-1],
                "b" * 40,
                "--output",
                str(wrong_output),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn("must resolve to a commit object", wrong.stderr)
        self.assertFalse(wrong_output.exists())

        (self.root / "tracked-sentinel.txt").write_text("dirty\n", encoding="utf-8")
        dirty_output = self.base / "dirty-pages"
        dirty = subprocess.run(
            [*command, "--output", str(dirty_output)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(dirty.returncode, 0)
        self.assertIn("tracked changes", dirty.stderr)
        self.assertFalse(dirty_output.exists())

    def test_clean_core_autocrlf_checkout_publishes_only_committed_bytes(self) -> None:
        source = RELEASE_SUPPORT.fixture_root(self.base / "transform-source")
        manifest = RELEASE_SUPPORT.complete_manifest(source)
        manifest["status"] = "public-approved"
        public_fixture(source)
        contract_bound_runtime = (
            set(RELEASE_SUPPORT.FIXTURE_FILES) & set(PAGES.RUNTIME_FILES)
        ) | {"installation/digital-twin.json"}
        for relative in contract_bound_runtime:
            shutil.copyfile(ROOT / relative, source / relative)
        RELEASE_SUPPORT.write_manifest(source, manifest)
        RELEASE_SUPPORT.rebind_progressive_receipt(source, manifest)
        commit = RELEASE_SUPPORT.initialize_git_fixture(source)
        root = self.base / "transform-checkout"
        subprocess.run(
            ["git", "clone", "-q", "--no-checkout", str(source), str(root)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "core.autocrlf", "true"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "checkout", "-q", commit],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn(b"\r\n", (root / "arrival.js").read_bytes())
        committed = subprocess.run(
            ["git", "-C", str(root), "show", f"{commit}:arrival.js"],
            check=True,
            capture_output=True,
        ).stdout
        self.assertNotIn(b"\r\n", committed)
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(status.stdout, "", status.stdout)
        release_output = self.base / "transformed-release"
        release_receipt = RELEASE_BUILD.build(
            root,
            release_output,
            "public",
            commit,
            require_git_source=True,
        )
        selected_medium = next(
            medium
            for medium in manifest["media"]
            if "public" in medium["required_for"]
        )
        source_relative = selected_medium["source"]["path"]
        destination = selected_medium["source"]["destination"]
        committed_medium = subprocess.run(
            ["git", "-C", str(root), "show", f"{commit}:{source_relative}"],
            check=True,
            capture_output=True,
        ).stdout
        self.assertEqual(
            (release_output / destination).read_bytes(),
            committed_medium,
        )
        self.assertNotEqual(
            (release_output / destination).read_bytes(),
            (root / source_relative).read_bytes(),
        )
        output = self.base / "transformed-pages"
        PAGES.build(
            root,
            output,
            commit,
            release_artifact=release_output,
            require_git_source=True,
        )
        self.assertEqual((output / "arrival.js").read_bytes(), committed)
        self.assertNotEqual(
            (output / "arrival.js").read_bytes(),
            (root / "arrival.js").read_bytes(),
        )

        plain = self.base / "plain-checkout"
        subprocess.run(
            ["git", "clone", "-q", str(source), str(plain)],
            check=True,
            capture_output=True,
            text=True,
        )
        RELEASE_BUILD.verify_artifact(
            release_output,
            commit,
            source_root=plain,
        )
        PAGES.verify_artifact(
            output,
            commit,
            require_source_manifest=True,
            source_root=plain,
        )
        self.assertEqual(release_receipt["source"]["commit"], commit)

    def test_git_replacement_objects_cannot_rewrite_published_provenance(self) -> None:
        root = self.base / "replacement-source"
        public_fixture(root)
        claimed_commit = RELEASE_SUPPORT.initialize_git_fixture(root)
        arrival = root / "arrival.js"
        arrival.write_bytes(arrival.read_bytes() + b"// replacement payload\n")
        subprocess.run(
            ["git", "-C", str(root), "add", "arrival.js"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "user.name=Danse Test",
                "-c",
                "user.email=danse-test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "replacement payload",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        replacement_commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(root), "replace", claimed_commit, replacement_commit],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "checkout", "--detach", "-q", claimed_commit],
            check=True,
            capture_output=True,
            text=True,
        )
        rewritten = subprocess.run(
            ["git", "-C", str(root), "show", f"{claimed_commit}:arrival.js"],
            check=True,
            capture_output=True,
        ).stdout
        raw = subprocess.run(
            ["git", "-C", str(root), "show", f"{claimed_commit}:arrival.js"],
            check=True,
            capture_output=True,
            env=PAGES.provenance_git_env(),
        ).stdout
        self.assertNotEqual(rewritten, raw)
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(status.stdout, "", status.stdout)
        with self.assertRaisesRegex(PAGES.ArtifactError, "replacement object refs"):
            PAGES.build(
                root,
                self.base / "replacement-pages",
                claimed_commit,
                require_git_source=True,
            )

    def test_bare_tree_sha_cannot_pose_as_source_commit(self) -> None:
        root = self.base / "tree-source"
        public_fixture(root)
        add_release_validation_fixture(root)
        commit = RELEASE_SUPPORT.initialize_git_fixture(root)
        output = self.base / "tree-pages"
        PAGES.build(root, output, commit, require_git_source=True)
        tree = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD^{tree}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        manifest_path = output / PAGES.ARTIFACT_MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source"]["commit"] = tree
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            PAGES.ArtifactError,
            "must resolve to a commit object",
        ):
            PAGES.verify_artifact(
                output,
                tree,
                require_source_manifest=True,
                source_root=root,
            )

    def test_forged_loose_runtime_object_under_claimed_hash_is_rejected(self) -> None:
        root = self.base / "corrupt-object-source"
        public_fixture(root)
        add_release_validation_fixture(root)
        commit = RELEASE_SUPPORT.initialize_git_fixture(root)
        object_id = subprocess.run(
            ["git", "-C", str(root), "rev-parse", f"{commit}:arrival.js"],
            check=True,
            capture_output=True,
            text=True,
            env=PAGES.provenance_git_env(),
        ).stdout.strip()
        forged = (root / "arrival.js").read_bytes() + b"\n// forged payload\n"
        RELEASE_SUPPORT.replace_loose_object_bytes(root, object_id, "blob", forged)
        accepted_without_integrity_check = subprocess.run(
            ["git", "-C", str(root), "show", f"{commit}:arrival.js"],
            check=True,
            capture_output=True,
            env=PAGES.provenance_git_env(),
        ).stdout
        self.assertEqual(accepted_without_integrity_check, forged)

        with self.assertRaisesRegex(
            PAGES.ArtifactError,
            "failed raw Git object integrity verification",
        ):
            PAGES.build(
                root,
                self.base / "corrupt-object-pages",
                commit,
                require_git_source=True,
            )

    def test_shared_release_provenance_errors_are_translated(self) -> None:
        self.assertEqual(
            Path(PAGES._RELEASE_CONTRACT.__file__).resolve(),
            (ROOT / "scripts/release_contract.py").resolve(),
        )
        with mock.patch.object(
            PAGES,
            "require_release_commit_object",
            side_effect=PAGES.ReleaseContractError("shared provenance rejection"),
        ):
            with self.assertRaisesRegex(
                PAGES.ArtifactError,
                "shared provenance rejection",
            ):
                PAGES.require_commit_object(self.root, TEST_COMMIT)

    def test_release_builder_keeps_its_own_checkout_scoped_contract(self) -> None:
        scoped_before = {
            name for name in sys.modules if name.startswith("danse_release_contract_")
        }
        copies = []
        for name in ("checkout-a", "checkout-b"):
            scripts = self.base / name / "scripts"
            scripts.mkdir(parents=True)
            for leaf in ("build-pages.py", "build-release.py", "release_contract.py"):
                shutil.copyfile(ROOT / "scripts" / leaf, scripts / leaf)
            spec = importlib.util.spec_from_file_location(
                f"pages_{name}",
                scripts / "build-pages.py",
            )
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            copies.append(module)

        first, second = copies
        generic = sys.modules.get("release_contract")
        isolated_path = [
            entry
            for entry in sys.path
            if not entry or Path(entry).name != "scripts"
        ]
        with mock.patch.object(sys, "path", isolated_path):
            builder = first._load_release_builder()
        self.assertIsNot(builder.ReleaseError, second.ReleaseContractError)
        self.assertEqual(
            Path(builder._RELEASE_CONTRACT.__file__).resolve(),
            (self.base / "checkout-a/scripts/release_contract.py").resolve(),
        )
        self.assertEqual(
            Path(first._RELEASE_CONTRACT.__file__).resolve(),
            Path(builder._RELEASE_CONTRACT.__file__).resolve(),
        )
        self.assertNotEqual(
            Path(builder._RELEASE_CONTRACT.__file__).resolve(),
            Path(second._RELEASE_CONTRACT.__file__).resolve(),
        )
        self.assertIs(sys.modules.get("release_contract"), generic)
        self.assertEqual(
            {
                name
                for name in sys.modules
                if name.startswith("danse_release_contract_")
            },
            scoped_before,
        )

    def test_committed_symlink_materialized_as_regular_file_is_rejected(self) -> None:
        root = self.base / "symlink-source"
        public_fixture(root)
        add_release_validation_fixture(root)
        arrival = root / "arrival.js"
        arrival.unlink()
        arrival.symlink_to("index.html")
        commit = RELEASE_SUPPORT.initialize_git_fixture(root)
        subprocess.run(
            ["git", "-C", str(root), "config", "core.symlinks", "false"],
            check=True,
            capture_output=True,
            text=True,
        )
        arrival.unlink()
        subprocess.run(
            ["git", "-C", str(root), "checkout", "--", "arrival.js"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertTrue(arrival.is_file())
        self.assertFalse(arrival.is_symlink())
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(status.stdout, "", status.stdout)

        with self.assertRaisesRegex(
            PAGES.ArtifactError,
            "must be a regular committed file: arrival.js",
        ):
            PAGES.build(
                root,
                self.base / "symlink-pages",
                commit,
                require_git_source=True,
            )

    def test_ambient_git_repository_redirect_cannot_substitute_source(self) -> None:
        root = self.base / "redirected-source"
        public_fixture(root)
        claimed_commit = RELEASE_SUPPORT.initialize_git_fixture(root)
        alternate = self.base / "redirected-alternate"
        subprocess.run(
            ["git", "clone", "-q", str(root), str(alternate)],
            check=True,
            capture_output=True,
            text=True,
        )
        alternate_arrival = alternate / "arrival.js"
        alternate_arrival.write_bytes(
            alternate_arrival.read_bytes() + b"// alternate repository payload\n"
        )
        subprocess.run(
            ["git", "-C", str(alternate), "add", "arrival.js"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(alternate),
                "-c",
                "user.name=Danse Test",
                "-c",
                "user.email=danse-test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "alternate repository payload",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        alternate_commit = subprocess.run(
            ["git", "-C", str(alternate), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        shutil.copy2(alternate_arrival, root / "arrival.js")
        self.assertNotEqual(claimed_commit, alternate_commit)

        redirect = {
            "GIT_DIR": str(alternate / ".git"),
            "GIT_WORK_TREE": str(root),
        }
        ambient = os.environ.copy()
        ambient.update(redirect)
        redirected_identity = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            env=ambient,
        ).stdout.strip()
        redirected_status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            env=ambient,
        ).stdout
        self.assertEqual(redirected_identity, alternate_commit)
        self.assertEqual(redirected_status, "")

        with mock.patch.dict(os.environ, redirect, clear=False):
            with self.assertRaisesRegex(
                PAGES.ArtifactError,
                "must resolve to a commit object",
            ):
                PAGES.build(
                    root,
                    self.base / "redirected-pages",
                    alternate_commit,
                    require_git_source=True,
                )

    def test_provenance_git_environment_scrubs_all_ambient_git_controls(self) -> None:
        controls = {
            "git_dir": "/alternate/repository",
            "GIT_WORK_TREE": "/alternate/worktree",
            "GIT_COMMON_DIR": "/alternate/common",
            "GIT_OBJECT_DIRECTORY": "/alternate/objects",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/alternate/objects-2",
            "GIT_INDEX_FILE": "/alternate/index",
            "GIT_NAMESPACE": "alternate",
            "GIT_SHALLOW_FILE": "/alternate/shallow",
            "GIT_CONFIG": "/alternate/config",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.worktree",
            "GIT_CONFIG_VALUE_0": "/alternate/worktree",
        }
        with mock.patch.dict(os.environ, controls, clear=False):
            clean = PAGES.provenance_git_env()
        for key in controls:
            self.assertNotIn(key, clean)
        self.assertEqual(clean["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual(clean["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(clean["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(clean["GIT_CONFIG_SYSTEM"], os.devnull)
        self.assertEqual(clean["GIT_ATTR_NOSYSTEM"], "1")

    def test_legacy_git_grafts_cannot_rewrite_published_provenance(self) -> None:
        root = self.base / "graft-source"
        public_fixture(root)
        commit = RELEASE_SUPPORT.initialize_git_fixture(root)
        graft_query = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-path", "info/grafts"],
            check=True,
            capture_output=True,
            text=True,
            env=PAGES.provenance_git_env(),
        ).stdout.strip()
        graft = Path(graft_query)
        if not graft.is_absolute():
            graft = root / graft
        graft.parent.mkdir(parents=True, exist_ok=True)
        graft.write_text(f"{commit}\n", encoding="utf-8")
        with self.assertRaisesRegex(PAGES.ArtifactError, "legacy Git graft"):
            PAGES.build(
                root,
                self.base / "grafted-pages",
                commit,
                require_git_source=True,
            )

    def test_verified_public_release_adds_only_declared_outputs_and_keeps_artwork_at_root(self) -> None:
        release, release_source = release_artifact_fixture(self.base, "public")
        selected, binding = PAGES.public_release_files(
            release,
            TEST_COMMIT,
            source_root=release_source,
            allow_worktree_manifest=True,
        )
        root_index = (self.root / "index.html").read_bytes()
        manifest = PAGES.build(
            self.root,
            self.output,
            TEST_COMMIT,
            release_artifact=release,
            release_source_root=release_source,
        )
        paths = {record["path"] for record in manifest["files"]}
        self.assertEqual(manifest["release"], binding)
        self.assertTrue(set(selected) <= paths)
        self.assertIn("project/index.html", paths)
        self.assertIn("pitch/danse-installation-pitch.pdf", paths)
        self.assertIn("accessibility/captions.en.vtt", paths)
        self.assertIn("press/credits.txt", paths)
        self.assertIn("media/assets/press-still-primary.bin", paths)
        self.assertNotIn("media/assets/score-driven-master.bin", paths)
        self.assertEqual((self.output / "index.html").read_bytes(), root_index)
        self.assertNotEqual(
            (self.output / "project/index.html").read_bytes(),
            root_index,
        )
        project_map = json.loads((self.output / PAGES.PROJECT_MAP).read_text(encoding="utf-8"))
        self.assertTrue(all(node["status"] == "admitted" for node in project_map["nodes"]))
        self.assertEqual(
            {node["href"] for node in project_map["nodes"] if node["product_id"]},
            {"./project/", "./project/#cubism", "./project/#glitch", "./project/#ballet-score", "./project/#evidence"},
        )
        markup = Markup()
        markup.feed((self.output / "project/index.html").read_text(encoding="utf-8"))
        self.assertTrue({"cubism", "glitch", "ballet-score", "evidence"} <= set(markup.by_id))
        hrefs = {
            attrs["href"]
            for tag, attrs in markup.tags
            if tag == "a" and attrs.get("href")
        }
        expected_resources = {
            "../pitch/danse-installation-pitch.pdf",
            "../accessibility/accessibility.md",
            "../accessibility/captions.en.vtt",
            "../accessibility/transcript.txt",
            "../press/press-kit.md",
            "../press/credits.txt",
        }
        self.assertTrue(expected_resources <= hrefs)
        for href in hrefs:
            parsed = urlsplit(href)
            if parsed.scheme or parsed.netloc:
                continue
            if not parsed.path:
                if parsed.fragment:
                    self.assertIn(parsed.fragment, markup.by_id, href)
                continue
            target = (self.output / "project" / unquote(parsed.path)).resolve()
            self.assertTrue(target.is_relative_to(self.output.resolve()), href)
            if target.is_dir():
                target = target / "index.html"
            self.assertTrue(target.is_file(), href)
            relative = target.relative_to(self.output.resolve()).as_posix()
            self.assertFalse(
                relative.startswith(("release/", "submission/", "installation/", "rights/")),
                href,
            )
        self.assertFalse(any(path.startswith("release/") for path in paths))
        self.assertFalse(any(path.startswith("installation/") for path in paths))
        self.assertFalse(any(path.startswith("submission/") for path in paths))

    def test_partially_admitted_study_map_fails_closed(self) -> None:
        PAGES.build(self.root, self.output, TEST_COMMIT)
        map_path = self.output / PAGES.PROJECT_MAP
        project_map = json.loads(map_path.read_text(encoding="utf-8"))
        node = next(node for node in project_map["nodes"] if node["product_id"])
        node["status"] = "admitted"
        node["availability"] = "available"
        node["href"] = node["route"] + (
            f"#{node['fragment']}" if node["fragment"] else ""
        )
        map_path.write_text(
            json.dumps(project_map, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest_path = self.output / PAGES.ARTIFACT_MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        record = next(
            record for record in manifest["files"] if record["path"] == PAGES.PROJECT_MAP
        )
        record["bytes"] = map_path.stat().st_size
        record["sha256"] = PAGES.sha256(map_path)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PAGES.ArtifactError, "admits study routes partially"):
            PAGES.verify_artifact(self.output, TEST_COMMIT)

    def test_receipted_project_link_outside_pages_boundary_fails(self) -> None:
        release, release_source = release_artifact_fixture(self.base, "public")
        PAGES.build(
            self.root,
            self.output,
            TEST_COMMIT,
            release_artifact=release,
            release_source_root=release_source,
        )
        project = self.output / "project/index.html"
        project.write_text(
            project.read_text(encoding="utf-8").replace(
                "</main>",
                '<p><a href="../release/manifest.json">Source manifest</a></p></main>',
            ),
            encoding="utf-8",
        )
        self.rehash_project_manifest()
        with self.assertRaisesRegex(PAGES.ArtifactError, "project links failed verification"):
            PAGES.verify_artifact(
                self.output,
                TEST_COMMIT,
                allow_unbound_release_fixture=True,
            )

    def test_rehashed_pages_manifest_cannot_admit_weakened_project_security(self) -> None:
        release, release_source = release_artifact_fixture(self.base, "public")
        PAGES.build(
            self.root,
            self.output,
            TEST_COMMIT,
            release_artifact=release,
            release_source_root=release_source,
        )
        project = self.output / "project/index.html"
        project.write_text(
            project.read_text(encoding="utf-8").replace(
                RELEASE_BUILD.PROJECT_CSP,
                "default-src * 'unsafe-inline' 'unsafe-eval'",
            ),
            encoding="utf-8",
        )
        self.rehash_project_manifest()
        with self.assertRaisesRegex(PAGES.ArtifactError, "project security failed verification"):
            PAGES.verify_artifact(
                self.output,
                TEST_COMMIT,
                allow_unbound_release_fixture=True,
            )

    def test_rehashed_pages_manifest_cannot_admit_browser_security_bypasses(self) -> None:
        attacks = {
            "inert-template": lambda value: value.replace(
                f'  <meta http-equiv="Content-Security-Policy" content="{RELEASE_BUILD.PROJECT_CSP}">\n',
                "",
            ).replace(
                '  <meta name="referrer" content="no-referrer">\n',
                "",
            ).replace(
                "</head>",
                '<template><meta http-equiv="Content-Security-Policy" '
                f'content="{RELEASE_BUILD.PROJECT_CSP}">'
                '<meta name="referrer" content="no-referrer"></template></head>',
            ),
            "referrer-override": lambda value: value.replace(
                '<a class="skip" href="#content">',
                '<a class="skip" href="#content" referrerpolicy="unsafe-url">',
            ),
            "dns-prefetch": lambda value: value.replace(
                "</head>",
                '<link rel="dns-prefetch" href="//attacker.example"></head>',
            ),
            "unmanifested-image": lambda value: value.replace(
                "</main>",
                '<img alt="unmanifested" '
                'src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">'
                "</main>",
            ),
            "head-after-body": lambda value: value.replace(
                "<head>", "<body></body><head>", 1
            ),
            "image-input": lambda value: value.replace(
                "</main>",
                '<input type="image" alt="unmanifested" '
                'src="https://attacker.example/pixel.png"></main>',
            ),
            "external-open-graph-image": lambda value: value.replace(
                "</head>",
                '<meta property="og:image" '
                'content="https://attacker.example/unproven.jpg"></head>',
            ),
            "external-canonical": lambda value: value.replace(
                RELEASE_BUILD.PROJECT_CANONICAL_URL,
                "https://attacker.example/project/",
            ),
            "pre-csp-html-style": lambda value: value.replace(
                '<html lang="en">',
                '<html lang="en" '
                'style="background-image:url(https://attacker.example/pixel.png)">',
            ),
        }
        for label, attack in attacks.items():
            with self.subTest(attack=label):
                case = self.base / label
                output = case / "pages"
                release, release_source = release_artifact_fixture(case, "public")
                PAGES.build(
                    self.root,
                    output,
                    TEST_COMMIT,
                    release_artifact=release,
                    release_source_root=release_source,
                )
                self.output = output
                project = output / "project/index.html"
                project.write_text(
                    attack(project.read_text(encoding="utf-8")),
                    encoding="utf-8",
                )
                self.rehash_project_manifest()
                with self.assertRaisesRegex(
                    PAGES.ArtifactError,
                    "project security failed verification",
                ):
                    PAGES.verify_artifact(
                        output,
                        TEST_COMMIT,
                        allow_unbound_release_fixture=True,
                    )

    def test_rehashed_pages_manifest_cannot_change_public_claims(self) -> None:
        release, release_source = release_artifact_fixture(self.base, "public")
        PAGES.build(
            self.root,
            self.output,
            TEST_COMMIT,
            release_artifact=release,
            release_source_root=release_source,
        )
        attacks = {
            "title": lambda value: value.replace(
                "<title>Danse - a room that never repeats | Project</title>",
                "<title>Unreceipted exhibition claim</title>",
            ),
            "description": lambda value: value.replace(
                'name="description" content="',
                'name="description" content="Unreceipted synopsis. ',
                1,
            ),
            "heading": lambda value: value.replace(
                "<h1>THE THING WITHOUT A NAME</h1>",
                "<h1>Unreceipted public title</h1>",
            ),
            "status-copy": lambda value: value.replace(
                "8 gates remain blocked in the bound reference ledger",
                "Every installation gate is approved worldwide",
            ),
        }
        baseline = self.output
        for label, attack in attacks.items():
            with self.subTest(attack=label):
                case = self.base / f"pages-claims-{label}"
                shutil.copytree(baseline, case)
                self.output = case
                project = case / "project/index.html"
                project.write_text(
                    attack(project.read_text(encoding="utf-8")),
                    encoding="utf-8",
                )
                self.rehash_project_manifest()
                with self.assertRaisesRegex(
                    PAGES.ArtifactError,
                    "project bytes drifted from the verified release receipt",
                ):
                    PAGES.verify_artifact(
                        case,
                        TEST_COMMIT,
                        allow_unbound_release_fixture=True,
                    )
        self.output = baseline

    def test_release_bearing_artifact_cannot_skip_source_authentication(self) -> None:
        release, release_source = release_artifact_fixture(self.base, "public")
        PAGES.build(
            self.root,
            self.output,
            TEST_COMMIT,
            release_artifact=release,
            release_source_root=release_source,
        )
        project = self.output / "project/index.html"
        project.write_text(
            project.read_text(encoding="utf-8").replace(
                "THE THING WITHOUT A NAME",
                "UNRECEIPTED PUBLIC CLAIM",
            ),
            encoding="utf-8",
        )
        manifest_path = self.output / PAGES.ARTIFACT_MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        record = next(
            item for item in manifest["files"] if item["path"] == "project/index.html"
        )
        record["bytes"] = project.stat().st_size
        record["sha256"] = PAGES.sha256(project)
        manifest["release"]["project_sha256"] = record["sha256"]
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            PAGES.ArtifactError,
            "release-bearing artifact verification requires source-manifest",
        ):
            PAGES.verify_artifact(self.output, TEST_COMMIT)

    def test_missing_or_draft_release_artifact_fails_before_pages_bytes(self) -> None:
        missing = self.base / "missing-release"
        with self.assertRaisesRegex(PAGES.ArtifactError, "missing or symlinked"):
            PAGES.build(
                self.root,
                self.output,
                TEST_COMMIT,
                release_artifact=missing,
            )
        self.assertFalse(self.output.exists())

        draft, draft_source = release_artifact_fixture(self.base, "draft")
        with self.assertRaisesRegex(PAGES.ArtifactError, "public-phase"):
            PAGES.build(
                self.root,
                self.output,
                TEST_COMMIT,
                release_artifact=draft,
                release_source_root=draft_source,
            )
        self.assertFalse(self.output.exists())

    def test_release_artifact_wrong_sha_tamper_and_extra_file_fail_closed(self) -> None:
        release, release_source = release_artifact_fixture(self.base, "public")
        with self.assertRaisesRegex(PAGES.ArtifactError, "does not match expected"):
            PAGES.build(
                self.root,
                self.output,
                "b" * 40,
                release_artifact=release,
                release_source_root=release_source,
            )
        self.assertFalse(self.output.exists())

        (release / "project/index.html").write_bytes(b"tampered\n")
        with self.assertRaisesRegex(PAGES.ArtifactError, "digest mismatch"):
            PAGES.build(
                self.root,
                self.output,
                TEST_COMMIT,
                release_artifact=release,
                release_source_root=release_source,
            )
        self.assertFalse(self.output.exists())

        release, release_source = release_artifact_fixture(self.base / "extra", "public")
        write(release / "unrecorded-private.txt")
        with self.assertRaisesRegex(PAGES.ArtifactError, "inventory mismatch"):
            PAGES.build(
                self.root,
                self.output,
                TEST_COMMIT,
                release_artifact=release,
                release_source_root=release_source,
            )
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_symlinked_release_artifact_or_file_fails_closed(self) -> None:
        release, release_source = release_artifact_fixture(self.base, "public")
        alias = self.base / "release-alias"
        alias.symlink_to(release, target_is_directory=True)
        with self.assertRaisesRegex(PAGES.ArtifactError, "missing or symlinked"):
            PAGES.build(
                self.root,
                self.output,
                TEST_COMMIT,
                release_artifact=alias,
                release_source_root=release_source,
            )
        self.assertFalse(self.output.exists())

        project = release / "project/index.html"
        outside = self.base / "outside-project.html"
        write(outside, project.read_bytes())
        project.unlink()
        project.symlink_to(outside)
        with self.assertRaisesRegex(PAGES.ArtifactError, "non-regular"):
            PAGES.build(
                self.root,
                self.output,
                TEST_COMMIT,
                release_artifact=release,
                release_source_root=release_source,
            )
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_allowlisted_source_symlink_fails_closed(self) -> None:
        target = self.base / "outside.html"
        write(target, b"outside\n")
        (self.root / "index.html").unlink()
        (self.root / "index.html").symlink_to(target)
        with self.assertRaisesRegex(PAGES.ArtifactError, "symlink"):
            PAGES.build(self.root, self.output, TEST_COMMIT)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_allowlisted_source_directory_symlink_fails_closed(self) -> None:
        public = self.root / "corpus/plates/screen"
        outside = self.base / "outside-screen"
        write(outside / "FRAME.webp", b"outside\n")
        shutil.rmtree(public)
        public.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(PAGES.ArtifactError, "symlink"):
            PAGES.build(self.root, self.output, TEST_COMMIT)

    def test_manifest_path_escape_fails_closed(self) -> None:
        path = self.root / "corpus/manifest.json"
        manifest = json.loads(path.read_text())
        manifest["tiers"]["browse"]["plates"] = "../../submission/<id>.webp"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(PAGES.ArtifactError, "must declare"):
            PAGES.build(self.root, self.output, TEST_COMMIT)

    def test_unsafe_frame_id_fails_closed(self) -> None:
        path = self.root / "corpus/manifest.json"
        manifest = json.loads(path.read_text())
        manifest["frames"][0]["id"] = "../private"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(PAGES.ArtifactError, "unsafe corpus frame id"):
            PAGES.build(self.root, self.output, TEST_COMMIT)

    def test_tampered_artifact_digest_fails_closed(self) -> None:
        PAGES.build(self.root, self.output, TEST_COMMIT)
        (self.output / "arrival.js").write_bytes(b"tampered\n")
        with self.assertRaisesRegex(PAGES.ArtifactError, "digest mismatch"):
            PAGES.verify_artifact(self.output, TEST_COMMIT)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_symlink_in_built_artifact_fails_closed(self) -> None:
        PAGES.build(self.root, self.output, TEST_COMMIT)
        target = self.base / "outside.html"
        write(target, b"outside\n")
        published = self.output / "index.html"
        published.unlink()
        published.symlink_to(target)
        with self.assertRaisesRegex(PAGES.ArtifactError, "non-regular file"):
            PAGES.verify_artifact(self.output, TEST_COMMIT)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_symlinked_artifact_root_fails_closed(self) -> None:
        PAGES.build(self.root, self.output, TEST_COMMIT)
        alias = self.base / "pages-alias"
        alias.symlink_to(self.output, target_is_directory=True)
        with self.assertRaisesRegex(PAGES.ArtifactError, "root must not be a symlink"):
            PAGES.verify_artifact(alias, TEST_COMMIT)

    def test_wrong_deployed_source_sha_fails_closed(self) -> None:
        PAGES.build(self.root, self.output, TEST_COMMIT)
        with self.assertRaisesRegex(PAGES.ArtifactError, "does not match expected"):
            PAGES.verify_artifact(self.output, "b" * 40)

    def test_source_manifest_verification_uses_requested_root(self) -> None:
        source_root, output, commit = authenticated_public_pages_fixture(
            self.base / "requested-root"
        )
        command = [
            sys.executable,
            str(ROOT / "scripts/build-pages.py"),
            "--root",
            str(source_root),
            "--verify",
            str(output),
            "--source-commit",
            commit,
        ]
        verified = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertIn(f"files from {commit} verified", verified.stdout)

    def test_pages_release_requires_its_receipted_generation_toolchain(self) -> None:
        source_root, output, commit = authenticated_public_pages_fixture(
            self.base / "historical-toolchain"
        )
        manifest_path = output / PAGES.ARTIFACT_MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for dependency in ("python", "pypdf", "reportlab"):
            with self.subTest(dependency=dependency):
                case = copy.deepcopy(manifest)
                case["release"]["toolchain"][dependency] += "-future"
                manifest_path.write_text(
                    json.dumps(case, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    PAGES.ArtifactError,
                    "exact receipted generation toolchain",
                ):
                    PAGES.verify_artifact(
                        output,
                        commit,
                        require_source_manifest=True,
                        source_root=source_root,
                    )
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def test_pages_public_phase_validates_committed_media_evidence(self) -> None:
        source_root, output, _commit = authenticated_public_pages_fixture(
            self.base / "forged-evidence"
        )
        source_manifest_path = source_root / "release/manifest.json"
        source_manifest = json.loads(
            source_manifest_path.read_text(encoding="utf-8")
        )
        medium = next(
            item
            for item in source_manifest["media"]
            if item["id"] == "press-still-primary"
        )
        medium["source"]["sha256"] = "0" * 64
        RELEASE_SUPPORT.write_manifest(source_root, source_manifest)
        subprocess.run(
            ["git", "-C", str(source_root), "add", "release/manifest.json"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(source_root),
                "-c",
                "user.name=Danse Test",
                "-c",
                "user.email=danse-test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "forge Pages media evidence",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        forged_commit = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        committed_manifest = subprocess.run(
            [
                "git",
                "-C",
                str(source_root),
                "show",
                f"{forged_commit}:release/manifest.json",
            ],
            check=True,
            capture_output=True,
        ).stdout
        pages_manifest_path = output / PAGES.ARTIFACT_MANIFEST
        pages_manifest = json.loads(
            pages_manifest_path.read_text(encoding="utf-8")
        )
        pages_manifest["source"]["commit"] = forged_commit
        pages_manifest["release"]["manifest_sha256"] = hashlib.sha256(
            committed_manifest
        ).hexdigest()
        pages_manifest_path.write_text(
            json.dumps(pages_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            PAGES.ArtifactError,
            "media press-still-primary source digest mismatch",
        ):
            PAGES.verify_artifact(
                output,
                forged_commit,
                require_source_manifest=True,
                source_root=source_root,
            )

    def test_self_rehashed_pages_manifest_cannot_add_private_file(self) -> None:
        add_release_validation_fixture(self.root)
        commit = RELEASE_SUPPORT.initialize_git_fixture(self.root)
        PAGES.build(
            self.root,
            self.output,
            commit,
            require_git_source=True,
        )
        private = self.output / "release/private.txt"
        write(private, b"private source bytes\n")
        manifest_path = self.output / PAGES.ARTIFACT_MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"].append(
            {
                "path": "release/private.txt",
                "bytes": private.stat().st_size,
                "sha256": PAGES.sha256(private),
            }
        )
        manifest["files"].sort(key=lambda record: record["path"])
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PAGES.ArtifactError, "extra=.*release/private.txt"):
            PAGES.verify_artifact(
                self.output,
                commit,
                require_source_manifest=True,
                source_root=self.root,
            )

    def test_self_rehashed_pages_manifest_cannot_substitute_runtime_bytes(self) -> None:
        add_release_validation_fixture(self.root)
        commit = RELEASE_SUPPORT.initialize_git_fixture(self.root)
        PAGES.build(
            self.root,
            self.output,
            commit,
            require_git_source=True,
        )
        runtime = self.output / "arrival.js"
        runtime.write_bytes(b"self-receipted replacement runtime\n")
        manifest_path = self.output / PAGES.ARTIFACT_MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        record = next(item for item in manifest["files"] if item["path"] == "arrival.js")
        record["bytes"] = runtime.stat().st_size
        record["sha256"] = PAGES.sha256(runtime)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PAGES.ArtifactError, "changed=.*arrival.js"):
            PAGES.verify_artifact(
                self.output,
                commit,
                require_source_manifest=True,
                source_root=self.root,
            )

    def test_self_rehashed_pages_manifest_cannot_substitute_public_press_copy(self) -> None:
        source_root, output, commit = authenticated_public_pages_fixture(
            self.base / "press-substitution"
        )
        press = output / "press/press-kit.md"
        press.write_text(
            "# False approved biography and rights claim\n",
            encoding="utf-8",
        )
        manifest_path = output / PAGES.ARTIFACT_MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        record = next(
            item for item in manifest["files"] if item["path"] == "press/press-kit.md"
        )
        record["bytes"] = press.stat().st_size
        record["sha256"] = PAGES.sha256(press)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PAGES.ArtifactError, "changed=.*press/press-kit.md"):
            PAGES.verify_artifact(
                output,
                commit,
                require_source_manifest=True,
                source_root=source_root,
            )

    def test_self_rehashed_pages_manifest_cannot_omit_public_release(self) -> None:
        source_root, output, commit = authenticated_public_pages_fixture(
            self.base / "release-omission"
        )
        manifest_path = output / PAGES.ARTIFACT_MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        runtime_paths = set(PAGES.source_files(source_root))
        release_paths = {
            record["path"]
            for record in manifest["files"]
            if record["path"] not in runtime_paths
        }
        self.assertTrue(release_paths)
        for relative in release_paths:
            (output / relative).unlink()
        manifest["files"] = [
            record
            for record in manifest["files"]
            if record["path"] in runtime_paths
        ]
        manifest["release"] = None
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            PAGES.ArtifactError,
            "admits public phase .* required release binding and files are absent",
        ):
            PAGES.verify_artifact(
                output,
                commit,
                require_source_manifest=True,
                source_root=source_root,
            )

    def test_tampered_pose_vendor_source_fails_closed(self) -> None:
        vendor = self.root / PAGES.VENDOR_BASE / "vision_bundle.mjs"
        vendor.write_bytes(b"tampered\n")
        with self.assertRaisesRegex(PAGES.ArtifactError, "pose vendor digest mismatch"):
            PAGES.build(self.root, self.output, TEST_COMMIT)

    def test_digest_valid_pose_vendor_with_external_runtime_fails_closed(self) -> None:
        vendor = self.root / PAGES.VENDOR_BASE / "vision_bundle.mjs"
        data = b'fetch("https://odml.pa.googleapis.com/v1/log");\n'
        vendor.write_bytes(data)
        manifest_path = self.root / PAGES.VENDOR_MANIFEST
        manifest = json.loads(manifest_path.read_text())
        manifest["files"][0]["bytes"] = len(data)
        manifest["files"][0]["sha256"] = hashlib.sha256(data).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(PAGES.ArtifactError, "forbidden runtime CDN"):
            PAGES.build(self.root, self.output, TEST_COMMIT)

    def test_pose_vendor_module_cannot_import_outside_public_boundary(self) -> None:
        vendor = self.root / PAGES.VENDOR_BASE / "vision_bundle.mjs"
        data = b'import "../../../submission/private.js";\n'
        vendor.write_bytes(data)
        manifest_path = self.root / PAGES.VENDOR_MANIFEST
        manifest = json.loads(manifest_path.read_text())
        manifest["files"][0]["bytes"] = len(data)
        manifest["files"][0]["sha256"] = hashlib.sha256(data).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(PAGES.ArtifactError, "imports non-public dependency"):
            PAGES.build(self.root, self.output, TEST_COMMIT)


class InterfaceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "index.html").read_text(encoding="utf-8")
        cls.markup = Markup()
        cls.markup.feed(cls.html)
        cls.script = "\n".join(cls.markup.scripts)
        cls.styles = (ROOT / "interface/styles.css").read_text(encoding="utf-8")

    def test_browser_launcher_requires_the_canonical_apple_metal_identity(self) -> None:
        runner = (ROOT / "render/browser.py").read_text(encoding="utf-8")
        self.assertIn("if not renderer_matches_context(name, render_context):", runner)
        self.assertTrue(
            BROWSER.renderer_matches_context(
                "ANGLE (Apple, ANGLE Metal Renderer: Apple M5 Pro, Unspecified Version)",
                BROWSER.CANONICAL_RENDER_CONTEXT,
            )
        )
        self.assertFalse(
            BROWSER.renderer_matches_context(
                "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device), SwiftShader driver)",
                BROWSER.CANONICAL_RENDER_CONTEXT,
            )
        )
        self.assertNotIn("any(w in name.lower() for w in WANTED)", runner)
        self.assertEqual(
            BROWSER.APPLE_ANGLE_METAL_RENDERER.pattern,
            RELEASE_SUPPORT.CONTRACT.APPLE_ANGLE_METAL_RENDERER.pattern,
        )
        accepted = (
            "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)",
            "ANGLE (Apple, ANGLE Metal Renderer: Apple M5 Pro, Unspecified Version)",
        )
        rejected = (
            "Apple Metal Renderer",
            "ANGLE (Apple, OpenGL Renderer: Apple M5, Unspecified Version)",
            "ANGLE (Google, ANGLE Metal Renderer: Apple M5, Unspecified Version)",
            "ANGLE (Apple, ANGLE Metal Renderer: Apple M5, SwiftShader software rasterizer)",
            "ANGLE (Apple, ANGLE Metal Renderer: Apple M5, Unspecified Version) trailing",
            "ANGLE (Apple, ANGLE Metal Renderer: Apple M5, Unspecified Version)\n",
            "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device), SwiftShader driver)",
        )
        for renderer in accepted:
            with self.subTest(renderer=renderer):
                self.assertIsNotNone(BROWSER.APPLE_ANGLE_METAL_RENDERER.fullmatch(renderer))
        for renderer in rejected:
            with self.subTest(renderer=renderer):
                self.assertIsNone(BROWSER.APPLE_ANGLE_METAL_RENDERER.fullmatch(renderer))

    def test_five_category_surface_and_advanced_sheet_are_accessible(self) -> None:
        tag, hud = self.markup.by_id["hud"]
        self.assertEqual(tag, "section")
        self.assertIn("hidden", hud)
        self.assertEqual(hud["aria-hidden"], "true")
        tag, toggle = self.markup.by_id["hud-toggle"]
        self.assertEqual(tag, "button")
        self.assertEqual(toggle["type"], "button")
        self.assertIn("hidden", toggle)
        self.assertEqual(toggle["aria-hidden"], "true")
        self.assertEqual(toggle["aria-controls"], "hud")
        self.assertEqual(toggle["aria-expanded"], "false")
        self.assertEqual(toggle["aria-label"], "Show Danse controls")
        tag, dock = self.markup.by_id["danse-dock"]
        self.assertEqual((tag, dock["aria-label"]), ("nav", "Danse control categories"))
        self.assertIn("hidden", dock)
        self.assertEqual(dock["aria-hidden"], "true")
        categories = [
            attrs["data-category"]
            for tag, attrs in self.markup.tags
            if tag == "button" and attrs.get("data-category")
        ]
        self.assertEqual(categories, ["hold", "river", "score", "presence", "map"])
        self.assertIn('#danse-dock[hidden],#surface-tray[hidden],#hud[hidden],#hud-toggle[hidden] { display:none; }', self.styles)
        self.assertIn("min-width: 48px; min-height: 48px", self.html)

    def test_keyboard_buttons_and_browser_probe_share_named_actions(self) -> None:
        self.assertIn("function setHudVisible(visible)", self.script)
        self.assertIn('hud.setAttribute("aria-hidden", String(!visible))', self.script)
        self.assertIn('hudToggle.setAttribute("aria-expanded", String(visible))', self.script)
        self.assertIn('hudToggle.addEventListener("click", toggleControls)', self.script)
        self.assertIn("const command = shortcutAction(event)", self.script)
        self.assertIn("createControlActions({", self.script)
        self.assertIn("actions: controlBus.actions", self.script)
        self.assertIn("keyboard-instructions", self.markup.by_id)
        self.assertIn("touch-instructions", self.markup.by_id)

    def test_controls_initialize_before_listening_and_open_the_project_atomically(self) -> None:
        bus = self.script.index("controlBus = createControlActions({")
        listener = self.script.index('hudToggle.addEventListener("click", toggleControls)')
        reveal = self.script.index("dock.hidden = false;")
        self.assertLess(bus, listener)
        self.assertLess(listener, reveal)
        self.assertLess(self.script.index("projectMap.addEventListener(\"cancel\""), reveal)
        self.assertLess(self.script.index('addEventListener("keydown"'), reveal)
        self.assertIn('dock.removeAttribute("aria-hidden")', self.script[reveal:])
        self.assertIn("hudToggle.hidden = false;", self.script[reveal:])
        self.assertIn('hudToggle.removeAttribute("aria-hidden")', self.script[reveal:])
        toggle = self.script.split("function toggleControls() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("if (!controlBus) return;", toggle)
        self.assertIn("if (projectMap.open) controlBus.actions.close();", toggle)

    def test_renderer_failure_keeps_the_controls_and_keyboard_fail_readable(self) -> None:
        self.assertIn("renderer-fallback", self.markup.by_id)
        initialization = self.script.split("let baseRenderer = null;", 1)[1].split("// Non-null", 1)[0]
        self.assertIn("try {", initialization)
        self.assertIn("let candidateRenderer = null;", initialization)
        self.assertIn("candidateRenderer = new Renderer(canvas, corpus);", initialization)
        self.assertIn("await corpus.prime(candidateRenderer.gl", initialization)
        self.assertLess(
            initialization.index("await corpus.prime(candidateRenderer.gl"),
            initialization.index("baseRenderer = candidateRenderer;"),
        )
        self.assertIn("baseRenderer = null;", initialization.split("catch (error)", 1)[1])
        self.assertIn('el("renderer-fallback").hidden = false;', initialization)
        self.assertIn("renderer-fallback-reason", self.markup.by_id)
        self.assertIn(
            'el("renderer-fallback-reason").textContent = rendererFallbackMessage;',
            initialization,
        )
        self.assertIn(
            'el("stage-description").textContent = rendererFallbackMessage;',
            initialization,
        )
        self.assertIn("const rendererUnavailableReason = candidateRenderer", initialization)
        self.assertIn('"a required corpus image or texture could not be prepared"', initialization)
        self.assertIn('"WebGL2 could not initialize"', initialization)
        self.assertIn("Danse visual renderer unavailable: ${rendererUnavailableReason}", initialization)
        frame = self.script.split("function frame() {", 1)[1].split("\n}", 1)[0]
        self.assertLess(frame.index("interaction.tick("), frame.index("if (!renderer)"))
        self.assertIn("const fallback = step(corpus, river.seed, t, program", frame)
        self.assertIn("renderDetails(t, fallback.state);", frame)
        self.assertIn("requestAnimationFrame(frame);", frame.split("if (!renderer)", 1)[1])
        details = self.script.split("function renderDetails(t, state, rendered = null) {", 1)[1].split("\n}", 1)[0]
        self.assertIn('el("river").textContent = hex(river.seed);', details)
        self.assertIn(': "unavailable";', details)
        self.assertIn('el("interaction-summary").textContent = `${interaction.mode}${embodied}`;', details)
        self.assertIn("requestAnimationFrame(frame);", self.script)
        self.assertIn("get rendererAvailable() { return rendererFailure === null; }", self.script)

    def test_live_frame_synchronizes_the_pressed_score_movement(self) -> None:
        details = self.script.split(
            "function renderDetails(t, state, rendered = null) {", 1
        )[1].split("\n}", 1)[0]
        self.assertIn("movement.id === state.movement", details)
        self.assertIn("controlBus.getState().movement !== liveMovement", details)
        self.assertIn("type: ACTIONS.SET_MOVEMENT, value: liveMovement", details)

    def test_project_atomically_replaces_the_visual_details_sheet(self) -> None:
        helper = self.script.split("async function openMap(trigger) {", 1)[1].split("\n}", 1)[0]
        self.assertLess(helper.index("setHudVisible(false)"), helper.index("controlBus.actions.openMap()"))

    def test_project_record_is_local_fail_readable_and_not_fetch_gated(self) -> None:
        helper = self.script.split("async function openMap(trigger) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("projectMap.showModal()", helper)
        self.assertNotIn("fetch(", helper)
        self.assertNotIn("loadProjectMap", self.script)
        for section in ("project-artwork", "project-film", "project-status", "project-readings", "project-evidence"):
            self.assertIn(section, self.markup.by_id)
        self.assertIn("161 registered raw photographs + 1 archival composite", self.html)
        self.assertIn("05:50.896", self.html)
        self.assertIn("not yet encoded or rendered", self.html)

    def test_transient_feedback_stays_above_every_progressive_surface(self) -> None:
        self.assertIn("#toast {\n    position: fixed; z-index: 8;", self.html)
        self.assertIn("#danse-dock {\n  position:fixed; z-index:6;", self.styles)
        self.assertIn("#hud {\n  position:fixed; z-index:7;", self.styles)

    def test_opening_a_tray_closes_the_visual_details_sheet_first(self) -> None:
        helper = self.script.split("function openTray(category, trigger) {", 1)[1].split("\n}", 1)[0]
        self.assertLess(helper.index("setHudVisible(false)"), helper.index("controlBus.actions.openTray(category)"))
        self.assertIn('surfaceTrigger = controlBus.getState().surface === `tray:${category}` ? null : trigger;', helper)

    def test_hash_navigation_expires_pending_river_undo(self) -> None:
        handler = self.script.split('addEventListener("hashchange", async (event) => {', 1)[1].split("/** Wind this river", 1)[0]
        self.assertIn("previousRiver = null;", handler)
        self.assertIn("previousRememberedRiver = null;", handler)
        self.assertIn('el("river-undo").disabled = true;', handler)

    def test_river_actions_expire_pending_hash_navigation(self) -> None:
        new_river = self.script.split("function newRiver() {", 1)[1].split("\n}", 1)[0]
        undo_river = self.script.split("function undoRiver() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("navigationGeneration += 1;", new_river)
        self.assertLess(undo_river.index("if (!previousRiver) return null;"), undo_river.index("navigationGeneration += 1;"))

    def test_shifted_river_undo_restores_its_pre_mint_address(self) -> None:
        new_river = self.script.split("function newRiver() {", 1)[1].split("\n}", 1)[0]
        undo_river = self.script.split("function undoRiver() {", 1)[1].split("\n}", 1)[0]
        hash_navigation = self.script.split(
            'addEventListener("hashchange", async (event) => {', 1
        )[1].split("/** Wind this river", 1)[0]
        self.assertIn("let previousRiverUrl = null;", self.script)
        self.assertIn("const projectFragment = Boolean(projectSectionFor(location.hash));", new_river)
        self.assertIn("const freshRememberedRiver = currentRememberedRiver", new_river)
        self.assertIn('Arrival.rememberedRiverForUndo(currentRememberedRiver, "", recalled)', new_river)
        self.assertIn("const carriedProjectUrl = projectReturnRiverUrl", new_river)
        self.assertIn("previousRiverUrl = projectFragment && carriedProjectUrl && freshRememberedRiver", new_river)
        self.assertIn(": Arrival.href(river, { mode: program ? null : \"free\" });", new_river)
        self.assertIn(
            "const restoredUrl = previousRiverUrl ?? Arrival.href(river);",
            undo_river,
        )
        self.assertIn("Arrival.withMode(restoredUrl, program ? null : \"free\")", undo_river)
        self.assertIn("previousRiverUrl = null;", undo_river)
        self.assertIn("previousRiverUrl = null;", hash_navigation)
        self.assertIn("previousRememberedRiver = freshRememberedRiver;", new_river)
        self.assertIn("currentRememberedRiver = river;", new_river)
        self.assertIn("currentRememberedRiver = previousRememberedRiver;", undo_river)
        project_navigation = hash_navigation.split("const generation =", 1)[0]
        self.assertNotIn("currentRememberedRiver =", project_navigation)
        self.assertIn("currentRememberedRiver = Arrival.rememberedRiverForUndo", hash_navigation)

    def test_legacy_project_fragments_open_the_moved_live_section(self) -> None:
        redirect = (ROOT / "404.html").read_text(encoding="utf-8")
        state = (ROOT / "interface/state.js").read_text(encoding="utf-8")
        self.assertIn(r"/^\/(?:[A-Za-z0-9._~-]+\/)?project(?:\/index\.html)?\/?$/", redirect)
        self.assertIn("${location.search}${location.hash}", redirect)
        self.assertIn('evidence: "project-evidence"', state)
        self.assertIn('"ballet-score": "project-film"', state)
        self.assertIn("projectSectionFor", self.script)
        helper = self.script.split(
            "async function openProjectSection(section = projectSectionFor(location.hash)) {", 1
        )[1].split("\n}", 1)[0]
        self.assertIn("await openMap(", helper)
        self.assertIn('target?.scrollIntoView({ block: "start" })', helper)
        self.assertIn("target?.focus({ preventScroll: true })", helper)
        self.assertIn("void openProjectSection();", self.script)

    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_legacy_project_redirect_executes_only_for_anchored_optional_prefixes(self) -> None:
        markup = (ROOT / "404.html").read_text(encoding="utf-8")
        script = markup.split("<script>", 1)[1].split("</script>", 1)[0]

        def redirect(pathname: str, search: str = "?view=legacy", fragment: str = "#evidence") -> str | None:
            probe = f"""
const result = {{ value: null }};
globalThis.location = {{
  pathname: {json.dumps(pathname)},
  search: {json.dumps(search)},
  hash: {json.dumps(fragment)},
  replace: (value) => {{ result.value = value; }},
}};
{script}
console.log(JSON.stringify(result.value));
"""
            completed = subprocess.run(
                ["node", "--input-type=module", "--eval", probe],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            return json.loads(completed.stdout)

        destination = "https://danse.pages.dev/?view=legacy#evidence"
        for path in (
            "/project",
            "/project/",
            "/project/index.html",
            "/the-thing-without-a-name/project",
            "/the-thing-without-a-name/project/",
            "/the-thing-without-a-name/project/index.html",
        ):
            with self.subTest(path=path):
                self.assertEqual(redirect(path), destination)
        for path in (
            "/projectile",
            "/project/extra",
            "/prefix/nested/project/",
            "//project/",
            "/the-thing-without-a-name/projectile",
            "/the-thing-without-a-name/project/index.htm",
        ):
            with self.subTest(path=path):
                self.assertIsNone(redirect(path))

    def test_legacy_project_fragment_survives_the_river_url_updater(self) -> None:
        updater = self.script.split("setInterval(() => {", 1)[1].split("}, 1000);", 1)[0]
        guard = "if (shifted || (projectMap.open && projectSectionFor(location.hash))) return;"
        self.assertIn(guard, updater)
        self.assertLess(updater.index(guard), updater.index("history.replaceState"))

    def test_closing_project_immediately_restores_a_mode_complete_river_url(self) -> None:
        close = self.script.split("function closeVisualSurfaces() {", 1)[1].split("\n}", 1)[0]
        capture = (
            "const closedProjectFragment = projectMap.open "
            "&& Boolean(projectSectionFor(location.hash));"
        )
        canonical = (
            'currentRiverUrl = Arrival.href(river, '
            '{ mode: program ? null : "free" });'
        )
        self.assertIn(capture, close)
        self.assertIn("if (closedProjectFragment) {", close)
        self.assertIn(canonical, close)
        self.assertIn('history.replaceState(null, "", currentRiverUrl);', close)
        self.assertIn("projectReturnRiverUrl = null;", close)
        self.assertLess(close.index(capture), close.index("projectMap.close()"))
        self.assertLess(close.index("projectMap.close()"), close.index(canonical))
        self.assertLess(close.index(canonical), close.index("projectReturnRiverUrl = null;"))

        persist = self.script.split("function persistProgramMode() {", 1)[1].split("\n}", 1)[0]
        set_program = self.script.split("async function setProgram(value) {", 1)[1].split("\n}", 1)[0]
        self.assertIn(
            "projectSectionFor(location.hash) ? Arrival.href(river) : location.href",
            persist,
        )
        self.assertIn(
            'Arrival.withMode(currentUrl, program ? null : "free")',
            persist,
        )
        free = set_program.split('if (value === "free") {', 1)[1].split("}", 1)[0]
        score = set_program.split("const loaded =", 1)[1]
        self.assertLess(free.index("program = null;"), free.index("persistProgramMode();"))
        self.assertLess(free.index("persistProgramMode();"), free.index("return true;"))
        self.assertLess(score.index("program = loaded;"), score.index("persistProgramMode();"))
        self.assertLess(score.index("persistProgramMode();"), score.index("return true;"))

    def test_new_river_replaces_a_legacy_project_fragment_immediately(self) -> None:
        new_river = self.script.split("function newRiver() {", 1)[1].split("\n}", 1)[0]
        capture = "const projectFragment = Boolean(projectSectionFor(location.hash));"
        replacement = (
            'currentRiverUrl = Arrival.href(river, '
            '{ mode: program ? null : "free" });'
        )
        self.assertIn(capture, new_river)
        self.assertIn(replacement, new_river)
        self.assertIn('history.replaceState(null, "", currentRiverUrl);', new_river)
        self.assertLess(new_river.index(capture), new_river.index("Arrival.mint()"))
        self.assertLess(new_river.index("Arrival.mint()"), new_river.index(replacement))

    def test_project_back_forward_unmasks_river_and_urls_are_immediate(self) -> None:
        hash_navigation = self.script.split(
            'addEventListener("hashchange", async (event) => {', 1
        )[1].split("/** Wind this river", 1)[0]
        new_river = self.script.split("function newRiver() {", 1)[1].split("\n}", 1)[0]
        undo_river = self.script.split("function undoRiver() {", 1)[1].split("\n}", 1)[0]
        close = "if (projectMap.open) controlBus?.actions.close();"
        self.assertIn(close, hash_navigation)
        self.assertLess(hash_navigation.index(close), hash_navigation.index("Arrival.arrive(fragment)"))
        canonical = 'currentRiverUrl = Arrival.href(river, { mode: program ? null : "free" });'
        self.assertIn(canonical, new_river)
        self.assertLess(new_river.index("Arrival.mint()"), new_river.index(canonical))
        self.assertIn("history.replaceState(", undo_river)
        self.assertIn(
            "const restoredUrl = previousRiverUrl ?? Arrival.href(river);",
            undo_river,
        )
        self.assertIn("projectSectionFor(new URL(restoredUrl).hash)", undo_river)
        self.assertIn("Arrival.withMode(restoredUrl, program ? null : \"free\")", undo_river)

    def test_project_navigation_preserves_self_contained_rivers_and_defers_impurity(self) -> None:
        handler = self.script.split(
            'addEventListener("hashchange", async (event) => {', 1
        )[1].split("/** Wind this river", 1)[0]
        new_river = self.script.split("function newRiver() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("let projectReturnRiverUrl = null;", self.script)
        self.assertIn("const oldFragment = new URL(event.oldURL).hash;", handler)
        self.assertIn("let currentRiverUrl = projectSectionFor(location.hash)", self.script)
        self.assertIn("if (!projectSectionFor(oldFragment)) projectReturnRiverUrl = currentRiverUrl;", handler)
        self.assertNotIn("projectReturnRiverUrl = event.oldURL", handler)
        self.assertIn("Arrival.hasSelfContainedRiver(new URL(carriedProjectUrl).hash)", new_river)
        self.assertIn("projectFragment && carriedProjectUrl", new_river)
        self.assertIn(": Arrival.href(river, { mode: program ? null : \"free\" });", new_river)
        self.assertIn("const fragment = location.hash;", handler)
        self.assertLess(handler.index("const fragment = location.hash;"), handler.index("await Program.load()"))
        self.assertLess(handler.index("if (generation !== navigationGeneration) return;"), handler.index("Arrival.arrive(fragment)"))
        self.assertLess(handler.index("Arrival.arrive(fragment)"), handler.index("river = nextRiver;"))
        self.assertLess(handler.index("river = nextRiver;"), handler.index("currentRiverUrl = location.href;"))

    def test_undo_never_installs_a_closed_project_fragment(self) -> None:
        undo_river = self.script.split("function undoRiver() {", 1)[1].split("\n}", 1)[0]
        project_branch = undo_river.split(
            "projectSectionFor(new URL(restoredUrl).hash)", 1
        )[1].split(": Arrival.withMode", 1)[0]
        self.assertIn(
            'Arrival.href(river, { mode: program ? null : "free" })',
            project_branch,
        )
        self.assertNotIn("? restoredUrl", undo_river)
        self.assertLess(
            undo_river.index("history.replaceState("),
            undo_river.index("currentRiverUrl = location.href;"),
        )

    def test_reduced_motion_preserves_the_actual_manual_hold(self) -> None:
        handler = self.script.split("function reducedMotionChanged({ matches }) {", 1)[1].split("\n}", 1)[0]
        self.assertIn('playback === "running"', handler)
        self.assertIn('playback === "held-reduced"', handler)
        self.assertNotIn('playback === "held-user"', handler.split("flash", 1)[0])

    def test_primary_receipts_and_async_navigation_stay_synchronized(self) -> None:
        self.assertIn('el("river-seed").textContent = `Current river: ${hex(river.seed)}`', self.script)
        self.assertIn("let currentRememberedRiver = Arrival.rememberedRiverForUndo", self.script)
        self.assertIn("if (previousRememberedRiver) Arrival.remember(previousRememberedRiver)", self.script)
        self.assertIn("const programIntent = ++programGeneration", self.script)
        self.assertIn("if (programIntent === programGeneration) program = loaded", self.script)
        self.assertIn("if (generation !== programGeneration) return false", self.script)
        self.assertIn('value: program ? "score-led" : "free"', self.script)
        handler = self.script.split('addEventListener("hashchange", async (event) => {', 1)[1].split("/** Wind this river", 1)[0]
        self.assertIn("if (programIntent === programGeneration) {", handler)
        self.assertLess(
            handler.index("if (programIntent === programGeneration) {", handler.index("river = nextRiver;")),
            handler.index('controlBus?.actions.sync({ type: ACTIONS.SET_PROGRAM'),
        )
        self.assertIn("describeProgram();", self.script)

    def test_music_toggle_and_advanced_receipt_follow_authoritative_state(self) -> None:
        music = self.script.split("async function toggleMusic(intent) {", 1)[1].split("\n}", 1)[0]
        hold = self.script.split("function toggleHold() {", 1)[1].split("\n}", 1)[0]
        subscription = self.script.split("controlBus.subscribe((state) => {", 1)[1].split("\n});", 1)[0]
        self.assertIn('if (intent === "stopped")', music)
        self.assertIn("await scoreAudio.start(scoreSecondAt(at()))", music)
        self.assertIn("if (heldAt !== null)", music)
        held = music.split("if (heldAt !== null)", 1)[1]
        self.assertIn("scoreAudio.sync(scoreSecondAt(at()), true);", held)
        self.assertNotIn("scoreAudio.stop();", held.split("return", 1)[0])
        self.assertIn("scoreAudio.stop();", music.split("catch (error)", 1)[1])
        self.assertIn("const previousHeldAt = heldAt;", hold)
        self.assertIn("heldAt = previousHeldAt;", hold.split("catch (error)", 1)[1])
        self.assertIn("throw error;", hold.split("catch (error)", 1)[1])
        self.assertIn("renderMusicReceipt(state);", subscription)

    def test_shared_river_uses_only_allowlisted_presentation_state(self) -> None:
        share = self.script.split("async function shareRiver() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("const sharedRiver = river;", share)
        self.assertIn("if (river !== sharedRiver) return false;", share)
        self.assertIn("sharePresentationState(controlBus.getState())", share)
        self.assertIn("sharePresentationUrl(Arrival.href(sharedRiver), presentation)", share)
        self.assertNotIn("location.href", share)

    def test_slow_replay_receipt_cannot_replace_a_later_presence_choice(self) -> None:
        receipt = self.script.split('el("receipt-load").addEventListener("change", async (event) => {', 1)[1].split("\n});", 1)[0]
        presence = self.script.split("async function setPresence(value) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("const generation = ++presenceOperationGeneration;", receipt)
        self.assertIn('controlBus?.actions.status("presence", "Loading replay receipt…")', receipt)
        self.assertGreaterEqual(receipt.count("generation !== presenceOperationGeneration"), 2)
        self.assertIn("const generation = ++presenceOperationGeneration;", presence)
        self.assertIn("if (generation !== presenceOperationGeneration) return controlBus.getState().presence;", presence)

    def test_presence_status_clear_renders_the_authoritative_interaction_message(self) -> None:
        interaction = self.script.split("function renderInteraction(snapshot) {", 1)[1].split("\n}", 1)[0]
        subscription = self.script.split("controlBus.subscribe((state) => {", 1)[1].split("\n});", 1)[0]
        self.assertIn('controlBus?.getState().status.presence || message', interaction)
        self.assertIn('el("presence-receipt").textContent = state.status.presence || presenceMessage();', subscription)

    def test_pre_activation_fallback_values_are_applied_when_presence_starts(self) -> None:
        fallback = self.script.split("function fallbackValues() {", 1)[1].split("\n}", 1)[0]
        presence = self.script.split("async function setPresence(value) {", 1)[1].split("\n}", 1)[0]
        self.assertIn('value("fallback-x")', fallback)
        self.assertIn('value("fallback-y")', fallback)
        self.assertIn('value("fallback-open")', fallback)
        self.assertIn('value("fallback-reach")', fallback)
        self.assertIn('interaction.setFallback(fallbackValues(), at())', presence)

    def test_map_and_browser_checks_preserve_visible_and_timing_gates(self) -> None:
        self.assertIn('#project-map [aria-disabled="true"]', self.styles)
        self.assertIn(
            '#project-map a:not([aria-disabled="true"]) { display:inline-flex;',
            self.styles,
        )
        self.assertIn(".fallback-grid input, .fallback-grid select", self.html)
        self.assertIn("#hud summary { box-sizing: border-box; min-height: 44px", self.html)
        browser = (ROOT / "render/browser.py").read_text(encoding="utf-8")
        self.assertIn("presence-receipt')?.textContent", browser)
        self.assertIn("document.getElementById('veil').hidden", browser)
        self.assertIn("def touch_targets(scope: str, label: str)", browser)
        self.assertIn('touch_targets("#project-map", "Project")', browser)
        self.assertIn('touch_targets("#hud", "no-WebGL Details")', browser)
        fallback = browser.split("fallback_movement_variants =", 1)[1].split(
            'page.click("#fallback-start")', 1
        )[0]
        self.assertIn("danse.program.movements.findIndex", fallback)
        self.assertIn("movement[\"control\"] == movement[\"expected\"]", fallback)
        self.assertIn("movement[\"selected\"] == [movement[\"expected\"]]", fallback)
        self.assertGreaterEqual(fallback.count("0x12345678"), 2)
        self.assertGreaterEqual(fallback.count("0x87654321"), 2)
        self.assertNotIn("danse.controlState.movement === 2", fallback)
        self.assertNotIn("danse.controlState.movement === 3", fallback)

    def test_controls_replay_exercises_the_shipped_score_audio_lifecycle(self) -> None:
        browser = (ROOT / "render/browser.py").read_text(encoding="utf-8")
        shipped = browser.split(
            "# The unavailable-score visit above proves fail-readable controls.", 1
        )[1].split("page.add_init_script", 1)[0]
        self.assertIn('page.goto(f"{base}/index.html", wait_until="load")', shipped)
        self.assertIn("danse.controlState.music === 'playing'", shipped)
        self.assertIn("danse.controlState.music === 'suspended-by-hold'", shipped)
        self.assertIn("danse.controlState.music === 'stopped'", shipped)
        self.assertGreaterEqual(shipped.count("#music-tray"), 3)
        self.assertEqual(shipped.count("[data-category=\"hold\"]"), 2)

    def test_share_feedback_has_its_own_polite_live_region(self) -> None:
        tag, toast = self.markup.by_id["toast"]
        self.assertEqual(tag, "div")
        self.assertEqual(toast["role"], "status")
        self.assertEqual(toast["aria-live"], "polite")
        self.assertEqual(toast["aria-atomic"], "true")
        self.assertIn("hidden", toast)
        self.assertIn('const toast = el("toast")', self.script)
        self.assertNotIn('el("keys")', self.script)

    def test_optional_score_failure_announces_fallback_without_disabling_the_artwork(self) -> None:
        self.assertIn("await MusicalScore.loadOptional(scoreUrl", self.script)
        self.assertIn("scoreLoadFailure = error", self.script)
        self.assertIn("continuing with the default artwork", self.script)
        self.assertIn(
            'if (scoreLoadFailure) flash("Musical score unavailable · continuing with the default artwork", 8000)',
            self.script,
        )
        film = (ROOT / "film.html").read_text(encoding="utf-8")
        self.assertIn("scoreUrl !== null ? await MusicalScore.load(scoreUrl) : null", film)
        self.assertNotIn("MusicalScore.loadOptional", film)

    def test_canvas_has_a_text_description_and_canonical_metadata(self) -> None:
        tag, canvas = self.markup.by_id["stage"]
        self.assertEqual(tag, "canvas")
        self.assertEqual(canvas["role"], "img")
        self.assertEqual(canvas["aria-describedby"], "stage-description")
        self.assertTrue(canvas["aria-label"])
        self.assertIn("stage-description", self.markup.by_id)
        canonical = [
            attrs
            for tag, attrs in self.markup.tags
            if tag == "link" and attrs.get("rel") == "canonical"
        ]
        self.assertEqual(
            canonical[0]["href"], "https://danse.pages.dev/"
        )
        descriptions = [
            attrs
            for tag, attrs in self.markup.tags
            if tag == "meta" and attrs.get("name") == "description"
        ]
        self.assertIn("Anthony J. Padavano", descriptions[0]["content"])
        self.assertIn("<title>Danse Macabre — a room that never repeats</title>", self.html)

    def test_layout_uses_mobile_safe_areas_and_reduced_motion_holds_a_frame(self) -> None:
        self.assertIn("viewport-fit=cover", self.html)
        self.assertIn("env(safe-area-inset-bottom)", self.styles)
        self.assertIn("@media (max-width: 640px)", self.html)
        self.assertIn("@media (prefers-reduced-motion:reduce)", self.styles)
        self.assertIn("min-height:calc(64px + env(safe-area-inset-bottom))", self.styles)
        self.assertIn("min-width:44px; min-height:44px", self.styles)
        self.assertIn('matchMedia("(prefers-reduced-motion: reduce)")', self.script)
        self.assertIn(
            "let heldAt = reducedMotion.matches ? Arrival.now(river) : null", self.script
        )
        self.assertIn('reducedMotion.addEventListener("change"', self.script)

    def test_local_interaction_is_explicit_private_and_has_fallbacks(self) -> None:
        _, video = self.markup.by_id["pose-video"]
        self.assertIn("hidden", video)
        self.assertEqual(video["aria-hidden"], "true")
        for button_id in ("camera-start", "camera-retry", "fallback-start", "interaction-stop"):
            tag, attrs = self.markup.by_id[button_id]
            self.assertEqual(tag, "button")
            self.assertEqual(attrs["type"], "button")
        status_tag, status = self.markup.by_id["interaction-status"]
        self.assertEqual(status_tag, "p")
        self.assertEqual(status["role"], "status")
        self.assertEqual(status["aria-live"], "polite")
        for control_id in ("fallback-x", "fallback-y", "fallback-open", "fallback-reach"):
            tag, attrs = self.markup.by_id[control_id]
            self.assertEqual(tag, "input")
            self.assertEqual(attrs["type"], "range")
        self.assertIn('el("camera-start").addEventListener("click"', self.script)
        self.assertNotIn("getUserMedia(", self.script)
        self.assertIn("frames stay in memory on this device", self.html)
        self.assertIn("raw landmarks are discarded immediately", self.html)

    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_inline_module_has_valid_javascript_syntax(self) -> None:
        modules = [
            script
            for script, (_, attrs) in zip(self.markup.scripts, [tag for tag in self.markup.tags if tag[0] == "script"])
            if attrs.get("type") == "module"
        ]
        self.assertEqual(len(modules), 1)
        with tempfile.TemporaryDirectory() as temporary:
            module = Path(temporary) / "index.mjs"
            module.write_text(modules[0], encoding="utf-8")
            done = subprocess.run(
                ["node", "--check", str(module)], capture_output=True, text=True, check=False
            )
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
