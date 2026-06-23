import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as chat_app


class ChatAppTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        chat_app.DATABASE = str(Path(self.temp_dir.name) / "test.db")
        chat_app.UPLOAD_FOLDER = str(Path(self.temp_dir.name) / "uploads")
        chat_app.SKILLS_DIR = str(Path(self.temp_dir.name) / "skills")
        Path(chat_app.UPLOAD_FOLDER).mkdir()
        Path(chat_app.SKILLS_DIR).mkdir()
        chat_app.init_db()
        chat_app.app.config.update(TESTING=True, SECRET_KEY="test")
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

    @patch("app.call_ollama", return_value="Hello Alice")
    def test_ollama_reply_is_added_as_third_participant(self, call_ollama):
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
        self.assertEqual([message["user"] for message in messages], ["Alice", "Ollama"])
        self.assertEqual(messages[1]["role"], "assistant")
        call_ollama.assert_called_once_with("test-model")

    @patch(
        "app.call_agent",
        return_value=(
            "I created the program.",
            [{"step": 1, "tool": "write_file", "result": "Wrote app.py"}],
        ),
    )
    def test_agent_mode_records_reply_and_tool_trace(self, call_agent):
        response = self.client.post(
            "/",
            data={
                "username": "Alice",
                "message": "Write a program",
                "ask_ollama": "on",
                "agent_mode": "on",
                "ollama_model": "tool-model",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        messages = chat_app.get_messages()
        self.assertEqual(messages[1]["user"], "Ollama Agent")
        self.assertIn("write_file", messages[1]["trace"])
        call_agent.assert_called_once_with("tool-model")

    def test_history_contains_human_names_and_assistant_replies(self):
        chat_app.add_message("Alice", "How are you?")
        chat_app.add_message("Ollama", "Doing well.", role="assistant")

        history = chat_app.build_ollama_history()

        self.assertEqual(history[0], {"role": "user", "content": "Alice: How are you?"})
        self.assertEqual(history[1], {"role": "assistant", "content": "Doing well."})


if __name__ == "__main__":
    unittest.main()
