"""
test_tts_multi_engine.py - Comprehensive Unit & Integration Tests for Multi-Engine TTS
Validates VieNeu-TTS, OmniVoice, Edge-TTS, AI Native Voice, and fallback mechanisms.
"""

import os
import sys
import unittest
import asyncio
from pathlib import Path

# Fix Windows console utf-8 encoding
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from tts_manager import (
    list_all_voices,
    synthesize_voice,
    VIENEU_VOICES_CONFIG,
    OMNIVOICE_VOICES_CONFIG,
    EDGE_VOICES_CONFIG,
    get_media_duration,
)


class TestMultiEngineTTS(unittest.TestCase):

    def test_01_voice_catalog_structure(self):
        voices = list_all_voices()
        self.assertGreater(len(voices), 30)

        # Kiểm tra đủ các categories
        langs = {v["lang"] for v in voices}
        self.assertIn("ai", langs)
        self.assertIn("vieneu", langs)
        self.assertIn("omnivoice", langs)
        self.assertIn("vi", langs)
        self.assertIn("en", langs)

        # Kiểm tra VieNeu presets
        vieneu_ids = [v["id"] for v in voices if v["lang"] == "vieneu"]
        self.assertIn("vieneu:TrucLy", vieneu_ids)
        self.assertIn("vieneu:DucTri", vieneu_ids)
        self.assertIn("vieneu:Adam", vieneu_ids)

        # Kiểm tra OmniVoice presets
        omni_ids = [v["id"] for v in voices if v["lang"] == "omnivoice"]
        self.assertIn("omnivoice:vi_narrator_male", omni_ids)
        self.assertIn("omnivoice:vi_narrator_female", omni_ids)

        print(f" [PASS] Test 1: Voice Catalog day du {len(voices)} giong da engine")

    def test_02_vieneu_tts_synthesis(self):
        async def _run():
            out_file = "test_out_vieneu.mp3"
            try:
                dur = await synthesize_voice(
                    text="Xin chào, đây là thử nghiệm giọng đọc Trúc Ly VieNeu TTS.",
                    voice="vieneu:TrucLy",
                    output_path=out_file,
                )
                self.assertGreater(dur, 0.5)
                self.assertTrue(os.path.exists(out_file))
                actual_dur = get_media_duration(out_file)
                self.assertGreater(actual_dur, 0.5)
                print(f" [PASS] Test 2: VieNeu-TTS synthesis thành công (duration: {dur:.2f}s)")
            finally:
                if os.path.exists(out_file):
                    os.remove(out_file)

        asyncio.run(_run())

    def test_03_edge_tts_synthesis(self):
        async def _run():
            out_file = "test_out_edge.mp3"
            try:
                dur = await synthesize_voice(
                    text="Xin chào, đây là thử nghiệm giọng đọc Hoài My Edge TTS.",
                    voice="vi-VN-HoaiMyNeural",
                    output_path=out_file,
                )
                self.assertGreater(dur, 0.5)
                self.assertTrue(os.path.exists(out_file))
                actual_dur = get_media_duration(out_file)
                self.assertGreater(actual_dur, 0.5)
                print(f" [PASS] Test 3: Edge-TTS synthesis thành công (duration: {dur:.2f}s)")
            finally:
                if os.path.exists(out_file):
                    os.remove(out_file)

        asyncio.run(_run())

    def test_04_omnivoice_synthesis_or_fallback(self):
        async def _run():
            out_file = "test_out_omni.mp3"
            try:
                dur = await synthesize_voice(
                    text="Thử nghiệm tính năng sinh giọng nói OmniVoice đa ngôn ngữ.",
                    voice="omnivoice:vi_narrator_male",
                    output_path=out_file,
                )
                self.assertGreater(dur, 0.5)
                self.assertTrue(os.path.exists(out_file))
                actual_dur = get_media_duration(out_file)
                self.assertGreater(actual_dur, 0.5)
                print(f" [PASS] Test 4: OmniVoice synthesis thành công (duration: {dur:.2f}s)")
            finally:
                if os.path.exists(out_file):
                    os.remove(out_file)

        asyncio.run(_run())

    def test_05_ai_native_voice_mode(self):
        async def _run():
            out_file = "test_out_ai.mp3"
            try:
                # Chế độ AI Native Voice không sinh TTS rời, trả về 0.0
                dur = await synthesize_voice(
                    text="Lời thoại nhân vật",
                    voice="ai_voice",
                    output_path=out_file,
                )
                self.assertEqual(dur, 0.0)
                self.assertFalse(os.path.exists(out_file))
                print(f" [PASS] Test 5: AI Native Voice mode hoat dong chuan (bo qua TTS long tieng)")
            finally:
                if os.path.exists(out_file):
                    os.remove(out_file)

        asyncio.run(_run())

    def test_06_robust_fallback_unknown_voice(self):
        async def _run():
            out_file = "test_out_fallback.mp3"
            try:
                # Khi gọi giọng lạ không tồn tại, hệ thống tự fallback an toàn không làm crash render
                dur = await synthesize_voice(
                    text="Kiểm tra fallback an toàn khi voice bị lỗi.",
                    voice="invalid_voice_id_xyz",
                    output_path=out_file,
                )
                self.assertGreater(dur, 0.5)
                self.assertTrue(os.path.exists(out_file))
                print(f" [PASS] Test 6: Fallback an toan tu dong sinh am thanh du phong ({dur:.2f}s)")
            finally:
                if os.path.exists(out_file):
                    os.remove(out_file)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
