import json
import os
import re
import sqlite3
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree
from uuid import uuid4
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
from werkzeug.utils import secure_filename

from agent import run_agent


app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")
FLASK_HOST = os.environ.get("FLASK_HOST", "0.0.0.0")
FLASK_PORT = int(os.environ.get("FLASK_PORT", "5000"))
DATABASE = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "chat.db"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "120"))
OLLAMA_NUM_CTX = os.environ.get("OLLAMA_NUM_CTX")
AGENT_MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "8"))
AGENT_CONTEXT_MESSAGES = int(os.environ.get("AGENT_CONTEXT_MESSAGES", "100"))
MAX_UPLOAD_DOCUMENTS = int(os.environ.get("MAX_UPLOAD_DOCUMENTS", "5"))
DOCUMENT_TEXT_LIMIT = int(os.environ.get("DOCUMENT_TEXT_LIMIT", "20000"))
AGENT_WORKSPACE = os.environ.get(
    "AGENT_WORKSPACE", os.path.join(BASE_DIR, "agent_workspace")
)
BASE_INSTRUCTIONS_FILE = os.environ.get(
    "BASE_INSTRUCTIONS_FILE", os.path.join(BASE_DIR, "base_instructions.md")
)
ORCHESTRATOR_INSTRUCTIONS_FILE = os.environ.get(
    "ORCHESTRATOR_INSTRUCTIONS_FILE",
    os.path.join(BASE_DIR, "orchestrator_instructions.md"),
)
SKILLS_DIR = os.environ.get("SKILLS_DIR", os.path.join(BASE_DIR, "skills"))

app.secret_key = SECRET_KEY  # required for sessions

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(AGENT_WORKSPACE, exist_ok=True)

AGENT_STATUS_LOCK = threading.Lock()
AGENT_STATUS = {
    "active": False,
    "state": "idle",
    "message": "Agent is idle.",
    "step": None,
    "tool": None,
    "arguments": {},
    "events": [],
    "jobs": {},
    "updated_at": None,
}


