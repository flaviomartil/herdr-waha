import hashlib
import hmac
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def herdr(*args):
    result = subprocess.run([os.environ.get("HERDR_BIN", "herdr"), *args], capture_output=True, text=True, timeout=15, check=True)
    return result.stdout


def agents():
    return json.loads(herdr("agent", "list"))["result"]["agents"]


def key(agent):
    return agent["pane_id"], agent["terminal_id"]


def label(agent):
    return f'{Path(agent.get("cwd") or "unknown").name} · {agent.get("agent") or "agent"}'


class Bridge:
    def __init__(self, base_url, api_key, session, hook_key, operator_id, state_path):
        if not base_url.startswith(("http://", "https://")) or not operator_id.endswith("@c.us"):
            raise ValueError("WAHA_URL must be HTTP(S) and WHATSAPP_OPERATOR_ID must end in @c.us")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.session = session
        self.hook_key = hook_key.encode()
        self.operator_id = operator_id
        self.state_path = Path(state_path)
        self.lock = threading.RLock()
        self.seen = set(self.state_path.read_text().splitlines()) if self.state_path.exists() else set()
        self.replies = {}
        self.listed = []
        self.statuses = {}
        self.started = False

    def verified(self, body, headers):
        if headers.get("X-Webhook-Hmac-Algorithm") != "sha512":
            return False
        actual = headers.get("X-Webhook-Hmac", "")
        expected = hmac.new(self.hook_key, body, hashlib.sha512).hexdigest()
        return hmac.compare_digest(actual, expected)

    def send(self, text, agent_key=None):
        payload = json.dumps({"session": self.session, "chatId": self.operator_id, "text": text[:3800]}).encode()
        request = urllib.request.Request(
            self.base_url + "/api/sendText", data=payload,
            headers={"X-Api-Key": self.api_key, "Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.load(response)
        message_id = result.get("id")
        if isinstance(message_id, dict):
            message_id = message_id.get("_serialized")
        if agent_key and message_id:
            with self.lock:
                self.replies[message_id] = agent_key
        return message_id

    def command(self, text, reply_id=""):
        parts = text.strip().split()
        if not parts:
            return "Commands: /agents, /status, /screen N, /send N text, /keys N key", None
        action = parts[0]
        if action in ("/agents", "/status"):
            current = agents()
            with self.lock:
                self.listed = [key(a) for a in current]
            if not current:
                return "No active agents.", None
            lines = [f'{i}. {a.get("agent_status", "unknown")} {label(a)}' for i, a in enumerate(current, 1)]
            return "\n".join(lines) + "\nUse /screen N, /send N text, or /keys N key.", None
        if action in ("/screen", "/send", "/keys"):
            if len(parts) < 2 or not parts[1].isdigit():
                return f"Usage: {action} N" + (" text" if action == "/send" else " key" if action == "/keys" else ""), None
            with self.lock:
                listed = list(self.listed)
            n = int(parts[1])
            if n < 1 or n > len(listed):
                return "Run /agents, then use a listed number.", None
            agent_key = listed[n - 1]
            current = next((a for a in agents() if key(a) == agent_key), None)
            if current is None:
                return "That agent is no longer active.", None
            if action == "/screen":
                screen = herdr("agent", "read", current["pane_id"], "--lines", "60", "--source", "recent")
                return label(current) + "\n" + screen[-3000:], agent_key
            if len(parts) < 3:
                return f"Usage: {action} N " + ("text" if action == "/send" else "key"), None
            if action == "/send":
                herdr("agent", "prompt", current["pane_id"], " ".join(parts[2:]))
            else:
                herdr("agent", "send-keys", current["pane_id"], *parts[2:])
            return "Sent to " + label(current) + ".", agent_key
        with self.lock:
            agent_key = self.replies.get(reply_id)
        if agent_key is None:
            return "Reply to an agent card or use /agents and /send N text.", None
        current = next((a for a in agents() if key(a) == agent_key), None)
        if current is None:
            return "That agent is no longer active.", None
        herdr("agent", "prompt", current["pane_id"], text)
        return "Sent to " + label(current) + ".", agent_key

    def handle(self, event):
        payload = event.get("payload") or {}
        if (event.get("event") != "message" or event.get("session") != self.session
                or payload.get("fromMe") or payload.get("chatId") != self.operator_id
                or payload.get("from") != self.operator_id or not payload.get("id")
                or not isinstance(payload.get("body"), str) or not payload["body"].strip()):
            return
        message_id = payload["id"]
        if "\n" in message_id or "\r" in message_id:
            raise ValueError("invalid message ID")
        with self.lock:
            if message_id in self.seen:
                return
            self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self.state_path.open("a") as file:
                file.write(message_id + "\n")
                file.flush()
                os.fsync(file.fileno())
            self.seen.add(message_id)
            self.started = True
        reply_id = (payload.get("replyTo") or {}).get("id", "")
        try:
            answer, agent_key = self.command(payload["body"], reply_id)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyError, OSError, ValueError) as exc:
            answer, agent_key = f"Herdr command failed: {type(exc).__name__}", None
        self.send(answer, agent_key)

    def poll(self):
        while True:
            time.sleep(15)
            try:
                current = agents()
                notices = []
                with self.lock:
                    for agent in current:
                        agent_key = key(agent)
                        status = agent.get("agent_status", "unknown")
                        previous = self.statuses.get(agent_key)
                        if self.started and previous and previous != status and status in ("blocked", "done"):
                            notices.append((agent, agent_key))
                        self.statuses[agent_key] = status
                for agent, agent_key in notices:
                    try:
                        self.send(f'{label(agent)}: {agent["agent_status"]}. Use /agents and /screen N, or /keys N key for a dialog.', agent_key)
                    except (OSError, urllib.error.URLError, ValueError) as exc:
                        print(f"notification failed: {type(exc).__name__}", flush=True)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyError, ValueError) as exc:
                print(f"agent poll failed: {type(exc).__name__}", flush=True)


def serve(bridge, listen):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/webhook/waha":
                self.send_error(404)
                return
            size = int(self.headers.get("Content-Length", 0))
            if size < 1 or size > 1048576:
                self.send_error(413)
                return
            body = self.rfile.read(size)
            if not bridge.verified(body, self.headers):
                self.send_error(403)
                return
            try:
                bridge.handle(json.loads(body))
            except (OSError, ValueError, urllib.error.URLError) as exc:
                print(f"webhook failed: {type(exc).__name__}", flush=True)
                self.send_error(503)
                return
            self.send_response(200)
            self.end_headers()

    host, port = listen.rsplit(":", 1)
    threading.Thread(target=bridge.poll, daemon=True).start()
    ThreadingHTTPServer((host, int(port)), Handler).serve_forever()


def main():
    names = ("WAHA_URL", "WAHA_API_KEY", "WAHA_SESSION", "WAHA_WEBHOOK_HMAC_KEY", "WHATSAPP_OPERATOR_ID")
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise SystemExit("Missing: " + ", ".join(missing))
    state = os.environ.get("HERDR_WA_STATE", str(Path.home() / ".config/herdr-waha/seen"))
    bridge = Bridge(*(os.environ[name] for name in names), state)
    agents()
    serve(bridge, os.environ.get("HERDR_WA_LISTEN", "127.0.0.1:8080"))


if __name__ == "__main__":
    main()
