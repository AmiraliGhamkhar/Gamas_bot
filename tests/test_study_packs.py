"""Tests for Study Pack data model, private file storage, manifest, and backfill."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gamas_bot.db import Database
from gamas_bot.study_storage import read_private_asset, resolve_private_asset, save_private_asset


class StudyPackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.assets_dir = root / "private_assets"
        self.db = Database(root / "packs.db")
        await self.db.connect()
        await self.db.ensure_user(101, "Alice")
        await self.db.ensure_user(202, "Bob")

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def test_create_pack_add_assets_and_manifest(self) -> None:
        p1 = await self.db.study_packs.study_pack_create(101, "فصل اول", source_type="audio")
        p2 = await self.db.study_packs.study_pack_create(101, "فصل اول", source_type="audio")
        self.assertNotEqual(p1, p2)

        rel, size, sha = save_private_asset(
            self.assets_dir, 101, p1, "lecture.mp3", b"audio-bytes"
        )
        a1 = await self.db.study_packs.study_asset_add(
            p1, 101, kind="original_source", title="فایل اصلی",
            storage_path=rel, mime_type="audio/mpeg", byte_size=size, sha256=sha,
        )
        self.assertGreater(a1, 0)

        manifest = await self.db.study_packs.study_pack_manifest(101, p1)
        assert manifest is not None
        self.assertEqual(manifest["pack_id"], p1)
        self.assertEqual(manifest["processing_status"], "ready")
        self.assertEqual(len(manifest["assets"]), 1)
        self.assertNotIn("storage_path", manifest["assets"][0])

    async def test_duplicate_asset_handling(self) -> None:
        p1 = await self.db.study_packs.study_pack_create(101, "جزوه")
        rel, size, sha = save_private_asset(
            self.assets_dir, 101, p1, "doc.pdf", b"pdf-content"
        )
        a1 = await self.db.study_packs.study_asset_add(
            p1, 101, kind="pdf", title="PDF",
            storage_path=rel, mime_type="application/pdf", byte_size=size, sha256=sha,
        )
        a2 = await self.db.study_packs.study_asset_add(
            p1, 101, kind="pdf", title="PDF",
            storage_path=rel, mime_type="application/pdf", byte_size=size, sha256=sha,
        )
        self.assertEqual(a1, a2)
        self.assertEqual(len(await self.db.study_packs.study_pack_assets(101, p1)), 1)

    async def test_file_retrieval_and_missing_asset_recovery(self) -> None:
        p1 = await self.db.study_packs.study_pack_create(101, "متن")
        rel, size, sha = save_private_asset(
            self.assets_dir, 101, p1, "notes.txt", b"hello world"
        )
        self.assertEqual(read_private_asset(self.assets_dir, rel), b"hello world")

        # Simulate missing file on disk
        resolve_private_asset(self.assets_dir, rel).unlink()
        self.assertIsNone(read_private_asset(self.assets_dir, rel))

    async def test_unauthorized_access_and_invalid_paths(self) -> None:
        p1 = await self.db.study_packs.study_pack_create(101, "محرمانه")
        self.assertIsNone(await self.db.study_packs.study_pack_get(202, p1))
        self.assertFalse(await self.db.study_packs.study_pack_delete(202, p1))
        self.assertIsNone(await self.db.study_packs.study_pack_manifest(202, p1))

        with self.assertRaises(ValueError):
            resolve_private_asset(self.assets_dir, "../../etc/passwd")
        with self.assertRaises(ValueError):
            read_private_asset(self.assets_dir, "../secret.txt")

    async def test_partial_processing_failure_status(self) -> None:
        p1 = await self.db.study_packs.study_pack_create(101, "ویدیو", status="processing")
        ok = await self.db.study_packs.study_pack_update_status(101, p1, "partial")
        self.assertTrue(ok)
        pack = await self.db.study_packs.study_pack_get(101, p1)
        assert pack is not None
        self.assertEqual(pack["status"], "partial")

    async def test_migration_backfill_dry_run_and_live(self) -> None:
        await self.db.library_add(101, "booklet", 1, 2, title="جزوه قدیمی", delivered=True)
        dry = await self.db.study_packs.backfill_from_library(dry_run=True)
        self.assertEqual(dry["scanned"], 1)
        self.assertEqual(dry["created"], 0)
        self.assertEqual(len(await self.db.study_packs.study_pack_list(101)), 0)

        live = await self.db.study_packs.backfill_from_library(dry_run=False)
        self.assertEqual(live["scanned"], 1)
        self.assertEqual(live["created"], 1)
        packs = await self.db.study_packs.study_pack_list(101)
        self.assertEqual(len(packs), 1)
        self.assertEqual(packs[0]["title"], "جزوه قدیمی")
