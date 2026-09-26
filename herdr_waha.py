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


STATUS_EMOJIS = {
    "working": "⚡",
    "idle": "✅",
    "blocked": "❓",
    "done": "🏆",
    "exited": "🏁",
    "unknown": "👀",
}


def herdr(*args):
    result = subprocess.run([os.environ.get("HERDR_BIN", "herdr"), *args], capture_output=True, text=True, timeout=15, check=True)
    return result.stdout


def agents():
    return json.loads(herdr("agent", "list"))["result"]["agents"]


def key(agent):
    return agent["pane_id"], agent["terminal_id"]


def label(agent):
    return f'{Path(agent.get("cwd") or "unknown").name} · {agent.get("agent") or "agent"}'


def same_phone(configured_id, incoming_id):
    if not configured_id or not incoming_id:
        return False
    if configured_id == incoming_id:
        return True
    c = str(configured_id).split("@")[0].strip()
    i = str(incoming_id).split("@")[0].strip()
    if c == i:
        return True
    if c.startswith("55") and i.startswith("55") and len(c) >= 4 and len(i) >= 4:
        if c[2:4] == i[2:4]:
            rest_c = c[4:]
            rest_i = i[4:]
            if len(rest_c) == 9 and len(rest_i) == 8 and rest_c[1:] == rest_i:
                return True
            if len(rest_c) == 8 and len(rest_i) == 9 and rest_i[1:] == rest_c:
                return True
    return False


