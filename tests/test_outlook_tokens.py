from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import outlook_tokens


class OutlookTokenTests(unittest.TestCase):
    def test_provider_defaults_by_platform(self) -> None:
        with (
            patch.object(outlook_tokens, "load_env", return_value={}),
            patch.object(outlook_tokens.platform, "system", return_value="Darwin"),
        ):
            self.assertEqual(outlook_tokens.provider_name(), "safari")
        with (
            patch.object(outlook_tokens, "load_env", return_value={}),
            patch.object(outlook_tokens.platform, "system", return_value="Windows"),
        ):
            self.assertEqual(outlook_tokens.provider_name(), "edge")

    def test_provider_override_is_validated(self) -> None:
        with patch.object(outlook_tokens, "load_env", return_value={"OUTLOOK_TOKEN_PROVIDER": "invalid"}):
            with self.assertRaisesRegex(RuntimeError, "auto, safari, or edge"):
                outlook_tokens.provider_name()

    def test_edge_executable_accepts_configured_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = pathlib.Path(directory) / "msedge.exe"
            executable.touch()
            with (
                patch.object(outlook_tokens, "load_env", return_value={"OUTLOOK_EDGE_PATH": str(executable)}),
                patch.object(outlook_tokens.shutil, "which", return_value=None),
            ):
                self.assertEqual(outlook_tokens._edge_executable(), executable)

    def test_active_port_reads_edge_discovery_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = pathlib.Path(directory)
            (profile / "DevToolsActivePort").write_text("43123\n/devtools/browser/example\n", encoding="utf-8")
            self.assertEqual(outlook_tokens._active_port(profile), (43123, "/devtools/browser/example"))

    def test_edge_launch_uses_dedicated_loopback_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = pathlib.Path(directory)
            with (
                patch.object(outlook_tokens, "_edge_executable", return_value=pathlib.Path("msedge.exe")),
                patch.object(outlook_tokens, "_active_port", return_value=(43123, "/devtools/browser/example")),
                patch.object(outlook_tokens, "_targets", return_value=[{"type": "page"}]),
                patch.object(outlook_tokens.subprocess, "Popen") as launch,
            ):
                self.assertEqual(
                    outlook_tokens._launch_edge(profile),
                    (43123, "/devtools/browser/example"),
                )
            command = launch.call_args.args[0]
            self.assertIn("--remote-debugging-port=0", command)
            self.assertIn("--remote-debugging-address=127.0.0.1", command)
            self.assertIn(f"--user-data-dir={profile}", command)

    def test_cdp_call_returns_matching_response(self) -> None:
        connection = MagicMock()
        connection.recv.side_effect = [
            json.dumps({"method": "Runtime.consoleAPICalled"}),
            json.dumps({"id": 1, "result": {"result": {"value": "token"}}}),
        ]
        with patch.object(outlook_tokens, "create_connection", return_value=connection):
            result = outlook_tokens._cdp_call("ws://127.0.0.1:9222/devtools/page/example", "Runtime.evaluate", {})
        self.assertEqual(result["result"]["value"], "token")
        connection.close.assert_called_once()

    def test_edge_token_reads_outlook_tab(self) -> None:
        tab = {
            "type": "page",
            "url": "https://outlook.office.com/mail/",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/example",
        }
        with (
            patch.object(outlook_tokens, "_edge_endpoint", return_value=(9222, "/devtools/browser/example")),
            patch.object(outlook_tokens, "_targets", return_value=[tab]),
            patch.object(outlook_tokens, "_cdp_call", return_value={"result": {"value": "header.payload.signature"}}),
        ):
            self.assertEqual(outlook_tokens._edge_token(), "header.payload.signature")


if __name__ == "__main__":
    unittest.main()
