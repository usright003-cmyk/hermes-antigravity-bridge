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
            parse_tool_calls(text, allowed_tool_names={"memory"})

    def test_rejects_malformed_tagged_payload(self):
        with self.assertRaisesRegex(InvalidToolCall, "malformed"):
            parse_tool_calls("<tool_call>{not json}</tool_call>", allowed_tool_names={"memory"})

    def test_accepts_fenced_json_and_generates_deterministic_test_id(self):
        text = '<tool_call>```json\n{"name":"read_file","arguments":"{\\"path\\":\\"a.txt\\"}"}\n```</tool_call>'
        result = parse_tool_calls(
            text,
            allowed_tool_names={"read_file"},
            id_factory=lambda: "call_test",
        )
        self.assertEqual(result.tool_calls[0]["id"], "call_test")
        self.assertEqual(json.loads(result.tool_calls[0]["function"]["arguments"]), {"path": "a.txt"})


if __name__ == "__main__":
    unittest.main()
