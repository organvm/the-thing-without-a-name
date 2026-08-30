#!/usr/bin/env python3
"""Portable checks for the Danse public artifact and its hidden control surface."""

from __future__ import annotations

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

    def test_cli_accepts_only_the_clean_exact_git_checkout(self) -> None:
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
        self.assertIn("does not match checkout HEAD", wrong.stderr)
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

    def test_clean_checkout_transform_cannot_change_published_runtime_bytes(self) -> None:
        source = self.base / "transform-source"
        public_fixture(source)
        (source / ".gitattributes").write_text(
            "arrival.js text eol=crlf\n",
            encoding="utf-8",
        )
        commit = RELEASE_SUPPORT.initialize_git_fixture(source)
        root = self.base / "transform-checkout"
        subprocess.run(
            ["git", "clone", "-q", str(source), str(root)],
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
        with self.assertRaisesRegex(
            PAGES.ArtifactError,
            "allowlisted source bytes drifted from the declared commit: arrival.js",
        ):
            PAGES.build(
                root,
                self.base / "transformed-pages",
                commit,
                require_git_source=True,
            )

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
                "does not match checkout HEAD",
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
        markup = Markup()
        markup.feed((self.output / "project/index.html").read_text(encoding="utf-8"))
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

    def test_self_rehashed_pages_manifest_cannot_add_private_file(self) -> None:
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

    def test_hud_is_really_hidden_and_disclosed_by_an_accessible_touch_target(self) -> None:
        tag, hud = self.markup.by_id["hud"]
        self.assertEqual(tag, "section")
        self.assertIn("hidden", hud)
        self.assertEqual(hud["aria-hidden"], "true")
        tag, toggle = self.markup.by_id["hud-toggle"]
        self.assertEqual(tag, "button")
        self.assertEqual(toggle["type"], "button")
        self.assertEqual(toggle["aria-controls"], "hud")
        self.assertEqual(toggle["aria-expanded"], "false")
        self.assertEqual(toggle["aria-label"], "Show Danse controls")
        self.assertIn("#hud[hidden] { display: none; }", self.html)
        self.assertIn("min-width: 48px; min-height: 48px", self.html)

    def test_keyboard_and_touch_controls_keep_aria_state_in_sync(self) -> None:
        self.assertIn("function setHudVisible(visible)", self.script)
        self.assertIn('hud.setAttribute("aria-hidden", String(!visible))', self.script)
        self.assertIn('hudToggle.setAttribute("aria-expanded", String(visible))', self.script)
        self.assertIn('hudToggle.addEventListener("click"', self.script)
        self.assertIn('const key = e.key.toLowerCase()', self.script)
        self.assertIn('if (key === "h")', self.script)
        self.assertIn('if (e.key === "Escape")', self.script)
        self.assertIn("keyboard-instructions", self.markup.by_id)
        self.assertIn("touch-instructions", self.markup.by_id)

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
            canonical[0]["href"], "https://organvm.github.io/the-thing-without-a-name/"
        )
        descriptions = [
            attrs
            for tag, attrs in self.markup.tags
            if tag == "meta" and attrs.get("name") == "description"
        ]
        self.assertIn("Anthony J. Padavano", descriptions[0]["content"])
        self.assertIn("<title>Danse — a room that never repeats</title>", self.html)

    def test_layout_uses_mobile_safe_areas_and_reduced_motion_holds_a_frame(self) -> None:
        self.assertIn("viewport-fit=cover", self.html)
        self.assertIn("env(safe-area-inset-bottom)", self.html)
        self.assertIn("@media (max-width: 640px)", self.html)
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.html)
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
