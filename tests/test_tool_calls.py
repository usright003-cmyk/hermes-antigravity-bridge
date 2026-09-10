import json
import unittest

from hermes_antigravity_bridge.errors import InvalidToolCall
from hermes_antigravity_bridge.tool_calls import parse_tool_calls


class ToolCallAdapterTests(unittest.TestCase):
    def test_parses_allowed_tool_call(self):
        text = (
            'Before\n<tool_call>{"id":"call_1","type":"function",'
            '"function":{"name":"memory","arguments":{"action":"add","content":"fact"}}}'
            '</tool_call>\nAfter'
        )
        result = parse_tool_calls(text, allowed_tool_names={"memory"})
        self.assertEqual(result.text, "Before\n\nAfter")
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0]["function"]["name"], "memory")
        self.assertEqual(
            json.loads(result.tool_calls[0]["function"]["arguments"])["action"],
            "add",
        )

    def test_rejects_unadvertised_tool(self):
        text = '<tool_call>{"name":"terminal","arguments":{"command":"id"}}</tool_call>'
        with self.assertRaisesRegex(InvalidToolCall, "not advertised by Hermes"):
            parse_tool_calls(text, allowed_tool_names={"memory"}, mode="strict")

    def test_rejects_malformed_tagged_payload(self):
        with self.assertRaisesRegex(InvalidToolCall, "malformed"):
            parse_tool_calls("<tool_call>{not json}</tool_call>", allowed_tool_names={"memory"}, mode="strict")

    def test_compatible_mode_recovers_from_malformed_payload(self):
        text = "Thinking...\n<tool_call>{not json at all}</tool_call>\nReady."
        result = parse_tool_calls(text, allowed_tool_names={"memory"}, mode="compatible")
        self.assertEqual(result.text, text)
        self.assertEqual(result.tool_calls, ())

    def test_compatible_mode_skips_unadvertised_tool(self):
        text = '<tool_call>{"name":"terminal","arguments":{"command":"id"}}</tool_call>'
        result = parse_tool_calls(text, allowed_tool_names={"memory"}, mode="compatible")
        self.assertEqual(result.tool_calls, ())

    def test_parses_deeply_nested_json_arguments(self):
        # Crucial test: previous regex prematurely closed on the first nested '}'
        text = (
            '<tool_call>{\n'
            '  "name": "edit_file",\n'
            '  "arguments": {\n'
            '    "path": "config.py",\n'
            '    "nested": {"level1": {"level2": "value"}},\n'
            '    "tags": ["a", "b"]\n'
            '  }\n'
            '}</tool_call>'
        )
        result = parse_tool_calls(text, allowed_tool_names={"edit_file"})
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0]["function"]["name"], "edit_file")
        args = json.loads(result.tool_calls[0]["function"]["arguments"])
        self.assertEqual(args["nested"]["level1"]["level2"], "value")

    def test_parses_unclosed_tool_call_tag_at_eof(self):
        text = (
            'I will check status.\n'
            '<tool_call>{"name": "get_status", "arguments": {"check": true}}'
        )
        result = parse_tool_calls(text, allowed_tool_names={"get_status"})
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0]["function"]["name"], "get_status")
        self.assertEqual(result.text, "I will check status.")

    def test_auto_repairs_trailing_commas(self):
        text = (
            '<tool_call>{\n'
            '  "name": "save_data",\n'
            '  "arguments": {\n'
            '    "key": "test",\n'
            '  },\n'
            '}</tool_call>'
        )
        result = parse_tool_calls(text, allowed_tool_names={"save_data"})
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0]["function"]["name"], "save_data")

    def test_parses_multiple_tool_calls(self):
        text = (
            '<tool_call>{"name": "tool_a", "arguments": {"x": 1}}</tool_call>\n'
            'Some intermediate note.\n'
            '<tool_call>{"name": "tool_b", "arguments": {"y": 2}}</tool_call>'
        )
        result = parse_tool_calls(text, allowed_tool_names={"tool_a", "tool_b"})
        self.assertEqual(len(result.tool_calls), 2)
        self.assertEqual(result.tool_calls[0]["function"]["name"], "tool_a")
        self.assertEqual(result.tool_calls[1]["function"]["name"], "tool_b")
        self.assertEqual(result.text, "Some intermediate note.")

    def test_accepts_fenced_json_and_generates_deterministic_test_id(self):
        text = '<tool_call>```json\n{"name":"read_file","arguments":"{\\"path\\":\\"a.txt\\"}"}\n```</tool_call>'
        result = parse_tool_calls(
            text,
            allowed_tool_names={"read_file"},
            id_factory=lambda: "call_test",
        )
        self.assertEqual(result.tool_calls[0]["id"], "call_test")
        self.assertEqual(json.loads(result.tool_calls[0]["function"]["arguments"]), {"path": "a.txt"})

    def test_plain_text_with_tool_call_tag_when_no_tools_advertised(self):
        text = "To make a tool call, you can use the <tool_call> tag."
        result = parse_tool_calls(text, allowed_tool_names=())
        self.assertEqual(result.text, text)
        self.assertEqual(result.tool_calls, ())


if __name__ == "__main__":
    unittest.main()