class Bridge:
    def __init__(self, base_url, api_key, session, hook_key, operator_id, state_path, allowed_chat=None):
        operator_id = operator_id.strip()
        if not operator_id.endswith("@c.us"):
            operator_id = f"{operator_id}@c.us"
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("WAHA_URL must be HTTP(S)")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.session = session
        self.hook_key = hook_key.encode()
        self.operator_id = operator_id
        self.allowed_chat = allowed_chat.strip() if allowed_chat else None
        self.last_chat_id = self.allowed_chat or self.operator_id
        self.state_path = Path(state_path)
        self.channels_path = Path(os.environ.get("HERDR_WA_CHANNELS", str(Path.home() / ".config/herdr-waha/agent_channels.json")))
        self.sync_channels = os.environ.get("HERDR_WA_SYNC_CHANNELS", "1") in ("1", "true", "yes", "on")
        raw_lines = str(os.environ.get("HERDR_WA_TERMINAL_LINES", "100")).strip().lower()
        if raw_lines in ("0", "all", "full", "tudo", "max"):
            self.terminal_lines = 1500
        else:
            try:
                self.terminal_lines = max(10, int(raw_lines))
            except ValueError:
                self.terminal_lines = 100
        self.stream_terminal = os.environ.get("HERDR_WA_STREAM_TERMINAL", "0") in ("1", "true", "yes", "on")
        self.community_id = os.environ.get("HERDR_WA_COMMUNITY_ID", "120363427816264803@g.us")
        self.announce_chat = os.environ.get("HERDR_WA_COMMUNITY_ANNOUNCE", "120363411783439303@g.us")
        self.notify_target = os.environ.get("HERDR_WA_NOTIFY_TARGET", "channels").strip().lower()

        self.lock = threading.RLock()
        self.seen = set(self.state_path.read_text().splitlines()) if self.state_path.exists() else set()
        self.replies = {}
        self.listed = []
        self.statuses = {}
        self.started = True

        self.channels = {}
        if self.channels_path.exists():
            try:
                self.channels = json.loads(self.channels_path.read_text())
            except Exception:
                self.channels = {}

    def get_admin_groups(self):
        try:
            req = urllib.request.Request(
                f"{self.base_url}/api/{self.session}/groups",
                headers={
                    "X-Api-Key": self.api_key,
                    "Accept": "application/json",
                    "User-Agent": "ZapForge-Herdr-Bridge/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.load(resp)
            groups = list(data.values()) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            res = []
            for g in groups:
                is_comm = bool(g.get("isCommunity"))
                is_ann = bool(g.get("isCommunityAnnounce"))
                parts = g.get("participants") or []
                is_adm = is_comm or any(p.get("admin") in ("admin", "superadmin") for p in parts)
                if is_adm or is_comm or is_ann:
                    res.append({
                        "id": g.get("id"),
                        "subject": g.get("subject") or "Sem nome",
                        "is_community": is_comm,
                        "is_announce": is_ann,
                        "size": g.get("size") or len(parts),
                    })
            return res
        except Exception as exc:
            print(f"failed to fetch admin groups: {exc}", flush=True)
            return []

    def read_terminal(self, pane_id, lines=None):
        n = lines if lines is not None else self.terminal_lines
        try:
            return herdr("agent", "read", pane_id, "--lines", str(n), "--source", "recent")
        except Exception as exc:
            return f"Erro ao ler terminal: {exc}"

    def sync_to_zapforge(self, group_id, label, role, text, metadata=None):
        try:
            payload = json.dumps({
                "session": self.session,
                "groupId": group_id,
                "label": label,
                "role": role,
                "text": text[:25000],
                "metadata": metadata or {},
            }).encode()
            req = urllib.request.Request(
                self.base_url + "/api/herdr/sync-message",
                data=payload,
                headers={
                    "X-Api-Key": self.api_key,
                    "Content-Type": "application/json",
                    "User-Agent": "ZapForge-Herdr-Bridge/1.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass
        except Exception as exc:
            pass

    def save_channels(self):
        try:
            self.channels_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = self.channels_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.channels, indent=2))
            tmp.replace(self.channels_path)
        except Exception as exc:
            print(f"failed to save channels: {exc}", flush=True)

    def verified(self, body, headers):
        algo = headers.get("X-Webhook-Hmac-Algorithm") or headers.get("x-webhook-hmac-algorithm")
        if algo != "sha512":
            return False
        actual = headers.get("X-Webhook-Hmac") or headers.get("x-webhook-hmac", "")
        expected = hmac.new(self.hook_key, body, hashlib.sha512).hexdigest()
        return hmac.compare_digest(actual, expected)

    def send(self, text, agent_key=None, target_chat=None):
        destination = target_chat or self.last_chat_id or self.operator_id
        payload = json.dumps({"session": self.session, "chatId": destination, "text": text[:3800]}).encode()
        request = urllib.request.Request(
            self.base_url + "/api/sendText", data=payload,
            headers={
                "X-Api-Key": self.api_key,
                "Content-Type": "application/json",
                "User-Agent": "ZapForge-Herdr-Bridge/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=12) as response:
            result = json.load(response)
        message_id = result.get("id")
        if isinstance(message_id, dict):
            message_id = message_id.get("_serialized")
        if message_id:
            with self.lock:
                self.seen.add(str(message_id))
                if agent_key:
                    self.replies[message_id] = agent_key
        return message_id

    def create_group(self, name, participants):
        payload = json.dumps({
            "session": self.session,
            "name": name[:100],
            "participants": participants,
        }).encode()
        request = urllib.request.Request(
            self.base_url + "/api/createGroup", data=payload,
            headers={
                "X-Api-Key": self.api_key,
                "Content-Type": "application/json",
                "User-Agent": "ZapForge-Herdr-Bridge/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)

    def set_group_subject(self, group_id, subject):
        payload = json.dumps({
            "session": self.session,
            "groupId": group_id,
            "subject": subject[:100],
        }).encode()
        request = urllib.request.Request(
            self.base_url + "/api/setGroupSubject", data=payload,
            headers={
                "X-Api-Key": self.api_key,
                "Content-Type": "application/json",
                "User-Agent": "ZapForge-Herdr-Bridge/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    def record_seen(self, message_id):
        if not message_id or "\n" in message_id or "\r" in message_id:
            return False
        if message_id in self.seen:
            return False
        self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.state_path.open("a") as file:
            file.write(message_id + "\n")
            file.flush()
            os.fsync(file.fileno())
        self.seen.add(message_id)
        return True

    def find_channel_by_chat(self, chat_id):
        if not chat_id:
            return None, None
        with self.lock:
            for pane_id, ch in self.channels.items():
                if ch.get("group_id") == chat_id:
                    return pane_id, ch
        return None, None

    def command(self, text, reply_id="", chat_id=""):
        parts = text.strip().split()
        if not parts:
            return "Comandos disponíveis: /agents, /status, /screen N, /send N <texto>, /keys N <tecla>", None
        action = parts[0]
        if action in ("/agents", "/status"):
            current = agents()
            with self.lock:
                self.listed = [key(a) for a in current]
            if not current:
                return "Nenhum agente ativo no momento.", None
            lines = []
            for i, a in enumerate(current, 1):
                st = a.get("agent_status", "unknown")
                emoji = STATUS_EMOJIS.get(st, "👀")
                lbl = label(a)
                ch = self.channels.get(a["pane_id"])
                ch_indicator = " [canal ativo]" if ch and ch.get("group_id") else ""
                lines.append(f"{i}. {emoji} {lbl} ({st}){ch_indicator}")
            footer = "\n_Use /screen N, /send N <texto>, ou /keys N <tecla>._"
            if chat_id and str(chat_id).endswith("@g.us"):
                footer += f"\n(ID deste grupo: {chat_id})"
            return "\n".join(lines) + footer, None

        if action in ("/groups", "/admin_groups", "/comunidades", "/comunidade"):
            adms = self.get_admin_groups()
            if not adms:
                return "Nenhum grupo ou comunidade onde você é administrador foi encontrado.", None
            lines = ["📋 *Grupos e Comunidades onde você é Administrador:*\n"]
            for i, g in enumerate(adms[:20], 1):
                tipo = "🌐 [Comunidade]" if g["is_community"] else ("📢 [Avisos]" if g["is_announce"] else "👥 [Grupo]")
                lines.append(f"{i}. {tipo} *{g['subject']}*\n   `{g['id']}`")
            lines.append(f"\n⚙️ *Configuração Atual:*\n• Destino: *{self.notify_target}*\n• Avisos/Concluídos: `{self.announce_chat or 'nenhum'}`\n\n💡 *Comandos:*\n• `/notify channels` (somente canais dos agentes)\n• `/notify community` (canais + comunidade de avisos)\n• `/notify dm` (somente privado)\n• `/set_announce <ID@g.us>` (muda canal de avisos)")
            return "\n".join(lines), None

        if action == "/notify":
            if len(parts) > 1 and parts[1].lower() in ("channels", "groups", "community", "dm", "both"):
                self.notify_target = parts[1].lower()
                return f"✅ Modo de notificação alterado para: *{self.notify_target}*.\n(Agora alertas de bloqueado/concluído respeitarão esta escolha).", None
            return f"Modo atual: *{self.notify_target}*.\n\nOpções disponíveis:\n• `/notify channels` (apenas canais dos agentes)\n• `/notify community` (canais dos agentes + avisos)\n• `/notify dm` (apenas WhatsApp pessoal)\n• `/notify both` (canais + pessoal)", None

        if action == "/set_announce":
            if len(parts) > 1 and "@g.us" in parts[1]:
                self.announce_chat = parts[1].strip()
                return f"✅ Canal de avisos/concluídos atualizado para:\n`{self.announce_chat}`", None
            return "Uso: `/set_announce <ID@g.us>`\n(Veja a lista com `/groups`)", None

        if action in ("/screen", "/send", "/keys"):
            if len(parts) < 2 or not parts[1].isdigit():
                return f"Uso: {action} N" + (" <texto>" if action == "/send" else " <tecla>" if action == "/keys" else ""), None
            with self.lock:
                listed = list(self.listed)
            n = int(parts[1])
            if n < 1 or n > len(listed):
                return "Execute /agents primeiro para obter a numeração atual.", None
            agent_key = listed[n - 1]
            current = next((a for a in agents() if key(a) == agent_key), None)
            if current is None:
                return "Este agente não está mais ativo.", None
            if action == "/screen":
                screen = herdr("agent", "read", current["pane_id"], "--lines", "60", "--source", "recent")
                return f"🖥️ *{label(current)}:*\n\n```\n{screen[-3000:]}\n```", agent_key
            if len(parts) < 3:
                return f"Uso: {action} N " + ("<texto>" if action == "/send" else "<tecla>"), None
            if action == "/send":
                herdr("agent", "prompt", current["pane_id"], " ".join(parts[2:]))
            else:
                herdr("agent", "send-keys", current["pane_id"], *parts[2:])
            return f"Enviado para {label(current)}.", agent_key

        with self.lock:
            agent_key = self.replies.get(reply_id)
        if agent_key is None:
            return "Responda a um card de agente ou use /agents e /send N <texto>.", None
        current = next((a for a in agents() if key(a) == agent_key), None)
        if current is None:
            return "Este agente não está mais ativo.", None
        herdr("agent", "prompt", current["pane_id"], text)
        return f"Enviado para {label(current)}.", agent_key

    def handle(self, event):
        payload = event.get("payload") or {}
        incoming_chat = payload.get("chatId") or payload.get("from") or ""
        incoming_from = payload.get("participant") or payload.get("from") or ""
        meta = event.get("metadata") or {}
        session_matches = (
            event.get("session") == self.session
            or same_phone(self.session, event.get("session"))
            or meta.get("instanceId") == self.session
            or meta.get("instanceName") == self.session
            or meta.get("phone") == self.session
            or same_phone(self.session, meta.get("phone"))
        )
        if (event.get("event") != "message" or not session_matches
                or not payload.get("id")
                or not isinstance(payload.get("body"), str) or not payload["body"].strip()):
            return

        sender_matches = same_phone(self.operator_id, incoming_from)
        if not sender_matches:
            return

        body_text = payload["body"].strip()
        message_id = payload["id"]

        # 1. Check if message is inside an agent's dedicated WhatsApp group/channel
        pane_id, ch = self.find_channel_by_chat(incoming_chat)
        if ch:
            if not self.record_seen(message_id):
                return
            print(f"direct channel input for {pane_id} ({ch.get('label')}): '{body_text}'", flush=True)

            lower = body_text.lower()
            if lower.startswith("/screen"):
                lines = self.terminal_lines
                parts = body_text.split()
                if len(parts) > 1:
                    if parts[1].lower() in ("full", "all", "tudo", "0", "max"):
                        lines = 1500
                    elif parts[1].isdigit():
                        lines = min(2000, max(10, int(parts[1])))
                try:
                    screen = self.read_terminal(pane_id, lines=lines)
                    clean_screen = screen.strip()
                    self.sync_to_zapforge(incoming_chat, ch.get("label"), "assistant", f"🖥️ Terminal ({lines} linhas):\n\n```\n{clean_screen}\n```", metadata={"terminal": True, "lines": lines})

                    if len(clean_screen) > 3400:
                        wa_screen = clean_screen[-3200:]
                        notice = f"\n\n_💡 Exibindo as últimas linhas ({len(wa_screen)} de {len(clean_screen)} caracteres). Histórico completo salvo no ZapForge._"
                    else:
                        wa_screen = clean_screen
                        notice = ""
                    screen_text = f"🖥️ *Terminal ({ch.get('label')} - {lines} linhas):*\n\n```\n{wa_screen}\n```" + notice
                    self.send(screen_text, target_chat=incoming_chat)
                except Exception as exc:
                    self.send(f"Erro ao ler terminal: {exc}", target_chat=incoming_chat)
                return

            if lower.startswith("/keys "):
                keys = body_text.split()[1:]
                try:
                    herdr("agent", "send-keys", pane_id, *keys)
                    keys_msg = f"⌨️ Tecla(s) enviada(s): `{' '.join(keys)}`"
                    self.send(keys_msg, target_chat=incoming_chat)
                    self.sync_to_zapforge(incoming_chat, ch.get("label"), "operator", body_text)
                except Exception as exc:
                    self.send(f"Erro ao enviar teclas: {exc}", target_chat=incoming_chat)
                return

            # Short responses when waiting for input or single key options
            short_keys = {
                "y": "y", "yes": "y", "s": "y", "sim": "y",
                "n": "n", "no": "n", "nao": "n", "não": "n",
                "enter": "Enter", "esc": "Escape", "escape": "Escape",
                "q": "q", "c": "c",
            }
            if lower in short_keys or (body_text.isdigit() and len(body_text) <= 2):
                key_to_send = short_keys.get(lower, body_text)
                try:
                    herdr("agent", "send-keys", pane_id, key_to_send)
                    key_msg = f"⌨️ Tecla `{key_to_send}` enviada."
                    self.send(key_msg, target_chat=incoming_chat)
                    self.sync_to_zapforge(incoming_chat, ch.get("label"), "operator", body_text)
                except Exception as exc:
                    self.send(f"Erro ao enviar tecla: {exc}", target_chat=incoming_chat)
                return

            # Normal prompt dispatch
            try:
                herdr("agent", "prompt", pane_id, body_text)
                self.send(f"📨 Prompt enviado para `{ch.get('label')}`.", target_chat=incoming_chat)
                self.sync_to_zapforge(incoming_chat, ch.get("label"), "operator", body_text)
            except Exception as exc:
                self.send(f"Erro ao enviar prompt: {exc}", target_chat=incoming_chat)
            return

        # 2. Check if message is in the allowed control chat (or operator DM)
        if self.allowed_chat and not (incoming_chat == self.allowed_chat or same_phone(self.allowed_chat, incoming_chat)):
            return

        reply_id = (payload.get("replyTo") or {}).get("id", "")
        is_cmd = body_text.startswith("/") or (reply_id and reply_id in self.replies)
        if not is_cmd:
            return

        if not self.record_seen(message_id):
            return

        self.last_chat_id = incoming_chat
        print(f"executing control command: '{body_text}' from {incoming_from} in {incoming_chat}", flush=True)
        try:
            answer, agent_key = self.command(body_text, reply_id, chat_id=incoming_chat)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyError, OSError, ValueError) as exc:
            answer, agent_key = f"Erro no comando Herdr: {type(exc).__name__}", None
        sent_id = self.send(answer, agent_key, target_chat=incoming_chat)
        print(f"response sent to {incoming_chat} (msgId: {sent_id})", flush=True)

    def reconcile_channels(self):
        try:
            current = agents()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyError, ValueError) as exc:
            print(f"agent poll failed: {type(exc).__name__}", flush=True)
            return

        current_panes = {a["pane_id"]: a for a in current}

        # 1. Reconcile live agents (create group or update status)
        for pane_id, agent in current_panes.items():
            st = agent.get("agent_status", "unknown")
            lbl = label(agent)
            emoji = STATUS_EMOJIS.get(st, "👀")
            target_subject = f"{emoji} {lbl}"[:100]

            with self.lock:
                ch = self.channels.get(pane_id)

            if not ch and self.sync_channels:
                print(f"auto-creating WhatsApp channel for {pane_id} ({lbl})...", flush=True)
                try:
                    res = self.create_group(target_subject, [self.operator_id])
                    group_id = res.get("id")
                    if group_id:
                        ch = {
                            "group_id": group_id,
                            "pane_id": pane_id,
                            "terminal_id": agent.get("terminal_id"),
                            "label": lbl,
                            "cwd": agent.get("cwd"),
                            "status": st,
                            "subject": target_subject,
                            "retired": False,
                            "created_at": int(time.time()),
                        }
                        with self.lock:
                            self.channels[pane_id] = ch
                            self.save_channels()
                        print(f"created WhatsApp channel {group_id} for {lbl}", flush=True)

                        welcome_text = (
                            f"🤖 *Canal do Agente Ativo*\n\n"
                            f"🏷️ *Agente:* `{agent.get('agent', 'agent')}`\n"
                            f"📁 *Pasta:* `{agent.get('cwd', '')}`\n"
                            f"💻 *Pane:* `{pane_id}`\n"
                            f"📊 *Status:* {emoji} `{st}`\n\n"
                            f"💬 *Como usar:*\n"
                            f"• Digite qualquer mensagem neste grupo para enviar diretamente como prompt.\n"
                            f"• Se o agente estiver aguardando aprovação (❓), responda com `y`, `n`, `1`, `2` ou sua resposta.\n"
                            f"• Use `/screen` (ou `/screen full`) para ver o terminal recente a qualquer momento."
                        )
                        self.send(welcome_text, target_chat=group_id)
                        self.sync_to_zapforge(group_id, lbl, "assistant", welcome_text)

                        if self.announce_chat and self.announce_chat != group_id:
                            ann_text = (
                                f"📢 *Novo Agente no Swarm!*\n\n"
                                f"Canal: *{target_subject}*\n"
                                f"📁 `{agent.get('cwd', '')}` · `{agent.get('agent', '')}`\n"
                                f"_Interaja diretamente no canal do agente._"
                            )
                            try:
                                self.send(ann_text, target_chat=self.announce_chat)
                            except Exception as ann_err:
                                print(f"failed to send announcement for {lbl}: {ann_err}", flush=True)
                except Exception as exc:
                    print(f"failed to create channel for {lbl}: {exc}", flush=True)
                    continue

            if ch:
                prev_status = ch.get("status")
                group_id = ch.get("group_id")

                if prev_status != st and group_id:
                    print(f"agent {pane_id} ({lbl}) status: {prev_status} -> {st}", flush=True)
                    try:
                        self.set_group_subject(group_id, target_subject)
                        ch["subject"] = target_subject
                    except Exception as exc:
                        print(f"failed to update group subject for {group_id}: {exc}", flush=True)

                    if st == "blocked":
                        try:
                            screen = self.read_terminal(pane_id)
                            blocked_msg = (
                                f"❓ *Aguardando resposta do operador!*\n\n"
                                f"```\n{screen[-2500:]}\n```\n\n"
                                f"_💡 Digite diretamente aqui sua resposta (ex: y, n, 1, 2, ou um texto)._"
                            )
                            # 1. Envia para o canal dedicado do agente
                            if self.notify_target in ("channels", "community", "both"):
                                self.send(blocked_msg, target_chat=group_id)
                            self.sync_to_zapforge(group_id, lbl, "assistant", blocked_msg)

                            # 2. Avisa na comunidade (canal de avisos/general)
                            if self.notify_target in ("channels", "community", "both") and self.announce_chat and self.announce_chat != group_id:
                                self.send(f"⚠️ *Agente bloqueado (❓)*: {lbl}\nAcesse o canal do agente para responder.", target_chat=self.announce_chat)

                            # 3. Manda no privado SOMENTE se explicitamente configurado como dm ou both
                            if self.notify_target in ("dm", "both") and self.operator_id:
                                self.send(f"⚠️ *Agente bloqueado (❓)*: {lbl}\nAcesse o canal do agente para responder.", target_chat=self.operator_id)
                        except Exception as exc:
                            print(f"failed to send blocked alert for {pane_id}: {exc}", flush=True)

                    elif st == "done":
                        try:
                            screen = self.read_terminal(pane_id)
                            done_msg = (
                                f"🏆 *Tarefa Concluída!*\n\n"
                                f"```\n{screen[-2500:]}\n```\n\n"
                                f"_Pronto para o próximo prompt._"
                            )
                            # 1. Envia para o canal dedicado do agente
                            if self.notify_target in ("channels", "community", "both"):
                                self.send(done_msg, target_chat=group_id)
                            self.sync_to_zapforge(group_id, lbl, "assistant", done_msg)

                            # 2. Avisa na comunidade (canal de avisos/concluídos)
                            if self.notify_target in ("channels", "community", "both") and self.announce_chat and self.announce_chat != group_id:
                                self.send(f"🏆 *Tarefa concluída*: {lbl}\nResultado gravado no canal do agente.", target_chat=self.announce_chat)

                            # 3. Manda no privado SOMENTE se explicitamente configurado como dm ou both
                            if self.notify_target in ("dm", "both") and self.operator_id:
                                self.send(f"🏆 *Tarefa concluída*: {lbl}", target_chat=self.operator_id)
                        except Exception as exc:
                            print(f"failed to send done alert for {pane_id}: {exc}", flush=True)

                    ch["status"] = st
                    with self.lock:
                        self.save_channels()

        # 2. Check for exited agents
        for pane_id, ch in list(self.channels.items()):
            if pane_id not in current_panes and not ch.get("retired"):
                group_id = ch.get("group_id")
                lbl = ch.get("label", pane_id)
                print(f"agent {pane_id} ({lbl}) exited.", flush=True)
                if group_id:
                    try:
                        exited_subj = f"🏁 {lbl}"[:100]
                        self.set_group_subject(group_id, exited_subj)
                        self.send("🏁 *Agente finalizado/desconectado.*", target_chat=group_id)
                    except Exception as exc:
                        print(f"failed to mark group exited for {group_id}: {exc}", flush=True)
                ch["status"] = "exited"
                ch["retired"] = True
                with self.lock:
                    self.save_channels()

    def poll(self):
        while True:
            time.sleep(10)
            self.reconcile_channels()


def serve(bridge, listen):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/webhook/waha", "/health", "/"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok","service":"herdr-waha"}')
            else:
                self.send_error(404)

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
    allowed_chat = os.environ.get("HERDR_WA_CHAT_ID") or os.environ.get("HERDR_WA_GROUP_ID")
    bridge = Bridge(*(os.environ[name] for name in names), state, allowed_chat=allowed_chat)
    agents()
    serve(bridge, os.environ.get("HERDR_WA_LISTEN", "127.0.0.1:8080"))


if __name__ == "__main__":
    main()
