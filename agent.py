import html
import json
import os
import re
import shlex
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import Request, urlopen


MAX_FILE_BYTES = 200_000
MAX_TOOL_OUTPUT = 12_000
BLOCKED_COMMANDS = {
    "chmod",
    "chown",
    "curl",
    "dd",
    "docker",
    "git",
    "kill",
    "killall",
    "mount",
    "nc",
    "netcat",
    "npm",
    "pip",
    "pip3",
    "pkill",
    "rm",
    "shutdown",
    "ssh",
    "sudo",
    "wget",
}


class SearchResultParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self.current_url = None
        self.current_text = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "a" and "result__a" in attributes.get("class", ""):
            self.current_url = attributes.get("href")
            self.current_text = []

    def handle_data(self, data):
        if self.current_url:
            self.current_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.current_url:
            self.results.append(
                {
                    "title": " ".join("".join(self.current_text).split()),
                    "url": clean_search_url(self.current_url),
                }
            )
            self.current_url = None
            self.current_text = []


def clean_search_url(url):
    parsed = urlparse(url)
    target = parse_qs(parsed.query).get("uddg")
    return unquote(target[0]) if target else url


def truncate(value, limit=MAX_TOOL_OUTPUT):
    value = str(value)
    if len(value) <= limit:
        return value
    return f"{value[:limit]}\n...[truncated]"


