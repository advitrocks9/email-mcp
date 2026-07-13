from __future__ import annotations

import base64
import json
import time
import unittest
from unittest.mock import patch

import outlook


class OutlookBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        outlook._cache = {"token": None, "exp": 0.0}

    def test_token_cache_uses_jwt_expiry(self) -> None:
        payload = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 900}).encode()).decode().rstrip("=")
        token = f"header.{payload}.signature"
        with patch.object(outlook, "extract_token", return_value=token) as extract:
            self.assertEqual(outlook.get_token(), token)
            self.assertEqual(outlook.get_token(), token)
        extract.assert_called_once()

    def test_list_messages_keeps_outlook_rest_contract(self) -> None:
        response = {
            "value": [
                {
                    "Id": "message-1",
                    "From": {"EmailAddress": {"Name": "Imperial", "Address": "helpdesk@imperial.ac.uk"}},
                    "Subject": "Welcome",
                    "ReceivedDateTime": "2026-07-13T10:00:00Z",
                    "IsRead": False,
                    "BodyPreview": "Welcome to Imperial",
                }
            ],
            "@odata.count": 1,
        }
        with patch.object(outlook, "_auth", return_value={}), patch.object(outlook, "http", return_value=response):
            page = outlook.list_messages_page(folder="inbox", limit=10)
        self.assertEqual(page["total_count"], 1)
        self.assertEqual(page["messages"][0]["provider"], "outlook")
        self.assertTrue(page["messages"][0]["unread"])

    def test_create_draft_keeps_outlook_rest_contract(self) -> None:
        with (
            patch.object(outlook, "_auth", return_value={}),
            patch.object(outlook, "http", return_value={"Id": "draft-1"}) as request,
            patch.object(outlook, "audit"),
        ):
            result = outlook.create_draft("to@example.com", "Subject", "Body", cc="cc@example.com")
        self.assertEqual(result, {"ok": True, "draft_id": "draft-1"})
        payload = request.call_args.args[3]
        self.assertEqual(payload["ToRecipients"][0]["EmailAddress"]["Address"], "to@example.com")
        self.assertEqual(payload["CcRecipients"][0]["EmailAddress"]["Address"], "cc@example.com")


if __name__ == "__main__":
    unittest.main()
