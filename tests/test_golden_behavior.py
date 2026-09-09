import hashlib
import json
import unittest
from pathlib import Path

from hermes_antigravity_bridge.integrations.hermes import HermesPromptBuilder

FIXTURE = Path(__file__).parent / "golden" / "reference_prompts.json"


def materialize(case):
    generator = case.get("generator")
    if not generator:
        return case["messages"]
    if generator["kind"] == "pathological_non_latest":
        return [
            {"role": "system", "content": "SYSTEM_HEAD\n" + ("s" * generator["system_repeat"]) + "\nMEMORY_TAIL"},
            {"role": "user", "content": "old topic"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "LATEST_LOW_ENTROPY_3003:" + ("Z" * generator["latest_repeat"])},
        ]
    if generator["kind"] == "large_history":
        messages = [{"role": "system", "content": "Hermes memory: large-history"}]
        for index in range(generator["turn_pairs"]):
            messages.extend([
                {"role": "user", "content": f"historical-topic-{index} " + ("x" * generator["user_repeat"])},
                {"role": "assistant", "content": f"historical-answer-{index} " + ("y" * generator["assistant_repeat"])},
            ])
        messages.append({"role": "user", "content": generator["latest"]})
        return messages
    raise AssertionError(f"unknown generator: {generator['kind']}")


class GoldenBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.builder = HermesPromptBuilder()

    def test_reference_source_is_the_validated_production_source(self):
        self.assertEqual(
            self.fixture["reference_server_sha256"],
            "3702f5ef3e10f532487777ffcfe7b91b573ce1ec73b665d17f511acd37c28e9a",
        )

    def test_prompt_outputs_match_validated_reference(self):
        for case in self.fixture["cases"]:
            with self.subTest(case=case["name"]):
                prompt = self.builder.build(
                    materialize(case),
                    tools=case.get("tools") or None,
                    max_chars=case["max_chars"],
                )
                self.assertEqual(len(prompt), case["expected_length"])
                self.assertEqual(
                    hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    case["expected_sha256"],
                )
                if "expected_prompt" in case:
                    self.assertEqual(prompt, case["expected_prompt"])


if __name__ == "__main__":
    unittest.main()
