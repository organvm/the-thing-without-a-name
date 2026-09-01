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
        real_atomic_json = ASSETS._atomic_json

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

        def record_receipt(*args, **kwargs):
            events.append("receipt")
            return real_atomic_json(*args, **kwargs)

        with (
            mock.patch.object(ASSETS.os, "fchmod", side_effect=record_mode),
            mock.patch.object(ASSETS.os, "fsync", side_effect=record_sync),
            mock.patch.object(ASSETS, "_atomic_json", side_effect=record_receipt),
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
        real_atomic_json = ASSETS._atomic_json

        def record_sync(descriptor):
            if descriptor_matches_path(descriptor, cache):
                events.append("asset-file")
            elif descriptor_matches_path(descriptor, cache.parent):
                events.append("cache-directory")
            elif descriptor_matches_path(descriptor, target.parent):
                events.append("target-directory")
            return real_fsync(descriptor)

        def record_receipt(*args, **kwargs):
            events.append("receipt")
            return real_atomic_json(*args, **kwargs)

        with (
            mock.patch.object(ASSETS.os, "fsync", side_effect=record_sync),
            mock.patch.object(ASSETS, "_atomic_json", side_effect=record_receipt),
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
        real_fsync = os.fsync
        rejected = False

        def reject_verified_file_once(descriptor):
            nonlocal rejected
            if not rejected and stat.S_ISREG(os.fstat(descriptor).st_mode):
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

    def test_ancestor_directory_sync_failure_cleans_creation_and_blocks_pull(self) -> None:
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
        self.assertFalse((self.fixture.root / ".asset-cache").exists())
        self.assertFalse((self.fixture.root / "pipeline").exists())
        self.assertFalse(json.loads(self.fixture.receipt.read_text(encoding="utf-8"))["ok"])

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
        self.assertFalse(cache.exists())
        self.assertFalse(target.exists())
        self.assertFalse(json.loads(self.fixture.receipt.read_text(encoding="utf-8"))["ok"])

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
        self.assertFalse(target.exists())
        self.assertFalse(json.loads(self.fixture.receipt.read_text(encoding="utf-8"))["ok"])

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
        ), self.assertRaisesRegex(ASSETS.AssetError, "parent changed"):
            ASSETS._publish_no_overwrite(self.fixture.root, asset.target, asset)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list(held_parent.iterdir()), [])

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
            self.assertRaisesRegex(ASSETS.AssetError, "parent changed during publication"),
        ):
            ASSETS._publish_no_overwrite(self.fixture.root, asset.target, asset)
        self.assertFalse(target.exists())

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
        ), self.assertRaisesRegex(ASSETS.AssetError, "parent changed"):
            ASSETS._publish_no_overwrite(self.fixture.root, asset.target, asset)
        self.assertTrue(swapped)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list(held_parent.iterdir()), [])

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
        ), self.assertRaisesRegex(ASSETS.AssetError, "parent changed"):
            ASSETS._cache_from_sources(
                self.fixture.root,
                asset,
                allow_file=True,
                source_root=self.fixture.source,
                timeout=1.0,
            )
        self.assertTrue(swapped)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list(held_parent.iterdir()), [])

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
        real_link = os.link

        def publish_then_swap(source, destination, **kwargs):
            result = real_link(source, destination, **kwargs)
            receipt_parent.rename(held_parent)
            receipt_parent.symlink_to(checkout_destination, target_is_directory=True)
            return result

        with (
            mock.patch.object(ASSETS.os, "link", side_effect=publish_then_swap),
            self.assertRaisesRegex(ASSETS.AssetError, "parent changed during publication"),
        ):
            ASSETS._atomic_json(
                receipt,
                {"ok": True},
                no_overwrite=True,
                forbidden_root=self.fixture.root,
            )
        self.assertFalse((checkout_destination / receipt.name).exists())
        self.assertEqual(list(held_parent.iterdir()), [])

    def test_atomic_json_fsyncs_parent_after_temporary_cleanup(self) -> None:
        output = self.fixture.base / "durable-output.json"
        real_fsync = os.fsync
        synced_directories = []

        def record_sync(descriptor):
            synced_directories.append(stat_mode_descriptor(descriptor))
            return real_fsync(descriptor)

        with mock.patch.object(ASSETS.os, "fsync", side_effect=record_sync):
            ASSETS._atomic_json(output, {"durable": True}, no_overwrite=True)
        self.assertEqual(synced_directories, ["file", "directory"])
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
        self.assertFalse(blocked.exists())

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
            self.assertRaisesRegex(ASSETS.AssetError, "durably synchronized"),
        ):
            ASSETS._atomic_json(output, {"durable": False}, no_overwrite=True)
        self.assertTrue(rejected)
        self.assertFalse((self.fixture.base / "blocked-parent").exists())
        self.assertFalse(output.exists())

    def test_receipt_parent_swap_during_directory_sync_removes_published_link(self) -> None:
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
            self.assertRaisesRegex(ASSETS.AssetError, "durable publication"),
        ):
            ASSETS._atomic_json(
                receipt,
                {"ok": True},
                no_overwrite=True,
                forbidden_root=self.fixture.root,
            )
        self.assertTrue(swapped)
        self.assertFalse((checkout_destination / receipt.name).exists())
        self.assertEqual(list(held_parent.iterdir()), [])

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
        with self.assertRaisesRegex(ASSETS.AssetError, "already exists"):
            ASSETS.inventory(
                self.fixture.source,
                output,
                lock_id="generated-assets",
                profile="generic",
                repository_commit=self.fixture.head,
                rights_class="private",
            )

    def test_inventory_rejects_content_mutation_before_publication(self) -> None:
        output = self.fixture.base / "mutable-lock.json"
        source = self.fixture.source / "pipeline/raw/IMG_1570.JPG"
        real_snapshot = ASSETS._inventory_snapshot
        calls = 0

        def mutate_after_first_snapshot(root):
            nonlocal calls
            proof = real_snapshot(root)
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

        def add_file_after_first_snapshot(root):
            nonlocal calls
            proof = real_snapshot(root)
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
        self.assertEqual(calls, 2)
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
            "https://user@example.com/private",
            "https://example.com/private?signature=secret",
            "https://example.com/private#fragment",
            "https://example.com:444/private",
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
            with self.subTest(field=field), self.assertRaises(jsonschema.ValidationError):
                jsonschema.Draft202012Validator(lock_schema).validate(nfd_path)
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
