# Switchboard AI

One REST API and web UI for the AI command-line tools installed on your Windows PC.

| Provider | Binary | Comes from | Models |
|---|---|---|---|
| **Claude Code** | `claude.exe` | Claude Code extension (Antigravity IDE, VS Code, Cursor, Windsurf) | Claude Opus / Sonnet / Haiku / Fable |
| **Antigravity** | `agy.exe` | Antigravity CLI (`~/.gemini/bin`) | Gemini 3.x, GPT-OSS 120B, Claude Thinking |

Switchboard AI works with **both, either one, or neither**. It finds whatever is installed at startup, and routes each request to the right binary. Missing providers show up as "not installed" and never break the others. Requests run on the subscriptions those tools are already signed in with.

📖 **New here?** Read [HOW_IT_WORKS.md](HOW_IT_WORKS.md) first. It explains the whole workflow as a simple story, with a step-by-step Postman guide.

---

## Install

From PyPI:

```bash
pip install switchboard-router            # core
pip install "switchboard-router[api]"     # + official Claude API provider
```

From a clone of this repository (editable: code changes apply without reinstalling):

```bash
git clone https://github.com/meetdhamecha/switchboard-ai.git
cd switchboard-ai
pip install -e ".[api,test]"
```

On Windows you can also double-click `start.bat`, which creates a virtual environment on first run.

## Quick start

```bash
switchboard-ai status      # what was found?
switchboard-ai server      # http://localhost:8000
switchboard-ai ask "hello" --model gemini-3.8-flash-high
```

`python -m switchboard_ai …` works the same way. Settings are read from a `.env` file in the current folder; copy `.env.example` to start.

> **Backend and frontend are one process.** The server hosts the UI itself, so there is no separate frontend to start. Always open **http://localhost:8000**.
> If you open `static/index.html` some other way (VS Code Live Server, or double-clicking the file), the page still talks to `http://localhost:8000`, but the server must be running. You can change the backend address under **Settings → Backend URL**.

Open **http://localhost:8000**. The model picker, the effort level and the Memory toggle are all in the chat box. The UI has these views:

- **Chat** — streaming chat with any model. You can switch models mid-conversation, even across providers. It shows tool calls, has Stop and Regenerate buttons, keeps history in the browser, and has light and dark themes.
- **Agent** — give a task and a folder. The model reads and edits files there, and you can continue the task with follow-ups.
- **API & Docs** — ready-to-copy snippets (cURL, Python, streaming, OpenAI SDK, JS) with your URL and model filled in.
- **Status** — which binaries were found, sign-in state, and the list of live model processes.

Interactive API docs: **http://localhost:8000/docs**

---

## REST API

```http
GET    /health                 server + provider status
GET    /models                 all models; each has a unique "key" like "claude:claude-sonnet-5"
GET    /auth/accounts          overview of Claude and Antigravity accounts & tokens
GET    /auth/tokens            extract access & refresh tokens (?reveal=true for unmasked)
POST   /auth/login             launch interactive login window for Claude or Antigravity
POST   /auth/refresh           refresh access & refresh tokens
POST   /chat                   chat → full JSON
POST   /chat/stream            chat → Server-Sent Events
POST   /agent/task             agent task → full JSON
POST   /agent/task/stream      agent task → Server-Sent Events
GET    /sessions               warm conversations per provider
DELETE /sessions/{id}          close one
GET    /v1/models              OpenAI-compatible
POST   /v1/chat/completions    OpenAI-compatible (stream or not)
```

### Chat

```bash
curl http://localhost:8000/chat -H "Content-Type: application/json" -d '{
  "model": "gemini-3.8-flash-high",
  "messages": [{"role": "user", "content": "Hello!"}],
  "session_id": "my-chat-1"
}'
```

Response:

```json
{ "provider": "antigravity", "model": "gemini-3.8-flash-high", "content": "Hi! …",
  "usage": {...}, "tools": [], "notices": [], "latency_ms": 2140, ... }
```

**Naming a model**

- A bare id (`"claude-opus-5"`, `"gemini-3.1-pro-high"`) goes to whichever provider has it.
- Some ids exist in both providers (e.g. `claude-sonnet-4-6`). For those, prefix the provider: `"antigravity:claude-sonnet-4-6"`. You can also send `"provider": "antigravity"`.
- Leave `model` out to use the default. If the default's provider isn't installed, the first model of an available provider is used.

