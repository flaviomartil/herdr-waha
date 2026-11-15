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
        self.bridge.channels_path = Path(self.temp.name) / "channels.json"
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
            self.assertEqual(text, "Este agente não está mais ativo.")
            call.assert_not_called()

    def test_send_uses_zapforge_headers_and_returns_id(self):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

        def open_request(request, timeout):
            self.assertTrue(request.full_url.endswith("/send/text"))
            self.assertEqual(request.get_header("Token"), "api-key")
            self.assertEqual(request.get_header("Instance"), "connected")
            self.assertEqual(json.loads(request.data), {"number": "5511999999999", "text": "hello"})
            return Response(b'{"message":"success","data":{"Info":{"ID":"outbound"}}}')

        with patch("herdr_waha.urllib.request.urlopen", side_effect=open_request):
            self.assertEqual(self.bridge.send("hello", ("pane", "first")), "outbound")
        self.assertEqual(self.bridge.replies["outbound"], ("pane", "first"))

    def test_send_to_group_is_rejected(self):
        with patch("herdr_waha.urllib.request.urlopen") as call:
            with self.assertRaises(ValueError):
                self.bridge.send("hello", target_chat="120363@g.us", skip_typing=True)
        call.assert_not_called()

    def test_send_to_group_uses_full_jid_when_groups_enabled(self):
        self.bridge.groups = True
        with patch.object(self.bridge, "call", return_value={"data": {"Info": {"ID": "g1"}}}) as call:
            self.assertEqual(self.bridge.send("hello", target_chat="120363001@g.us", skip_typing=True), "g1")
        call.assert_called_once_with("POST", "/send/text", {"number": "120363001@g.us", "text": "hello"}, timeout=12)

    def test_number_of_keeps_group_jid_and_strips_contact_suffix(self):
        self.assertEqual(herdr_waha.Bridge.number_of("120363001@g.us"), "120363001@g.us")
        self.assertEqual(herdr_waha.Bridge.number_of("5511999999999@c.us"), "5511999999999")

    def test_create_group_reads_full_jid_from_zapforge(self):
        with patch.object(self.bridge, "call", return_value={"data": {"JID": {"User": "120363012345678901", "Server": "g.us"}}}):
            self.assertEqual(self.bridge.create_group("Agente", [self.bridge.operator_id])["id"], "120363012345678901@g.us")

    def test_group_channel_created_and_notified_when_groups_enabled(self):
        self.bridge.groups = True
        self.bridge.notify_target = "channels"
        self.bridge.sync_channels = True
        blocked = dict(self.agent, agent_status="blocked")
        with patch("herdr_waha.agents", side_effect=[[self.agent], [blocked]]), \
                patch.object(self.bridge, "create_group", return_value={"id": "120363001@g.us"}) as create, \
                patch.object(self.bridge, "send") as send, \
                patch.object(self.bridge, "archive_chat"), \
                patch.object(self.bridge, "pin_chat"), \
                patch.object(self.bridge, "sync_to_zapforge"), \
                patch.object(self.bridge, "read_terminal", return_value="screen"):
            self.bridge.reconcile_channels()
            self.bridge.reconcile_channels()
        create.assert_called_once()
        self.assertEqual(self.bridge.channels["pane"]["group_id"], "120363001@g.us")
        targets = [call.kwargs.get("target_chat") for call in send.call_args_list]
        self.assertEqual(targets.count("120363001@g.us"), 2)
        self.assertIn(self.bridge.announce_chat, targets)

    def test_presence_react_edit_use_message_endpoints(self):
        calls = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

        def open_request(request, timeout):
            calls.append((request.get_method(), request.full_url.split("zapforge.test")[1], json.loads(request.data)))
            return Response(b'{"message":"success"}')

        self.bridge.base_url = "https://zapforge.test"
        with patch("herdr_waha.urllib.request.urlopen", side_effect=open_request):
            self.bridge.send_presence("5511999999999@c.us")
            self.bridge.react("m1", "x", "5511999999999@c.us")
            self.assertTrue(self.bridge.edit_message("5511999999999@c.us", "m1", "new"))
        self.assertEqual(calls, [
            ("POST", "/message/presence", {"number": "5511999999999", "presence": "composing"}),
            ("POST", "/message/react", {"number": "5511999999999", "messageId": "m1", "emoji": "x"}),
            ("POST", "/message/edit", {"number": "5511999999999", "messageId": "m1", "text": "new"}),
        ])

    def test_create_failure_backs_off_and_persists(self):
        self.bridge.groups = True
        self.bridge.sync_channels = True
        clock = [1000.0]
        with patch("herdr_waha.agents", return_value=[self.agent]), \
                patch("herdr_waha.time.time", side_effect=lambda: clock[0]), \
                patch.object(self.bridge, "create_group", side_effect=OSError("boom")) as create:
            self.bridge.reconcile_channels()
            self.bridge.reconcile_channels()
            self.assertEqual(create.call_count, 1)
            clock[0] += 61
            self.bridge.reconcile_channels()
            self.assertEqual(create.call_count, 2)
            self.assertEqual(self.bridge.backoff["pane"]["count"], 2)
            self.assertEqual(self.bridge.backoff["pane"]["next_at"], clock[0] + 60)
            for _ in range(20):
                clock[0] = self.bridge.backoff["pane"]["next_at"] + 1
                self.bridge.reconcile_channels()
            self.assertEqual(self.bridge.backoff["pane"]["next_at"] - clock[0], 3600)
        saved = json.loads(self.bridge.channels_path.read_text())
        self.assertIn("pane", saved["__backoff__"])
    def test_groups_disabled_notifies_operator_by_dm_only(self):
        self.assertFalse(self.bridge.groups)
        self.assertEqual(self.bridge.notify_target, "dm")
        blocked = dict(self.agent, agent_status="blocked")
        with patch("herdr_waha.agents", side_effect=[[self.agent], [blocked], [blocked]]), \
                patch.object(self.bridge, "read_terminal", return_value="screen"), \
                patch.object(self.bridge, "create_group") as create, \
                patch.object(self.bridge, "send") as send:
            self.bridge.reconcile_channels()
            send.assert_not_called()
            self.bridge.reconcile_channels()
            self.bridge.reconcile_channels()
        create.assert_not_called()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.kwargs["target_chat"], "5511999999999@c.us")

    def test_group_chat_operator_and_non_operator(self):
        group_id = "120363028392000000@g.us"
        event = {
            "event": "message", "session": "connected",
            "payload": {
                "id": "group_msg_1",
                "from": group_id,
                "chatId": group_id,
                "participant": "5511999999999@c.us",
                "body": "/agents",
            },
        }
        with patch("herdr_waha.agents", return_value=[self.agent]), patch.object(self.bridge, "send") as send:
            # 1. Non-operator in group is rejected
            event["payload"]["participant"] = "5511888887777@c.us"
            self.bridge.handle(event)
            send.assert_not_called()

            # 2. Operator in group is accepted and reply is directed to group chatId
            event["payload"]["participant"] = "5511999999999@c.us"
            self.bridge.handle(event)
            send.assert_called_once()
            self.assertEqual(send.call_args.kwargs.get("target_chat"), group_id)

    def test_self_chat_with_from_me(self):
        event = {
            "event": "message", "session": "connected",
            "payload": {
                "id": "self_msg_1",
                "from": "5511999999999@c.us",
                "chatId": "5511999999999@c.us",
                "fromMe": True,
                "body": "/agents",
            },
        }
        with patch("herdr_waha.agents", return_value=[self.agent]), patch.object(self.bridge, "send") as send:
            self.bridge.handle(event)
            send.assert_called_once()
            self.assertEqual(send.call_args.kwargs.get("target_chat"), "5511999999999@c.us")

    def test_allowed_chat_restriction(self):
        allowed_group = "120363028392111111@g.us"
        restricted_bridge = herdr_waha.Bridge(
            "https://waha.example", "api-key", "connected", "hook-key",
            "5511999999999@c.us", Path(self.temp.name) / "seen_restricted",
            allowed_chat=allowed_group,
        )
        event = {
            "event": "message", "session": "connected",
            "payload": {
                "id": "restricted_msg_1",
                "from": "other_chat@c.us",
                "chatId": "other_chat@c.us",
                "participant": "5511999999999@c.us",
                "body": "/agents",
            },
        }
        with patch("herdr_waha.agents", return_value=[self.agent]), patch.object(restricted_bridge, "send") as send:
            # Not in allowed group -> rejected
            restricted_bridge.handle(event)
            send.assert_not_called()

            # In allowed group -> accepted
            event["payload"]["chatId"] = allowed_group
            event["payload"]["from"] = allowed_group
            event["payload"]["id"] = "restricted_msg_2"
            restricted_bridge.handle(event)
            send.assert_called_once()
            self.assertEqual(send.call_args.kwargs.get("target_chat"), allowed_group)

    def test_non_commands_are_silently_ignored(self):
        event = {
            "event": "message", "session": "connected",
            "payload": {
                "id": "chat_msg_1",
                "from": "120363028392000000@g.us",
                "chatId": "120363028392000000@g.us",
                "participant": "5511999999999@c.us",
                "body": "Bom dia pessoal, alguém revisou o PR?",
            },
        }
    def test_reaction_approval(self):
        ch_id = "120363028392999999@g.us"
        self.bridge.channels["pane"] = {
            "group_id": ch_id, "label": "test-agent", "status": "blocked", "pane_id": "pane"
        }
        event = {
            "event": "message.reaction", "session": "connected",
            "payload": {
                "id": "react_1",
                "chatId": ch_id,
                "participant": "5511999999999@c.us",
                "reaction": {"text": "👍", "messageId": "msg_prompt_1"},
            },
        }
        with patch("herdr_waha.herdr") as mock_herdr, patch.object(self.bridge, "send") as mock_send, patch.object(self.bridge, "react") as mock_react, patch.object(self.bridge, "sync_to_zapforge"):
            self.bridge.handle(event)
            mock_herdr.assert_any_call("agent", "send-keys", "pane", "y", "Enter")
            mock_send.assert_called_once()
            mock_react.assert_called_once_with("msg_prompt_1", "✅", "120363028392999999@g.us")

    def test_reaction_abort(self):
        ch_id = "120363028392999999@g.us"
        self.bridge.channels["pane"] = {
            "group_id": ch_id, "label": "test-agent", "status": "running", "pane_id": "pane"
        }
        event = {
            "event": "message.reaction", "session": "connected",
            "payload": {
                "id": "react_2",
                "chatId": ch_id,
                "participant": "5511999999999@c.us",
                "reaction": {"text": "🛑", "messageId": "msg_prompt_2"},
            },
        }
        with patch("herdr_waha.herdr") as mock_herdr, patch.object(self.bridge, "send") as mock_send, patch.object(self.bridge, "react") as mock_react, patch.object(self.bridge, "sync_to_zapforge"):
            self.bridge.handle(event)
            mock_herdr.assert_any_call("agent", "send-keys", "pane", "C-c", "C-c")
            mock_send.assert_called_once()
            mock_react.assert_called_once_with("msg_prompt_2", "🛑", "120363028392999999@g.us")


if __name__ == "__main__":
    unittest.main()
