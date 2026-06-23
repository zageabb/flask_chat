# Flask Chat

A small Flask group chat with SQLite message persistence, file attachments, and an
optional local Ollama participant. Ollama can also operate as a bounded autonomous
agent with coding, command, file, and web-research tools.

## Run locally

Install and start [Ollama](https://ollama.com/), then pull a model:

```sh
ollama pull llama3.2
```

Install the Python dependency and run the app:

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open <http://localhost:5000>. Select **Ask Ollama / agent to join in** when you
want the model to reply. The orchestrator decides whether to answer directly or
use tools such as web search, URL fetching, file access, and guarded commands.
Messages and tool traces are stored in `chat.db`.
Open clients poll for new messages every two seconds, so conversations stay in
sync without refreshing the whole page.
The toolbar shows the currently loaded Markdown skills and can clear the shared
chat history. Clearing chat does not delete uploads or files in `agent_workspace/`.

Use a model that supports tool calling, such as a current Qwen model. Models that
do not support Ollama tool calls may still answer directly, but will not be able
to use the agent tools reliably.

## Agent skills and tools

The agent can:

- list, read, and write files under `agent_workspace/`;
- run guarded, non-interactive commands from that workspace;
- search the public web and fetch readable page text;
- make several tool decisions before returning its final answer.

The agent's core policy is editable in `base_instructions.md`. Additional skill
playbooks live under `skills/`; every Markdown file in that directory is loaded
into the system instructions. Tool activity is visible under each agent response.

This is bounded autonomy, not a hardened OS sandbox. Shell syntax, destructive
commands, installers, Git, and common network commands are blocked, and execution
has time/output limits. For untrusted users, run the application in an isolated
container or VM with no secrets and only the intended workspace mounted.

## Configuration

- `DATABASE_PATH`: SQLite database path. Defaults to `chat.db` in the project.
- `OLLAMA_URL`: Ollama server URL. Defaults to `http://localhost:11434`.
- `OLLAMA_MODEL`: default model name. Defaults to `llama3.2`.
- `OLLAMA_TIMEOUT`: request timeout in seconds. Defaults to `120`.
- `AGENT_MAX_STEPS`: maximum model/tool loop iterations. Defaults to `8`.
- `AGENT_WORKSPACE`: directory available to file and command tools.
- `BASE_INSTRUCTIONS_FILE`: path to the main agent instruction file.
- `SKILLS_DIR`: directory containing Markdown skill playbooks.
- `SEARXNG_URL`: SearXNG base URL used by the web-search tool. Defaults to
  `http://192.168.1.249:8081`; set it to an empty value to use the public fallback.

When Flask runs in Docker and Ollama runs on the host, set `OLLAMA_URL` to
`http://host.docker.internal:11434`. The image stores SQLite data in
`/app/data/chat.db`; mount `/app/data` if the database should survive container
replacement.

The agent loop follows Ollama's native tool-calling API:
<https://docs.ollama.com/capabilities/tool-calling>.

SearXNG must allow JSON responses from `/search?format=json`. If Flask runs in
Docker, the container must be able to route to the configured LAN address.
