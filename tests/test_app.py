from io import BytesIO
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import ANY, patch

import app as chat_app


class ChatAppTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        chat_app.DATABASE = str(Path(self.temp_dir.name) / "test.db")
        chat_app.UPLOAD_FOLDER = str(Path(self.temp_dir.name) / "uploads")
        chat_app.SKILLS_DIR = str(Path(self.temp_dir.name) / "skills")
        chat_app.AGENT_WORKSPACE = str(Path(self.temp_dir.name) / "agent_workspace")
        Path(chat_app.UPLOAD_FOLDER).mkdir()
        Path(chat_app.SKILLS_DIR).mkdir()
        Path(chat_app.AGENT_WORKSPACE, "outputs").mkdir(parents=True)
        chat_app.init_db()
        chat_app.app.config.update(
            TESTING=True,
            SECRET_KEY="test",
            TESTING_SYNC_AGENT=True,
        )
        with chat_app.AGENT_STATUS_LOCK:
            chat_app.AGENT_STATUS["jobs"] = {}
            chat_app.AGENT_STATUS.update(
                {
                    "active": False,
                    "state": "idle",
                    "message": "Agent is idle.",
                    "step": None,
                    "tool": None,
                    "arguments": {},
                    "events": [],
                    "updated_at": None,
                }
            )
        self.client = chat_app.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_message_is_persisted_in_sqlite(self):
        response = self.client.post(
            "/",
            data={"username": "Alice", "message": "Hello"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Hello", response.data)
        self.assertEqual(chat_app.get_messages()[0]["user"], "Alice")

    def test_messages_api_returns_only_newer_messages(self):
        chat_app.add_message("Alice", "First")
        first_id = chat_app.get_messages()[0]["id"]
        chat_app.add_message("Bob", "Second", file_path="example.txt", original_name="example.txt")

        response = self.client.get(f"/api/messages?after_id={first_id}")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["user"], "Bob")
        self.assertEqual(payload["messages"][0]["upload_url"], "/uploads/example.txt")
        self.assertEqual(payload["message_count"], 2)
        self.assertEqual(payload["latest_id"], payload["messages"][0]["id"])

    def test_messages_api_returns_multiple_attachments(self):
        chat_app.add_message(
            "Alice",
            "Docs",
            file_path='["one.txt", "two.txt"]',
            original_name='["one.txt", "two.txt"]',
        )

        response = self.client.get("/api/messages?after_id=0")

        self.assertEqual(response.status_code, 200)
        attachments = response.get_json()["messages"][0]["attachments"]
        self.assertEqual(len(attachments), 2)
        self.assertEqual(attachments[0]["upload_url"], "/uploads/one.txt")

    def test_messages_api_rejects_invalid_message_id(self):
        response = self.client.get("/api/messages?after_id=not-a-number")

        self.assertEqual(response.status_code, 400)

    def test_clear_chat_removes_messages(self):
        chat_app.add_message("Alice", "Hello")

        response = self.client.post("/api/messages/clear")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["cleared"])
        self.assertEqual(response.get_json()["clear_version"], 1)
        self.assertEqual(chat_app.get_messages(), [])

        second_response = self.client.post("/api/messages/clear")
        self.assertEqual(second_response.get_json()["clear_version"], 2)

    def test_shutdown_api_reports_shutdown_in_testing(self):
        response = self.client.post("/api/shutdown")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["shutting_down"])

    def test_agent_outputs_route_serves_generated_files(self):
        Path(chat_app.AGENT_WORKSPACE, "outputs", "report.md").write_text(
            "# Report", encoding="utf-8"
        )

        response = self.client.get("/agent_outputs/report.md")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"# Report", response.data)
        response.close()

    def test_skills_api_lists_markdown_skills(self):
        Path(chat_app.SKILLS_DIR, "coding.md").write_text(
            "# Coding skill\n\nWrite and verify code safely.",
            encoding="utf-8",
        )

        response = self.client.get("/api/skills")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["skills"],
            [
                {
                    "name": "Coding skill",
                    "slug": "coding",
                    "description": "Write and verify code safely.",
                }
            ],
        )

    def test_agent_status_api_reports_current_state(self):
        chat_app.set_agent_status(
            "job123",
            title="Search Ollama",
            active=True,
            state="running_tool",
            message="Searching the web.",
            tool="web_search",
            arguments={"query": "ollama"},
        )

        response = self.client.get("/api/agent/status")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["active"])
        self.assertEqual(payload["tool"], "web_search")
        self.assertEqual(payload["arguments"]["query"], "ollama")
        self.assertEqual(payload["events"][-1]["message"], "Searching the web.")
        self.assertEqual(payload["jobs"][0]["id"], "job123")
        self.assertEqual(payload["jobs"][0]["title"], "Search Ollama")

    def test_agent_job_title_is_summarized_from_request(self):
        title = chat_app.summarize_agent_job_title(
            "Please search the web and tell me what Hitachi Energy does in simple terms"
        )

        self.assertLessEqual(len(title), 48)
        self.assertTrue(title.startswith("Please search the web"))

    def test_agent_job_title_uses_filename_when_message_is_empty(self):
        self.assertEqual(
            chat_app.summarize_agent_job_title("", "report.pdf"),
            "Review report.pdf",
        )

    @patch("app.call_agent", return_value=("Hello Alice", []))
    def test_ollama_reply_uses_orchestrator(self, call_agent):
        response = self.client.post(
            "/",
            data={
                "username": "Alice",
                "message": "Hello Ollama",
                "ask_ollama": "on",
                "ollama_model": "test-model",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        messages = chat_app.get_messages()
        self.assertEqual([message["user"] for message in messages], ["Alice", "Ollama Agent"])
        self.assertEqual(messages[1]["role"], "assistant")
        self.assertIsNone(messages[1]["trace"])
        call_agent.assert_called_once_with("test-model", ANY, ANY)

    @patch(
        "app.call_agent",
        return_value=(
            "I created the program.",
            [{"step": 1, "tool": "write_file", "result": "Wrote app.py"}],
        ),
    )
    def test_orchestrator_records_reply_and_tool_trace(self, call_agent):
        response = self.client.post(
            "/",
            data={
                "username": "Alice",
                "message": "Write a program",
                "ask_ollama": "on",
                "ollama_model": "tool-model",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        messages = chat_app.get_messages()
        self.assertEqual(messages[1]["user"], "Ollama Agent")
        self.assertIn("write_file", messages[1]["trace"])
        call_agent.assert_called_once_with("tool-model", ANY, ANY)

    @patch(
        "app.call_agent",
        return_value=(
            "Search complete.",
            [
                {
                    "step": 1,
                    "tool": "web_search",
                    "status": "Web search returned 1 result(s).",
                    "result": "[]",
                }
            ],
        ),
    )
    def test_fetch_post_returns_json_for_orchestrator(self, call_agent):
        response = self.client.post(
            "/",
            data={
                "username": "Alice",
                "message": "Search for Ollama",
                "ask_ollama": "on",
                "ollama_model": "tool-model",
            },
            headers={"X-Requested-With": "fetch"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertIsNotNone(response.get_json()["agent_job_id"])
        status = self.client.get("/api/agent/status").get_json()
        self.assertEqual(status["jobs"][0]["title"], "Search for Ollama")
        self.assertEqual(len(chat_app.get_messages()), 2)

    @patch("app.call_agent", return_value=("Summary complete.", []))
    def test_uploaded_text_documents_are_added_to_agent_context(self, call_agent):
        response = self.client.post(
            "/",
            data={
                "username": "Alice",
                "message": "Summarize these",
                "ask_ollama": "on",
                "ollama_model": "tool-model",
                "files": [
                    (BytesIO(b"alpha notes"), "alpha.txt"),
                    (BytesIO(b"beta notes"), "beta.md"),
                ],
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 302)
        agent_messages = call_agent.call_args.args[1]
        self.assertIn("alpha notes", agent_messages[-1]["content"])
        self.assertIn("beta notes", agent_messages[-1]["content"])

    def test_upload_rejects_more_than_five_documents(self):
        response = self.client.post(
            "/",
            data={
                "username": "Alice",
                "message": "Too many",
                "ask_ollama": "on",
                "files": [
                    (BytesIO(f"doc {index}".encode()), f"doc{index}.txt")
                    for index in range(6)
                ],
            },
            content_type="multipart/form-data",
            headers={"X-Requested-With": "fetch"},
        )

        self.assertEqual(response.status_code, 500)
        self.assertIn("no more than 5", response.get_json()["error"])

    def test_docx_and_xlsx_text_can_be_extracted(self):
        docx_path = Path(self.temp_dir.name) / "sample.docx"
        with zipfile.ZipFile(docx_path, "w") as archive:
            archive.writestr(
                "word/document.xml",
                (
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    "<w:body><w:p><w:r><w:t>Hello from Word</w:t></w:r></w:p></w:body>"
                    "</w:document>"
                ),
            )

        xlsx_path = Path(self.temp_dir.name) / "sample.xlsx"
        with zipfile.ZipFile(xlsx_path, "w") as archive:
            archive.writestr(
                "xl/sharedStrings.xml",
                (
                    '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    "<si><t>Company</t></si><si><t>Hitachi Energy</t></si>"
                    "</sst>"
                ),
            )
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                (
                    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    '<sheetData><row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row></sheetData>'
                    "</worksheet>"
                ),
            )

        self.assertIn("Hello from Word", chat_app.extract_document_text(docx_path, "sample.docx"))
        self.assertIn("Hitachi Energy", chat_app.extract_document_text(xlsx_path, "sample.xlsx"))

    @patch("app.call_agent")
    def test_fetch_post_returns_before_async_agent_finishes(self, call_agent):
        chat_app.app.config["TESTING_SYNC_AGENT"] = False
        started = threading.Event()
        release = threading.Event()

        def slow_agent(model, messages, job_id):
            started.set()
            release.wait(timeout=5)
            return "Async complete.", []

        call_agent.side_effect = slow_agent

        response = self.client.post(
            "/",
            data={
                "username": "Alice",
                "message": "Search in the background",
                "ask_ollama": "on",
                "ollama_model": "tool-model",
            },
            headers={"X-Requested-With": "fetch"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertTrue(started.wait(timeout=1))
        self.assertEqual(len(chat_app.get_messages()), 1)

        release.set()
        for _ in range(20):
            if len(chat_app.get_messages()) == 2:
                break
            time.sleep(0.05)
        self.assertEqual(len(chat_app.get_messages()), 2)

    def test_history_contains_human_names_and_assistant_replies(self):
        chat_app.add_message("Alice", "How are you?")
        chat_app.add_message("Ollama", "Doing well.", role="assistant")

        history = chat_app.build_ollama_history()

        self.assertEqual(history[0], {"role": "user", "content": "Alice: How are you?"})
        self.assertEqual(history[1], {"role": "assistant", "content": "Doing well."})

    def test_agent_context_history_starts_with_chat_window_context(self):
        chat_app.add_message("Alice", "Earlier request")
        chat_app.add_message("Ollama", "Earlier answer", role="assistant")
        chat_app.add_message(
            "Alice",
            "Please use this document",
            file_path='["brief.txt"]',
            original_name='["brief.txt"]',
            document_text="Attached document text for analysis:\n\n## Attached document: brief.txt\nImportant notes",
        )

        history = chat_app.build_agent_context_history()

        self.assertEqual(history[0]["role"], "user")
        self.assertIn("Chat window context", history[0]["content"])
        self.assertIn("Alice: Earlier request", history[0]["content"])
        self.assertIn("Ollama Agent: Earlier answer", history[0]["content"])
        self.assertIn("Attached files: brief.txt", history[0]["content"])
        self.assertIn("Extracted document text is available", history[0]["content"])
        self.assertIn("Important notes", history[-1]["content"])


if __name__ == "__main__":
    unittest.main()