def safe_path(workspace, relative_path):
    root = Path(workspace).resolve()
    candidate = (root / relative_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("Path must stay inside the agent workspace.")
    return candidate


def list_files(workspace, path="."):
    target = safe_path(workspace, path)
    if not target.exists():
        return f"Path does not exist: {path}"
    if target.is_file():
        return str(target.relative_to(Path(workspace).resolve()))

    entries = []
    for item in sorted(target.rglob("*")):
        if len(entries) >= 500:
            entries.append("...[file listing truncated]")
            break
        relative = item.relative_to(Path(workspace).resolve())
        entries.append(f"{relative}/" if item.is_dir() else str(relative))
    return "\n".join(entries) or "(empty workspace)"


def read_file(workspace, path):
    target = safe_path(workspace, path)
    if not target.is_file():
        return f"File does not exist: {path}"
    if target.stat().st_size > MAX_FILE_BYTES:
        return f"File is too large to read ({target.stat().st_size} bytes)."
    return truncate(target.read_text(encoding="utf-8", errors="replace"))


def write_file(workspace, path, content):
    target = safe_path(workspace, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        return f"Refused: content exceeds {MAX_FILE_BYTES} bytes."
    target.write_text(content, encoding="utf-8")
    return f"Wrote {len(encoded)} bytes to {target.relative_to(Path(workspace).resolve())}"


def run_command(workspace, command):
    try:
        arguments = shlex.split(command)
    except ValueError as error:
        return f"Invalid command: {error}"
    if not arguments:
        return "No command supplied."

    executable = Path(arguments[0]).name.lower()
    if executable in BLOCKED_COMMANDS:
        return f"Command blocked by policy: {executable}"
    if any(
        token in command
        for token in ("&&", "||", ";", "|", ">", "<", "`", "$(", "\n", "\r")
    ):
        return "Shell operators and redirections are not allowed."
    if executable in {"python", "python3", "node"} and any(
        flag in arguments for flag in ("-c", "-e", "--eval")
    ):
        return "Inline code execution is not allowed; write a workspace file first."

    try:
        completed = subprocess.run(
            arguments,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=20,
            shell=False,
            env={
                "HOME": workspace,
                "PATH": os.environ.get("PATH", ""),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
    except FileNotFoundError:
        return f"Command not found: {arguments[0]}"
    except subprocess.TimeoutExpired:
        return "Command timed out after 20 seconds."

    output = "\n".join(
        part for part in (completed.stdout.strip(), completed.stderr.strip()) if part
    )
    return truncate(f"Exit code: {completed.returncode}\n{output or '(no output)'}")


def http_get(url, timeout=15):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http and https URLs are allowed.")
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 FlaskChatAgent/1.0"})
    with urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "")
        body = response.read(MAX_FILE_BYTES).decode("utf-8", errors="replace")
    return content_type, body


def web_search(query, max_results=5):
    max_results = max(1, min(int(max_results), 10))
    _, body = http_get(f"https://html.duckduckgo.com/html/?q={quote_plus(query)}")
    parser = SearchResultParser()
    parser.feed(body)
    results = parser.results[:max_results]
    if not results:
        return "No search results found."
    return json.dumps(results, indent=2)


def fetch_url(url):
    content_type, body = http_get(url)
    if "html" in content_type:
        body = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", body)
        body = re.sub(r"(?s)<[^>]+>", " ", body)
        body = html.unescape(body)
        body = " ".join(body.split())
    return truncate(body)


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files inside the private agent workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file from the private agent workspace.",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {"path": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or replace a text file in the private agent workspace.",
            "parameters": {
                "type": "object",
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run one non-interactive command in the private agent workspace. "
                "Shell operators, package installation, networking commands, git, and "
                "destructive commands are blocked."
            ),
            "parameters": {
                "type": "object",
                "required": ["command"],
                "properties": {"command": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the public internet and return result titles and URLs.",
            "parameters": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "default": 5},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch and extract readable text from an HTTP or HTTPS URL.",
            "parameters": {
                "type": "object",
                "required": ["url"],
                "properties": {"url": {"type": "string"}},
            },
        },
    },
]


def execute_tool(name, arguments, workspace):
    tools = {
        "list_files": lambda: list_files(workspace, arguments.get("path", ".")),
        "read_file": lambda: read_file(workspace, arguments["path"]),
        "write_file": lambda: write_file(workspace, arguments["path"], arguments["content"]),
        "run_command": lambda: run_command(workspace, arguments["command"]),
        "web_search": lambda: web_search(
            arguments["query"], arguments.get("max_results", 5)
        ),
        "fetch_url": lambda: fetch_url(arguments["url"]),
    }
    if name not in tools:
        return f"Unknown tool: {name}"
    try:
        return tools[name]()
    except (KeyError, TypeError, ValueError) as error:
        return f"Invalid tool arguments: {error}"
    except (HTTPError, URLError, TimeoutError) as error:
        return f"Network tool failed: {error}"


def load_agent_instructions(base_instructions_file, skills_dir, workspace):
    base = Path(base_instructions_file).read_text(encoding="utf-8")
    skill_sections = []
    for skill_file in sorted(Path(skills_dir).glob("*.md")):
        skill_sections.append(
            f"\n## Skill: {skill_file.stem}\n{skill_file.read_text(encoding='utf-8')}"
        )
    return (
        f"{base.strip()}\n\n"
        f"Your private workspace is {Path(workspace).resolve()}. "
        "All file and command tools are confined there.\n"
        + "\n".join(skill_sections)
    )


def run_agent(
    model,
    messages,
    ollama_chat,
    workspace,
    base_instructions_file,
    skills_dir,
    max_steps=8,
):
    agent_messages = [
        {
            "role": "system",
            "content": load_agent_instructions(
                base_instructions_file, skills_dir, workspace
            ),
        },
        *messages,
    ]
    trace = []

    for step in range(1, max_steps + 1):
        response_message = ollama_chat(model, agent_messages, TOOL_SCHEMAS)
        agent_messages.append(response_message)
        tool_calls = response_message.get("tool_calls") or []
        if not tool_calls:
            content = (response_message.get("content") or "").strip()
            return content or "The agent completed without a text response.", trace

        for tool_call in tool_calls:
            function = tool_call.get("function", {})
            name = function.get("name", "")
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            result = execute_tool(name, arguments, workspace)
            trace.append(
                {
                    "step": step,
                    "tool": name,
                    "arguments": arguments,
                    "result": truncate(result, 1_000),
                }
            )
            agent_messages.append(
                {"role": "tool", "tool_name": name, "content": str(result)}
            )

    agent_messages.append(
        {
            "role": "user",
            "content": (
                "You have reached the tool-step limit. Stop using tools and give the "
                "best final answer possible from the work completed."
            ),
        }
    )
    final_message = ollama_chat(model, agent_messages, [])
    return (
        (final_message.get("content") or "Agent stopped at its step limit.").strip(),
        trace,
    )
