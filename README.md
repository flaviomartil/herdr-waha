# Herdr WAHA

Control local Herdr agents from one authorized WhatsApp private chat through an existing WAHA session. Uses Python's standard library and the installed `herdr` CLI.

## Run

Run this bridge on the machine where Herdr is available. Set these environment variables in your service manager or a private environment file outside the repository:

| Variable | Value |
| --- | --- |
| `WAHA_URL` | WAHA API base URL, such as `https://waha.example.com` |
| `WAHA_API_KEY` | WAHA API key with permission to send messages |
| `WAHA_SESSION` | Name of the already connected WAHA session |
| `WAHA_WEBHOOK_HMAC_KEY` | New shared secret for this webhook |
| `WHATSAPP_OPERATOR_ID` | Your private chat ID, such as `55DDDNUMBER@c.us` |

Optional: `HERDR_BIN` (default `herdr`), `HERDR_WA_LISTEN` (default `127.0.0.1:8080`), and `HERDR_WA_STATE` (default `~/.config/herdr-waha/seen`). Start with `python3 herdr_waha.py`. The server exits if Herdr cannot list agents.

Publish `POST https://YOUR_HERDR_HOST/webhook/waha` through an HTTPS reverse proxy or tunnel. The WAHA server must be able to reach that address. Keep WAHA's API protected; only the webhook needs to reach this bridge.

## Configure the connected WAHA session

Add this object to the session's existing `config.webhooks` array:

```json
{
  "url": "https://YOUR_HERDR_HOST/webhook/waha",
  "events": ["message"],
  "hmac": { "key": "YOUR_SHARED_SECRET" }
}
```

Set the bridge's `WAHA_WEBHOOK_HMAC_KEY` to the same secret. WAHA sends `X-Webhook-Hmac` with SHA-512; the bridge rejects requests with an invalid signature, wrong session, group chat, or unapproved sender. Fetch the current session configuration before updating it: WAHA's `PUT /api/sessions/{session}` takes a full configuration, so replacing it with only this fragment can remove ZapForge's existing webhooks. [WAHA webhooks](https://waha.devlike.pro/docs/how-to/events/) and [session updates](https://waha.devlike.pro/docs/how-to/sessions/).

## Commands

From the authorized WhatsApp number, message the WAHA account:

| Command | Action |
| --- | --- |
| `/agents` or `/status` | List agents with numbers and status |
| `/screen N` | Read the recent screen of agent N |
| `/send N text` | Submit a prompt to agent N |
| `/keys N key` | Send a terminal key to agent N when it is blocked on a dialog |

You can also reply to an agent's screen or status message with a prompt. Some WAHA engines omit the replied-to message ID; use `/send N text` then. Agent numbers come from the last `/agents` response. Before sending, the bridge checks the pane and terminal IDs again to prevent a reused pane from receiving an old command. WAHA sends outbound text with [`POST /api/sendText`](https://waha.devlike.pro/docs/how-to/send-messages/).

After your first authorized message, the bridge reports later transitions to `blocked` or `done`. It stores processed inbound message IDs outside the repository to avoid repeated commands when WAHA retries a webhook. It does not read or post in communities, groups, or channels.

Run `python3 -m unittest -q` for the local checks.
