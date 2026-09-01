"""Portable adversarial tests for the production asset parity CLI."""

from __future__ import annotations

import copy
import hashlib
import http.client
import importlib.util
import io
import json
import os
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
            "https://localhost/a",
            "https://127.0.0.1/a",
            "https://host.local/a",
            "https://[::1]/a",
        ):
            with self.subTest(url=url), self.assertRaises(ASSETS.AssetError):
                ASSETS._validate_source({"kind": "https", "url": url})
        for path in (".", "./", "../outside", "/absolute", ".git/refs/heads/main", r"windows\outside"):
            with self.subTest(path=path), self.assertRaises(ASSETS.AssetError):
                ASSETS._safe_relative(path, "test path")

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

        with mock.patch.object(ASSETS.os, "link", side_effect=swap_parent_then_link):
            ASSETS._publish_no_overwrite(self.fixture.root, asset.target, asset)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual((held_parent / "IMG_1570.JPG").read_bytes(), self.fixture.payload)

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
            ASSETS._github_release_asset(source, 1.0)
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
            "index.html",
            "verify.html",
            "probe.html",
            "arrival.js",
            "styles.css",
        ):
            with self.subTest(path=path):
                self.assertEqual(workflow.count(f'      - "{path}"'), 2)

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
            "https://localhost/private",
            "https://127.0.0.1/private",
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


if __name__ == "__main__":
    unittest.main()