**`session_id`** keeps a conversation warm. The model process stays alive between turns, so replies start sooner, and only the newest message is sent to it. You can keep sending the full `messages` history: if the process was restarted, the server replays that history automatically. Leave `session_id` out for stateless one-off calls.

**`effort`**: `low | medium | high` (Claude also accepts `xhigh | max`).

### Streaming events (`/chat/stream`, `/agent/task/stream`)

Each line is `data: {json}`:

| type | fields |
|---|---|
| `session` | provider, model (sent first) |
| `text` | content (a piece of the reply) |
| `tool_use` | id, name, detail — may be sent again with the same id once the full input is known |
| `tool_result` | id, ok, content (error text) |
| `notice` | content, e.g. a tool that was blocked |
| `result` | usage, native_session_id, duration_ms |
| `error` | content |
| `done` | (sent last) |

### Agent tasks

```bash
curl http://localhost:8000/agent/task -H "Content-Type: application/json" -d '{
  "task": "Read the Python files and write SUMMARY.md",
  "model": "claude-sonnet-5",
  "working_dir": "C:\\Projects\\myapp"
}'
```

To continue the same task, send the returned `native_session_id` back as `"resume"`, using the same `working_dir`.

- **Claude**: can use the tools in `AGENT_ALLOWED_TOOLS`. A request can pass `"tools": [...]` to narrow that list, but can never add to it.
- **Antigravity**: runs in `accept-edits` mode, so it can read and edit files, but shell commands are blocked.
- `AGENT_ALLOW_SHELL=1` gives both providers shell access. Only enable it on a machine that nobody else can reach.

### OpenAI SDK

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="anything-or-your-API_KEY")
r = client.chat.completions.create(model="claude:claude-sonnet-5",
                                   messages=[{"role": "user", "content": "Hi"}])
print(r.choices[0].message.content)
```

Send the header `X-Session-Id: <id>` to keep an OpenAI-style conversation warm.

---

## How it works

```
client ──HTTP──► server.py ──► providers/ registry ──► claude.py ─┐
                                   (routes by model)   antigravity.py ─┤
                                                                       ▼
                                          process.py: warm CLI processes
                                          (stdin: one JSON message per turn,
                                           stdout: stream-json events)
```

- **Both CLIs run in persistent stream-json mode.** A process stays alive for each conversation. Measured cold starts are claude.exe ≈ 2 s and agy.exe ≈ 12 s, while a warm agy turn takes ≈ 2 s.
- **Spare processes.** After each request, one process is pre-spawned for the model that was just used. The next new conversation or stateless call on that model then starts warm, not cold.
- **One event format.** Each provider has its own parser that turns the binary's output into the same events. The server, the stream smoothing (`pacing.py`), and the UI never need to know which binary produced them.
- **Self-healing sessions.** If a process dies or the client presses Stop mid-reply, that process is retired. The next turn starts a fresh one and replays the history.

---

## Security

- The server binds to **127.0.0.1** by default, so only this PC can reach it. If you set `API_HOST=0.0.0.0` to open it to your LAN, set `API_KEY` too.
- Chat tools are read-only (`WebSearch,WebFetch`). File editing only happens through `/agent`, and only inside the requested folder. Use `AGENT_ALLOWED_ROOTS` to limit which folders are allowed.
- Shell access is off unless `AGENT_ALLOW_SHELL=1`.
- Never commit `.env` or `~/.claude/.credentials.json`.

## Configuration

Copy `.env.example` to `.env` inside `switchboard_ai/`. Every setting is documented there and is optional.

## Troubleshooting

| Problem | Fix |
|---|---|
| Claude Code "not found" | Install the Claude Code extension, or set `CLAUDE_BINARY_PATH` |
| Claude answers "please log in" | Open the Claude Code extension once and sign in |
| Antigravity "not found" | Install the Antigravity CLI, or set `AGY_BINARY_PATH` |
| First reply is slow | That's the cold start. Later replies on the same model are warm |
| "X was blocked" notice | That tool isn't pre-approved. See `CHAT_ALLOWED_TOOLS` / `AGENT_ALLOWED_TOOLS` |
