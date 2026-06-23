import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import agent


class AgentToolsTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = self.temp_dir.name

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_file_tools_stay_inside_workspace(self):
        result = agent.write_file(self.workspace, "src/hello.py", "print('hello')")

        self.assertIn("Wrote", result)
        self.assertEqual(
            agent.read_file(self.workspace, "src/hello.py"), "print('hello')"
        )
        with self.assertRaises(ValueError):
            agent.safe_path(self.workspace, "../outside.txt")

    def test_dangerous_commands_are_blocked(self):
        self.assertIn("blocked", agent.run_command(self.workspace, "rm -rf ."))
        self.assertIn(
            "not allowed",
            agent.run_command(self.workspace, "python app.py > output.txt"),
        )
        self.assertIn(
            "not allowed",
            agent.run_command(self.workspace, "python3 -c print(1)"),
        )

    def test_search_result_parser_extracts_links(self):
        parser = agent.SearchResultParser()
        parser.feed(
            '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com">'
            "Example result</a>"
        )

        self.assertEqual(
            parser.results,
            [{"title": "Example result", "url": "https://example.com"}],
        )

    @patch("agent.http_get")
    def test_web_search_uses_searxng_json_api(self, http_get):
        http_get.return_value = (
            "application/json",
            json.dumps(
                {
                    "results": [
                        {
                            "title": "Ollama docs",
                            "url": "https://example.com/ollama",
                            "content": "Tool calling documentation",
                        }
                    ]
                }
            ),
        )

        results = json.loads(agent.web_search("ollama tools", 3))

        self.assertEqual(results[0]["title"], "Ollama docs")
        self.assertIn("/search?q=ollama+tools&format=json", http_get.call_args.args[0])

    @patch("agent.duckduckgo_search")
    @patch("agent.searxng_search", side_effect=URLError("offline"))
    def test_web_search_falls_back_when_searxng_is_offline(
        self, searxng_search, duckduckgo_search
    ):
        duckduckgo_search.return_value = [
            {"title": "Fallback", "url": "https://example.com"}
        ]

        results = json.loads(agent.web_search("fallback", 2))

        self.assertEqual(results[0]["title"], "Fallback")

    def test_agent_loop_executes_tool_and_returns_final_answer(self):
        instructions = Path(self.workspace) / "instructions.md"
        skills = Path(self.workspace) / "skills"
        instructions.write_text("Be useful.", encoding="utf-8")
        skills.mkdir()
        (skills / "coding.md").write_text("Write code.", encoding="utf-8")
        responses = iter(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "write_file",
                                "arguments": {
                                    "path": "hello.py",
                                    "content": "print('hello')",
                                },
                            }
                        }
                    ],
                },
                {"role": "assistant", "content": "Created hello.py."},
            ]
        )

        def fake_chat(model, messages, tools):
            return next(responses)

        answer, trace = agent.run_agent(
            model="test",
            messages=[{"role": "user", "content": "Create hello.py"}],
            ollama_chat=fake_chat,
            workspace=self.workspace,
            base_instructions_file=instructions,
            skills_dir=skills,
        )

        self.assertEqual(answer, "Created hello.py.")
        self.assertEqual(trace[0]["tool"], "write_file")
        self.assertTrue((Path(self.workspace) / "hello.py").exists())

    def test_string_tool_arguments_are_supported(self):
        responses = iter(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {"path": "note.txt", "content": "hello"}
                                ),
                            }
                        }
                    ],
                },
                {"role": "assistant", "content": "Done."},
            ]
        )
        instructions = Path(self.workspace) / "instructions.md"
        skills = Path(self.workspace) / "skills"
        instructions.write_text("Be useful.", encoding="utf-8")
        skills.mkdir()

        answer, _ = agent.run_agent(
            "test",
            [{"role": "user", "content": "Write a note"}],
            lambda model, messages, tools: next(responses),
            self.workspace,
            instructions,
            skills,
        )

        self.assertEqual(answer, "Done.")
        self.assertEqual((Path(self.workspace) / "note.txt").read_text(), "hello")
