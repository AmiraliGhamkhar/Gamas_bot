"""Regression tests for the installed ``gamas-bot`` console entry point."""

from __future__ import annotations

import inspect
import unittest
from unittest import mock

from gamas_bot.__main__ import main


class ConsoleEntryPointTests(unittest.TestCase):
    def test_main_is_a_synchronous_no_argument_entry_point(self):
        """Setuptools invokes a console script as ``sys.exit(main())``."""
        self.assertFalse(inspect.iscoroutinefunction(main))
        with mock.patch("gamas_bot.__main__.run") as run:
            self.assertIsNone(main())
        run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
