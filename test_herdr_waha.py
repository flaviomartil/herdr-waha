import hashlib
import hmac
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import herdr_waha


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bridge = herdr_waha.Bridge(
            "https://waha.example", "api-key", "connected", "hook-key",
            "5511999999999@c.us", Path(self.temp.name) / "seen",
        )
        self.agent = {
            "pane_id": "pane", "terminal_id": "first", "cwd": "/projects/demo",
            "agent": "codex", "agent_status": "idle",
        }

    def test_hmac_and_sender_and_retry(self):
        body = json.dumps({
            "event": "message", "session": "connected",
            "payload": {"id": "incoming", "from": "5511999999999@c.us", "chatId": "5511999999999@c.us", "body": "/agents"},
        }).encode()
        headers = {
            "X-Webhook-Hmac-Algorithm": "sha512",
            "X-Webhook-Hmac": hmac.new(b"hook-key", body, hashlib.sha512).hexdigest(),
        }
        self.assertTrue(self.bridge.verified(body, headers))
        self.assertFalse(self.bridge.verified(body + b" ", headers))
        event = json.loads(body)
        with patch("herdr_waha.agents", return_value=[self.agent]), patch.object(self.bridge, "send") as send:
            event["payload"]["from"] = "other@c.us"
            self.bridge.handle(event)
            send.assert_not_called()
            event["payload"]["from"] = "5511999999999@c.us"
            self.bridge.handle(event)
            self.bridge.handle(event)
            send.assert_called_once()

    def test_brazilian_phone_ninth_digit_tolerance(self):
        self.assertTrue(herdr_waha.same_phone("5511999998888@c.us", "551199998888@c.us"))
        self.assertTrue(herdr_waha.same_phone("551199998888@c.us", "5511999998888@c.us"))
        self.assertTrue(herdr_waha.same_phone("5511999998888", "5511999998888@c.us"))
        self.assertFalse(herdr_waha.same_phone("5511999998888@c.us", "5521999998888@c.us"))
        self.assertFalse(herdr_waha.same_phone("5511999998888@c.us", "5511888887777@c.us"))

    def test_routing_rejects_reused_pane(self):
        with patch("herdr_waha.agents", return_value=[self.agent]), patch("herdr_waha.herdr", return_value="screen") as call:
            self.bridge.command("/agents")
            text, agent_key = self.bridge.command("/screen 1")
            self.assertIn("screen", text)
            self.bridge.replies["outbound"] = agent_key
            self.bridge.command("do work", "outbound")
            call.assert_any_call("agent", "prompt", "pane", "do work")
        reused = dict(self.agent, terminal_id="second")
        with patch("herdr_waha.agents", return_value=[reused]), patch("herdr_waha.herdr") as call:
            text, _ = self.bridge.command("do more", "outbound")
            self.assertEqual(text, "That agent is no longer active.")
            call.assert_not_called()

    def test_send_uses_waha_session_and_api_key(self):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

        def open_request(request, timeout):
            self.assertEqual(request.get_header("X-api-key"), "api-key")
            self.assertEqual(json.loads(request.data), {
                "session": "connected", "chatId": "5511999999999@c.us", "text": "hello",
            })
            return Response(b'{"id":"outbound"}')

        with patch("herdr_waha.urllib.request.urlopen", side_effect=open_request):
            self.assertEqual(self.bridge.send("hello", ("pane", "first")), "outbound")
        self.assertEqual(self.bridge.replies["outbound"], ("pane", "first"))


if __name__ == "__main__":
    unittest.main()
