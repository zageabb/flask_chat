import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

from agent import run_agent


app = Flask(__name__)
app.secret_key = "supersecretkey"  # required for sessions

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
DATABASE = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "chat.db"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "120"))
AGENT_MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "8"))
AGENT_WORKSPACE = os.environ.get(
    "AGENT_WORKSPACE", os.path.join(BASE_DIR, "agent_workspace")
)
BASE_INSTRUCTIONS_FILE = os.environ.get(
    "BASE_INSTRUCTIONS_FILE", os.path.join(BASE_DIR, "base_instructions.md")
)
SKILLS_DIR = os.environ.get("SKILLS_DIR", os.path.join(BASE_DIR, "skills"))

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(AGENT_WORKSPACE, exist_ok=True)


def get_db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    database_dir = os.path.dirname(os.path.abspath(DATABASE))
    os.makedirs(database_dir, exist_ok=True)

    connection = get_db()
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT NOT NULL,
                text TEXT,
                file TEXT,
                filename TEXT,
                role TEXT NOT NULL DEFAULT 'user',
                trace TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "trace" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN trace TEXT")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                clear_version INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO chat_state (id, clear_version) VALUES (1, 0)"
        )
        connection.commit()
    finally:
        connection.close()


def add_message(
    user,
    text=None,
    file_path=None,
    original_name=None,
    role="user",
    trace=None,
):
    connection = get_db()
    try:
        connection.execute(
            """
            INSERT INTO messages (user, text, file, filename, role, trace)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user,
                text,
                file_path,
                original_name,
                role,
                json.dumps(trace) if trace else None,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def get_messages(limit=None):
    query = "SELECT * FROM messages ORDER BY id"
    parameters = ()

    if limit:
        query = """
            SELECT * FROM (
                SELECT * FROM messages ORDER BY id DESC LIMIT ?
            ) ORDER BY id
        """
        parameters = (limit,)

    connection = get_db()
    try:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]
    finally:
        connection.close()


def get_messages_after(message_id):
    connection = get_db()
    try:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM messages WHERE id > ? ORDER BY id",
                (message_id,),
            ).fetchall()
        ]
    finally:
        connection.close()


def get_chat_state():
    connection = get_db()
    try:
        row = connection.execute(
            """
            SELECT COUNT(*) AS message_count, COALESCE(MAX(id), 0) AS latest_id
            FROM messages
            """
        ).fetchone()
        state = dict(row)
        state["clear_version"] = connection.execute(
            "SELECT clear_version FROM chat_state WHERE id = 1"
        ).fetchone()["clear_version"]
        return state
    finally:
        connection.close()


def clear_messages():
    connection = get_db()
    try:
        connection.execute("DELETE FROM messages")
        connection.execute(
            "UPDATE chat_state SET clear_version = clear_version + 1 WHERE id = 1"
        )
        connection.commit()
    finally:
        connection.close()


def get_available_skills():
    skills = []
    skills_path = Path(SKILLS_DIR)
    if not skills_path.is_dir():
        return skills

    for skill_file in sorted(skills_path.glob("*.md")):
        content = skill_file.read_text(encoding="utf-8")
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        heading = next(
            (line.lstrip("#").strip() for line in lines if line.startswith("#")),
            skill_file.stem.replace("_", " ").title(),
        )
        description = next(
            (line for line in lines if not line.startswith("#")),
            "No description provided.",
        )
        skills.append(
            {
                "name": heading,
                "slug": skill_file.stem,
                "description": description,
            }
        )
    return skills


def build_ollama_history():
    history = []

    for message in get_messages(limit=50):
        text = (message["text"] or "").strip()
        if message["filename"]:
            attachment = f"[Attached file: {message['filename']}]"
            text = f"{text}\n{attachment}".strip()
        if not text:
            continue

        if message["role"] == "assistant":
            history.append({"role": "assistant", "content": text})
        else:
            history.append(
                {
                    "role": "user",
                    "content": f"{message['user']}: {text}",
                }
            )

    return history


def ollama_chat(model, messages, tools=None):
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "stream": False,
            **({"tools": tools} if tools else {}),
        }
    ).encode("utf-8")
    ollama_request = Request(
        f"{OLLAMA_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(ollama_request, timeout=OLLAMA_TIMEOUT) as response:
            result = json.load(response)
    except HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama returned HTTP {error.code}: {details}") from error
    except URLError as error:
        raise RuntimeError(f"Could not connect to Ollama at {OLLAMA_URL}: {error.reason}") from error

    message = result.get("message", {})
    if not isinstance(message, dict):
        raise RuntimeError("Ollama returned an invalid chat response.")
    return message


def call_ollama(model):
    messages = [
        {
            "role": "system",
            "content": (
                "You are Ollama, a friendly third participant in a group chat. "
                "Respond naturally to the conversation. Human messages are prefixed "
                "with their display name."
            ),
        },
        *build_ollama_history(),
    ]
    content = (ollama_chat(model, messages).get("content") or "").strip()
    if not content:
        raise RuntimeError("Ollama returned an empty response.")
    return content


def call_agent(model):
    try:
        return run_agent(
            model=model,
            messages=build_ollama_history(),
            ollama_chat=ollama_chat,
            workspace=AGENT_WORKSPACE,
            base_instructions_file=BASE_INSTRUCTIONS_FILE,
            skills_dir=SKILLS_DIR,
            max_steps=AGENT_MAX_STEPS,
        )
    except OSError as error:
        raise RuntimeError(f"Agent configuration error: {error}") from error


@app.route("/", methods=["GET", "POST"])
def index():
    ollama_error = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()

        if username:
            session["username"] = username

        username = session.get("username", "Anon")
        text = request.form.get("message", "").strip()
        file = request.files.get("file")
        ask_ollama = request.form.get("ask_ollama") == "on"
        agent_mode = request.form.get("agent_mode") == "on"
        ollama_model = request.form.get("ollama_model", OLLAMA_MODEL).strip() or OLLAMA_MODEL
        session["ollama_model"] = ollama_model

        file_path = None
        original_name = None

        if file and file.filename:
            filename = f"{datetime.now().timestamp()}_{file.filename}"
            path = os.path.join(UPLOAD_FOLDER, filename)
            file.save(path)
            file_path = filename
            original_name = file.filename

        if text or file_path:
            add_message(username, text, file_path, original_name)

            if ask_ollama:
                try:
                    if agent_mode:
                        reply, trace = call_agent(ollama_model)
                        add_message(
                            "Ollama Agent",
                            reply,
                            role="assistant",
                            trace=trace,
                        )
                    else:
                        reply = call_ollama(ollama_model)
                        add_message("Ollama", reply, role="assistant")
                except RuntimeError as error:
                    ollama_error = str(error)

        if not ollama_error:
            return redirect(url_for("index"))

    chat_state = get_chat_state()
    return render_template(
        "index.html",
        messages=get_messages(),
        skills=get_available_skills(),
        clear_version=chat_state["clear_version"],
        username=session.get("username", ""),
        ollama_model=session.get("ollama_model", OLLAMA_MODEL),
        ollama_error=ollama_error,
    )


@app.route("/uploads/<path:filename>")
def uploads(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route("/api/messages")
def messages_api():
    try:
        after_id = max(0, int(request.args.get("after_id", "0")))
    except ValueError:
        return jsonify({"error": "after_id must be an integer"}), 400

    messages = get_messages_after(after_id)
    for message in messages:
        message["upload_url"] = (
            url_for("uploads", filename=message["file"]) if message["file"] else None
        )
    return jsonify({"messages": messages, **get_chat_state()})


@app.route("/api/skills")
def skills_api():
    return jsonify({"skills": get_available_skills()})


@app.route("/api/messages/clear", methods=["POST"])
def clear_messages_api():
    clear_messages()
    return jsonify({"cleared": True, **get_chat_state()})


init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