def set_agent_status(job_id, **updates):
    with AGENT_STATUS_LOCK:
        reset_events = updates.pop("reset_events", False)
        message = updates.get("message")
        jobs = AGENT_STATUS.setdefault("jobs", {})
        job = jobs.setdefault(
            job_id,
            {
                "id": job_id,
                "title": "Agent job",
                "active": False,
                "state": "queued",
                "message": "Agent job queued.",
                "step": None,
                "tool": None,
                "arguments": {},
                "events": [],
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "updated_at": None,
            },
        )
        events = [] if reset_events else list(job.get("events", []))
        if message:
            events.append(
                {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "message": message,
                }
            )
        events = events[-8:]
        job.update(
            {
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "events": events,
                **updates,
            }
        )
        active_jobs = [item for item in jobs.values() if item.get("active")]
        visible_jobs = sorted(
            jobs.values(), key=lambda item: item.get("updated_at") or "", reverse=True
        )[:10]
        current = active_jobs[-1] if active_jobs else (visible_jobs[0] if visible_jobs else None)
        AGENT_STATUS.update(
            {
                "active": bool(active_jobs),
                "state": current.get("state", "idle") if current else "idle",
                "message": current.get("message", "Agent is idle.") if current else "Agent is idle.",
                "step": current.get("step") if current else None,
                "tool": current.get("tool") if current else None,
                "arguments": current.get("arguments", {}) if current else {},
                "events": current.get("events", []) if current else [],
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        )


def get_agent_status():
    with AGENT_STATUS_LOCK:
        status = dict(AGENT_STATUS)
        jobs = sorted(
            AGENT_STATUS.get("jobs", {}).values(),
            key=lambda item: item.get("updated_at") or item.get("created_at") or "",
            reverse=True,
        )
        status["jobs"] = [dict(job) for job in jobs[:10]]
        return status


def get_db():
    connection = sqlite3.connect(DATABASE, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    database_dir = os.path.dirname(os.path.abspath(DATABASE))
    os.makedirs(database_dir, exist_ok=True)

    connection = get_db()
    try:
        connection.execute("PRAGMA journal_mode=WAL")
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
                document_text TEXT,
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
        if "document_text" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN document_text TEXT")
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
    document_text=None,
):
    connection = get_db()
    try:
        cursor = connection.execute(
            """
            INSERT INTO messages (user, text, file, filename, role, trace, document_text)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user,
                text,
                file_path,
                original_name,
                role,
                json.dumps(trace) if trace else None,
                document_text,
            ),
        )
        connection.commit()
        return cursor.lastrowid
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


def as_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.startswith("["):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        return parsed if isinstance(parsed, list) else [value]
    return [value]


def encode_attachment_values(values):
    values = [value for value in values if value]
    if not values:
        return None
    return values[0] if len(values) == 1 else json.dumps(values)


def message_attachments(message):
    files = as_list(message.get("file"))
    filenames = as_list(message.get("filename"))
    attachments = []
    for index, file_path in enumerate(files):
        if not file_path:
            continue
        filename = filenames[index] if index < len(filenames) else file_path
        attachments.append(
            {
                "file": file_path,
                "filename": filename,
                "upload_url": url_for("uploads", filename=file_path),
            }
        )
    return attachments


def prepare_message(message):
    prepared = dict(message)
    attachments = message_attachments(prepared)
    prepared["attachments"] = attachments
    prepared["upload_url"] = attachments[0]["upload_url"] if attachments else None
    return prepared


def prepare_messages(messages):
    return [prepare_message(message) for message in messages]


def clean_extracted_text(value):
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in value.splitlines())).strip()


def truncate_document_text(value):
    value = clean_extracted_text(value)
    if len(value) <= DOCUMENT_TEXT_LIMIT:
        return value
    return f"{value[:DOCUMENT_TEXT_LIMIT]}\n...[document text truncated]"


def extract_text_file(path):
    data = Path(path).read_bytes()[: DOCUMENT_TEXT_LIMIT * 2]
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return truncate_document_text(data.decode(encoding))
        except UnicodeDecodeError:
            continue
    return "[Could not decode this text-like file.]"


def extract_docx(path):
    parts = []
    with zipfile.ZipFile(path) as archive:
        names = [
            name
            for name in archive.namelist()
            if name.startswith("word/")
            and name.endswith(".xml")
            and ("document" in name or "header" in name or "footer" in name)
        ]
        for name in names:
            root = ElementTree.fromstring(archive.read(name))
            texts = [
                element.text
                for element in root.iter()
                if element.tag.endswith("}t") and element.text
            ]
            if texts:
                parts.append(" ".join(texts))
    return truncate_document_text("\n\n".join(parts))


def extract_xlsx(path):
    rows = []
    shared_strings = []
    with zipfile.ZipFile(path) as archive:
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.iter():
                if item.tag.endswith("}si"):
                    shared_strings.append(
                        " ".join(
                            text.text or ""
                            for text in item.iter()
                            if text.tag.endswith("}t")
                        ).strip()
                    )

        sheet_names = [
            name
            for name in archive.namelist()
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        ]
        for sheet_name in sheet_names[:10]:
            root = ElementTree.fromstring(archive.read(sheet_name))
            rows.append(f"Sheet: {Path(sheet_name).stem}")
            for row in root.iter():
                if not row.tag.endswith("}row"):
                    continue
                cells = []
                for cell in row:
                    if not cell.tag.endswith("}c"):
                        continue
                    cell_type = cell.attrib.get("t")
                    value = next(
                        (child.text for child in cell if child.tag.endswith("}v")),
                        "",
                    )
                    if cell_type == "s" and value.isdigit():
                        value = shared_strings[int(value)] if int(value) < len(shared_strings) else value
                    if value:
                        cells.append(value)
                if cells:
                    rows.append(" | ".join(cells))
    return truncate_document_text("\n".join(rows))


def extract_pdf(path):
    try:
        from pypdf import PdfReader
    except ImportError:
        return "[PDF text extraction requires the optional pypdf package.]"

    reader = PdfReader(path)
    pages = []
    for index, page in enumerate(reader.pages[:50], start=1):
        pages.append(f"Page {index}\n{page.extract_text() or ''}")
    return truncate_document_text("\n\n".join(pages))


def extract_document_text(path, original_name):
    extension = Path(original_name or path).suffix.lower()
    try:
        if extension == ".pdf":
            return extract_pdf(path)
        if extension == ".docx":
            return extract_docx(path)
        if extension == ".xlsx":
            return extract_xlsx(path)
        return extract_text_file(path)
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError, RuntimeError) as error:
        return f"[Could not extract text from {original_name}: {error}]"


def build_document_context(document_texts):
    sections = []
    for filename, text in document_texts:
        if text:
            sections.append(f"## Attached document: {filename}\n{text}")
    if not sections:
        return None
    return "Attached document text for analysis:\n\n" + "\n\n".join(sections)


def build_ollama_history():
    history = []

    for message in get_messages(limit=AGENT_CONTEXT_MESSAGES):
        text = (message["text"] or "").strip()
        filenames = as_list(message.get("filename"))
        if filenames:
            attachment = "[Attached files: " + ", ".join(filenames) + "]"
            text = f"{text}\n{attachment}".strip()
        if message.get("document_text"):
            text = f"{text}\n\n{message['document_text']}".strip()
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


def format_chat_context_message(messages):
    lines = [
        "Chat window context for the current request:",
        "Use this transcript to understand what has already been discussed, "
        "what the user has asked for previously, and any assistant answers "
        "already shown in the chat.",
        "",
    ]

    for index, message in enumerate(messages, start=1):
        text = (message["text"] or "").strip()
        filenames = as_list(message.get("filename"))
        speaker = "Ollama Agent" if message["role"] == "assistant" else message["user"]

        parts = []
        if text:
            parts.append(text)
        if filenames:
            parts.append("Attached files: " + ", ".join(filenames))
        if message.get("document_text"):
            parts.append("Extracted document text is available in this chat context.")
        if not parts:
            continue

        lines.append(f"{index}. {speaker}: " + "\n   ".join(parts))

    return "\n".join(lines).strip()


def build_agent_context_history():
    messages = get_messages(limit=AGENT_CONTEXT_MESSAGES)
    if not messages:
        return []

    context = format_chat_context_message(messages)
    history = build_ollama_history()
    if not context:
        return history

    return [{"role": "user", "content": context}, *history]


def ollama_chat(model, messages, tools=None):
    options = {}
    if OLLAMA_NUM_CTX:
        options["num_ctx"] = int(OLLAMA_NUM_CTX)

    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "stream": False,
            **({"options": options} if options else {}),
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


def call_agent(model, messages, job_id):
    set_agent_status(
        job_id,
        active=True,
        state="starting",
        message="Agent is starting.",
        step=None,
        tool=None,
        arguments={},
        reset_events=True,
    )

    def update_status(event):
        set_agent_status(
            job_id,
            active=event.get("state") not in {"complete", "error"},
            **event,
        )

    try:
        result = run_agent(
            model=model,
            messages=messages,
            ollama_chat=ollama_chat,
            workspace=AGENT_WORKSPACE,
            base_instructions_file=BASE_INSTRUCTIONS_FILE,
            orchestrator_instructions_file=ORCHESTRATOR_INSTRUCTIONS_FILE,
            skills_dir=SKILLS_DIR,
            max_steps=AGENT_MAX_STEPS,
            status_callback=update_status,
        )
        set_agent_status(
            job_id,
            active=False,
            state="complete",
            message="Agent finished.",
            tool=None,
            arguments={},
        )
        return result
    except OSError as error:
        set_agent_status(
            job_id,
            active=False,
            state="error",
            message=f"Agent configuration error: {error}",
            tool=None,
            arguments={},
        )
        raise RuntimeError(f"Agent configuration error: {error}") from error
    except RuntimeError as error:
        set_agent_status(
            job_id,
            active=False,
            state="error",
            message=str(error),
            tool=None,
            arguments={},
        )
        raise


def run_agent_job(job_id, model, messages):
    try:
        reply, trace = call_agent(model, messages, job_id)
        add_message(
            "Ollama Agent",
            reply,
            role="assistant",
            trace=trace,
        )
    except RuntimeError as error:
        add_message(
            "Ollama Agent",
            f"Agent error: {error}",
            role="assistant",
        )


def summarize_agent_job_title(text=None, filename=None, limit=48):
    source = (text or "").strip()
    if not source and filename:
        source = f"Review {filename}"
    if not source:
        return "Agent job"

    source = " ".join(source.split())
    source = source.strip(" \t\r\n\"'")
    if len(source) <= limit:
        return source

    shortened = source[: limit - 1].rsplit(" ", 1)[0].strip()
    return f"{shortened or source[: limit - 1]}…"


def start_agent_job(model, messages, title=None):
    job_id = uuid4().hex[:8]
    set_agent_status(
        job_id,
        title=title or "Agent job",
        active=True,
        state="queued",
        message="Agent job queued.",
        step=None,
        tool=None,
        arguments={},
        reset_events=True,
    )
    thread = threading.Thread(
        target=run_agent_job,
        args=(job_id, model, messages),
        daemon=True,
        name=f"agent-job-{job_id}",
    )
    if app.config.get("TESTING_SYNC_AGENT"):
        run_agent_job(job_id, model, messages)
    else:
        thread.start()
    return job_id


@app.route("/", methods=["GET", "POST"])
def index():
    ollama_error = None
    agent_job_id = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()

        if username:
            session["username"] = username

        username = session.get("username", "Anon")
        text = request.form.get("message", "").strip()
        uploaded_files = [
            file
            for file in request.files.getlist("files") + request.files.getlist("file")
            if file and file.filename
        ]
        ask_ollama = request.form.get("ask_ollama") == "on"
        ollama_model = request.form.get("ollama_model", OLLAMA_MODEL).strip() or OLLAMA_MODEL
        session["ollama_model"] = ollama_model

        file_paths = []
        original_names = []
        document_texts = []

        if len(uploaded_files) > MAX_UPLOAD_DOCUMENTS:
            ollama_error = f"Please attach no more than {MAX_UPLOAD_DOCUMENTS} documents at once."

        if not ollama_error:
            for file in uploaded_files[:MAX_UPLOAD_DOCUMENTS]:
                original_name = file.filename
                filename = f"{datetime.now().timestamp()}_{secure_filename(original_name)}"
                path = os.path.join(UPLOAD_FOLDER, filename)
                file.save(path)
                file_paths.append(filename)
                original_names.append(original_name)
                document_texts.append((original_name, extract_document_text(path, original_name)))

        if not ollama_error and (text or file_paths):
            add_message(
                username,
                text,
                encode_attachment_values(file_paths),
                encode_attachment_values(original_names),
                document_text=build_document_context(document_texts),
            )

            if ask_ollama:
                agent_job_id = start_agent_job(
                    ollama_model,
                    build_agent_context_history(),
                    summarize_agent_job_title(
                        text,
                        original_names[0] if original_names else None,
                    ),
                )

        if request.headers.get("X-Requested-With") == "fetch":
            if ollama_error:
                return jsonify({"ok": False, "error": ollama_error}), 500
            return jsonify({"ok": True, "agent_job_id": agent_job_id, **get_chat_state()})

        if not ollama_error:
            return redirect(url_for("index"))

    chat_state = get_chat_state()
    return render_template(
        "index.html",
        messages=prepare_messages(get_messages()),
        skills=get_available_skills(),
        clear_version=chat_state["clear_version"],
        username=session.get("username", ""),
        ollama_model=session.get("ollama_model", OLLAMA_MODEL),
        ollama_error=ollama_error,
    )


@app.route("/uploads/<path:filename>")
def uploads(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route("/agent_outputs/<path:filename>")
def agent_outputs(filename):
    return send_from_directory(os.path.join(AGENT_WORKSPACE, "outputs"), filename)


@app.route("/api/messages")
def messages_api():
    try:
        after_id = max(0, int(request.args.get("after_id", "0")))
    except ValueError:
        return jsonify({"error": "after_id must be an integer"}), 400

    messages = get_messages_after(after_id)
    messages = prepare_messages(messages)
    return jsonify({"messages": messages, **get_chat_state()})


@app.route("/api/skills")
def skills_api():
    return jsonify({"skills": get_available_skills()})


@app.route("/api/agent/status")
def agent_status_api():
    return jsonify(get_agent_status())


@app.route("/api/shutdown", methods=["POST"])
def shutdown_api():
    if app.config.get("TESTING"):
        return jsonify({"shutting_down": True})

    shutdown = request.environ.get("werkzeug.server.shutdown")
    if shutdown:
        shutdown()
    else:
        threading.Timer(0.25, lambda: os._exit(0)).start()
    return jsonify({"shutting_down": True})


@app.route("/api/messages/clear", methods=["POST"])
def clear_messages_api():
    clear_messages()
    return jsonify({"cleared": True, **get_chat_state()})


init_db()


if __name__ == "__main__":
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=True)
