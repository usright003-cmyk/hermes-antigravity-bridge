"""Comprehensive unit tests for native multimodal vision and media ingestion."""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_antigravity_bridge.backends.antigravity import AntigravityBackend
from hermes_antigravity_bridge.config import AntigravityConfig
from hermes_antigravity_bridge.integrations.hermes import HermesPromptBuilder
from hermes_antigravity_bridge.prompt.budget import PromptBudget
from hermes_antigravity_bridge.prompt.primitives import (
    extract_attached_media,
    format_user_uploaded_media_header,
)
from hermes_antigravity_bridge.service import ChatCompletionService


class MultimodalVisionTests(unittest.TestCase):
    def test_extract_attached_media_from_base64_image(self) -> None:
        tiny_gif = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
        data_url = f"data:image/gif;base64,{base64.b64encode(tiny_gif).decode('ascii')}"

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is this image?"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]

        media = extract_attached_media(messages)
        self.assertEqual(len(media), 1)
        kind, path = media[0]
        self.assertEqual(kind, "image")
        self.assertTrue(Path(path).exists())
        self.assertGreater(Path(path).stat().st_size, 0)

    def test_extract_attached_media_from_local_file_uri(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(b"\xFF\xD8\xFF\xE0" + b"\x00" * 20)
            tmp_path = Path(tmp.name).resolve()

        try:
            file_url = f"file://{tmp_path.as_posix()}"
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this frame."},
                        {"type": "image_url", "image_url": {"url": file_url}},
                    ],
                }
            ]

            media = extract_attached_media(messages)
            self.assertEqual(len(media), 1)
            kind, path = media[0]
            self.assertEqual(kind, "image")
            self.assertEqual(Path(path).resolve(), tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_extract_attached_media_multiple_video_frames(self) -> None:
        frames: list[Path] = []
        for i in range(3):
            with tempfile.NamedTemporaryFile(suffix=f"_frame_{i}.jpg", delete=False) as tmp:
                tmp.write(b"\xFF\xD8\xFF\xE0" + bytes([i]) * 10)
                frames.append(Path(tmp.name).resolve())

        try:
            content_parts: list[dict[str, str | dict[str, str]]] = [
                {"type": "text", "text": "Analyze these 3 video frames:"}
            ]
            for frame in frames:
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"file://{frame.as_posix()}"},
                })

            messages = [{"role": "user", "content": content_parts}]
            media = extract_attached_media(messages)
            self.assertEqual(len(media), 3)
            extracted_paths = [p for _, p in media]
            for frame in frames:
                self.assertIn(frame.as_posix(), extracted_paths)
        finally:
            for frame in frames:
                frame.unlink(missing_ok=True)

    def test_format_user_uploaded_media_header_formats_correctly(self) -> None:
        media_items = [
            ("image", "C:/media/frame1.jpg"),
            ("image", "C:/media/frame2.jpg"),
            ("video", "C:/media/clip.mp4"),
        ]
        header = format_user_uploaded_media_header(media_items)
        self.assertIn("The user has uploaded 2 image(s):", header)
        self.assertIn("- C:/media/frame1.jpg", header)
        self.assertIn("- C:/media/frame2.jpg", header)
        self.assertIn("The user has uploaded 1 video(s):", header)
        self.assertIn("- C:/media/clip.mp4", header)

    def test_hermes_prompt_builder_prepends_media_header_to_prompt(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
            tmp_path = Path(tmp.name).resolve()

        try:
            builder = HermesPromptBuilder(enforce_tool_isolation=True)
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Check this screenshot."},
                        {"type": "image_url", "image_url": {"url": f"file://{tmp_path.as_posix()}"}},
                    ],
                }
            ]
            prompt = builder.build(messages)
            self.assertTrue(prompt.startswith("The user has uploaded 1 image(s):"))
            self.assertIn(f"- {tmp_path.as_posix()}", prompt)
            self.assertIn("# CURRENT_USER_REQUEST_JSON", prompt)
            self.assertIn("Multimodal Media & Vision Rule", prompt)
            self.assertIn("native multimodal vision", prompt)
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_backend_build_command_includes_dangerously_skip_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = AntigravityBackend(
                AntigravityConfig(
                    binary=Path(tmp) / "agy",
                    home=Path(tmp) / "home",
                    runtime_dir=Path(tmp) / "runtime",
                    enforce_tool_isolation=True,
                )
            )
            cmd = backend.build_command("gemini-3.8-flash-high")
            self.assertIn("--dangerously-skip-permissions", cmd)
            self.assertIn("--sandbox", cmd)
            self.assertIn("--disable-slash-commands", cmd)

    def test_service_system_instructions_contain_multimodal_vision_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = AntigravityBackend(
                AntigravityConfig(
                    binary=Path(tmp) / "agy",
                    home=Path(tmp) / "home",
                    runtime_dir=Path(tmp) / "runtime",
                )
            )
            service = ChatCompletionService(
                backend=backend,
                prompt_builder=HermesPromptBuilder(),
                prompt_budget=PromptBudget(),
            )

            with patch.object(backend, "generate") as mock_gen:
                mock_gen.return_value = type("R", (), {"response": "ok", "stats": {}, "usage": {}})()
                service.complete({
                    "model": "gemini-3.8-flash",
                    "messages": [{"role": "user", "content": "hi"}],
                })
                prompt_arg = mock_gen.call_args[0][0]
                self.assertIn("MULTIMODAL & VISION EXCEPTION", prompt_arg)
                self.assertIn("native multimodal perception", prompt_arg)


if __name__ == "__main__":
    unittest.main()
