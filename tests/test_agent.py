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

    def test_document_creation_tools_write_outputs(self):
        markdown_result = agent.create_markdown(
            self.workspace, "summary", "# Summary\n\nHello"
        )
        csv_result = agent.create_csv(
            self.workspace,
            "companies",
            [{"Company": "Hitachi Energy", "Sector": "Energy"}],
        )
        docx_result = agent.create_docx(
            self.workspace, "brief", "Brief", "# Heading\nA paragraph"
        )
        xlsx_result = agent.create_xlsx(
            self.workspace,
            "table",
            [{"Company": "Hitachi Energy", "Sector": "Energy"}],
        )

        self.assertIn("outputs/summary.md", markdown_result)
        self.assertIn("outputs/companies.csv", csv_result)
        self.assertIn("outputs/brief.docx", docx_result)
        self.assertIn("outputs/table.xlsx", xlsx_result)
        self.assertTrue((Path(self.workspace) / "outputs" / "summary.md").exists())
        self.assertTrue((Path(self.workspace) / "outputs" / "companies.csv").exists())
        self.assertTrue((Path(self.workspace) / "outputs" / "brief.docx").exists())
        self.assertTrue((Path(self.workspace) / "outputs" / "table.xlsx").exists())

    def test_document_tools_tolerate_common_argument_aliases(self):
        result = agent.execute_tool(
            "create_markdown",
            {"text": "# Summary\n\nHello"},
            self.workspace,
        )

        self.assertIn("outputs/agent_output.md", result)
        self.assertEqual(
            (Path(self.workspace) / "outputs" / "agent_output.md").read_text(),
            "# Summary\n\nHello",
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
        self.assertEqual(results[0]["source"], "searxng")
        self.assertIn("/search?q=ollama+tools&format=json", http_get.call_args.args[0])

    @patch("agent.duckduckgo_search")
    @patch("agent.searxng_search", side_effect=URLError("offline"))
    def test_web_search_falls_back_when_searxng_is_offline(
        self, searxng_search, duckduckgo_search
    ):
        duckduckgo_search.return_value = [
            {
                "title": "Fallback",
                "url": "https://example.com",
                "source": "duckduckgo-fallback",
            }
        ]

        results = json.loads(agent.web_search("fallback", 2))

        self.assertEqual(results[0]["title"], "Fallback")
        self.assertEqual(results[0]["source"], "duckduckgo-fallback")

    def test_agent_loop_executes_tool_and_returns_final_answer(self):
        orchestrator = Path(self.workspace) / "orchestrator.md"
        instructions = Path(self.workspace) / "instructions.md"
        skills = Path(self.workspace) / "skills"
        orchestrator.write_text("Decide when tools are needed.", encoding="utf-8")
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
            orchestrator_instructions_file=orchestrator,
            skills_dir=skills,
        )

        self.assertEqual(answer, "Created hello.py.")
        self.assertEqual(trace[0]["tool"], "write_file")
        self.assertEqual(trace[0]["status"], "Wrote 14 bytes to hello.py")
        self.assertTrue((Path(self.workspace) / "hello.py").exists())

    def test_agent_loop_reports_live_tool_status(self):
        orchestrator = Path(self.workspace) / "orchestrator.md"
        instructions = Path(self.workspace) / "instructions.md"
        skills = Path(self.workspace) / "skills"
        orchestrator.write_text("Decide when tools are needed.", encoding="utf-8")
        instructions.write_text("Be useful.", encoding="utf-8")
        skills.mkdir()
        statuses = []
        responses = iter(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "web_search",
                                "arguments": {"query": "local search"},
                            }
                        }
                    ],
                },
                {"role": "assistant", "content": "Found it."},
            ]
        )

        with patch(
            "agent.web_search",
            return_value='[{"title": "A", "url": "https://a.test", "source": "searxng"}]',
        ):
            answer, trace = agent.run_agent(
                "test",
                [{"role": "user", "content": "Search"}],
                lambda model, messages, tools: next(responses),
                self.workspace,
                instructions,
                orchestrator,
                skills,
                status_callback=statuses.append,
            )

        self.assertEqual(answer, "Found it.")
        self.assertIn("deciding", statuses[0]["message"])
        self.assertIn("Searching the web", statuses[1]["message"])
        self.assertEqual(statuses[1]["tool"], "web_search")
        self.assertEqual(
            trace[0]["status"], "Web search returned 1 result(s) from SearXNG."
        )

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
        orchestrator = Path(self.workspace) / "orchestrator.md"
        instructions = Path(self.workspace) / "instructions.md"
        skills = Path(self.workspace) / "skills"
        orchestrator.write_text("Decide when tools are needed.", encoding="utf-8")
        instructions.write_text("Be useful.", encoding="utf-8")
        skills.mkdir()

        answer, _ = agent.run_agent(
            "test",
            [{"role": "user", "content": "Write a note"}],
            lambda model, messages, tools: next(responses),
            self.workspace,
            instructions,
            orchestrator,
            skills,
        )

        self.assertEqual(answer, "Done.")
        self.assertEqual((Path(self.workspace) / "note.txt").read_text(), "hello")

    def test_json_content_tool_call_is_executed(self):
        orchestrator = Path(self.workspace) / "orchestrator.md"
        instructions = Path(self.workspace) / "instructions.md"
        skills = Path(self.workspace) / "skills"
        orchestrator.write_text("Decide when tools are needed.", encoding="utf-8")
        instructions.write_text("Be useful.", encoding="utf-8")
        skills.mkdir()
        (Path(self.workspace) / "helium_info.txt").write_text(
            "Helium is a noble gas.", encoding="utf-8"
        )
        responses = iter(
            [
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "name": "read_file",
                            "parameters": {"path": "helium_info.txt"},
                        }
                    ),
                },
                {"role": "assistant", "content": "Helium is a noble gas."},
            ]
        )

        answer, trace = agent.run_agent(
            "test",
            [{"role": "user", "content": "What is helium?"}],
            lambda model, messages, tools: next(responses),
            self.workspace,
            instructions,
            orchestrator,
            skills,
        )

        self.assertEqual(answer, "Helium is a noble gas.")
        self.assertEqual(trace[0]["tool"], "read_file")

    def test_embedded_json_tool_call_is_executed_not_returned(self):
        orchestrator = Path(self.workspace) / "orchestrator.md"
        instructions = Path(self.workspace) / "instructions.md"
        skills = Path(self.workspace) / "skills"
        orchestrator.write_text("Decide when tools are needed.", encoding="utf-8")
        instructions.write_text("Be useful.", encoding="utf-8")
        skills.mkdir()
        responses = iter(
            [
                {
                    "role": "assistant",
                    "content": (
                        "I will fetch the official page now.\n"
                        '{"name":"fetch_url","parameters":{"url":"https://example.test/spec"}}'
                    ),
                },
                {"role": "assistant", "content": "Here is the final spec summary."},
            ]
        )

        with patch("agent.fetch_url", return_value="Official spec text"):
            answer, trace = agent.run_agent(
                "test",
                [{"role": "user", "content": "Get the spec"}],
                lambda model, messages, tools: next(responses),
                self.workspace,
                instructions,
                orchestrator,
                skills,
            )

        self.assertEqual(answer, "Here is the final spec summary.")
        self.assertEqual(trace[0]["tool"], "fetch_url")

    def test_planning_leak_is_not_returned_as_final_answer(self):
        orchestrator = Path(self.workspace) / "orchestrator.md"
        instructions = Path(self.workspace) / "instructions.md"
        skills = Path(self.workspace) / "skills"
        orchestrator.write_text("Decide when tools are needed.", encoding="utf-8")
        instructions.write_text("Be useful.", encoding="utf-8")
        skills.mkdir()
        responses = iter(
            [
                {
                    "role": "assistant",
                    "content": "I will use the fetch_url tool with parameters soon.",
                },
                {"role": "assistant", "content": "Final answer for the chat."},
            ]
        )

        answer, trace = agent.run_agent(
            "test",
            [{"role": "user", "content": "Get the spec"}],
            lambda model, messages, tools: next(responses),
            self.workspace,
            instructions,
            orchestrator,
            skills,
        )

        self.assertEqual(answer, "Final answer for the chat.")
        self.assertEqual(trace, [])

    def test_tool_arguments_content_shape_is_supported(self):
        tool_calls = agent.tool_calls_from_content(
            '{"tool":"web_search","arguments":{"query":"Hitachi Energy"}}'
        )

        self.assertEqual(tool_calls[0]["function"]["name"], "web_search")
        self.assertEqual(
            tool_calls[0]["function"]["arguments"]["query"], "Hitachi Energy"
        )
