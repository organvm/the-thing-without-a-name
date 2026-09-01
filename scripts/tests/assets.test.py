"""Portable adversarial tests for the production asset parity CLI."""

from __future__ import annotations

import copy
import hashlib
import http.client
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

try:
    import jsonschema
except ModuleNotFoundError:  # The repository's normal CI install provides it.
    jsonschema = None

ROOT = Path(__file__).resolve().parents[2]


def load_module():
    spec = importlib.util.spec_from_file_location("danse_assets_test", ROOT / "scripts/assets.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("asset parity module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ASSETS = load_module()


class Fixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        self.source = self.base / "source"
        self.root.mkdir()
        self.source.mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Asset Test"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.email", "assets@example.invalid"],
            check=True,
        )
        (self.root / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "--quiet", "-m", "fixture"], check=True)
        self.head = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.payload = b"danse-remote-parity\x00" * 97
        source = self.source / "pipeline/raw/IMG_1570.JPG"
        source.parent.mkdir(parents=True)
        source.write_bytes(self.payload)
        self.lock = self.base / "lock.json"
        self.receipt = self.base / "receipt.json"
        self.write_lock()

    def write_lock(self, **changes) -> None:
        row = {
            "id": "origin-img-1570",
            "target": "pipeline/.work/raw/IMG_1570.JPG",
            "sha256": hashlib.sha256(self.payload).hexdigest(),
            "bytes": len(self.payload),
            "media_type": "image/jpeg",
            "rights_class": "private",
            "required": True,
            "sources": [{"kind": "file", "path": "pipeline/raw/IMG_1570.JPG"}],
        }
        row.update(changes.pop("asset", {}))
        value = {
            "schema": ASSETS.LOCK_SCHEMA,
            "lock_id": "fixture-assets",
            "profile": "generic",
            "repository_commit": self.head,
            "assets": [row],
        }
        value.update(changes)
        self.lock.write_text(json.dumps(value), encoding="utf-8")

    def close(self) -> None:
        self.temporary.cleanup()


class AssetParityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_file_pull_populates_content_cache_and_target_exactly(self) -> None:
        code = ASSETS.main(
            [
                "pull",
                "--lock",
                str(self.fixture.lock),
                "--root",
                str(self.fixture.root),
                "--allow-file",
                "--file-source-root",
                str(self.fixture.source),
                "--receipt",
                str(self.fixture.receipt),
            ]
        )
        self.assertEqual(code, 0)
        target = self.fixture.root / "pipeline/.work/raw/IMG_1570.JPG"
        digest = hashlib.sha256(self.fixture.payload).hexdigest()
        cache = self.fixture.root / f".asset-cache/sha256/{digest[:2]}/{digest}"
        self.assertEqual(target.read_bytes(), self.fixture.payload)
        self.assertEqual(cache.read_bytes(), self.fixture.payload)
        self.assertEqual(list(cache.parent.glob(".danse-assets-retired-*")), [])
        self.assertEqual(target.stat().st_ino, cache.stat().st_ino)
        self.assertEqual(stat_mode(target), 0o444)
        receipt = self.fixture.receipt.read_text(encoding="utf-8")
        self.assertNotIn(str(self.fixture.root), receipt)
        self.assertNotIn(str(self.fixture.source), receipt)
        self.assertNotIn("IMG_1570.JPG", receipt)
        self.assertNotIn("pipeline/.work", receipt)
        self.assertTrue(json.loads(receipt)["ok"])
        self.assertEqual(ASSETS.main(["verify", "--lock", str(self.fixture.lock), "--root", str(self.fixture.root)]), 0)

    def test_pull_syncs_mode_cache_and_target_before_receipt(self) -> None:
        asset = ASSETS.load_lock(self.fixture.lock).assets[0]
        cache_parent = ASSETS._cache_path(self.fixture.root, asset, create=False).parent
        target_parent = (self.fixture.root / asset.target).parent
        durable_directories = {
            "root": self.fixture.root,
            "cache-root": self.fixture.root / ".asset-cache",
            "cache-algorithm": self.fixture.root / ".asset-cache/sha256",
            "cache-bucket": cache_parent,
            "pipeline": self.fixture.root / "pipeline",
            "private-root": self.fixture.root / "pipeline/.work",
            "target-parent": target_parent,
        }
        events = []
        real_fchmod = os.fchmod
        real_fsync = os.fsync
        real_publish = ASSETS.StagedJson.publish

        def record_mode(descriptor, mode):
            events.append("mode")
            return real_fchmod(descriptor, mode)

        def record_sync(descriptor):
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                events.append("file")
            else:
                for label, path in durable_directories.items():
                    if descriptor_matches_path(descriptor, path):
                        if label == "cache-bucket":
                            self.assertFalse(
                                any(name.startswith(".asset-") for name in os.listdir(descriptor))
                            )
                        events.append(label)
                        break
            return real_fsync(descriptor)

        def record_receipt(staged, *args, **kwargs):
            events.append("receipt")
            return real_publish(staged, *args, **kwargs)

        with (
            mock.patch.object(ASSETS.os, "fchmod", side_effect=record_mode),
            mock.patch.object(ASSETS.os, "fsync", side_effect=record_sync),
            mock.patch.object(ASSETS.StagedJson, "publish", new=record_receipt),
        ):
            code = ASSETS.main(
                [
                    "pull",
                    "--lock",
                    str(self.fixture.lock),
                    "--root",
                    str(self.fixture.root),
                    "--allow-file",
                    "--file-source-root",
                    str(self.fixture.source),
                    "--receipt",
                    str(self.fixture.receipt),
                ]
            )
        self.assertEqual(code, 0)
        mode_index = events.index("mode")
        self.assertEqual(events[mode_index - 1 : mode_index + 2], ["file", "mode", "file"])
        receipt_index = events.index("receipt")
        for label in durable_directories:
            with self.subTest(directory=label):
                self.assertLess(events.index(label), receipt_index)
        self.assertLess(events.index("cache-bucket"), events.index("target-parent"))
        self.assertTrue(json.loads(self.fixture.receipt.read_text(encoding="utf-8"))["ok"])

    def test_darwin_file_sync_orders_fsync_before_fullfsync(self) -> None:
        path = self.fixture.base / "darwin-durable.bin"
        path.write_bytes(b"durable")
        descriptor = os.open(path, os.O_RDONLY)
        events = []
        try:
            with (
                mock.patch.object(ASSETS.sys, "platform", "darwin"),
                mock.patch.object(ASSETS.fcntl, "F_FULLFSYNC", 51, create=True),
                mock.patch.object(ASSETS.os, "fsync", side_effect=lambda _fd: events.append("fsync")),
                mock.patch.object(
                    ASSETS.fcntl,
                    "fcntl",
                    side_effect=lambda _fd, command: events.append(("fullfsync", command)),
                ),
            ):
                ASSETS._fsync_asset_file(descriptor, "test asset")
        finally:
            os.close(descriptor)
        self.assertEqual(events, ["fsync", ("fullfsync", 51)])

    def test_darwin_file_sync_fails_closed_without_fullfsync(self) -> None:
        path = self.fixture.base / "darwin-blocked.bin"
        path.write_bytes(b"blocked")
        descriptor = os.open(path, os.O_RDONLY)
        try:
            with (
                mock.patch.object(ASSETS.sys, "platform", "darwin"),
                mock.patch.object(ASSETS.fcntl, "F_FULLFSYNC", None, create=True),
                mock.patch.object(ASSETS.os, "fsync"),
                self.assertRaisesRegex(ASSETS.AssetError, "cannot prove durable storage"),
            ):
                ASSETS._fsync_asset_file(descriptor, "test asset")
            with (
                mock.patch.object(ASSETS.sys, "platform", "darwin"),
                mock.patch.object(ASSETS.fcntl, "F_FULLFSYNC", 51, create=True),
                mock.patch.object(ASSETS.os, "fsync"),
                mock.patch.object(ASSETS.fcntl, "fcntl", side_effect=OSError("blocked")),
                self.assertRaisesRegex(ASSETS.AssetError, "fully synchronized"),
            ):
                ASSETS._fsync_asset_file(descriptor, "test asset")
        finally:
            os.close(descriptor)

    def test_non_darwin_file_sync_never_calls_fullfsync(self) -> None:
        path = self.fixture.base / "portable-durable.bin"
        path.write_bytes(b"portable")
        descriptor = os.open(path, os.O_RDONLY)
        try:
            with (
                mock.patch.object(ASSETS.sys, "platform", "linux"),
                mock.patch.object(ASSETS.os, "fsync") as portable_sync,
                mock.patch.object(ASSETS.fcntl, "fcntl") as full_sync,
            ):
                ASSETS._fsync_asset_file(descriptor, "test asset")
            portable_sync.assert_called_once_with(descriptor)
            full_sync.assert_not_called()
        finally:
            os.close(descriptor)

    def test_darwin_directory_sync_uses_directory_fsync_contract(self) -> None:
        descriptor = os.open(self.fixture.base, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            with (
                mock.patch.object(ASSETS.sys, "platform", "darwin"),
                mock.patch.object(ASSETS.os, "fsync") as directory_sync,
                mock.patch.object(ASSETS.fcntl, "fcntl") as full_sync,
            ):
                ASSETS._fsync_asset_directory(descriptor, "test directory")
            directory_sync.assert_called_once_with(descriptor)
            full_sync.assert_not_called()
        finally:
            os.close(descriptor)

    def test_darwin_published_inode_fullsync_follows_directory_barrier(self) -> None:
        parent = self.fixture.base / "darwin-publication"
        parent.mkdir()
        path = parent / "receipt.json"
        payload = b'{"ok":true}\n'
        path.write_bytes(payload)
        path.chmod(0o444)
        parent_descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        proof = ASSETS._inode_identity(path.stat())
        events = []

        def record_fsync(descriptor):
            events.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")

        try:
            with (
                mock.patch.object(ASSETS.sys, "platform", "darwin"),
                mock.patch.object(ASSETS.fcntl, "F_FULLFSYNC", 51, create=True),
                mock.patch.object(ASSETS.os, "fsync", side_effect=record_fsync),
                mock.patch.object(
                    ASSETS.fcntl,
                    "fcntl",
                    side_effect=lambda _fd, _command: events.append("fullfsync"),
                ),
            ):
                ASSETS._durably_sync_published_inode_at(
                    parent_descriptor,
                    path.name,
                    proof,
                    "test receipt",
                    expected_size=len(payload),
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                )
        finally:
            os.close(parent_descriptor)
        self.assertEqual(events, ["directory", "file", "fullfsync", "directory"])

    def test_verified_fast_path_syncs_inode_and_directories_before_receipt(self) -> None:
        self.assertEqual(
            ASSETS.main(
                [
                    "pull",
                    "--lock",
                    str(self.fixture.lock),
                    "--root",
                    str(self.fixture.root),
                    "--allow-file",
                    "--file-source-root",
                    str(self.fixture.source),
                ]
            ),
            0,
        )
        asset = ASSETS.load_lock(self.fixture.lock).assets[0]
        cache = ASSETS._cache_path(self.fixture.root, asset, create=False)
        target = self.fixture.root / asset.target
        events = []
        real_fsync = os.fsync
        real_publish = ASSETS.StagedJson.publish

        def record_sync(descriptor):
            if descriptor_matches_path(descriptor, cache):
                events.append("asset-file")
            elif descriptor_matches_path(descriptor, cache.parent):
                events.append("cache-directory")
            elif descriptor_matches_path(descriptor, target.parent):
                events.append("target-directory")
            return real_fsync(descriptor)

        def record_receipt(staged, *args, **kwargs):
            events.append("receipt")
            return real_publish(staged, *args, **kwargs)

        with (
            mock.patch.object(ASSETS.os, "fsync", side_effect=record_sync),
            mock.patch.object(ASSETS.StagedJson, "publish", new=record_receipt),
        ):
            code = ASSETS.main(
                [
                    "pull",
                    "--lock",
                    str(self.fixture.lock),
                    "--root",
                    str(self.fixture.root),
                    "--receipt",
                    str(self.fixture.receipt),
                ]
            )
        self.assertEqual(code, 0)
        receipt_index = events.index("receipt")
        self.assertGreaterEqual(events[:receipt_index].count("asset-file"), 2)
        for label in ("cache-directory", "target-directory"):
            with self.subTest(barrier=label):
                self.assertLess(events.index(label), receipt_index)
        self.assertTrue(json.loads(self.fixture.receipt.read_text(encoding="utf-8"))["ok"])

    def test_verified_fast_path_sync_failure_cannot_publish_receipt(self) -> None:
        self.assertEqual(
            ASSETS.main(
                [
                    "pull",
                    "--lock",
                    str(self.fixture.lock),
                    "--root",
                    str(self.fixture.root),
                    "--allow-file",
                    "--file-source-root",
                    str(self.fixture.source),
                ]
            ),
            0,
        )
        asset = ASSETS.load_lock(self.fixture.lock).assets[0]
        cache = ASSETS._cache_path(self.fixture.root, asset, create=False)
        target = self.fixture.root / asset.target
        real_fsync = os.fsync
        rejected = False

        def reject_verified_file_once(descriptor):
            nonlocal rejected
            if not rejected and (
                descriptor_matches_path(descriptor, cache)
                or descriptor_matches_path(descriptor, target)
            ):
                rejected = True
                raise OSError("injected verified file fsync failure")
            return real_fsync(descriptor)

        with mock.patch.object(ASSETS.os, "fsync", side_effect=reject_verified_file_once):
            code = ASSETS.main(
                [
                    "pull",
                    "--lock",
                    str(self.fixture.lock),
                    "--root",
                    str(self.fixture.root),
                    "--receipt",
                    str(self.fixture.receipt),
                ]
            )
        self.assertTrue(rejected)
        self.assertEqual(code, 1)
        self.assertFalse(self.fixture.receipt.exists())

    def test_ancestor_directory_sync_failure_retains_creation_without_receipt(self) -> None:
        real_fsync = os.fsync
        rejected = False

        def reject_root_directory_once(descriptor):
            nonlocal rejected
            if not rejected and descriptor_matches_path(descriptor, self.fixture.root):
                rejected = True
                raise OSError("injected ancestor directory fsync failure")
            return real_fsync(descriptor)

        with mock.patch.object(ASSETS.os, "fsync", side_effect=reject_root_directory_once):
            code = ASSETS.main(
                [
                    "pull",
                    "--lock",
                    str(self.fixture.lock),
                    "--root",
                    str(self.fixture.root),
                    "--allow-file",
                    "--file-source-root",
                    str(self.fixture.source),
                    "--receipt",
                    str(self.fixture.receipt),
                ]
            )
        self.assertTrue(rejected)
        self.assertEqual(code, 1)
        self.assertTrue((self.fixture.root / ".asset-cache").exists())
        self.assertFalse(self.fixture.receipt.exists())

    def test_cache_directory_sync_failure_cleans_link_and_blocks_receipt(self) -> None:
        asset = ASSETS.load_lock(self.fixture.lock).assets[0]
        cache = ASSETS._cache_path(self.fixture.root, asset, create=False)
        target = self.fixture.root / asset.target
        real_fsync = os.fsync
        rejected = False

        def reject_cache_directory_once(descriptor):
            nonlocal rejected
            if not rejected and descriptor_matches_path(descriptor, cache.parent):
                rejected = True
                raise OSError("injected cache directory fsync failure")
            return real_fsync(descriptor)

        with mock.patch.object(ASSETS.os, "fsync", side_effect=reject_cache_directory_once):
            code = ASSETS.main(
                [
                    "pull",
                    "--lock",
                    str(self.fixture.lock),
                    "--root",
                    str(self.fixture.root),
                    "--allow-file",
                    "--file-source-root",
                    str(self.fixture.source),
                    "--receipt",
                    str(self.fixture.receipt),
                ]
            )
        self.assertTrue(rejected)
        self.assertEqual(code, 1)
        self.assertTrue(cache.exists())
        self.assertFalse(target.exists())
        self.assertFalse(self.fixture.receipt.exists())

    def test_target_directory_sync_failure_cleans_link_and_blocks_receipt(self) -> None:
        asset = ASSETS.load_lock(self.fixture.lock).assets[0]
        cache = ASSETS._cache_path(self.fixture.root, asset, create=False)
        target = self.fixture.root / asset.target
        real_fsync = os.fsync
        rejected = False

        def reject_target_directory_once(descriptor):
            nonlocal rejected
            if not rejected and descriptor_matches_path(descriptor, target.parent):
                rejected = True
                raise OSError("injected target directory fsync failure")
            return real_fsync(descriptor)

        with mock.patch.object(ASSETS.os, "fsync", side_effect=reject_target_directory_once):
            code = ASSETS.main(
                [
                    "pull",
                    "--lock",
                    str(self.fixture.lock),
                    "--root",
                    str(self.fixture.root),
                    "--allow-file",
                    "--file-source-root",
                    str(self.fixture.source),
                    "--receipt",
                    str(self.fixture.receipt),
                ]
            )
        self.assertTrue(rejected)
        self.assertEqual(code, 1)
        self.assertTrue(cache.exists())
        self.assertTrue(target.exists())
        self.assertFalse(self.fixture.receipt.exists())

    def test_missing_required_asset_fails_closed(self) -> None:
        code = ASSETS.main(
            ["audit", "--lock", str(self.fixture.lock), "--root", str(self.fixture.root), "--receipt", str(self.fixture.receipt)]
        )
        self.assertEqual(code, 1)
        receipt = json.loads(self.fixture.receipt.read_text(encoding="utf-8"))
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["unresolved"], ["origin-img-1570"])

    def test_empty_lock_fails_at_runtime(self) -> None:
        value = json.loads(self.fixture.lock.read_text(encoding="utf-8"))
        value["assets"] = []
        self.fixture.lock.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ASSETS.AssetError, "at least one"):
            ASSETS.load_lock(self.fixture.lock)

    def test_wrong_source_digest_never_publishes_cache_or_target(self) -> None:
        self.fixture.write_lock(asset={"sha256": "0" * 64})
        code = ASSETS.main(
            [
                "pull",
                "--lock",
                str(self.fixture.lock),
                "--root",
                str(self.fixture.root),
                "--allow-file",
                "--file-source-root",
                str(self.fixture.source),
            ]
        )
        self.assertEqual(code, 1)
        self.assertFalse((self.fixture.root / "pipeline/.work/raw/IMG_1570.JPG").exists())
        self.assertFalse((self.fixture.root / ".asset-cache/sha256/00" / ("0" * 64)).exists())

    def test_existing_mismatch_is_not_overwritten(self) -> None:
        target = self.fixture.root / "pipeline/.work/raw/IMG_1570.JPG"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"operator bytes")
        code = ASSETS.main(
            [
                "pull",
                "--lock",
                str(self.fixture.lock),
                "--root",
                str(self.fixture.root),
                "--allow-file",
                "--file-source-root",
                str(self.fixture.source),
            ]
        )
        self.assertEqual(code, 1)
        self.assertEqual(target.read_bytes(), b"operator bytes")

    def test_file_source_requires_explicit_opt_in(self) -> None:
        self.assertEqual(
            ASSETS.main(["pull", "--lock", str(self.fixture.lock), "--root", str(self.fixture.root)]),
            1,
        )

    def test_lock_rejects_duplicate_keys_and_unsafe_urls_and_paths(self) -> None:
        self.fixture.lock.write_text(
            '{"schema":"danse.assets.lock.v1","schema":"danse.assets.lock.v1"}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ASSETS.AssetError, "duplicate"):
            ASSETS.load_lock(self.fixture.lock)
        for url in (
            "http://example.com/a",
            "https://user@example.com/a",
            "https://example.com/a?signature=secret",
            "https://example.com/a#x",
            "https://example.com:444/a",
            "https://example.com./a",
            "https://-bad.example/a",
            "https://example..com/a",
            "https://localhost/a",
            "https://LOCALHOST/a",
            "https://127.0.0.1/a",
            "https://999.999/a",
            "https://host.local/a",
            "https://[::1]/a",
        ):
            with self.subTest(url=url), self.assertRaises(ASSETS.AssetError):
                ASSETS._validate_source({"kind": "https", "url": url})
        for path in (
            ".",
            "./",
            "../outside",
            "/absolute",
            ".git/refs/heads/main",
            ".GIT/refs/heads/main",
            ".Asset-Cache/sha256/object",
            r"windows\outside",
        ):
            with self.subTest(path=path), self.assertRaises(ASSETS.AssetError):
                ASSETS._safe_relative(path, "test path")

    def test_lock_rejects_case_colliding_targets(self) -> None:
        value = json.loads(self.fixture.lock.read_text(encoding="utf-8"))
        duplicate = copy.deepcopy(value["assets"][0])
        duplicate["id"] = "origin-img-1570-case-collision"
        duplicate["target"] = "Pipeline/.work/raw/IMG_1570.JPG"
        value["assets"].append(duplicate)
        self.fixture.lock.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ASSETS.AssetError, "case-colliding"):
            ASSETS.load_lock(self.fixture.lock)

    def test_lock_rejects_file_and_ancestor_target_collisions_portably(self) -> None:
        for ancestor, descendant in (
            ("pipeline/output", "pipeline/output/asset.bin"),
            ("Pipeline/Output", "pipeline/output/asset.bin"),
        ):
            with self.subTest(ancestor=ancestor):
                value = json.loads(self.fixture.lock.read_text(encoding="utf-8"))
                value["assets"][0]["target"] = ancestor
                descendant_row = copy.deepcopy(value["assets"][0])
                descendant_row["id"] = "origin-img-1570-descendant"
                descendant_row["target"] = descendant
                value["assets"].append(descendant_row)
                self.fixture.lock.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(ASSETS.AssetError, "ancestor-colliding"):
                    ASSETS.load_lock(self.fixture.lock)
                self.fixture.write_lock()

    def test_paths_allow_nfc_and_reject_nfd_unicode_aliases(self) -> None:
        aliases = ("caf\u00e9/asset.bin", "cafe\u0301/asset.bin")
        self.assertEqual(
            ASSETS._portable_path_key(aliases[0]),
            ASSETS._portable_path_key(aliases[1]),
        )
        self.assertEqual(ASSETS._safe_relative(aliases[0], "test path"), aliases[0])
        self.assertEqual(ASSETS._safe_relative("日本/素材.bin", "test path"), "日本/素材.bin")
        value = json.loads(self.fixture.lock.read_text(encoding="utf-8"))
        value["assets"][0]["target"] = aliases[0]
        self.fixture.lock.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(ASSETS.load_lock(self.fixture.lock).assets[0].target, aliases[0])
        with self.assertRaisesRegex(ASSETS.AssetError, "safe POSIX-relative"):
            ASSETS._safe_relative(aliases[1], "test path")
        value["assets"][0]["target"] = aliases[1]
        self.fixture.lock.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ASSETS.AssetError, "safe POSIX-relative"):
            ASSETS.load_lock(self.fixture.lock)
        self.fixture.write_lock()

    def test_surrogate_and_malformed_source_locators_fail_as_controlled_asset_errors(self) -> None:
        invalid_sources = (
            {"kind": "https", "url": "https://example.com/\udcff"},
            {"kind": "https", "url": "https://[bad"},
            {"kind": "https", "url": "https://example.com/raw space"},
            {"kind": "https", "url": "https://example.com/raw\tcontrol"},
            {"kind": "https", "url": "https://example.com/raw-é"},
            {"kind": "https", "url": "https://example.com/back\\slash"},
            {"kind": "https", "url": "https://example.com/bad%escape"},
            {
                "kind": "github-release",
                "repository": "organvm/example",
                "tag": "v1-\udcff",
                "asset": "asset.bin",
            },
            {
                "kind": "github-release",
                "repository": "organvm/example",
                "tag": "v1",
                "asset": "asset-\udcff.bin",
            },
        )
        for source in invalid_sources:
            with self.subTest(source=source):
                self.fixture.write_lock(asset={"sources": [source]})
                with self.assertRaises(ASSETS.AssetError):
                    ASSETS.load_lock(self.fixture.lock)
                self.assertEqual(
                    ASSETS.main(
                        [
                            "audit",
                            "--lock",
                            str(self.fixture.lock),
                            "--root",
                            str(self.fixture.root),
                            "--receipt",
                            str(self.fixture.receipt),
                        ]
                    ),
                    1,
                )
                self.assertFalse(self.fixture.receipt.exists())
        self.fixture.write_lock()
        canonical = "https://example.com/caf%C3%A9/file%20name"
        self.assertEqual(ASSETS._https_url(canonical), canonical)
        self.assertEqual(urllib.request.Request(canonical).full_url, canonical)

    def test_pathological_json_is_controlled_blocked_without_receipt(self) -> None:
        depth = 10_000
        invalid_values = (
            '{"a":' * depth + "0" + "}" * depth,
            '{"a":' + "9" * 5_000 + "}",
            '{"a":NaN}',
            '{"a":Infinity}',
            '{"a":-Infinity}',
        )
        for raw in invalid_values:
            with self.subTest(prefix=raw[:20]):
                self.fixture.lock.write_text(raw, encoding="utf-8")
                with self.assertRaises(ASSETS.AssetError):
                    ASSETS.load_lock(self.fixture.lock)
                self.assertEqual(
                    ASSETS.main(
                        [
                            "audit",
                            "--lock",
                            str(self.fixture.lock),
                            "--root",
                            str(self.fixture.root),
                            "--receipt",
                            str(self.fixture.receipt),
                        ]
                    ),
                    1,
                )
                self.assertFalse(self.fixture.receipt.exists())

    def test_missing_parent_path_preserves_every_component(self) -> None:
        target = ASSETS._path_under(
            self.fixture.root,
            "pipeline/.work/raw/IMG_1570.JPG",
            create_parents=False,
        )
        self.assertEqual(
            target,
            self.fixture.root / "pipeline/.work/raw/IMG_1570.JPG",
        )

    def test_symlinked_cache_cannot_escape_checkout(self) -> None:
        outside = self.fixture.base / "outside-cache"
        outside.mkdir()
        (self.fixture.root / ".asset-cache").symlink_to(outside, target_is_directory=True)
        code = ASSETS.main(
            [
                "pull",
                "--lock",
                str(self.fixture.lock),
                "--root",
                str(self.fixture.root),
                "--allow-file",
                "--file-source-root",
                str(self.fixture.source),
            ]
        )
        self.assertEqual(code, 1)
        self.assertEqual(list(outside.iterdir()), [])

    def test_file_source_symlink_is_never_followed(self) -> None:
        source = self.fixture.source / "pipeline/raw/IMG_1570.JPG"
        outside = self.fixture.base / "outside-source"
        outside.write_bytes(self.fixture.payload)
        source.unlink()
        source.symlink_to(outside)
        code = ASSETS.main(
            [
                "pull",
                "--lock",
                str(self.fixture.lock),
                "--root",
                str(self.fixture.root),
                "--allow-file",
                "--file-source-root",
                str(self.fixture.source),
            ]
        )
        self.assertEqual(code, 1)
        self.assertFalse((self.fixture.root / "pipeline/.work/raw/IMG_1570.JPG").exists())

    def test_symlink_target_fails_without_following_it(self) -> None:
        outside = self.fixture.base / "outside"
        outside.write_bytes(b"outside")
        target = self.fixture.root / "pipeline/.work/raw/IMG_1570.JPG"
        target.parent.mkdir(parents=True)
        target.symlink_to(outside)
        self.assertEqual(
            ASSETS.main(["verify", "--lock", str(self.fixture.lock), "--root", str(self.fixture.root)]),
            1,
        )
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_target_parent_swap_cannot_redirect_publication(self) -> None:
        asset = ASSETS.load_lock(self.fixture.lock).assets[0]
        cache = ASSETS._cache_path(self.fixture.root, asset, create=True)
        cache.write_bytes(self.fixture.payload)
        cache.chmod(0o444)
        target_parent = self.fixture.root / "pipeline/.work/raw"
        target_parent.mkdir(parents=True)
        held_parent = self.fixture.root / "pipeline/.work/raw-held"
        outside = self.fixture.base / "outside-target"
        outside.mkdir()
        real_link = os.link

        def swap_parent_then_link(source, target, **kwargs):
            target_parent.rename(held_parent)
            target_parent.symlink_to(outside, target_is_directory=True)
            return real_link(source, target, **kwargs)

        with mock.patch.object(
            ASSETS.os,
            "link",
            side_effect=swap_parent_then_link,
        ), self.assertRaisesRegex(ASSETS.AssetError, "retained"):
            ASSETS._publish_no_overwrite(self.fixture.root, asset.target, asset)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual([path.name for path in held_parent.iterdir()], [Path(asset.target).name])

    def test_vanished_publication_during_parent_swap_is_controlled_blocked(self) -> None:
        asset = ASSETS.load_lock(self.fixture.lock).assets[0]
        cache = ASSETS._cache_path(self.fixture.root, asset, create=True)
        cache.write_bytes(self.fixture.payload)
        cache.chmod(0o444)
        target = self.fixture.root / asset.target
        target.parent.mkdir(parents=True)
        real_matches = ASSETS._parent_descriptor_matches
        checks = 0

        def remove_after_publication(root, relative, descriptor):
            nonlocal checks
            checks += 1
            if checks == 2:
                target.unlink()
                return False
            return real_matches(root, relative, descriptor)

        with (
            mock.patch.object(
                ASSETS,
                "_parent_descriptor_matches",
                side_effect=remove_after_publication,
            ),
            self.assertRaisesRegex(ASSETS.AssetError, "disappeared after publication"),
        ):
            ASSETS._publish_no_overwrite(self.fixture.root, asset.target, asset)
        self.assertFalse(target.exists())

    def test_parent_swap_cleanup_never_deletes_concurrent_replacement(self) -> None:
        asset = ASSETS.load_lock(self.fixture.lock).assets[0]
        cache = ASSETS._cache_path(self.fixture.root, asset, create=True)
        cache.write_bytes(self.fixture.payload)
        cache.chmod(0o444)
        target = self.fixture.root / asset.target
        target.parent.mkdir(parents=True)
        real_matches = ASSETS._parent_descriptor_matches
        checks = 0

        def replace_after_publication(root, relative, descriptor):
            nonlocal checks
            checks += 1
            if checks == 2:
                target.unlink()
                target.write_bytes(b"concurrent replacement")
                return False
            return real_matches(root, relative, descriptor)

        with (
            mock.patch.object(
                ASSETS,
                "_parent_descriptor_matches",
                side_effect=replace_after_publication,
            ),
            self.assertRaisesRegex(ASSETS.AssetError, "changed after publication.*retained"),
        ):
            ASSETS._publish_no_overwrite(self.fixture.root, asset.target, asset)
        self.assertEqual(target.read_bytes(), b"concurrent replacement")

    def test_target_parent_swap_after_final_identity_removes_publication(self) -> None:
        asset = ASSETS.load_lock(self.fixture.lock).assets[0]
        cache = ASSETS._cache_path(self.fixture.root, asset, create=True)
        cache.write_bytes(self.fixture.payload)
        cache.chmod(0o444)
        target_parent = self.fixture.root / "pipeline/.work/raw"
        target_parent.mkdir(parents=True)
        held_parent = self.fixture.root / "pipeline/.work/raw-held"
        outside = self.fixture.base / "outside-target-final"
        outside.mkdir()
        real_identity_at = ASSETS._identity_at
        swapped = False

        def swap_parent_after_final_identity(parent_descriptor, name, expected):
            nonlocal swapped
            state = real_identity_at(parent_descriptor, name, expected)
            if not swapped and name == Path(asset.target).name and state == "verified":
                target_parent.rename(held_parent)
                target_parent.symlink_to(outside, target_is_directory=True)
                swapped = True
            return state

        with mock.patch.object(
            ASSETS,
            "_identity_at",
            side_effect=swap_parent_after_final_identity,
        ), self.assertRaisesRegex(ASSETS.AssetError, "retained"):
            ASSETS._publish_no_overwrite(self.fixture.root, asset.target, asset)
        self.assertTrue(swapped)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual([path.name for path in held_parent.iterdir()], [Path(asset.target).name])

    def test_writable_preverified_target_is_rejected(self) -> None:
        target = self.fixture.root / "pipeline/.work/raw/IMG_1570.JPG"
        target.parent.mkdir(parents=True)
        target.write_bytes(self.fixture.payload)
        target.chmod(0o644)
        code = ASSETS.main(
            [
                "pull",
                "--lock",
                str(self.fixture.lock),
                "--root",
                str(self.fixture.root),
                "--allow-file",
                "--file-source-root",
                str(self.fixture.source),
            ]
        )
        self.assertEqual(code, 1)
        self.assertEqual(target.read_bytes(), self.fixture.payload)
        self.assertEqual(stat_mode(target), 0o644)

    def test_cache_parent_swap_cannot_redirect_hydration(self) -> None:
        asset = ASSETS.load_lock(self.fixture.lock).assets[0]
        relative = ASSETS._cache_relative(asset)
        cache_parent = (self.fixture.root / relative).parent
        held_parent = cache_parent.with_name(f"{cache_parent.name}-held")
        outside = self.fixture.base / "outside-cache-race"
        outside.mkdir()
        real_temporary = ASSETS._temporary_file_at
        swapped = False

        def swap_parent_then_create(parent):
            nonlocal swapped
            cache_parent.rename(held_parent)
            cache_parent.symlink_to(outside, target_is_directory=True)
            swapped = True
            return real_temporary(parent)

        with mock.patch.object(
            ASSETS,
            "_temporary_file_at",
            side_effect=swap_parent_then_create,
        ), self.assertRaisesRegex(ASSETS.AssetError, "retained"):
            ASSETS._cache_from_sources(
                self.fixture.root,
                asset,
                allow_file=True,
                source_root=self.fixture.source,
                timeout=1.0,
            )
        self.assertTrue(swapped)
        self.assertEqual(list(outside.iterdir()), [])
        held_names = {path.name for path in held_parent.iterdir()}
        self.assertIn(asset.sha256, held_names)
        self.assertFalse(any(name.startswith(".danse-assets-retired-") for name in held_names))

    def test_midstream_http_failure_uses_fallback_and_writes_receipt(self) -> None:
        sources = [
            {"kind": "https", "url": "https://example.com/first"},
            {"kind": "https", "url": "https://example.com/fallback"},
        ]
        self.fixture.write_lock(asset={"sources": sources})
        expected = len(self.fixture.payload)

        class Interrupted(io.BytesIO):
            def read(self, size=-1):
                raise http.client.IncompleteRead(b"partial", expected)

        with mock.patch.object(
            ASSETS,
            "_source_stream",
            side_effect=[Interrupted(), io.BytesIO(self.fixture.payload)],
        ) as source_stream:
            code = ASSETS.main(
                [
                    "pull",
                    "--lock",
                    str(self.fixture.lock),
                    "--root",
                    str(self.fixture.root),
                    "--receipt",
                    str(self.fixture.receipt),
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(source_stream.call_count, 2)
        self.assertTrue(json.loads(self.fixture.receipt.read_text(encoding="utf-8"))["ok"])

    def test_vanished_cache_temp_blocks_without_cache_or_target(self) -> None:
        real_rename = ASSETS._rename_noreplace_at
        real_unlink = os.unlink
        vanished = False

        def vanish_then_report_missing(descriptor, source, destination):
            nonlocal vanished
            if not vanished and str(source).startswith(".asset-"):
                vanished = True
                real_unlink(source, dir_fd=descriptor)
            return real_rename(descriptor, source, destination)

        with mock.patch.object(ASSETS, "_rename_noreplace_at", side_effect=vanish_then_report_missing):
            code = ASSETS.main(
                [
                    "pull",
                    "--lock",
                    str(self.fixture.lock),
                    "--root",
                    str(self.fixture.root),
                    "--allow-file",
                    "--file-source-root",
                    str(self.fixture.source),
                ]
            )
        self.assertTrue(vanished)
        self.assertEqual(code, 1)
        asset = ASSETS.load_lock(self.fixture.lock).assets[0]
        self.assertFalse(ASSETS._cache_path(self.fixture.root, asset, create=False).exists())
        self.assertFalse((self.fixture.root / asset.target).exists())

    def test_repository_commit_is_exact(self) -> None:
        self.fixture.write_lock(repository_commit="a" * 40)
        self.assertEqual(
            ASSETS.main(["audit", "--lock", str(self.fixture.lock), "--root", str(self.fixture.root)]),
            1,
        )

    def test_repository_root_and_checkout_state_are_exact(self) -> None:
        nested = self.fixture.root / "nested"
        nested.mkdir()
        self.assertEqual(
            ASSETS.main(["audit", "--lock", str(self.fixture.lock), "--root", str(nested)]),
            1,
        )
        (self.fixture.root / "README.md").write_text("dirty\n", encoding="utf-8")
        self.assertEqual(
            ASSETS.main(["audit", "--lock", str(self.fixture.lock), "--root", str(self.fixture.root)]),
            1,
        )

    def test_production_metadata_is_revalidated_after_checkout_verification(self) -> None:
        generic = ASSETS.load_lock(self.fixture.lock)
        production = ASSETS.Lock(
            generic.lock_id,
            "screendance-production",
            generic.repository_commit,
            generic.assets,
            generic.sha256,
        )
        events = []

        def verified_head(root, lock):
            events.append("head")
            return production.repository_commit

        def validate(assets, *, repository_root=None, repository_commit=None):
            events.append("production-metadata")
            self.assertEqual(repository_commit, production.repository_commit)

        with (
            mock.patch.object(ASSETS, "_repository_head", side_effect=verified_head),
            mock.patch.object(ASSETS, "_validate_production_assets", side_effect=validate),
        ):
            ASSETS._assert_repository_binding(self.fixture.root, production)
        self.assertEqual(events, ["head", "production-metadata", "head"])

        with (
            mock.patch.object(
                ASSETS,
                "_repository_head",
                return_value=production.repository_commit,
            ),
            mock.patch.object(
                ASSETS,
                "_validate_production_assets",
                side_effect=ASSETS.AssetError("authority metadata changed"),
            ),
            self.assertRaisesRegex(ASSETS.AssetError, "authority metadata changed"),
        ):
            ASSETS._assert_repository_binding(self.fixture.root, production)

    def test_hidden_git_index_flags_are_rejected(self) -> None:
        lock = ASSETS.load_lock(self.fixture.lock)
        for enable, disable in (
            ("--assume-unchanged", "--no-assume-unchanged"),
            ("--skip-worktree", "--no-skip-worktree"),
        ):
            with self.subTest(flag=enable):
                subprocess.run(
                    ["git", "-C", str(self.fixture.root), "update-index", enable, "README.md"],
                    check=True,
                )
                try:
                    with self.assertRaisesRegex(ASSETS.AssetError, "index|skip-worktree"):
                        ASSETS._repository_head(self.fixture.root, lock)
                finally:
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(self.fixture.root),
                            "update-index",
                            disable,
                            "README.md",
                        ],
                        check=True,
                    )

    def test_repository_head_rejects_real_midscan_checkout_switch(self) -> None:
        original = subprocess.run(
            ["git", "-C", str(self.fixture.root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        (self.fixture.root / "README.md").write_text("second commit\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.fixture.root), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(self.fixture.root), "commit", "--quiet", "-m", "second"],
            check=True,
        )
        second = subprocess.run(
            ["git", "-C", str(self.fixture.root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        real_run = subprocess.run
        switched = False

        def switch_after_status(command, *args, **kwargs):
            nonlocal switched
            result = real_run(command, *args, **kwargs)
            if not switched and "status" in command:
                real_run(
                    ["git", "-C", str(self.fixture.root), "checkout", "--quiet", original],
                    check=True,
                )
                switched = True
            return result

        try:
            with (
                mock.patch.object(ASSETS.subprocess, "run", side_effect=switch_after_status),
                self.assertRaisesRegex(ASSETS.AssetError, "identity changed"),
            ):
                ASSETS._repository_head(self.fixture.root)
            self.assertTrue(switched)
        finally:
            real_run(
                ["git", "-C", str(self.fixture.root), "checkout", "--quiet", second],
                check=True,
            )

    def test_repository_head_rejects_tracked_mutation_after_first_status(self) -> None:
        real_run = subprocess.run
        mutated = False

        def mutate_after_first_status(command, *args, **kwargs):
            nonlocal mutated
            result = real_run(command, *args, **kwargs)
            if not mutated and "status" in command:
                (self.fixture.root / "README.md").write_text("changed mid-census\n", encoding="utf-8")
                mutated = True
            return result

        with (
            mock.patch.object(ASSETS.subprocess, "run", side_effect=mutate_after_first_status),
            self.assertRaisesRegex(ASSETS.AssetError, "state changed|tracked or staged"),
        ):
            ASSETS._repository_head(self.fixture.root)
        self.assertTrue(mutated)

    def test_git_identity_commands_disable_all_object_replacements(self) -> None:
        real_run = subprocess.run
        git_environments = []

        def capture_environment(command, *args, **kwargs):
            if command and command[0] == "git":
                git_environments.append(dict(kwargs.get("env", {})))
            return real_run(command, *args, **kwargs)

        with (
            mock.patch.dict(
                os.environ,
                {
                    "GIT_REPLACE_REF_BASE": "refs/replace-attacker",
                    "GIT_OBJECT_DIRECTORY": "/untrusted/object-store",
                },
            ),
            mock.patch.object(ASSETS.subprocess, "run", side_effect=capture_environment),
        ):
            self.assertEqual(ASSETS._repository_head(self.fixture.root), self.fixture.head)
            self.assertEqual(ASSETS._git_head_commit(self.fixture.root), self.fixture.head)
            ASSETS._git_absolute_path(self.fixture.root, "--absolute-git-dir")
            ASSETS._git_reference_lock_path(self.fixture.root)
            self.assertEqual(
                ASSETS._tracked_bytes(self.fixture.root, self.fixture.head, "README.md"),
                (self.fixture.root / "README.md").read_bytes(),
            )
        self.assertTrue(git_environments)
        for environment in git_environments:
            self.assertEqual(environment.get("GIT_NO_REPLACE_OBJECTS"), "1")
            self.assertNotIn("GIT_REPLACE_REF_BASE", environment)
            self.assertNotIn("GIT_OBJECT_DIRECTORY", environment)

    def test_operation_lease_resolves_linked_worktree_gitdir_and_index(self) -> None:
        worktree = self.fixture.base / "linked-worktree"
        subprocess.run(
            [
                "git",
                "-C",
                str(self.fixture.root),
                "worktree",
                "add",
                "--quiet",
                "--detach",
                str(worktree),
                "HEAD",
            ],
            check=True,
        )
        lease = None
        try:
            self.assertTrue((worktree / ".git").is_file())
            with (
                mock.patch.object(ASSETS.sys, "platform", "darwin"),
                mock.patch.object(
                    ASSETS,
                    "_fsync_asset_file",
                    side_effect=lambda descriptor, _label: os.fsync(descriptor),
                ),
            ):
                lease = ASSETS._create_operation_lease(worktree.resolve())
            self.assertNotEqual(lease.git_directory, worktree / ".git")
            index = ASSETS._git_absolute_path(worktree, "--git-path", "index")
            self.assertEqual(lease.index_name, f"{index.name}.lock")
            self.assertTrue(descriptor_matches_path(lease.index_parent_descriptor, index.parent))
            owner = json.loads(
                (lease.git_directory / lease.operation_name).read_text(encoding="utf-8")
            )
            self.assertEqual(set(owner), {"pid", "process_start_id", "token"})
            self.assertRegex(owner["process_start_id"], r"^(?:[0-9a-f]{64}|unverifiable)$")
            self.assertNotIn("/", json.dumps(owner))
            lease.validate()
        finally:
            if lease is not None:
                lease.close()
            subprocess.run(
                ["git", "-C", str(self.fixture.root), "worktree", "remove", "--force", str(worktree)],
                check=True,
            )

    def test_operation_lease_rejects_root_swap_during_establishment(self) -> None:
        root = self.fixture.root.resolve()
        replacement = self.fixture.base / "replacement-repo"
        held = self.fixture.base / "held-repo"
        subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(root), str(replacement)],
            check=True,
        )
        real_sync = ASSETS._fsync_asset_directory
        swapped = False

        def swap_after_git_directory_sync(descriptor, label):
            nonlocal swapped
            result = real_sync(descriptor, label)
            if label == "Git directory" and not swapped:
                root.rename(held)
                replacement.rename(root)
                swapped = True
            return result

        with (
            mock.patch.object(
                ASSETS,
                "_fsync_asset_directory",
                side_effect=swap_after_git_directory_sync,
            ),
            self.assertRaisesRegex(ASSETS.AssetError, "root changed|lease establishment"),
        ):
            ASSETS._create_operation_lease(root)
        self.assertTrue(swapped)
        second = ASSETS._create_operation_lease(root)
        second.close()

    def test_operation_lease_rejects_git_directory_swap(self) -> None:
        root = self.fixture.root.resolve()
        alternate = self.fixture.base / "alternate-repo"
        held_git = root / ".git-held"
        subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(root), str(alternate)],
            check=True,
        )
        lease = ASSETS._create_operation_lease(root)
        (root / ".git").rename(held_git)
        (alternate / ".git").rename(root / ".git")
        with self.assertRaisesRegex(ASSETS.AssetError, "administrative paths changed"):
            lease.validate()
        with self.assertRaisesRegex(ASSETS.AssetError, "released safely"):
            lease.close()
        second = ASSETS._create_operation_lease(root)
        second.close()

    def test_linked_worktree_pointer_swap_cannot_create_two_root_leases(self) -> None:
        first = self.fixture.base / "linked-first"
        second = self.fixture.base / "linked-second"
        for path in (first, second):
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.fixture.root),
                    "worktree",
                    "add",
                    "--quiet",
                    "--detach",
                    str(path),
                    "HEAD",
                ],
                check=True,
            )
        original_pointer = (first / ".git").read_text(encoding="utf-8")
        lease = ASSETS._create_operation_lease(first.resolve())
        (first / ".git").write_text((second / ".git").read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaisesRegex(ASSETS.AssetError, "exact Git checkout root|administrative"):
            lease.validate()
        # The directory-descriptor flock belongs to the visible checkout root,
        # not whichever administrative directory the mutable .git pointer names.
        # A second acquisition must therefore remain blocked while the original
        # (now invalid) lease is still live.
        with self.assertRaisesRegex(ASSETS.AssetError, "checkout root lease"):
            ASSETS._create_operation_lease(first.resolve())
        with self.assertRaisesRegex(ASSETS.AssetError, "released safely"):
            lease.close()
        (first / ".git").write_text(original_pointer, encoding="utf-8")

    def test_operation_lease_fails_closed_on_preexisting_or_crash_stale_locks(self) -> None:
        root = self.fixture.root.resolve()
        git_directory = ASSETS._git_absolute_path(root, "--absolute-git-dir").resolve()
        index = ASSETS._git_absolute_path(root, "--git-path", "index")
        index_lock = Path(f"{index}.lock")
        operation_lock = git_directory / "danse-assets-operation.lock"

        index_lock.write_text("active Git owner\n", encoding="utf-8")
        try:
            with self.assertRaisesRegex(ASSETS.AssetError, "manual|remove it manually"):
                ASSETS._create_operation_lease(root)
            self.assertTrue(index_lock.exists())
            self.assertFalse(operation_lock.exists())
        finally:
            index_lock.unlink(missing_ok=True)

        operation_lock.write_text('{"pid":999999,"started_ns":1,"token":"stale"}\n', encoding="utf-8")
        try:
            with self.assertRaisesRegex(ASSETS.AssetError, "stale|remove it manually"):
                ASSETS._create_operation_lease(root)
            self.assertTrue(operation_lock.exists())
        finally:
            operation_lock.unlink(missing_ok=True)

    def test_operation_lease_cleanup_never_deletes_replacement(self) -> None:
        root = self.fixture.root.resolve()
        lease = ASSETS._create_operation_lease(root)
        operation = lease.git_directory / lease.operation_name
        index_lock = ASSETS._git_absolute_path(root, "--git-path", "index")
        index_lock = Path(f"{index_lock}.lock")
        operation.unlink()
        replacement = b"replacement lease owner\n"
        operation.write_bytes(replacement)
        try:
            with self.assertRaisesRegex(ASSETS.AssetError, "manual recovery|released safely"):
                lease.close()
            self.assertEqual(operation.read_bytes(), replacement)
            self.assertTrue(index_lock.exists())
        finally:
            operation.unlink(missing_ok=True)
            index_lock.unlink(missing_ok=True)

    def test_operation_lease_retirement_retains_post_validation_replacement(self) -> None:
        root = self.fixture.root.resolve()
        lease = ASSETS._create_operation_lease(root)
        operation = lease.git_directory / lease.operation_name
        replacement = b"post-validation replacement owner\n"
        real_rename = ASSETS._rename_noreplace_at
        replaced = False

        def replace_at_retirement(descriptor, source, destination):
            nonlocal replaced
            if source == lease.operation_name and not replaced:
                operation.unlink()
                operation.write_bytes(replacement)
                replaced = True
            return real_rename(descriptor, source, destination)

        with (
            mock.patch.object(ASSETS, "_rename_noreplace_at", side_effect=replace_at_retirement),
            self.assertRaisesRegex(ASSETS.AssetError, "manual recovery|released safely"),
        ):
            lease.close()
        self.assertTrue(replaced)
        self.assertFalse(operation.exists())
        retired = list(lease.git_directory.glob(".danse-assets-retired-*"))
        self.assertIn(replacement, [path.read_bytes() for path in retired])

    def test_git_remains_blocked_after_first_lease_retirement(self) -> None:
        root = self.fixture.root.resolve()
        lease = ASSETS._create_operation_lease(root)
        original = (root / "README.md").read_bytes()
        real_retire = ASSETS._retire_lease_link
        attempts: list[tuple[int, int]] = []

        def mutate_after_operation_retirement(descriptor, name, proof, label):
            retired = real_retire(descriptor, name, proof, label)
            if label == "asset operation lease":
                (root / "README.md").write_text("mutation in lease handoff\n", encoding="utf-8")
                add = subprocess.run(
                    ["git", "-C", str(root), "add", "README.md"],
                    capture_output=True,
                    check=False,
                )
                commit = subprocess.run(
                    ["git", "-C", str(root), "commit", "-m", "forbidden handoff mutation"],
                    capture_output=True,
                    check=False,
                )
                attempts.append((add.returncode, commit.returncode))
                (root / "README.md").write_bytes(original)
            return retired

        with mock.patch.object(
            ASSETS,
            "_retire_lease_link",
            side_effect=mutate_after_operation_retirement,
        ):
            lease.close()
        self.assertEqual(len(attempts), 1)
        self.assertNotEqual(attempts[0][0], 0)
        self.assertNotEqual(attempts[0][1], 0)

    def test_reference_lease_blocks_real_update_ref(self) -> None:
        root = self.fixture.root.resolve()
        environment = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Asset Test",
            "GIT_AUTHOR_EMAIL": "asset@example.invalid",
            "GIT_COMMITTER_NAME": "Asset Test",
            "GIT_COMMITTER_EMAIL": "asset@example.invalid",
        }
        tree = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD^{tree}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        alternate = subprocess.run(
            ["git", "-C", str(root), "commit-tree", tree],
            input="alternate commit\n",
            capture_output=True,
            text=True,
            check=True,
            env=environment,
        ).stdout.strip()
        original = self.fixture.head
        lease = ASSETS._create_operation_lease(root)
        try:
            update = subprocess.run(
                ["git", "-C", str(root), "update-ref", "HEAD", alternate],
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(update.returncode, 0)
            self.assertEqual(ASSETS._git_head_commit(root), original)
        finally:
            lease.close()

    def test_operation_lease_writes_complete_owner_after_short_writes(self) -> None:
        root = self.fixture.root.resolve()
        real_write = os.write
        writes = 0

        def short_write(descriptor, payload):
            nonlocal writes
            writes += 1
            return real_write(descriptor, payload[: max(1, len(payload) // 3)])

        with mock.patch.object(ASSETS.os, "write", side_effect=short_write):
            lease = ASSETS._create_operation_lease(root)
        try:
            operation = json.loads(
                (lease.git_directory / lease.operation_name).read_text(encoding="utf-8")
            )
            index_path = ASSETS._git_absolute_path(root, "--git-path", "index")
            index = json.loads(Path(f"{index_path}.lock").read_text(encoding="utf-8"))
            self.assertEqual(operation, index)
            self.assertEqual(set(operation), {"pid", "process_start_id", "token"})
            self.assertGreater(writes, 2)
        finally:
            lease.close()

    def test_lease_close_failure_after_staging_never_publishes_receipt(self) -> None:
        real_retire = ASSETS._retire_lease_link

        def reject_lease_release(descriptor, name, proof, label):
            if label == "Git index operation lease":
                raise ASSETS.CleanupDurabilityError("injected lease cleanup failure")
            return real_retire(descriptor, name, proof, label)

        with mock.patch.object(
            ASSETS,
            "_retire_lease_link",
            side_effect=reject_lease_release,
        ):
            code = ASSETS.main(
                [
                    "audit",
                    "--lock",
                    str(self.fixture.lock),
                    "--root",
                    str(self.fixture.root),
                    "--receipt",
                    str(self.fixture.receipt),
                ]
            )
        self.assertEqual(code, 1)
        self.assertFalse(self.fixture.receipt.exists())
        self.assertEqual(list(self.fixture.base.glob(".receipt-stage-*")), [])
        git_directory = ASSETS._git_absolute_path(
            self.fixture.root.resolve(), "--absolute-git-dir"
        )
        index_path = ASSETS._git_absolute_path(
            self.fixture.root.resolve(), "--git-path", "index"
        )
        (git_directory / "danse-assets-operation.lock").unlink(missing_ok=True)
        Path(f"{index_path}.lock").unlink(missing_ok=True)

    def test_git_index_mutation_is_blocked_during_receipt_window(self) -> None:
        original = (self.fixture.root / "README.md").read_bytes()
        real_stage = ASSETS._stage_json
        attempted = False

        def attempt_git_mutation(*args, **kwargs):
            nonlocal attempted
            (self.fixture.root / "README.md").write_text("mutated during receipt\n", encoding="utf-8")
            result = subprocess.run(
                ["git", "-C", str(self.fixture.root), "add", "README.md"],
                capture_output=True,
                text=True,
                check=False,
            )
            attempted = True
            self.assertNotEqual(result.returncode, 0)
            (self.fixture.root / "README.md").write_bytes(original)
            return real_stage(*args, **kwargs)

        with mock.patch.object(ASSETS, "_stage_json", side_effect=attempt_git_mutation):
            code = ASSETS.main(
                [
                    "pull",
                    "--lock",
                    str(self.fixture.lock),
                    "--root",
                    str(self.fixture.root),
                    "--allow-file",
                    "--file-source-root",
                    str(self.fixture.source),
                    "--receipt",
                    str(self.fixture.receipt),
                ]
            )
        self.assertTrue(attempted)
        self.assertEqual(code, 0)

    def test_staged_receipt_is_published_without_unlocked_reserialization(self) -> None:
        retained_before = set(self.fixture.base.glob(".danse-assets-retired-*"))
        with mock.patch.object(
            ASSETS,
            "_atomic_json",
            side_effect=AssertionError("receipt payload was regenerated after lease release"),
        ):
            code = ASSETS.main(
                [
                    "pull",
                    "--lock",
                    str(self.fixture.lock),
                    "--root",
                    str(self.fixture.root),
                    "--allow-file",
                    "--file-source-root",
                    str(self.fixture.source),
                    "--receipt",
                    str(self.fixture.receipt),
                ]
            )
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(self.fixture.receipt.read_text(encoding="utf-8"))["ok"])
        self.assertEqual(
            set(self.fixture.base.glob(".danse-assets-retired-*")),
            retained_before,
        )

    def test_staged_publication_retains_unproved_source_substitution(self) -> None:
        output = self.fixture.base / "staged-source-substitution.json"
        staged = ASSETS._stage_json(output, {"ok": True})
        attacker = b'{"attacker":true}\n'
        real_rename = ASSETS._rename_noreplace_at
        substituted = False

        def replace_stage_before_rename(descriptor, source, destination):
            nonlocal substituted
            if source == staged.name and not substituted:
                os.unlink(source, dir_fd=descriptor)
                replacement = os.open(
                    source,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o400,
                    dir_fd=descriptor,
                )
                try:
                    os.write(replacement, attacker)
                finally:
                    os.close(replacement)
                substituted = True
            return real_rename(descriptor, source, destination)

        try:
            with (
                mock.patch.object(
                    ASSETS,
                    "_rename_noreplace_at",
                    side_effect=replace_stage_before_rename,
                ),
                self.assertRaisesRegex(ASSETS.AssetError, "invalidated|retained"),
            ):
                staged.publish(output)
            self.assertTrue(substituted)
            self.assertEqual(output.read_bytes(), attacker)
        finally:
            staged.close()

    def test_staged_publication_never_moves_caller_final_name_substitution(self) -> None:
        output = self.fixture.base / "staged-final-substitution.json"
        staged = ASSETS._stage_json(output, {"ok": True})
        attacker = b'{"caller":true}\n'
        real_rename = ASSETS._rename_noreplace_at
        substituted = False

        def replace_final_after_rename(descriptor, source, destination):
            nonlocal substituted
            result = real_rename(descriptor, source, destination)
            if destination == output.name and not substituted:
                output.unlink()
                output.write_bytes(attacker)
                output.chmod(0o444)
                substituted = True
            return result

        try:
            with (
                mock.patch.object(
                    ASSETS,
                    "_rename_noreplace_at",
                    side_effect=replace_final_after_rename,
                ),
                self.assertRaisesRegex(ASSETS.AssetError, "retained|changed after publication"),
            ):
                staged.publish(output)
            self.assertTrue(substituted)
            self.assertEqual(output.read_bytes(), attacker)
        finally:
            staged.close()

    def test_staged_publication_never_moves_late_final_substitution(self) -> None:
        output = self.fixture.base / "staged-late-final-substitution.json"
        staged = ASSETS._stage_json(output, {"ok": True})
        attacker = b'{"caller":"late"}\n'
        real_sync = ASSETS._durably_sync_published_inode_at
        substituted = False

        def substitute_during_durable_sync(*args, **kwargs):
            nonlocal substituted
            if not substituted:
                output.unlink()
                output.write_bytes(attacker)
                output.chmod(0o444)
                substituted = True
            raise ASSETS.AssetError("injected late durability failure")

        try:
            with (
                mock.patch.object(
                    ASSETS,
                    "_durably_sync_published_inode_at",
                    side_effect=substitute_during_durable_sync,
                ),
                self.assertRaisesRegex(
                    ASSETS.CleanupDurabilityError,
                    "changed after publication and was retained",
                ),
            ):
                staged.publish(output)
            self.assertTrue(substituted)
            self.assertEqual(output.read_bytes(), attacker)
        finally:
            staged.close()

    def test_asset_mutation_during_receipt_staging_blocks_snapshot_commit(self) -> None:
        self.assertEqual(
            ASSETS.main(
                [
                    "pull",
                    "--lock",
                    str(self.fixture.lock),
                    "--root",
                    str(self.fixture.root),
                    "--allow-file",
                    "--file-source-root",
                    str(self.fixture.source),
                ]
            ),
            0,
        )
        asset = ASSETS.load_lock(self.fixture.lock).assets[0]
        target = self.fixture.root / asset.target
        real_stage = ASSETS._stage_json
        mutated = False

        def mutate_after_staging(*args, **kwargs):
            nonlocal mutated
            staged = real_stage(*args, **kwargs)
            target.chmod(0o644)
            target.write_bytes(b"x" * len(self.fixture.payload))
            target.chmod(0o444)
            mutated = True
            return staged

        with mock.patch.object(ASSETS, "_stage_json", side_effect=mutate_after_staging):
            code = ASSETS.main(
                [
                    "pull",
                    "--lock",
                    str(self.fixture.lock),
                    "--root",
                    str(self.fixture.root),
                    "--receipt",
                    str(self.fixture.receipt),
                ]
            )
        self.assertTrue(mutated)
        self.assertEqual(code, 1)
        self.assertFalse(self.fixture.receipt.exists())

    def test_complete_target_set_is_stable_before_report_construction(self) -> None:
        lock = ASSETS.load_lock(self.fixture.lock)
        asset = lock.assets[0]
        target = self.fixture.root / asset.target
        target.parent.mkdir(parents=True)
        target.write_bytes(self.fixture.payload)
        target.chmod(0o444)
        real_identity = ASSETS._identity_proof_under
        mutated = False

        def remove_earlier_target_while_cache_is_checked(root, relative, checked_asset):
            nonlocal mutated
            proof = real_identity(root, relative, checked_asset)
            if relative == ASSETS._cache_relative(asset) and not mutated:
                target.unlink()
                mutated = True
            return proof

        with (
            mock.patch.object(
                ASSETS,
                "_identity_proof_under",
                side_effect=remove_earlier_target_while_cache_is_checked,
            ),
            self.assertRaisesRegex(ASSETS.AssetError, "target tree changed"),
        ):
            ASSETS.inspect("verify", lock, self.fixture.root)
        self.assertTrue(mutated)

    def test_final_target_scan_rechecks_clean_checkout_binding(self) -> None:
        lock = ASSETS.load_lock(self.fixture.lock)
        asset = lock.assets[0]
        target = self.fixture.root / asset.target
        target.parent.mkdir(parents=True)
        target.write_bytes(self.fixture.payload)
        target.chmod(0o444)
        real_identity = ASSETS._identity_proof_under
        target_scans = 0

        def dirty_checkout_after_final_target(root, relative, checked_asset):
            nonlocal target_scans
            proof = real_identity(root, relative, checked_asset)
            if relative == asset.target:
                target_scans += 1
                if target_scans == 2:
                    (self.fixture.root / "README.md").write_text(
                        "concurrent tracked mutation\n",
                        encoding="utf-8",
                    )
            return proof

        with (
            mock.patch.object(
                ASSETS,
                "_identity_proof_under",
                side_effect=dirty_checkout_after_final_target,
            ),
            self.assertRaisesRegex(ASSETS.AssetError, "tracked or staged changes"),
        ):
            ASSETS.inspect("verify", lock, self.fixture.root)
        self.assertEqual(target_scans, 2)

    def test_receipts_are_immutable(self) -> None:
        self.assertEqual(
            ASSETS.main(
                [
                    "audit",
                    "--lock",
                    str(self.fixture.lock),
                    "--root",
                    str(self.fixture.root),
                    "--receipt",
                    str(self.fixture.receipt),
                ]
            ),
            1,
        )
        before = self.fixture.receipt.read_bytes()
        self.assertEqual(
            ASSETS.main(
                [
                    "audit",
                    "--lock",
                    str(self.fixture.lock),
                    "--root",
                    str(self.fixture.root),
                    "--receipt",
                    str(self.fixture.receipt),
                ]
            ),
            1,
        )
        self.assertEqual(self.fixture.receipt.read_bytes(), before)

    def test_receipt_output_cannot_enter_verified_checkout(self) -> None:
        direct = self.fixture.root / "pipeline/.work/verification.json"
        alias = self.fixture.base / "checkout-alias"
        alias.symlink_to(self.fixture.root, target_is_directory=True)
        for receipt in (direct, alias / ".asset-cache/receipt.json"):
            with self.subTest(receipt=receipt):
                self.assertEqual(
                    ASSETS.main(
                        [
                            "audit",
                            "--lock",
                            str(self.fixture.lock),
                            "--root",
                            str(self.fixture.root),
                            "--receipt",
                            str(receipt),
                        ]
                    ),
                    1,
                )
                self.assertFalse(receipt.exists())

    def test_receipt_parent_swap_cannot_redirect_descriptor_publication(self) -> None:
        receipt_parent = self.fixture.base / "external-receipts"
        receipt_parent.mkdir()
        receipt = receipt_parent / "proof.json"
        held_parent = self.fixture.base / "external-receipts-held"
        checkout_destination = self.fixture.root / ".asset-cache/redirected-receipts"
        checkout_destination.mkdir(parents=True)
        real_rename = ASSETS._rename_noreplace_at
        swapped = False

        def publish_then_swap(descriptor, source, destination):
            nonlocal swapped
            result = real_rename(descriptor, source, destination)
            if not swapped:
                receipt_parent.rename(held_parent)
                receipt_parent.symlink_to(checkout_destination, target_is_directory=True)
                swapped = True
            return result

        with (
            mock.patch.object(ASSETS, "_rename_noreplace_at", side_effect=publish_then_swap),
            self.assertRaisesRegex(ASSETS.AssetError, "retained"),
        ):
            ASSETS._atomic_json(
                receipt,
                {"ok": True},
                no_overwrite=True,
                forbidden_root=self.fixture.root,
            )
        self.assertFalse((checkout_destination / receipt.name).exists())
        held_names = {path.name for path in held_parent.iterdir()}
        self.assertEqual(held_names, {receipt.name})
        self.assertEqual(
            json.loads((held_parent / receipt.name).read_text(encoding="utf-8")),
            {"ok": True},
        )

    def test_atomic_json_fsyncs_parent_after_temporary_cleanup(self) -> None:
        output = self.fixture.base / "durable-output.json"
        real_fsync = os.fsync
        synced_directories = []

        def record_sync(descriptor):
            synced_directories.append(stat_mode_descriptor(descriptor))
            return real_fsync(descriptor)

        with mock.patch.object(ASSETS.os, "fsync", side_effect=record_sync):
            ASSETS._atomic_json(output, {"durable": True}, no_overwrite=True)
        self.assertEqual(
            synced_directories,
            ["file", "file", "directory", "file", "directory"],
        )
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"durable": True})

        blocked = self.fixture.base / "unsynced-output.json"

        def reject_directory_sync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("injected directory fsync failure")
            return real_fsync(descriptor)

        with (
            mock.patch.object(ASSETS.os, "fsync", side_effect=reject_directory_sync),
            self.assertRaisesRegex(ASSETS.AssetError, "durably synchronized"),
        ):
            ASSETS._atomic_json(blocked, {"durable": False}, no_overwrite=True)
        self.assertEqual(
            json.loads(blocked.read_text(encoding="utf-8")),
            {"durable": False},
        )

    def test_exact_cleanup_surfaces_barrier_failure_and_preserves_replacement(self) -> None:
        parent = self.fixture.base / "cleanup-parent"
        parent.mkdir()
        descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        original = parent / "object"
        original.write_bytes(b"original")
        proof = ASSETS._inode_identity(original.stat())
        try:
            with (
                mock.patch.object(
                    ASSETS,
                    "_fsync_asset_directory",
                    side_effect=ASSETS.AssetError("cleanup barrier failed"),
                ),
                self.assertRaisesRegex(ASSETS.AssetError, "cleanup barrier failed"),
            ):
                ASSETS._remove_temporary_link(descriptor, original.name, proof, "test object")
            self.assertFalse(original.exists())

            original.write_bytes(b"first")
            stale_proof = ASSETS._inode_identity(original.stat())
            real_rename = ASSETS._rename_noreplace_at
            replacement = b"replacement"

            def replace_at_retirement(retirement_descriptor, source, destination):
                if source == original.name:
                    original.unlink()
                    original.write_bytes(replacement)
                return real_rename(retirement_descriptor, source, destination)

            with (
                mock.patch.object(
                    ASSETS,
                    "_rename_noreplace_at",
                    side_effect=replace_at_retirement,
                ),
                self.assertRaisesRegex(ASSETS.AssetError, "changed during retirement"),
            ):
                ASSETS._remove_temporary_link(
                    descriptor,
                    original.name,
                    stale_proof,
                    "test object",
                )
            self.assertFalse(original.exists())
            retired = list(parent.glob(".danse-assets-retired-*"))
            self.assertIn(replacement, [path.read_bytes() for path in retired])
        finally:
            os.close(descriptor)

    def test_retirement_never_overwrites_a_concurrent_quarantine_collision(self) -> None:
        parent = self.fixture.base / "retirement-collision"
        parent.mkdir()
        descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        source = parent / "owned"
        source.write_bytes(b"owned bytes")
        proof = ASSETS._inode_identity(source.stat())
        collision = b"somebody else's quarantine bytes"
        real_rename = ASSETS._rename_noreplace_at
        injected_name = None

        def collide_at_create_only_rename(retirement_descriptor, name, destination):
            nonlocal injected_name
            if injected_name is None:
                injected_name = destination
                (parent / destination).write_bytes(collision)
            return real_rename(retirement_descriptor, name, destination)

        try:
            with mock.patch.object(
                ASSETS,
                "_rename_noreplace_at",
                side_effect=collide_at_create_only_rename,
            ):
                retired_name = ASSETS._retire_named_link(
                    descriptor,
                    source.name,
                    proof,
                    "collision probe",
                    missing_ok=False,
                )
            self.assertIsNotNone(injected_name)
            self.assertNotEqual(retired_name, injected_name)
            self.assertEqual((parent / injected_name).read_bytes(), collision)
            self.assertEqual((parent / retired_name).read_bytes(), b"owned bytes")
            self.assertFalse(source.exists())
        finally:
            os.close(descriptor)

    def test_create_only_rename_rejects_nul_names_before_syscall(self) -> None:
        parent = self.fixture.base / "nul-rename"
        parent.mkdir()
        descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        (parent / "source").write_bytes(b"source")
        try:
            with self.assertRaises(OSError):
                ASSETS._rename_noreplace_at(descriptor, "source", "actual.json\x00suffix")
            self.assertEqual((parent / "source").read_bytes(), b"source")
            self.assertFalse((parent / "actual.json").exists())
        finally:
            os.close(descriptor)

    def test_vanished_receipt_temp_blocks_without_authoritative_output(self) -> None:
        output = self.fixture.base / "vanished-temp-receipt.json"
        real_rename = ASSETS._rename_noreplace_at
        real_unlink = os.unlink
        vanished = False

        def vanish_then_report_missing(descriptor, source, destination):
            nonlocal vanished
            if not vanished and str(source).startswith(".receipt-"):
                vanished = True
                real_unlink(source, dir_fd=descriptor)
            return real_rename(descriptor, source, destination)

        with (
            mock.patch.object(
                ASSETS,
                "_rename_noreplace_at",
                side_effect=vanish_then_report_missing,
            ),
            self.assertRaisesRegex(ASSETS.AssetError, "published atomically"),
        ):
            ASSETS._atomic_json(output, {"ok": True}, no_overwrite=True)
        self.assertTrue(vanished)
        self.assertFalse(output.exists())

    def test_atomic_json_rejects_final_name_substitution_without_deleting_it(self) -> None:
        output = self.fixture.base / "substituted-receipt.json"
        attacker = b'{"attacker":true}\n'
        real_rename = ASSETS._rename_noreplace_at
        substituted = False

        def substitute_after_create_only_rename(descriptor, source, destination):
            nonlocal substituted
            result = real_rename(descriptor, source, destination)
            if destination == output.name and not substituted:
                output.unlink()
                output.write_bytes(attacker)
                output.chmod(0o444)
                substituted = True
            return result

        with (
            mock.patch.object(
                ASSETS,
                "_rename_noreplace_at",
                side_effect=substitute_after_create_only_rename,
            ),
            self.assertRaisesRegex(
                ASSETS.AssetError,
                "retained|changed after publication",
            ),
        ):
            ASSETS._atomic_json(output, {"ok": True}, no_overwrite=True)
        self.assertTrue(substituted)
        self.assertEqual(output.read_bytes(), attacker)

    def test_atomic_json_never_moves_late_final_substitution(self) -> None:
        output = self.fixture.base / "atomic-late-final-substitution.json"
        attacker = b'{"caller":"late"}\n'
        substituted = False

        def substitute_during_durable_sync(*args, **kwargs):
            nonlocal substituted
            if not substituted:
                output.unlink()
                output.write_bytes(attacker)
                output.chmod(0o444)
                substituted = True
            raise ASSETS.AssetError("injected late durability failure")

        with (
            mock.patch.object(
                ASSETS,
                "_durably_sync_published_inode_at",
                side_effect=substitute_during_durable_sync,
            ),
            self.assertRaisesRegex(
                ASSETS.CleanupDurabilityError,
                "changed after publication and was retained",
            ),
        ):
            ASSETS._atomic_json(output, {"ok": True}, no_overwrite=True)
        self.assertTrue(substituted)
        self.assertEqual(output.read_bytes(), attacker)

    def test_cache_proof_failure_after_link_retains_published_inode(self) -> None:
        asset = ASSETS.load_lock(self.fixture.lock).assets[0]
        real_identity = ASSETS._guarded_identity_at

        def reject_published_cache(descriptor, name, guard, label):
            if label == "published cache object":
                raise ASSETS.AssetError("injected post-link proof failure")
            return real_identity(descriptor, name, guard, label)

        with (
            mock.patch.object(
                ASSETS,
                "_guarded_identity_at",
                side_effect=reject_published_cache,
            ),
            self.assertRaisesRegex(ASSETS.AssetError, "injected post-link proof failure"),
        ):
            ASSETS._cache_from_sources(
                self.fixture.root,
                asset,
                allow_file=True,
                source_root=self.fixture.source,
                timeout=1.0,
            )
        self.assertEqual(
            ASSETS._cache_path(self.fixture.root, asset, create=False).read_bytes(),
            self.fixture.payload,
        )

    def test_target_proof_failure_after_link_retains_published_inode(self) -> None:
        asset = ASSETS.load_lock(self.fixture.lock).assets[0]
        cache = ASSETS._cache_path(self.fixture.root, asset, create=True)
        cache.write_bytes(self.fixture.payload)
        cache.chmod(0o444)
        real_identity = ASSETS._inode_identity_at

        def reject_published_target(descriptor, name, label):
            if label == "published asset target":
                raise ASSETS.AssetError("injected post-link proof failure")
            return real_identity(descriptor, name, label)

        with (
            mock.patch.object(
                ASSETS,
                "_inode_identity_at",
                side_effect=reject_published_target,
            ),
            self.assertRaisesRegex(ASSETS.AssetError, "retained"),
        ):
            ASSETS._publish_no_overwrite(self.fixture.root, asset.target, asset)
        self.assertEqual((self.fixture.root / asset.target).read_bytes(), self.fixture.payload)

    def test_receipt_proof_failure_after_link_retains_published_inode(self) -> None:
        output = self.fixture.base / "post-link-proof-failure.json"
        real_identity = ASSETS._guarded_identity_at

        def reject_published_receipt(descriptor, name, guard, label):
            if label == "published receipt":
                raise ASSETS.AssetError("injected post-link proof failure")
            return real_identity(descriptor, name, guard, label)

        with (
            mock.patch.object(
                ASSETS,
                "_guarded_identity_at",
                side_effect=reject_published_receipt,
            ),
            self.assertRaisesRegex(ASSETS.AssetError, "retained"),
        ):
            ASSETS._atomic_json(output, {"ok": True}, no_overwrite=True)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"ok": True})

    def test_atomic_json_persists_every_new_output_ancestor(self) -> None:
        output = self.fixture.base / "external/a/b/proof.json"
        directories = {
            "base": self.fixture.base,
            "external": self.fixture.base / "external",
            "a": self.fixture.base / "external/a",
            "b": self.fixture.base / "external/a/b",
        }
        events = []
        real_fsync = os.fsync

        def record_sync(descriptor):
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                events.append("file")
            else:
                for label, path in directories.items():
                    if descriptor_matches_path(descriptor, path):
                        events.append(label)
                        break
            return real_fsync(descriptor)

        with mock.patch.object(ASSETS.os, "fsync", side_effect=record_sync):
            ASSETS._atomic_json(output, {"durable": True}, no_overwrite=True)
        file_index = events.index("file")
        for label in ("base", "external", "a"):
            with self.subTest(directory=label):
                self.assertLess(events.index(label), file_index)
        self.assertGreater(events.index("b"), file_index)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"durable": True})

    def test_output_ancestor_sync_failure_cleans_directory_and_blocks_output(self) -> None:
        output = self.fixture.base / "blocked-parent/a/proof.json"
        real_fsync = os.fsync
        rejected = False

        def reject_base_once(descriptor):
            nonlocal rejected
            if not rejected and descriptor_matches_path(descriptor, self.fixture.base):
                rejected = True
                raise OSError("injected output ancestor sync failure")
            return real_fsync(descriptor)

        with (
            mock.patch.object(ASSETS.os, "fsync", side_effect=reject_base_once),
            self.assertRaisesRegex(ASSETS.AssetError, "retained"),
        ):
            ASSETS._atomic_json(output, {"durable": False}, no_overwrite=True)
        self.assertTrue(rejected)
        self.assertTrue((self.fixture.base / "blocked-parent").exists())
        self.assertFalse(output.exists())

    def test_receipt_parent_swap_during_directory_sync_retains_published_link(self) -> None:
        receipt_parent = self.fixture.base / "late-swap-receipts"
        receipt_parent.mkdir()
        receipt = receipt_parent / "proof.json"
        held_parent = self.fixture.base / "late-swap-receipts-held"
        checkout_destination = self.fixture.root / ".asset-cache/late-redirect"
        checkout_destination.mkdir(parents=True)
        real_fsync = os.fsync
        swapped = False

        def sync_then_swap(descriptor):
            nonlocal swapped
            result = real_fsync(descriptor)
            if stat.S_ISDIR(os.fstat(descriptor).st_mode) and not swapped:
                receipt_parent.rename(held_parent)
                receipt_parent.symlink_to(checkout_destination, target_is_directory=True)
                swapped = True
            return result

        with (
            mock.patch.object(ASSETS.os, "fsync", side_effect=sync_then_swap),
            self.assertRaisesRegex(ASSETS.AssetError, "retained"),
        ):
            ASSETS._atomic_json(
                receipt,
                {"ok": True},
                no_overwrite=True,
                forbidden_root=self.fixture.root,
            )
        self.assertTrue(swapped)
        self.assertFalse((checkout_destination / receipt.name).exists())
        held_names = {path.name for path in held_parent.iterdir()}
        self.assertEqual(held_names, {receipt.name})
        self.assertEqual(
            json.loads((held_parent / receipt.name).read_text(encoding="utf-8")),
            {"ok": True},
        )

    def test_inventory_is_deterministic_closed_and_no_overwrite(self) -> None:
        duplicate = self.fixture.source / "other/duplicate.JPG"
        duplicate.parent.mkdir()
        duplicate.write_bytes(self.fixture.payload)
        output = self.fixture.base / "generated-lock.json"
        value = ASSETS.inventory(
            self.fixture.source,
            output,
            lock_id="generated-assets",
            profile="generic",
            repository_commit=self.fixture.head,
            rights_class="private",
        )
        self.assertEqual(len(value["assets"]), 2)
        self.assertEqual(len({row["id"] for row in value["assets"]}), 2)
        self.assertTrue(all(row["id"].startswith("asset-") for row in value["assets"]))
        self.assertTrue(all("pipeline" not in row["id"] for row in value["assets"]))
        self.assertEqual(value["assets"][0]["sources"][0]["kind"], "file")
        self.assertEqual(list(self.fixture.base.glob(".receipt-*")), [])
        self.assertEqual(list(self.fixture.base.glob(".danse-assets-retired-*")), [])
        with self.assertRaisesRegex(ASSETS.AssetError, "already exists"):
            ASSETS.inventory(
                self.fixture.source,
                output,
                lock_id="generated-assets",
                profile="generic",
                repository_commit=self.fixture.head,
                rights_class="private",
            )

    def test_inventory_rejects_output_overlap_before_generic_or_production_writes(self) -> None:
        git_directory = ASSETS._git_absolute_path(
            self.fixture.root.resolve(), "--absolute-git-dir"
        )
        index_path = ASSETS._git_absolute_path(self.fixture.root.resolve(), "--git-path", "index")
        outputs = (
            self.fixture.source,
            self.fixture.source / "generated-lock.json",
            self.fixture.source / "nested/generated-lock.json",
        )
        for profile in ("generic", "screendance-production"):
            for output in outputs:
                with self.subTest(profile=profile, output=output):
                    with (
                        mock.patch.object(ASSETS, "ROOT", self.fixture.root),
                        self.assertRaisesRegex(ASSETS.AssetError, "outside.*source root"),
                    ):
                        ASSETS.inventory(
                            self.fixture.source,
                            output,
                            lock_id="overlap-assets",
                            profile=profile,
                            repository_commit=self.fixture.head,
                            rights_class="private",
                        )
                    self.assertFalse((self.fixture.source / "nested").exists())
                    self.assertFalse((git_directory / "danse-assets-operation.lock").exists())
                    self.assertFalse(Path(f"{index_path}.lock").exists())

    def test_inventory_rejects_nul_output_before_generic_or_production_writes(self) -> None:
        truncated = self.fixture.base / "actual-lock.json"
        output = Path(f"{truncated}\x00suffix")
        for profile in ("generic", "screendance-production"):
            with self.subTest(profile=profile):
                with self.assertRaisesRegex(ASSETS.AssetError, "output path is invalid"):
                    ASSETS.inventory(
                        self.fixture.source,
                        output,
                        lock_id="nul-output-assets",
                        profile=profile,
                        repository_commit=self.fixture.head,
                        rights_class="private",
                    )
                self.assertFalse(truncated.exists())
        self.assertEqual(
            ASSETS.main(
                [
                    "inventory",
                    "--source-root",
                    str(self.fixture.source),
                    "--output",
                    str(output),
                    "--lock-id",
                    "nul-output-assets",
                    "--repository-commit",
                    self.fixture.head,
                ]
            ),
            1,
        )
        self.assertFalse(truncated.exists())

    def test_looped_output_and_receipt_parents_fail_without_visible_output(self) -> None:
        first = self.fixture.base / "loop-a"
        second = self.fixture.base / "loop-b"
        first.symlink_to(second, target_is_directory=True)
        second.symlink_to(first, target_is_directory=True)
        inventory_output = first / "inventory.json"
        with self.assertRaisesRegex(ASSETS.AssetError, "resolved safely"):
            ASSETS.inventory(
                self.fixture.source,
                inventory_output,
                lock_id="loop-output-assets",
                profile="generic",
                repository_commit=self.fixture.head,
                rights_class="private",
            )
        self.assertFalse((self.fixture.base / "inventory.json").exists())

        receipt = first / "receipt.json"
        self.assertEqual(
            ASSETS.main(
                [
                    "audit",
                    "--lock",
                    str(self.fixture.lock),
                    "--root",
                    str(self.fixture.root),
                    "--receipt",
                    str(receipt),
                ]
            ),
            1,
        )
        self.assertFalse((self.fixture.base / "receipt.json").exists())

    def test_inventory_allows_source_below_a_nonoverlapping_output_parent(self) -> None:
        container = self.fixture.base / "inventory-container"
        source = container / "source"
        source.mkdir(parents=True)
        (source / "asset.bin").write_bytes(b"asset")
        output = container / "lock.json"
        value = ASSETS.inventory(
            source,
            output,
            lock_id="nested-source-assets",
            profile="generic",
            repository_commit=self.fixture.head,
            rights_class="private",
        )
        self.assertEqual(len(value["assets"]), 1)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), value)
        self.assertEqual(list(container.glob(".danse-assets-retired-*")), [])

    def test_inventory_parent_symlink_repoint_cannot_redirect_output_into_source(self) -> None:
        relative = "pipeline/raw/IMG_1570.JPG"
        real_inventory_value = ASSETS._inventory_value
        for profile in ("generic", "screendance-production"):
            with self.subTest(profile=profile):
                outside = self.fixture.base / f"outside-{profile}"
                outside.mkdir()
                alias = self.fixture.base / f"output-alias-{profile}"
                alias.symlink_to(outside, target_is_directory=True)
                output_name = f"{profile}-lock.json"
                output = alias / output_name
                repointed = False

                def repoint_after_inventory(*args, **kwargs):
                    nonlocal repointed
                    value = real_inventory_value(*args, **kwargs)
                    alias.unlink()
                    alias.symlink_to(self.fixture.source, target_is_directory=True)
                    repointed = True
                    return value

                with (
                    mock.patch.object(ASSETS, "ROOT", self.fixture.root),
                    mock.patch.object(ASSETS, "_production_targets", return_value={relative}),
                    mock.patch.object(ASSETS, "_validate_production_assets"),
                    mock.patch.object(
                        ASSETS,
                        "_inventory_value",
                        side_effect=repoint_after_inventory,
                    ),
                ):
                    value = ASSETS.inventory(
                        self.fixture.source,
                        output,
                        lock_id=f"{profile}-assets",
                        profile=profile,
                        repository_commit=self.fixture.head,
                        rights_class="private",
                    )
                self.assertTrue(repointed)
                self.assertEqual(
                    json.loads((outside / output_name).read_text(encoding="utf-8")),
                    value,
                )
                self.assertFalse((self.fixture.source / output_name).exists())

    @unittest.skipUnless(os.name == "posix", "byte-name fixture requires POSIX")
    def test_inventory_rejects_non_utf8_source_name_without_output_or_traceback(self) -> None:
        source = self.fixture.base / "non-utf8-source"
        source.mkdir()
        byte_path = os.fsencode(source) + b"/asset-\xff.bin"
        descriptor = os.open(byte_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, b"private bytes")
        finally:
            os.close(descriptor)
        output = self.fixture.base / "non-utf8-lock.json"
        with self.assertRaisesRegex(ASSETS.AssetError, "safe POSIX-relative"):
            ASSETS.inventory(
                source,
                output,
                lock_id="non-utf8-assets",
                profile="generic",
                repository_commit=self.fixture.head,
                rights_class="private",
            )
        self.assertFalse(output.exists())

    def test_inventory_rejects_content_mutation_before_publication(self) -> None:
        output = self.fixture.base / "mutable-lock.json"
        source = self.fixture.source / "pipeline/raw/IMG_1570.JPG"
        real_snapshot = ASSETS._inventory_snapshot
        calls = 0

        def mutate_after_first_snapshot(root, *args, **kwargs):
            nonlocal calls
            proof = real_snapshot(root, *args, **kwargs)
            calls += 1
            if calls == 1:
                source.write_bytes(b"x" * len(self.fixture.payload))
            return proof

        with mock.patch.object(
            ASSETS,
            "_inventory_snapshot",
            side_effect=mutate_after_first_snapshot,
        ), self.assertRaisesRegex(ASSETS.AssetError, "completed scan"):
            ASSETS.inventory(
                self.fixture.source,
                output,
                lock_id="mutable-assets",
                profile="generic",
                repository_commit=self.fixture.head,
                rights_class="private",
            )
        self.assertEqual(calls, 2)
        self.assertFalse(output.exists())

    def test_inventory_rejects_late_file_before_publication(self) -> None:
        output = self.fixture.base / "late-lock.json"
        real_snapshot = ASSETS._inventory_snapshot
        calls = 0

        def add_file_after_first_snapshot(root, *args, **kwargs):
            nonlocal calls
            proof = real_snapshot(root, *args, **kwargs)
            calls += 1
            if calls == 1:
                late = self.fixture.source / "late/private.bin"
                late.parent.mkdir()
                late.write_bytes(b"late")
            return proof

        with mock.patch.object(
            ASSETS,
            "_inventory_snapshot",
            side_effect=add_file_after_first_snapshot,
        ), self.assertRaisesRegex(ASSETS.AssetError, "completed scan"):
            ASSETS.inventory(
                self.fixture.source,
                output,
                lock_id="late-assets",
                profile="generic",
                repository_commit=self.fixture.head,
                rights_class="private",
            )
        self.assertEqual(calls, 1)
        self.assertFalse(output.exists())

    def test_inventory_rejects_source_root_replacement_after_completed_scan(self) -> None:
        relative = "pipeline/raw/IMG_1570.JPG"
        real_inventory_value = ASSETS._inventory_value
        for profile in ("generic", "screendance-production"):
            for replacement_kind in ("symlink", "directory"):
                with self.subTest(profile=profile, replacement=replacement_kind):
                    source = self.fixture.base / f"source-{profile}-{replacement_kind}"
                    asset = source / relative
                    asset.parent.mkdir(parents=True)
                    asset.write_bytes(self.fixture.payload)
                    held = self.fixture.base / f"held-{profile}-{replacement_kind}"
                    sibling = self.fixture.base / f"replacement-{profile}-{replacement_kind}"
                    if replacement_kind == "symlink":
                        sibling.symlink_to(held, target_is_directory=True)
                    else:
                        replacement_asset = sibling / relative
                        replacement_asset.parent.mkdir(parents=True)
                        replacement_asset.write_bytes(self.fixture.payload)
                    output = self.fixture.base / f"lock-{profile}-{replacement_kind}.json"
                    replaced = False

                    def replace_after_scan(*args, **kwargs):
                        nonlocal replaced
                        value = real_inventory_value(*args, **kwargs)
                        source.rename(held)
                        sibling.rename(source)
                        replaced = True
                        return value

                    with (
                        mock.patch.object(ASSETS, "ROOT", self.fixture.root),
                        mock.patch.object(ASSETS, "_production_targets", return_value={relative}),
                        mock.patch.object(ASSETS, "_validate_production_assets"),
                        mock.patch.object(
                            ASSETS,
                            "_inventory_value",
                            side_effect=replace_after_scan,
                        ),
                        self.assertRaisesRegex(ASSETS.AssetError, "source root moved|replaced"),
                    ):
                        ASSETS.inventory(
                            source,
                            output,
                            lock_id=f"{profile}-{replacement_kind}-assets",
                            profile=profile,
                            repository_commit=self.fixture.head,
                            rights_class="private",
                        )
                    self.assertTrue(replaced)
                    self.assertFalse(output.exists())

    def test_inventory_census_is_bound_to_the_held_source_root(self) -> None:
        source = self.fixture.base / "held-census-source"
        source.mkdir()
        (source / "asset.bin").write_bytes(b"selected source")
        held = self.fixture.base / "held-census-original"
        sibling = self.fixture.base / "held-census-sibling"
        sibling.mkdir()
        (sibling / "asset.bin").write_bytes(b"replacement source")
        output = self.fixture.base / "held-census-lock.json"
        real_snapshot = ASSETS._inventory_snapshot
        swapped = False

        def scan_replacement_then_restore(root, *args, **kwargs):
            nonlocal swapped
            source.rename(held)
            sibling.rename(source)
            swapped = True
            try:
                return real_snapshot(root, *args, **kwargs)
            finally:
                source.rename(sibling)
                held.rename(source)

        with (
            mock.patch.object(
                ASSETS,
                "_inventory_snapshot",
                side_effect=scan_replacement_then_restore,
            ),
            self.assertRaisesRegex(ASSETS.AssetError, "source root moved|replaced"),
        ):
            ASSETS.inventory(
                source,
                output,
                lock_id="held-census-assets",
                profile="generic",
                repository_commit=self.fixture.head,
                rights_class="private",
            )
        self.assertTrue(swapped)
        self.assertEqual((source / "asset.bin").read_bytes(), b"selected source")
        self.assertFalse(output.exists())

    def test_generic_inventory_rechecks_source_after_output_staging(self) -> None:
        source = self.fixture.base / "generic-staging-source"
        asset = source / "asset.bin"
        asset.parent.mkdir()
        asset.write_bytes(b"source")
        output = self.fixture.base / "generic-staging-lock.json"
        real_fsync = ASSETS.os.fsync
        mutated = False

        def mutate_during_file_sync(descriptor):
            nonlocal mutated
            result = real_fsync(descriptor)
            if not mutated and stat.S_ISREG(os.fstat(descriptor).st_mode):
                asset.write_bytes(b"change")
                mutated = True
            return result

        with (
            mock.patch.object(ASSETS.os, "fsync", side_effect=mutate_during_file_sync),
            self.assertRaisesRegex(ASSETS.AssetError, "source changed"),
        ):
            ASSETS.inventory(
                source,
                output,
                lock_id="generic-staging-assets",
                profile="generic",
                repository_commit=self.fixture.head,
                rights_class="private",
            )
        self.assertTrue(mutated)
        self.assertFalse(output.exists())

    def test_inventory_fails_closed_on_source_mutation_during_durable_publication(self) -> None:
        source = self.fixture.base / "durable-publication-source"
        asset = source / "asset.bin"
        asset.parent.mkdir()
        asset.write_bytes(b"source")
        output = self.fixture.base / "durable-publication-lock.json"
        real_sync = ASSETS._durably_sync_published_inode_at
        mutated = False

        def mutate_after_output_sync(*args, **kwargs):
            nonlocal mutated
            result = real_sync(*args, **kwargs)
            asset.write_bytes(b"change")
            mutated = True
            return result

        with (
            mock.patch.object(
                ASSETS,
                "_durably_sync_published_inode_at",
                side_effect=mutate_after_output_sync,
            ),
            self.assertRaisesRegex(ASSETS.CleanupDurabilityError, "retained"),
        ):
            ASSETS.inventory(
                source,
                output,
                lock_id="durable-publication-assets",
                profile="generic",
                repository_commit=self.fixture.head,
                rights_class="private",
            )
        self.assertTrue(mutated)
        self.assertTrue(output.exists())

    def test_production_inventory_rechecks_source_after_staged_payload_read(self) -> None:
        source = self.fixture.base / "production-publish-source"
        relative = "pipeline/raw/IMG_1570.JPG"
        asset = source / relative
        asset.parent.mkdir(parents=True)
        asset.write_bytes(self.fixture.payload)
        output = self.fixture.base / "production-publish-lock.json"
        real_payload = ASSETS.StagedJson.payload
        payload_reads = 0
        mutated = False

        def mutate_during_final_payload_read(staged):
            nonlocal payload_reads, mutated
            payload = real_payload(staged)
            payload_reads += 1
            if payload_reads == 3:
                asset.write_bytes(b"x" * len(self.fixture.payload))
                mutated = True
            return payload

        with (
            mock.patch.object(ASSETS, "ROOT", self.fixture.root),
            mock.patch.object(ASSETS, "_production_targets", return_value={relative}),
            mock.patch.object(ASSETS, "_validate_production_assets"),
            mock.patch.object(
                ASSETS.StagedJson,
                "payload",
                autospec=True,
                side_effect=mutate_during_final_payload_read,
            ),
            self.assertRaisesRegex(ASSETS.AssetError, "source changed"),
        ):
            ASSETS.inventory(
                source,
                output,
                lock_id="production-publish-assets",
                profile="screendance-production",
                repository_commit=self.fixture.head,
                rights_class="private",
            )
        self.assertEqual(payload_reads, 3)
        self.assertTrue(mutated)
        self.assertFalse(output.exists())

    def test_inventory_opens_files_relative_to_held_parent_descriptors(self) -> None:
        output = self.fixture.base / "descriptor-lock.json"
        real_open = ASSETS.os.open
        observed_parent_descriptors = []

        def record_file_open(path, flags, *args, dir_fd=None, **kwargs):
            if path == "IMG_1570.JPG":
                observed_parent_descriptors.append(dir_fd)
            return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

        with mock.patch.object(ASSETS.os, "open", side_effect=record_file_open):
            ASSETS.inventory(
                self.fixture.source,
                output,
                lock_id="descriptor-assets",
                profile="generic",
                repository_commit=self.fixture.head,
                rights_class="private",
            )
        self.assertGreaterEqual(len(observed_parent_descriptors), 2)
        self.assertTrue(all(value is not None for value in observed_parent_descriptors))

    def test_empty_generic_inventory_is_rejected_before_write(self) -> None:
        empty = self.fixture.base / "empty-source"
        empty.mkdir()
        output = self.fixture.base / "empty-lock.json"
        with self.assertRaisesRegex(ASSETS.AssetError, "at least one"):
            ASSETS.inventory(
                empty,
                output,
                lock_id="empty-assets",
                profile="generic",
                repository_commit=self.fixture.head,
                rights_class="private",
            )
        self.assertFalse(output.exists())

    def test_generic_inventory_rejects_unsafe_targets_before_write(self) -> None:
        for index, relative in enumerate((r"bad\name.bin", ".GIT/config")):
            with self.subTest(relative=relative):
                source = self.fixture.base / f"unsafe-source-{index}"
                target = source / relative
                target.parent.mkdir(parents=True)
                target.write_bytes(b"unsafe")
                output = self.fixture.base / f"unsafe-lock-{index}.json"
                with self.assertRaises(ASSETS.AssetError):
                    ASSETS.inventory(
                        source,
                        output,
                        lock_id="unsafe-assets",
                        profile="generic",
                        repository_commit=self.fixture.head,
                        rights_class="private",
                    )
                self.assertFalse(output.exists())

    def test_generic_inventory_rejects_case_colliding_targets_before_write(self) -> None:
        source = self.fixture.base / "case-colliding-source"
        source.mkdir()
        (source / "Asset.bin").write_bytes(b"first")
        (source / "asset.bin").write_bytes(b"second")
        if {entry.name for entry in source.iterdir()} != {"Asset.bin", "asset.bin"}:
            self.skipTest("case-insensitive filesystem cannot hold case-colliding names")
        output = self.fixture.base / "case-colliding-lock.json"
        with self.assertRaisesRegex(ASSETS.AssetError, "case-colliding"):
            ASSETS.inventory(
                source,
                output,
                lock_id="case-colliding-assets",
                profile="generic",
                repository_commit=self.fixture.head,
                rights_class="private",
            )
        self.assertFalse(output.exists())

    def test_generic_inventory_rejects_portable_ancestor_collisions_before_write(self) -> None:
        source = self.fixture.base / "ancestor-colliding-source"
        source.mkdir()
        payload = b"portable-ancestor"
        digest = hashlib.sha256(payload).hexdigest()
        entries = (
            ASSETS.InventoryEntry("Foo", "file", (1, 2, 0, len(payload), 3, 4), len(payload), digest),
            ASSETS.InventoryEntry(
                "foo/asset.bin",
                "file",
                (1, 3, 0, len(payload), 3, 4),
                len(payload),
                digest,
            ),
        )
        output = self.fixture.base / "ancestor-colliding-lock.json"
        with (
            mock.patch.object(ASSETS, "_inventory_snapshot", return_value=entries),
            self.assertRaisesRegex(ASSETS.AssetError, "ancestor-colliding"),
        ):
            ASSETS.inventory(
                source,
                output,
                lock_id="ancestor-colliding-assets",
                profile="generic",
                repository_commit=self.fixture.head,
                rights_class="private",
            )
        self.assertFalse(output.exists())

    def test_generic_inventory_allows_nfc_and_rejects_nfd_targets(self) -> None:
        source = self.fixture.base / "unicode-source"
        source.mkdir()
        payload = b"portable-unicode"
        digest = hashlib.sha256(payload).hexdigest()
        nfc = "caf\u00e9.bin"
        nfd = "cafe\u0301.bin"
        nfc_entry = ASSETS.InventoryEntry(nfc, "file", (1, 2, 0, len(payload), 3, 4), len(payload), digest)
        nfd_entry = ASSETS.InventoryEntry(nfd, "file", (1, 2, 0, len(payload), 3, 4), len(payload), digest)

        nfc_output = self.fixture.base / "unicode-nfc-lock.json"
        with mock.patch.object(ASSETS, "_inventory_snapshot", return_value=(nfc_entry,)):
            value = ASSETS.inventory(
                source,
                nfc_output,
                lock_id="unicode-assets",
                profile="generic",
                repository_commit=self.fixture.head,
                rights_class="private",
            )
        self.assertEqual(value["assets"][0]["target"], nfc)

        nfd_output = self.fixture.base / "unicode-nfd-lock.json"
        with (
            mock.patch.object(ASSETS, "_inventory_snapshot", return_value=(nfd_entry,)),
            self.assertRaisesRegex(ASSETS.AssetError, "safe POSIX-relative"),
        ):
            ASSETS.inventory(
                source,
                nfd_output,
                lock_id="unicode-assets",
                profile="generic",
                repository_commit=self.fixture.head,
                rights_class="private",
            )
        self.assertFalse(nfd_output.exists())

    def test_production_inventory_requires_exact_clean_script_checkout(self) -> None:
        output = self.fixture.base / "production-lock.json"
        with (
            mock.patch.object(ASSETS, "ROOT", self.fixture.root),
            self.assertRaisesRegex(ASSETS.AssetError, "exact clean script checkout"),
        ):
            ASSETS.inventory(
                self.fixture.source,
                output,
                lock_id="production-assets",
                profile="screendance-production",
                repository_commit="a" * 40,
                rights_class="private",
            )
        self.assertFalse(output.exists())

        (self.fixture.root / "README.md").write_text("dirty authority\n", encoding="utf-8")
        with (
            mock.patch.object(ASSETS, "ROOT", self.fixture.root),
            self.assertRaisesRegex(ASSETS.AssetError, "tracked or staged"),
        ):
            ASSETS.inventory(
                self.fixture.source,
                output,
                lock_id="production-assets",
                profile="screendance-production",
                repository_commit=self.fixture.head,
                rights_class="private",
            )
        self.assertFalse(output.exists())

    def test_production_inventory_blocks_git_mutation_during_atomic_handoff(self) -> None:
        output = self.fixture.base / "production-handoff-lock.json"
        relative = "pipeline/raw/IMG_1570.JPG"
        original = (self.fixture.root / "README.md").read_bytes()
        real_stage = ASSETS._stage_json
        attempts: list[tuple[int, int]] = []

        def attempt_commit_during_handoff(*args, **kwargs):
            (self.fixture.root / "README.md").write_text(
                "production handoff mutation\n",
                encoding="utf-8",
            )
            add = subprocess.run(
                ["git", "-C", str(self.fixture.root), "add", "README.md"],
                capture_output=True,
                check=False,
            )
            commit = subprocess.run(
                ["git", "-C", str(self.fixture.root), "commit", "-m", "forbidden inventory mutation"],
                capture_output=True,
                check=False,
            )
            attempts.append((add.returncode, commit.returncode))
            (self.fixture.root / "README.md").write_bytes(original)
            return real_stage(*args, **kwargs)

        with (
            mock.patch.object(ASSETS, "ROOT", self.fixture.root),
            mock.patch.object(ASSETS, "_production_targets", return_value={relative}),
            mock.patch.object(ASSETS, "_validate_production_assets"),
            mock.patch.object(ASSETS, "_stage_json", side_effect=attempt_commit_during_handoff),
        ):
            value = ASSETS.inventory(
                self.fixture.source,
                output,
                lock_id="production-assets",
                profile="screendance-production",
                repository_commit=self.fixture.head,
                rights_class="private",
            )
        self.assertEqual(value["repository_commit"], self.fixture.head)
        self.assertEqual(len(attempts), 1)
        self.assertNotEqual(attempts[0][0], 0)
        self.assertNotEqual(attempts[0][1], 0)
        self.assertTrue(output.exists())

    def test_production_inventory_publishes_only_the_guarded_staged_inode(self) -> None:
        output = self.fixture.base / "production-exact-proof-lock.json"
        relative = "pipeline/raw/IMG_1570.JPG"

        with (
            mock.patch.object(ASSETS, "ROOT", self.fixture.root),
            mock.patch.object(ASSETS, "_production_targets", return_value={relative}),
            mock.patch.object(ASSETS, "_validate_production_assets"),
            mock.patch.object(
                ASSETS,
                "_atomic_json",
                side_effect=AssertionError("production inventory reserialized after staging"),
            ),
        ):
            value = ASSETS.inventory(
                self.fixture.source,
                output,
                lock_id="production-assets",
                profile="screendance-production",
                repository_commit=self.fixture.head,
                rights_class="private",
            )
        self.assertEqual(value["repository_commit"], self.fixture.head)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), value)

    def test_production_inventory_close_failure_cannot_move_foreign_parent_output(self) -> None:
        output_parent = self.fixture.base / "production-output"
        output_parent.mkdir()
        output = output_parent / "lock.json"
        held_parent = self.fixture.base / "production-output-held"
        relative = "pipeline/raw/IMG_1570.JPG"
        foreign = b'{"foreign":true}\n'
        real_close = ASSETS.OperationLease.close
        swapped = False

        def swap_parent_then_report_close_failure(lease):
            nonlocal swapped
            output_parent.rename(held_parent)
            output_parent.mkdir()
            output.write_bytes(foreign)
            swapped = True
            real_close(lease)
            raise ASSETS.AssetError("injected lease close failure")

        with (
            mock.patch.object(ASSETS, "ROOT", self.fixture.root),
            mock.patch.object(ASSETS, "_production_targets", return_value={relative}),
            mock.patch.object(ASSETS, "_validate_production_assets"),
            mock.patch.object(
                ASSETS.OperationLease,
                "close",
                autospec=True,
                side_effect=swap_parent_then_report_close_failure,
            ),
            self.assertRaisesRegex(ASSETS.AssetError, "injected lease close failure"),
        ):
            ASSETS.inventory(
                self.fixture.source,
                output,
                lock_id="production-assets",
                profile="screendance-production",
                repository_commit=self.fixture.head,
                rights_class="private",
            )
        self.assertTrue(swapped)
        self.assertEqual(output.read_bytes(), foreign)
        held_names = {path.name for path in held_parent.iterdir()}
        self.assertNotIn(output.name, held_names)
        self.assertTrue(any(name.startswith(".danse-assets-retired-") for name in held_names))

    def test_production_inventory_success_leaves_only_the_requested_checkout_output(self) -> None:
        assets_directory = self.fixture.root / "assets"
        assets_directory.mkdir()
        output = assets_directory / "lock.v1.json"
        relative = "pipeline/raw/IMG_1570.JPG"

        with (
            mock.patch.object(ASSETS, "ROOT", self.fixture.root),
            mock.patch.object(ASSETS, "_production_targets", return_value={relative}),
            mock.patch.object(ASSETS, "_validate_production_assets"),
        ):
            value = ASSETS.inventory(
                self.fixture.source,
                output,
                lock_id="production-assets",
                profile="screendance-production",
                repository_commit=self.fixture.head,
                rights_class="private",
            )
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), value)
        self.assertEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.fixture.root),
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines(),
            ["?? assets/lock.v1.json"],
        )
        self.assertEqual(list(assets_directory.glob(".receipt-stage-*")), [])
        self.assertEqual(list(assets_directory.glob(".danse-assets-retired-*")), [])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO fixtures require POSIX")
    def test_inventory_rejects_special_files(self) -> None:
        os.mkfifo(self.fixture.source / "named-pipe")
        with self.assertRaisesRegex(ASSETS.AssetError, "non-regular"):
            ASSETS.inventory(
                self.fixture.source,
                self.fixture.base / "generated-lock.json",
                lock_id="generated-assets",
                profile="generic",
                repository_commit=self.fixture.head,
                rights_class="private",
            )

    def test_production_profile_is_exact_and_cannot_be_partial(self) -> None:
        self.assertEqual(len(ASSETS._production_targets()), 487)
        self.fixture.write_lock(profile="screendance-production")
        with self.assertRaisesRegex(ASSETS.AssetError, "487-object"):
            ASSETS.load_lock(self.fixture.lock)

    def test_complete_production_contract_accepts_only_exact_categories(self) -> None:
        origin_sha256, soundfont_sha256, soundfont_url = ASSETS._canonical_production_pins()
        rows = []
        for target in sorted(ASSETS._production_targets()):
            digest = hashlib.sha256(target.encode()).hexdigest()
            rights_class = "private"
            source = {"kind": "file", "path": target}
            if target == "pipeline/.work/raw/IMG_1594.JPG":
                digest = origin_sha256
            if target == ".work/music/MuseScore_General.sf3":
                digest = soundfont_sha256
                rights_class = "restricted"
                source = {"kind": "https", "url": soundfont_url}
                media_type = "audio/x-soundfont"
            elif target.startswith("pipeline/.work/raw/"):
                media_type = "image/jpeg" if target.lower().endswith((".jpg", ".jpeg")) else "image/png"
            elif target.startswith("pipeline/.work/vision/mask/"):
                media_type = "image/png"
            else:
                media_type = "application/json"
            rows.append(
                {
                    "id": ASSETS._opaque_asset_id(digest, target),
                    "target": target,
                    "sha256": digest,
                    "bytes": 1,
                    "media_type": media_type,
                    "rights_class": rights_class,
                    "required": True,
                    "sources": [source],
                }
            )
        value = {
            "schema": ASSETS.LOCK_SCHEMA,
            "lock_id": "complete-production-fixture",
            "profile": "screendance-production",
            "repository_commit": self.fixture.head,
            "assets": rows,
        }
        self.fixture.lock.write_text(json.dumps(value), encoding="utf-8")
        lock = ASSETS.load_lock(self.fixture.lock)
        self.assertEqual(len(lock.assets), 487)
        bound_root = self.fixture.base / "bound-checkout"
        for relative in (
            "corpus/manifest.json",
            "music/audio-toolchain.json",
            "submission/screendance-2027.yaml",
        ):
            destination = bound_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((ROOT / relative).read_bytes())
        bound_manifest = json.loads(
            (bound_root / "corpus/manifest.json").read_text(encoding="utf-8")
        )
        bound_manifest["frames"][0]["source"] = "BOUND_ONLY.JPG"
        (bound_root / "corpus/manifest.json").write_text(
            json.dumps(bound_manifest),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ASSETS.AssetError, "487-object"):
            ASSETS.load_lock(self.fixture.lock, repository_root=bound_root)
        extra = self.fixture.root / "pipeline/.work/raw/EXTRA.JPG"
        extra.parent.mkdir(parents=True)
        extra.write_bytes(b"stale ignored input")
        with self.assertRaisesRegex(ASSETS.AssetError, "undeclared file"):
            ASSETS._assert_locked_tree(self.fixture.root, lock)

        cases = {
            "target": lambda row: row.update(target="pipeline/.work/raw/EXTRA.JPG"),
            "required": lambda row: row.update(required=False),
            "rights": lambda row: row.update(rights_class="public"),
            "media": lambda row: row.update(media_type="application/octet-stream"),
            "source": lambda row: row.update(sources=[{"kind": "https", "url": "https://example.com/raw"}]),
            "personal file source": lambda row: row.update(
                sources=[{"kind": "file", "path": "Users/alice/archive/IMG_1594.JPG"}]
            ),
            "unauthenticated release": lambda row: row.update(
                sources=[
                    {
                        "kind": "github-release",
                        "repository": "organvm/private-assets",
                        "tag": "v1",
                        "asset": "payload.bin",
                    }
                ]
            ),
            "bytes": lambda row: row.update(bytes=0),
            "opaque id": lambda row: row.update(
                id="asset-0000000000000000-000000000000"
            ),
            "canonical pin": lambda row: row.update(sha256="0" * 64),
        }
        origin_index = next(
            index
            for index, row in enumerate(rows)
            if row["target"] == "pipeline/.work/raw/IMG_1594.JPG"
        )
        for label, mutate in cases.items():
            broken = copy.deepcopy(value)
            mutate(broken["assets"][origin_index])
            self.fixture.lock.write_text(json.dumps(broken), encoding="utf-8")
            with self.subTest(label=label), self.assertRaises(ASSETS.AssetError):
                ASSETS.load_lock(self.fixture.lock)

    def test_github_release_resolution_uses_exact_named_asset(self) -> None:
        metadata = json.dumps(
            {"assets": [{"name": "payload.bin", "url": "https://api.github.com/assets/1"}]}
        ).encode()
        response = mock.MagicMock()
        response.__enter__.return_value.read.side_effect = [metadata, b""]
        response.__exit__.return_value = False
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(ASSETS, "_open_url", return_value=response),
        ):
            url, headers = ASSETS._github_release_asset(
                {
                    "kind": "github-release",
                    "repository": "organvm/private-assets",
                    "tag": "v1",
                    "asset": "payload.bin",
                },
                1.0,
            )
        self.assertEqual(url, "https://api.github.com/assets/1")
        self.assertEqual(headers["Accept"], "application/octet-stream")

    def test_private_release_credentials_fail_closed_and_stay_redacted(self) -> None:
        source = {
            "kind": "github-release",
            "repository": "organvm/private-assets",
            "tag": "v1",
            "asset": "payload.bin",
            "token_env": "DANSE_PRIVATE_TOKEN",
        }
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(ASSETS, "_open_url") as open_url,
            self.assertRaisesRegex(ASSETS.AssetError, "credential is unavailable"),
        ):
            ASSETS._github_release_asset(source, 1.0)
        open_url.assert_not_called()

        secret = "secret\r\nX-Leak: exposed"
        with (
            mock.patch.dict(os.environ, {"DANSE_PRIVATE_TOKEN": secret}, clear=True),
            self.assertRaises(ASSETS.AssetError) as caught,
        ):
            ASSETS._github_release_asset(source, 1.0)
        self.assertNotIn(secret, str(caught.exception))

        public_metadata = mock.MagicMock()
        public_metadata.__enter__.return_value.read.side_effect = [
            json.dumps({"private": False}).encode(),
            b"",
        ]
        public_metadata.__exit__.return_value = False
        with (
            mock.patch.dict(
                os.environ,
                {"DANSE_PRIVATE_TOKEN": "printable-token"},
                clear=True,
            ),
            mock.patch.object(
                ASSETS,
                "_open_url",
                return_value=public_metadata,
            ) as open_url,
            self.assertRaisesRegex(ASSETS.AssetError, "private GitHub repository"),
        ):
            ASSETS._github_release_asset(
                source,
                1.0,
                require_private_repository=True,
            )
        open_url.assert_called_once()

    def test_authenticated_public_release_does_not_claim_private_custody(self) -> None:
        source = {
            "kind": "github-release",
            "repository": "organvm/public-assets",
            "tag": "v1",
            "asset": "payload.bin",
            "token_env": "DANSE_PUBLIC_TOKEN",
        }
        metadata = json.dumps(
            {"assets": [{"name": "payload.bin", "url": "https://api.github.com/assets/1"}]}
        ).encode()
        response = mock.MagicMock()
        response.__enter__.return_value.read.side_effect = [metadata, b""]
        response.__exit__.return_value = False
        with (
            mock.patch.dict(
                os.environ,
                {"DANSE_PUBLIC_TOKEN": "printable-token"},
                clear=True,
            ),
            mock.patch.object(ASSETS, "_open_url", return_value=response) as open_url,
        ):
            url, headers = ASSETS._github_release_asset(source, 1.0)
        self.assertEqual(url, "https://api.github.com/assets/1")
        self.assertEqual(headers["Authorization"], "Bearer printable-token")
        open_url.assert_called_once()

    def test_header_construction_errors_are_redacted_asset_errors(self) -> None:
        leaked = "do-not-echo-this-header"
        with (
            mock.patch.object(ASSETS, "_assert_public_https_host"),
            mock.patch.object(
                ASSETS.urllib.request,
                "Request",
                side_effect=ValueError(leaked),
            ),
            self.assertRaises(ASSETS.AssetError) as caught,
        ):
            ASSETS._open_url(
                "https://example.com/payload",
                {"Authorization": "Bearer safe-token"},
                1.0,
            )
        self.assertNotIn(leaked, str(caught.exception))
        with self.assertRaisesRegex(ASSETS.AssetError, "invalid header value"):
            ASSETS._validate_http_headers({"X-Danse": "unsafe\tvalue"})

    def test_github_release_redirect_allows_only_signed_asset_hosts(self) -> None:
        request = urllib.request.Request(
            "https://api.github.com/repos/organvm/private/releases/assets/1",
            headers={"Authorization": "Bearer secret"},
        )
        signed = "https://release-assets.githubusercontent.com/object?sig=temporary"
        address = [(2, 1, 6, "", ("140.82.112.1", 443))]
        with mock.patch.object(ASSETS.socket, "getaddrinfo", return_value=address):
            redirected = ASSETS._SafeRedirect(
                allow_github_asset_redirect=True
            ).redirect_request(request, None, 302, "Found", {}, signed)
            self.assertEqual(redirected.full_url, signed)
            self.assertIsNone(redirected.get_header("Authorization"))
            with self.assertRaises(ASSETS.AssetError):
                ASSETS._SafeRedirect().redirect_request(
                    request,
                    None,
                    302,
                    "Found",
                    {},
                    signed,
                )

    def test_metal_workflow_tracks_every_browser_contract_input(self) -> None:
        workflow = (ROOT / ".github/workflows/macos-metal-contract.yml").read_text(
            encoding="utf-8"
        )
        for path in (
            "corpus/**",
            "music/**",
            "interaction-test.html",
            "index.html",
            "verify.html",
            "probe.html",
            "arrival.js",
            "styles.css",
        ):
            with self.subTest(path=path):
                self.assertEqual(workflow.count(f'      - "{path}"'), 2)
        self.assertIn("args=(--check --verify --arrival --probe --interaction)", workflow)
        self.assertIn("grep -q -- '--controls'", workflow)
        self.assertIn("args+=(--controls)", workflow)
        self.assertIn('python render/browser.py "${args[@]}"', workflow)
        self.assertIn("run: python scripts/tests/assets.test.py", workflow)

    @unittest.skipIf(jsonschema is None, "jsonschema is not installed")
    def test_lock_and_redacted_receipt_match_tracked_schemas(self) -> None:
        lock_value = json.loads(self.fixture.lock.read_text(encoding="utf-8"))
        lock_schema = json.loads((ROOT / "assets/lock.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(lock_schema).validate(lock_value)
        for url in (
            "HTTPS://example.com/private",
            "https://user@example.com/private",
            "https://example.com/private?signature=secret",
            "https://example.com/private?",
            "https://example.com/private#fragment",
            "https://example.com/private#",
            "https://example.com:/private",
            "https://example.com:444/private",
            "https://example.com/raw space",
            "https://example.com/raw\tcontrol",
            "https://example.com/raw-é",
            "https://example.com/back\\slash",
            "https://example.com/bad%escape",
            "https://-bad.example/private",
            "https://example..com/private",
            "https://localhost/private",
            "https://LOCALHOST/private",
            "https://127.0.0.1/private",
            "https://999.999/private",
            "https://host.local/private",
            "https://[::1]/private",
        ):
            unsafe = copy.deepcopy(lock_value)
            unsafe["assets"][0]["sources"] = [{"kind": "https", "url": url}]
            with self.subTest(schema_url=url), self.assertRaises(jsonschema.ValidationError):
                jsonschema.Draft202012Validator(lock_schema).validate(unsafe)
            with self.subTest(runtime_url=url), self.assertRaises(ASSETS.AssetError):
                ASSETS._https_url(url)
        unsafe_path = copy.deepcopy(lock_value)
        unsafe_path["assets"][0]["target"] = ".git/refs/heads/main"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(lock_schema).validate(unsafe_path)
        for target in (".GIT/refs/heads/main", ".Asset-Cache/sha256/object"):
            unsafe_path = copy.deepcopy(lock_value)
            unsafe_path["assets"][0]["target"] = target
            with self.subTest(schema_target=target), self.assertRaises(
                jsonschema.ValidationError
            ):
                jsonschema.Draft202012Validator(lock_schema).validate(unsafe_path)
        for target in ("a/./b", "./a", "a/.", "a//b", "a/", ".", ".."):
            unsafe_path = copy.deepcopy(lock_value)
            unsafe_path["assets"][0]["target"] = target
            with self.subTest(schema_noncanonical_path=target), self.assertRaises(
                jsonschema.ValidationError
            ):
                jsonschema.Draft202012Validator(lock_schema).validate(unsafe_path)
            with self.subTest(runtime_noncanonical_path=target), self.assertRaisesRegex(
                ASSETS.AssetError,
                "safe POSIX-relative",
            ):
                ASSETS._safe_relative(target, "test path")
        for field in ("target", "file source"):
            nfc_path = copy.deepcopy(lock_value)
            if field == "target":
                nfc_path["assets"][0]["target"] = "caf\u00e9/asset.bin"
            else:
                nfc_path["assets"][0]["sources"] = [
                    {"kind": "file", "path": "caf\u00e9/asset.bin"}
                ]
            jsonschema.Draft202012Validator(lock_schema).validate(nfc_path)

            japanese_path = copy.deepcopy(lock_value)
            if field == "target":
                japanese_path["assets"][0]["target"] = "日本/素材.bin"
            else:
                japanese_path["assets"][0]["sources"] = [
                    {"kind": "file", "path": "日本/素材.bin"}
                ]
            jsonschema.Draft202012Validator(lock_schema).validate(japanese_path)

            nfd_path = copy.deepcopy(lock_value)
            if field == "target":
                nfd_path["assets"][0]["target"] = "cafe\u0301/asset.bin"
            else:
                nfd_path["assets"][0]["sources"] = [
                    {"kind": "file", "path": "cafe\u0301/asset.bin"}
                ]
            jsonschema.Draft202012Validator(lock_schema).validate(nfd_path)

            overlay_path = copy.deepcopy(lock_value)
            if field == "target":
                overlay_path["assets"][0]["target"] = "a\u0338/asset.bin"
            else:
                overlay_path["assets"][0]["sources"] = [
                    {"kind": "file", "path": "a\u0338/asset.bin"}
                ]
            jsonschema.Draft202012Validator(lock_schema).validate(overlay_path)

            hangul_nfd = copy.deepcopy(lock_value)
            if field == "target":
                hangul_nfd["assets"][0]["target"] = "\u1100\u1161/asset.bin"
            else:
                hangul_nfd["assets"][0]["sources"] = [
                    {"kind": "file", "path": "\u1100\u1161/asset.bin"}
                ]
            jsonschema.Draft202012Validator(lock_schema).validate(hangul_nfd)
        self.assertEqual(ASSETS._safe_relative("a\u0338/asset.bin", "test path"), "a\u0338/asset.bin")
        for non_nfc in ("cafe\u0301/asset.bin", "\u1100\u1161/asset.bin"):
            with self.subTest(runtime_non_nfc=non_nfc), self.assertRaisesRegex(
                ASSETS.AssetError,
                "safe POSIX-relative",
            ):
                ASSETS._safe_relative(non_nfc, "test path")
        for field in ("target", "file source"):
            unsafe_path = copy.deepcopy(lock_value)
            if field == "target":
                unsafe_path["assets"][0]["target"] = "foo\x00bar"
            else:
                unsafe_path["assets"][0]["sources"] = [
                    {"kind": "file", "path": "foo\x00bar"}
                ]
            with self.subTest(schema_path_field=field), self.assertRaises(
                jsonschema.ValidationError
            ):
                jsonschema.Draft202012Validator(lock_schema).validate(unsafe_path)
        for field in ("target", "file source", "github tag", "github asset"):
            unsafe = copy.deepcopy(lock_value)
            if field == "target":
                unsafe["assets"][0]["target"] = "bad-\udcff.bin"
            elif field == "file source":
                unsafe["assets"][0]["sources"] = [
                    {"kind": "file", "path": "bad-\udcff.bin"}
                ]
            else:
                unsafe["assets"][0]["sources"] = [
                    {
                        "kind": "github-release",
                        "repository": "organvm/example",
                        "tag": "bad-\udcff" if field == "github tag" else "v1",
                        "asset": "bad-\udcff.bin" if field == "github asset" else "asset.bin",
                    }
                ]
            with self.subTest(schema_surrogate_field=field), self.assertRaises(
                jsonschema.ValidationError
            ):
                jsonschema.Draft202012Validator(lock_schema).validate(unsafe)
        self.assertEqual(
            ASSETS.main(
                [
                    "pull",
                    "--lock",
                    str(self.fixture.lock),
                    "--root",
                    str(self.fixture.root),
                    "--allow-file",
                    "--file-source-root",
                    str(self.fixture.source),
                    "--receipt",
                    str(self.fixture.receipt),
                ]
            ),
            0,
        )
        receipt = json.loads(self.fixture.receipt.read_text(encoding="utf-8"))
        receipt_schema = json.loads(
            (ROOT / "assets/hydration-receipt.schema.json").read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator(
            receipt_schema,
            format_checker=jsonschema.FormatChecker(),
        ).validate(receipt)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def stat_mode_descriptor(descriptor: int) -> str:
    return "directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"


def descriptor_matches_path(descriptor: int, path: Path) -> bool:
    try:
        opened = os.fstat(descriptor)
        current = path.stat()
    except OSError:
        return False
    return (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)


if __name__ == "__main__":
    unittest.main()
