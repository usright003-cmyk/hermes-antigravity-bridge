import json
import unittest

from hermes_antigravity_bridge.errors import PromptTooLarge
from hermes_antigravity_bridge.integrations.hermes import HermesPromptBuilder
from hermes_antigravity_bridge.prompt.budget import PromptBudget


class PromptPolicyTests(unittest.TestCase):
    def setUp(self):
        self.builder = HermesPromptBuilder()

    def section(self, prompt, label, next_label):
        start = prompt.index(label) + len(label)
        end = prompt.index(next_label, start)
        return prompt[start:end]

    def test_every_emitted_jsonl_record_is_complete_after_clipping(self):
        messages = [{"role": "system", "content": "SYSTEM\n" + "\n".join(f"rule-{i}" for i in range(2000))}]
        for index in range(200):
            messages.extend([
                {"role": "user", "content": f"old-user-{index} " + ("x" * 400)},
                {"role": "assistant", "content": f"old-assistant-{index} " + ("y" * 400)},
            ])
        latest = "LATEST_JSONL_INVARIANT"
        messages.append({"role": "user", "content": latest})
        tools = [
            {"type": "function", "function": {"name": f"tool_{i}", "description": "desc", "parameters": {"type": "object", "properties": {"value": {"type": "string"}}}}}
            for i in range(50)
        ]
        prompt = self.builder.build(messages, tools=tools, max_chars=12_000)
        tool_text = self.section(
            prompt,
            "# HERMES_TOOL_SCHEMAS_JSONL\n",
            "\n# RECENT_CONVERSATION_JSONL\n",
        )
        history_text = self.section(
            prompt,
            "# RECENT_CONVERSATION_JSONL\n",
            "\n# CURRENT_USER_REQUEST_JSON\n",
        )
        latest_text = self.section(
            prompt,
            "# CURRENT_USER_REQUEST_JSON\n",
            "\n# CURRENT_REQUEST_GUARD\n",
        )
        for line in tool_text.splitlines():
            if line and not line.startswith("["):
                self.assertIsInstance(json.loads(line), dict)
        for line in history_text.splitlines():
            if line:
                self.assertIsInstance(json.loads(line), dict)
        self.assertEqual(json.loads(latest_text)["content"], latest)
        self.assertLessEqual(len(prompt), 12_000)

    def test_latest_user_content_is_never_normalized(self):
        latest = "LATEST:" + ("Z" * 700)
        prompt = self.builder.build([
            {"role": "system", "content": "s" * 10_000},
            {"role": "user", "content": latest},
        ])
        self.assertIn(latest, prompt)
        self.assertNotIn("U+005A", prompt)
        self.assertIn("[bridge compacted repeated character U+0073 x 10000]", prompt)

    def test_oversized_latest_request_fails_explicitly(self):
        with self.assertRaisesRegex(PromptTooLarge, "refusing to drop"):
            self.builder.build([{"role": "user", "content": "L" * 4_000_000}])

    def test_provider_limit_never_increases_global_character_cap(self):
        budget = PromptBudget(
            max_chars=64_000,
            output_token_reserve=8_192,
            chars_per_token=4,
            model_context_tokens={"small": 20_000, "large": 1_000_000},
        )
        self.assertEqual(budget.effective_chars("small"), 47_232)
        self.assertEqual(budget.effective_chars("large"), 64_000)
        self.assertEqual(budget.effective_chars("unknown"), 64_000)

    def test_default_gemini_models_have_1m_token_capacity(self):
        budget = PromptBudget()
        self.assertEqual(budget.effective_chars("gemini-3.8-flash"), 3_967_232)
        self.assertEqual(budget.effective_chars("gemini-3.1-pro"), 3_967_232)
        self.assertEqual(budget.effective_chars("antigravity"), 3_967_232)
        self.assertEqual(budget.effective_chars("claude-3-5-sonnet"), 767_232)
        self.assertEqual(budget.effective_chars("gpt-4o"), 479_232)
        self.assertEqual(budget.effective_chars("custom-local-model"), 4_000_000)

    def test_future_model_family_heuristics(self):
        budget = PromptBudget()
        self.assertEqual(budget.effective_chars("gemini-4.0-flash"), 3_967_232)
        self.assertEqual(budget.effective_chars("claude-4-opus"), 767_232)
        self.assertEqual(budget.effective_chars("gpt-5-mini"), 479_232)

    def test_multimodal_image_base64_decoded_to_media_file(self):
        import base64
        tiny_png = base64.b64encode(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR").decode("utf-8")
        data_url = f"data:image/png;base64,{tiny_png}"
        prompt = self.builder.build([
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in this picture?"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ])
        self.assertIn("What is in this picture?", prompt)
        self.assertIn("[Attached image file:", prompt)
        self.assertIn("- use view_file to inspect this image]", prompt)
        self.assertNotIn("data:image/png", prompt)

    def test_multimodal_local_file_path(self):
        prompt = self.builder.build([
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Inspect this video"},
                    {"type": "video_url", "video_url": {"url": "file:///C:/Users/HP/video.mp4"}},
                    {"type": "audio_url", "audio_url": {"url": "C:/Users/HP/audio.mp3"}},
                ],
            }
        ])
        self.assertIn("[Attached video file: C:/Users/HP/video.mp4 - use view_file to inspect this video]", prompt)
        self.assertIn("[Attached audio file: C:/Users/HP/audio.mp3 - use view_file to inspect this audio]", prompt)


if __name__ == "__main__":
    unittest.main()

