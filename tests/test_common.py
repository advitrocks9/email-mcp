from __future__ import annotations

import os
import pathlib
import stat
import tempfile
import unittest
from unittest.mock import patch

import common


class AuditTests(unittest.TestCase):
    def test_audit_is_private_and_survives_malformed_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = pathlib.Path(directory) / "actions.log"
            with patch.object(common, "AUDIT_LOG", log):
                common.audit({"provider": "gmail", "op": "mark_read", "id": "message-1"})
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("truncated json\n")
                entries = common.read_audit()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["provider"], "gmail")
            self.assertTrue(entries[0]["action_id"])
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
