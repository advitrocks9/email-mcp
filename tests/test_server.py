from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import server


class UndoTests(unittest.TestCase):
    def test_action_cannot_be_undone_twice(self) -> None:
        entries = [
            {
                "provider": "gmail",
                "op": "mark_read",
                "id": "message-1",
                "action_id": "action-1",
                "ts": "2026-07-13T10:00:00.000000",
            }
        ]
        backend = MagicMock()
        backend.undo.return_value = True

        def record(entry: dict[str, object]) -> None:
            entries.append(entry)

        with (
            patch.object(server, "read_audit", side_effect=lambda: list(entries)),
            patch.object(server, "audit", side_effect=record),
            patch.dict(server.PROVIDERS, {"gmail": backend}, clear=True),
        ):
            first = server.undo_last()
            second = server.undo_last()

        self.assertEqual(len(first["undone"]), 1)
        self.assertEqual(second["undone"], [])
        backend.undo.assert_called_once()


if __name__ == "__main__":
    unittest.main()
