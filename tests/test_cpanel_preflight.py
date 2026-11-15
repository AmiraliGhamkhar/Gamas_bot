"""Regression coverage for cPanel preflight capacity checks."""

from __future__ import annotations

import unittest
from unittest import mock

from scripts import cpanel_preflight
from scripts.cpanel_preflight import Result, check_temp_download_capacity
from tests.support import make_settings


MIB = 1024**2
GIB = 1024**3


class TempDownloadCapacityTests(unittest.TestCase):
    def test_one_gib_free_rejects_two_default_sized_downloads(self) -> None:
        """F-002: the preflight must fail before this profile can start."""
        settings = make_settings(
            max_file_size=GIB,
            doc_max_bytes=GIB,
            max_concurrent_downloads=2,
            temp_min_free_bytes=64 * MIB,
        )

        result = check_temp_download_capacity(settings, free_bytes=GIB)

        self.assertEqual(result.level, "FAIL")
        self.assertEqual(result.name, "Temp download capacity")
        self.assertIn("4.12 GiB", result.detail)
        self.assertIn("two working copies", result.detail)

    def test_capacity_accounts_for_the_larger_document_ceiling(self) -> None:
        settings = make_settings(
            max_file_size=64 * MIB,
            doc_max_bytes=256 * MIB,
            max_concurrent_downloads=1,
            temp_min_free_bytes=64 * MIB,
        )

        result = check_temp_download_capacity(settings, free_bytes=575 * MIB)

        self.assertEqual(result.level, "FAIL")
        self.assertIn("256 MiB maximum input", result.detail)

    def test_exact_concurrent_requirement_is_accepted(self) -> None:
        settings = make_settings(
            max_file_size=128 * MIB,
            doc_max_bytes=128 * MIB,
            max_concurrent_downloads=2,
            temp_min_free_bytes=64 * MIB,
        )
        required = 2 * (2 * 128 * MIB + 64 * MIB)

        result = check_temp_download_capacity(settings, free_bytes=required)

        self.assertEqual(result.level, "PASS")
        self.assertIn("0.62 GiB", result.detail)

    def test_run_all_includes_the_capacity_failure(self) -> None:
        settings = make_settings()
        capacity_failure = Result("FAIL", "Temp download capacity", "insufficient space")
        with (
            mock.patch.object(cpanel_preflight, "check_python", return_value=Result("PASS", "Python")),
            mock.patch.object(cpanel_preflight, "check_platform", return_value=Result("PASS", "Platform")),
            mock.patch.object(cpanel_preflight, "check_location", return_value=Result("PASS", "Location")),
            mock.patch.object(cpanel_preflight, "check_packages", return_value=[]),
            mock.patch.object(cpanel_preflight, "check_settings", return_value=([], settings)),
            mock.patch.object(cpanel_preflight, "check_directory", return_value=Result("PASS", "Directory")),
            mock.patch.object(
                cpanel_preflight,
                "check_temp_download_capacity",
                return_value=capacity_failure,
            ),
            mock.patch.object(
                cpanel_preflight,
                "check_private_receipt_directory",
                return_value=Result("PASS", "Receipts"),
            ),
            mock.patch.object(cpanel_preflight, "check_lock_support", return_value=Result("PASS", "Lock")),
            mock.patch.object(cpanel_preflight, "check_media_worker", return_value=Result("PASS", "Worker")),
        ):
            results = cpanel_preflight.run_all(network=False)

        self.assertIn(capacity_failure, results)


if __name__ == "__main__":
    unittest.main()
