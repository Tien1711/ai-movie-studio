"""
tts_manager.py - Unified Multi-Engine TTS Manager for AI Movie Studio
Supports:
1. VieNeu-TTS (On-device neural Vietnamese TTS with instant voice cloning, ONNX/CPU)
   Repository: https://github.com/pnnbao97/VieNeu-TTS
2. OmniVoice (k2-fsa massively multilingual >600 languages with voice design & zero-shot cloning)
   Repository: https://github.com/k2-fsa/OmniVoice
3. Microsoft Edge-TTS (Multi-language cloud neural TTS fallback)
4. AI Native Voice (Muse AI direct prompt dialogue synthesis)
"""

import os
import re
import sys
import asyncio
import tempfile
import subprocess
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

import imageio_ffmpeg

# Setup pydub ffmpeg path safely to prevent warnings
try:
    import pydub
    pydub.AudioSegment.converter = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    pass

logger = logging.getLogger("TTSManager")
logging.basicConfig(level=logging.INFO)

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()


def get_media_duration(file_path: str) -> float:
    """Lấy thời lượng file audio/video bằng ffmpeg."""
    if not os.path.exists(file_path):
        return 0.0
    try:
        cmd = [FFMPEG_EXE, "-i", file_path]
        proc = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, errors="replace")
        m = re.search(r"Duration:\s*(\d+):(\d+):([\d\.]+)", proc.stderr)
        if m:
            return float(m.group(1)) * 3600 + float(m.group(2)) * 60 + float(m.group(3))
    except Exception as exc:
        logger.warning(f"Error checking duration for {file_path}: {exc}")
    return 3.0


# ── 1. VIENEU-TTS ENGINE ────────────────────────────────────────────────────────

VIENEU_VOICES_CONFIG = [
    {"id": "vieneu:AdamBua",   "preset": "Adam bựa",   "gender": "male",   "region": "Bắc", "desc": "Nam Bắc · Hài hước, vui nhộn, meme troll", "sample_text": "Ê mấy ông giáo, hôm nay làm video gì vui vui nào!", "mode": "v3turbo"},
    {"id": "vieneu:TrucLy",    "preset": "Trúc Ly",    "gender": "female", "region": "Bắc", "desc": "Nữ Bắc · Tự nhiên, truyền cảm", "sample_text": "Xin chào, tôi là Trúc Ly, giọng đọc tự nhiên và truyền cảm của VieNeu TTS.", "mode": "v3nano"},
    {"id": "vieneu:DucTri",    "preset": "Đức Trí",    "gender": "male",   "region": "Bắc", "desc": "Nam Bắc · Kể chuyện, đọc truyện", "sample_text": "Xin chào, tôi là Đức Trí, giọng đọc truyền cảm cho phim ảnh và truyện đọc.", "mode": "v3nano"},
    {"id": "vieneu:ThienMinh", "preset": "Thiện Minh", "gender": "male",   "region": "Bắc", "desc": "Nam Bắc · Kể chuyện điện ảnh, hào hùng", "sample_text": "Chào mừng bạn đến với thế giới điện ảnh kỳ ảo của chúng tôi.", "mode": "v3turbo"},
    {"id": "vieneu:HaiDang",   "preset": "Hải Đăng",   "gender": "male",   "region": "Bắc", "desc": "Nam Bắc · Thanh lịch, tự nhiên", "sample_text": "Xin chào, tôi là Hải Đăng, giọng đọc thanh lịch và truyền cảm.", "mode": "v3turbo"},
    {"id": "vieneu:ThuyDung",  "preset": "Thùy Dung",  "gender": "female", "region": "Nam", "desc": "Nữ Nam · Tin tức, lưu loát", "sample_text": "Bản tin điện ảnh hôm nay sẽ mang đến cho quý vị những tin tức đặc sắc nhất.", "mode": "v3turbo"},
    {"id": "vieneu:QuangSon",  "preset": "Quang Sơn",  "gender": "male",   "region": "Trung", "desc": "Nam Trung · Ấm áp, mộc mạc", "sample_text": "Xin chào bà con cô bác, tôi là Quang Sơn, giọng đọc miền Trung mộc mạc.", "mode": "v3turbo"},
    {"id": "vieneu:MyDuyen",   "preset": "Mỹ Duyên",   "gender": "female", "region": "Bắc", "desc": "Nữ Bắc · Phong cách đọc truyện", "sample_text": "Xin chào, tôi là Mỹ Duyên, giọng kể chuyện truyền cảm và sâu lắng.", "mode": "v3nano"},
    {"id": "vieneu:AnhKhoi",   "preset": "Anh Khôi",   "gender": "male",   "region": "Bắc", "desc": "Nam Bắc · Kể chuyện hào hùng", "sample_text": "Xin chào, tôi là Anh Khôi, giọng đọc kể chuyện hào hùng và cuốn hút.", "mode": "v3nano"},
    {"id": "vieneu:MinhQuan",  "preset": "Minh Quân",  "gender": "male",   "region": "Bắc", "desc": "Nam Bắc · Tự nhiên, trầm ấm", "sample_text": "Xin chào, tôi là Minh Quân, giọng đọc tự nhiên và thân thiện.", "mode": "v3nano"},
    {"id": "vieneu:ManhDung",  "preset": "Mạnh Dũng",  "gender": "male",   "region": "Bắc", "desc": "Nam Bắc · Trầm ấm, chững chạc", "sample_text": "Xin chào, tôi là Mạnh Dũng, giọng đọc nam tính và chững chạc.", "mode": "v3nano"},
    {"id": "vieneu:HuuQuan",   "preset": "Hữu Quân",   "gender": "male",   "region": "Bắc", "desc": "Nam Bắc · Tin tức, phóng sự", "sample_text": "Xin chào, tôi là Hữu Quân, giọng đọc tin tức rõ ràng và dứt khoát.", "mode": "v3nano"},
    {"id": "vieneu:MaiAnh",    "preset": "Mai Anh",    "gender": "female", "region": "Bắc", "desc": "Nữ Bắc · Tin tức, thời sự", "sample_text": "Xin chào, tôi là Mai Anh, giọng đọc tin tức chuẩn và trang trọng.", "mode": "v3nano"},
    {"id": "vieneu:XuanTien",  "preset": "Xuân Tiên",  "gender": "female", "region": "Bắc", "desc": "Nữ Bắc · Phong cách tin tức", "sample_text": "Xin chào, tôi là Xuân Tiên, giọng đọc tin tức lưu loát.", "mode": "v3nano"},
    {"id": "vieneu:Adam",      "preset": "Adam",       "gender": "male",   "region": "Nam", "desc": "Nam Nam · Giọng đọc tự nhiên, hiện đại", "sample_text": "Xin chào bạn, tôi là Adam, giọng đọc miền Nam tự nhiên và hiện đại.", "mode": "v3nano"},
    {"id": "vieneu:AiHan",     "preset": "Ái Hân",     "gender": "female", "region": "Nam", "desc": "Nữ Nam · Tin tức, sinh động", "sample_text": "Xin chào bạn, tôi là Ái Hân, giọng đọc miền Nam sinh động và truyền cảm.", "mode": "v3nano"},
]

VIENEU_NAME_MAP = {
    "adambua": "Adam bựa", "adam bựa": "Adam bựa", "adam_bua": "Adam bựa", "adambựa": "Adam bựa", "bua": "Adam bựa", "bựa": "Adam bựa",
    "trucly": "Trúc Ly", "trúc ly": "Trúc Ly",
    "ductri": "Đức Trí", "đức trí": "Đức Trí",
    "thienminh": "Thiện Minh", "thiện minh": "Thiện Minh",
    "haidang": "Hải Đăng", "hải đăng": "Hải Đăng",
    "thuydung": "Thùy Dung", "thùy dung": "Thùy Dung",
    "quangson": "Quang Sơn", "quang sơn": "Quang Sơn",
    "myduyen": "Mỹ Duyên", "mỹ duyên": "Mỹ Duyên",
    "anhkhoi": "Anh Khôi", "anh khôi": "Anh Khôi",
    "minhquan": "Minh Quân", "minh quân": "Minh Quân",
    "manhdung": "Mạnh Dũng", "mạnh dũng": "Mạnh Dũng",
    "huuquan": "Hữu Quân", "hữu quân": "Hữu Quân",
    "maianh": "Mai Anh", "mai anh": "Mai Anh",
    "xuantien": "Xuân Tiên", "xuân tiên": "Xuân Tiên",
    "adam": "Adam",
    "aihan": "Ái Hân", "ái hân": "Ái Hân",
}


class VieNeuEngine:
    _nano_instance = None
    _turbo_instance = None

    @classmethod
    def get_nano_instance(cls):
        if cls._nano_instance is None:
            try:
                from vieneu import Vieneu
                logger.info("Initializing VieNeu-TTS (mode='v3nano' ONNX)...")
                cls._nano_instance = Vieneu(mode="v3nano")
                logger.info("VieNeu-TTS (v3nano) ready.")
            except Exception as e:
                logger.error(f"Failed to initialize VieNeu-TTS v3nano: {e}")
                raise
        return cls._nano_instance

    @classmethod
    def get_turbo_instance(cls):
        if cls._turbo_instance is None:
            try:
                from vieneu import Vieneu
                logger.info("Initializing VieNeu-TTS (mode='v3turbo' PyTorch)...")
                cls._turbo_instance = Vieneu(mode="v3turbo")
                logger.info("VieNeu-TTS (v3turbo) ready.")
            except Exception as e:
                logger.error(f"Failed to initialize VieNeu-TTS v3turbo: {e}")
                raise
        return cls._turbo_instance

    @classmethod
    def resolve_preset_name_and_mode(cls, voice_key: str):
        k = voice_key.replace("vieneu:", "").strip()
        normalized = k.lower()
        preset = VIENEU_NAME_MAP.get(normalized)
        mode = "v3nano"
        if not preset:
            for cfg in VIENEU_VOICES_CONFIG:
                if cfg["id"].lower() == voice_key.lower() or cfg["preset"].lower() == k.lower():
                    preset = cfg["preset"]
                    mode = cfg.get("mode", "v3nano")
                    break
        else:
            turbo_voices = {"Adam bựa", "Thiện Minh", "Hải Đăng", "Thùy Dung", "Quang Sơn"}
            mode = "v3turbo" if preset in turbo_voices else "v3nano"

        if not preset:
            preset = "Adam bựa" if ("bua" in normalized or "bựa" in normalized) else "Trúc Ly"
            mode = "v3turbo" if preset == "Adam bựa" else "v3nano"
        return preset, mode

    @classmethod
    def synthesize(
        cls,
        text: str,
        voice_key: str,
        output_path: str,
        ref_audio: Optional[str] = None,
        speed: float = 1.0,
    ) -> float:
        """Sinh audio VieNeu-TTS đồng bộ (hoặc bọc qua to_thread) và xuất ra MP3/WAV."""
        preset, mode = cls.resolve_preset_name_and_mode(voice_key)
        clean_text = text.strip() or "..."
        logger.info(f"[VieNeu] Synthesizing '{clean_text[:40]}...' with preset='{preset}', mode='{mode}'")

        try:
            engine = cls.get_turbo_instance() if mode == "v3turbo" else cls.get_nano_instance()
        except Exception as exc:
            logger.warning(f"Preferred VieNeu mode {mode} failed: {exc}. Trying fallback...")
            engine = cls.get_nano_instance() if mode == "v3turbo" else cls.get_turbo_instance()

        if ref_audio and os.path.exists(ref_audio):
            audio_data = engine.infer(text=clean_text, ref_audio=ref_audio, speed=speed)
        else:
            audio_data = engine.infer(text=clean_text, voice=preset, speed=speed)

        out_path_obj = Path(output_path)
        out_path_obj.parent.mkdir(parents=True, exist_ok=True)

        # Lưu ra file tạm WAV rồi chuyển sang output format (thường là MP3)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
            tmp_wav_path = tmp_wav.name

        try:
            engine.save(audio_data, tmp_wav_path)
            # Chuyển đổi định dạng nếu cần hoặc copy
            if out_path_obj.suffix.lower() == ".wav":
                import shutil
                shutil.copyfile(tmp_wav_path, output_path)
            else:
                cmd = [
                    FFMPEG_EXE, "-y",
                    "-i", tmp_wav_path,
                    "-codec:a", "libmp3lame",
                    "-qscale:a", "2",
                    output_path
                ]
                subprocess.run(cmd, check=True, capture_output=True)
        finally:
            if os.path.exists(tmp_wav_path):
                os.remove(tmp_wav_path)

        dur = get_media_duration(output_path)
        return dur if dur > 0.2 else max(2.0, len(clean_text) / 15.0)


# ── 2. OMNIVOICE ENGINE ─────────────────────────────────────────────────────────

OMNIVOICE_VOICES_CONFIG = [
    {
        "id": "omnivoice:vi_narrator_male",
        "label": "OmniVoice - Nam Thuyết Minh (Trầm ấm, chững chạc)",
        "gender": "male",
        "lang": "omnivoice",
        "instruct": "male, low pitch, middle-aged",
        "language": "vietnamese",
        "sample_text": "Chào mừng bạn đến với AI Movie Studio, hệ thống kiến tạo điện ảnh đa năng.",
    },
    {
        "id": "omnivoice:vi_narrator_female",
        "label": "OmniVoice - Nữ Dẫn Chuyện (Sâu lắng, truyền cảm)",
        "gender": "female",
        "lang": "omnivoice",
        "instruct": "female, moderate pitch, middle-aged",
        "language": "vietnamese",
        "sample_text": "Mỗi thước phim là một câu chuyện độc bản được kể bằng trái tim và công nghệ.",
    },
    {
        "id": "omnivoice:vi_casual_male",
        "label": "OmniVoice - Nam Tự Nhiên (Trẻ trung, hiện đại)",
        "gender": "male",
        "lang": "omnivoice",
        "instruct": "male, moderate pitch, young adult",
        "language": "vietnamese",
        "sample_text": "Chào các bạn! Hôm nay chúng ta sẽ cùng khám phá những công nghệ làm phim mới nhất.",
    },
    {
        "id": "omnivoice:vi_casual_female",
        "label": "OmniVoice - Nữ Tự Nhiên (Tươi sáng, năng động)",
        "gender": "female",
        "lang": "omnivoice",
        "instruct": "female, high pitch, young adult",
        "language": "vietnamese",
        "sample_text": "Xin chào mọi người! Cùng đồng hành và tận hưởng những khoảnh khắc tuyệt vời nhé.",
    },
    {
        "id": "omnivoice:en_cinematic_male",
        "label": "OmniVoice - English Cinematic Narrator (Deep Trailer)",
        "gender": "male",
        "lang": "omnivoice",
        "instruct": "male, very low pitch, middle-aged, american accent",
        "language": "english",
        "sample_text": "In a world reshaped by artificial intelligence, cinema enters a brand new era.",
    },
    {
        "id": "omnivoice:en_cinematic_female",
        "label": "OmniVoice - English Cinematic Narrator (British Eloquent)",
        "gender": "female",
        "lang": "omnivoice",
        "instruct": "female, moderate pitch, young adult, british accent",
        "language": "english",
        "sample_text": "Welcome to the future of creative filmmaking, powered by intelligent autonomous agents.",
    },
]


class OmniVoiceEngine:
    _instance = None
    _loading = False

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            import torch
            from omnivoice.models.omnivoice import OmniVoice
            logger.info("Loading OmniVoice model (k2-fsa/OmniVoice on CPU)...")
            cls._instance = OmniVoice.from_pretrained(
                "k2-fsa/OmniVoice",
                device_map="cpu",
                dtype=torch.float32,
            )
            logger.info("OmniVoice model loaded successfully.")
        return cls._instance

    @classmethod
    def is_available(cls) -> bool:
        try:
            import omnivoice
            return True
        except ImportError:
            return False

    @classmethod
    def synthesize(
        cls,
        text: str,
        voice_key: str,
        output_path: str,
        ref_audio: Optional[str] = None,
        ref_text: Optional[str] = None,
        instruct: Optional[str] = None,
        speed: float = 1.0,
    ) -> float:
        """Sinh audio bằng OmniVoice (k2-fsa/OmniVoice)."""
        import soundfile as sf
        model = cls.get_instance()

        clean_text = text.strip()
        if not clean_text:
            clean_text = "..."

        # Tìm config mặc định theo voice_key
        cfg = next((c for c in OMNIVOICE_VOICES_CONFIG if c["id"] == voice_key), None)
        lang = cfg.get("language", "vietnamese") if cfg else "vietnamese"
        final_instruct = instruct or (cfg.get("instruct") if cfg else None)

        logger.info(f"[OmniVoice] Synthesizing '{clean_text[:40]}...' lang={lang}, instruct='{final_instruct}', ref_audio={ref_audio}")

        audios = model.generate(
            text=clean_text,
            language=lang,
            instruct=final_instruct,
            ref_audio=ref_audio if (ref_audio and os.path.exists(ref_audio)) else None,
            ref_text=ref_text,
            speed=speed,
            num_step=16,
        )

        out_path_obj = Path(output_path)
        out_path_obj.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
            tmp_wav_path = tmp_wav.name

        try:
            sf.write(tmp_wav_path, audios[0], model.sampling_rate)
            if out_path_obj.suffix.lower() == ".wav":
                import shutil
                shutil.copyfile(tmp_wav_path, output_path)
            else:
                cmd = [
                    FFMPEG_EXE, "-y",
                    "-i", tmp_wav_path,
                    "-codec:a", "libmp3lame",
                    "-qscale:a", "2",
                    output_path
                ]
                subprocess.run(cmd, check=True, capture_output=True)
        finally:
            if os.path.exists(tmp_wav_path):
                os.remove(tmp_wav_path)

        dur = get_media_duration(output_path)
        return dur if dur > 0.2 else max(2.0, len(clean_text) / 15.0)


# ── 3. MICROSOFT EDGE-TTS & SILENT FALLBACK ────────────────────────────────────

EDGE_VOICES_CONFIG = [
    # Tiếng Việt
    {"id": "vi-VN-HoaiMyNeural",     "label": "Hoài My (Truyền cảm, tự nhiên)",   "lang": "vi", "gender": "female", "flag": "VN"},
    {"id": "vi-VN-NamMinhNeural",    "label": "Nam Minh (Trầm ấm, nội lực)",     "lang": "vi", "gender": "male",   "flag": "VN"},
    # Tiếng Anh
    {"id": "en-US-ChristopherNeural","label": "Christopher (Nam US)",             "lang": "en", "gender": "male",   "flag": "US"},
    {"id": "en-US-JennyNeural",      "label": "Jenny (Nữ US)",                   "lang": "en", "gender": "female", "flag": "US"},
    {"id": "en-US-GuyNeural",        "label": "Guy (Nam US - Hùng biện)",        "lang": "en", "gender": "male",   "flag": "US"},
    {"id": "en-US-AriaNeural",       "label": "Aria (Nữ US - Tự nhiên)",         "lang": "en", "gender": "female", "flag": "US"},
    {"id": "en-GB-RyanNeural",       "label": "Ryan (Nam UK)",                   "lang": "en", "gender": "male",   "flag": "GB"},
    {"id": "en-GB-SoniaNeural",      "label": "Sonia (Nữ UK)",                   "lang": "en", "gender": "female", "flag": "GB"},
    # Tiếng Trung
    {"id": "zh-CN-XiaoxiaoNeural",   "label": "Xiaoxiao (Nữ CN)",                "lang": "zh", "gender": "female", "flag": "CN"},
    {"id": "zh-CN-YunxiNeural",      "label": "Yunxi (Nam CN)",                  "lang": "zh", "gender": "male",   "flag": "CN"},
    # Tiếng Nhật
    {"id": "ja-JP-NanamiNeural",     "label": "Nanami (Nữ JP)",                  "lang": "ja", "gender": "female", "flag": "JP"},
    {"id": "ja-JP-KeitaNeural",      "label": "Keita (Nam JP)",                   "lang": "ja", "gender": "male",   "flag": "JP"},
    # Tiếng Hàn
    {"id": "ko-KR-SunHiNeural",      "label": "SunHi (Nữ KR)",                   "lang": "ko", "gender": "female", "flag": "KR"},
    {"id": "ko-KR-InJoonNeural",     "label": "InJoon (Nam KR)",                 "lang": "ko", "gender": "male",   "flag": "KR"},
    # Tiếng Thái
    {"id": "th-TH-PremwadeeNeural",  "label": "Premwadee (Nữ TH)",               "lang": "th", "gender": "female", "flag": "TH"},
    {"id": "th-TH-NiwatNeural",      "label": "Niwat (Nam TH)",                  "lang": "th", "gender": "male",   "flag": "TH"},
]


async def synthesize_edge_tts(text: str, voice: str, output_path: str) -> float:
    """Sinh audio bằng edge_tts không đồng bộ."""
    import edge_tts
    clean_text = text.strip()
    estimated_dur = max(2.0, len(clean_text) / 15.0)
    communicate = edge_tts.Communicate(clean_text, voice)
    await communicate.save(output_path)
    actual_dur = get_media_duration(output_path)
    return actual_dur if actual_dur and actual_dur > 0.3 else estimated_dur


def create_silent_audio_fallback(output_path: str, duration: float = 3.0) -> float:
    """Tạo audio im lặng dự phòng khẩn cấp để render video không bao giờ bị fail."""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    cmd = [
        FFMPEG_EXE, "-y",
        "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
        "-t", str(duration),
        "-q:a", "9",
        "-acodec", "libmp3lame",
        output_path
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
    except Exception as e:
        logger.error(f"Failed to create silent fallback audio: {e}")
    return duration


# ── 4. UNIFIED DISPATCHER & VOICE CATALOG ─────────────────────────────────────

def list_all_voices() -> List[Dict[str, Any]]:
    """Trả về toàn bộ danh mục giọng đọc theo thứ tự danh mục trực quan."""
    voices = []

    # 1. AI Native Voice
    voices.append({
        "id": "ai_voice",
        "label": "Giọng & Âm thanh gốc AI (Muse AI tự sinh tiếng & khẩu hình trực tiếp)",
        "lang": "ai",
        "gender": "neutral",
        "flag": "AI",
        "badge": "Native AI",
    })

    # 2. VieNeu-TTS (On-device neural Vietnamese TTS)
    for v in VIENEU_VOICES_CONFIG:
        voices.append({
            "id": v["id"],
            "label": f"{v['preset']} ({v['desc']})",
            "lang": "vieneu",
            "gender": v["gender"],
            "flag": "VN",
            "region": v["region"],
            "badge": "VieNeu-TTS",
        })

    # 3. OmniVoice (Multilingual Voice Design & Zero-Shot Cloning)
    for v in OMNIVOICE_VOICES_CONFIG:
        voices.append({
            "id": v["id"],
            "label": v["label"],
            "lang": "omnivoice",
            "gender": v["gender"],
            "flag": "OMNI",
            "badge": "OmniVoice",
        })

    # 4. Microsoft Edge-TTS (Cloud Voices)
    for v in EDGE_VOICES_CONFIG:
        voices.append({
            "id": v["id"],
            "label": v["label"],
            "lang": v["lang"],
            "gender": v["gender"],
            "flag": v["flag"],
            "badge": "Edge-TTS",
        })

    return voices


async def synthesize_voice(
    text: str,
    voice: str,
    output_path: str,
    ref_audio: Optional[str] = None,
    instruct: Optional[str] = None,
    speed: float = 1.0,
) -> float:
    """
    Hàm phân phối TTS duy nhất cho toàn bộ hệ thống AI Movie Studio.
    Hỗ trợ VieNeu-TTS, OmniVoice, Edge-TTS, AI Native Voice, tự động fallback an toàn.
    """
    clean_text = text.strip()
    if not clean_text or voice in ("ai_voice", "native", "none", "no_dub"):
        return 0.0

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    estimated_dur = max(2.0, len(clean_text) / 15.0)

    # 1. Dispatch VieNeu-TTS
    if voice.startswith("vieneu:"):
        try:
            dur = await asyncio.to_thread(
                VieNeuEngine.synthesize,
                text=clean_text,
                voice_key=voice,
                output_path=output_path,
                ref_audio=ref_audio,
                speed=speed,
            )
            if dur and dur > 0.2:
                return dur
        except Exception as e:
            logger.warning(f"[VieNeu Error] {e}. Falling back to Edge-TTS...")
            # Fallback sang Edge-TTS theo giới tính
            fb_voice = "vi-VN-NamMinhNeural" if any(x in voice.lower() for x in ["nam", "ductri", "adam", "anhkhoi", "huuquan", "minhquan", "manhdung"]) else "vi-VN-HoaiMyNeural"
            try:
                return await synthesize_edge_tts(clean_text, fb_voice, output_path)
            except Exception as e2:
                logger.error(f"[TTS Fallback Error] {e2}. Creating silent audio.")
                return create_silent_audio_fallback(output_path, estimated_dur)

    # 2. Dispatch OmniVoice
    elif voice.startswith("omnivoice:"):
        try:
            dur = await asyncio.to_thread(
                OmniVoiceEngine.synthesize,
                text=clean_text,
                voice_key=voice,
                output_path=output_path,
                ref_audio=ref_audio,
                instruct=instruct,
                speed=speed,
            )
            if dur and dur > 0.2:
                return dur
        except Exception as e:
            logger.warning(f"[OmniVoice Error] {e}. Falling back to VieNeu / Edge-TTS...")
            # Thử fallback sang VieNeu trước
            try:
                fb_vieneu = "vieneu:DucTri" if "male" in voice.lower() else "vieneu:TrucLy"
                return await asyncio.to_thread(
                    VieNeuEngine.synthesize,
                    text=clean_text,
                    voice_key=fb_vieneu,
                    output_path=output_path,
                    speed=speed,
                )
            except Exception:
                try:
                    fb_voice = "vi-VN-NamMinhNeural" if "male" in voice.lower() else "vi-VN-HoaiMyNeural"
                    return await synthesize_edge_tts(clean_text, fb_voice, output_path)
                except Exception as e3:
                    logger.error(f"[TTS Fallback Error] {e3}. Creating silent audio.")
                    return create_silent_audio_fallback(output_path, estimated_dur)

    # 3. Dispatch Edge-TTS
    else:
        try:
            return await synthesize_edge_tts(clean_text, voice, output_path)
        except Exception as e:
            logger.warning(f"[Edge-TTS Error] {e}. Trying VieNeu fallback...")
            try:
                # Nếu là tiếng Việt thì fallback sang VieNeu
                if voice.startswith("vi"):
                    fb_vn = "vieneu:DucTri" if "nam" in voice.lower() else "vieneu:TrucLy"
                    return await asyncio.to_thread(
                        VieNeuEngine.synthesize,
                        text=clean_text,
                        voice_key=fb_vn,
                        output_path=output_path,
                        speed=speed,
                    )
            except Exception:
                pass
            return create_silent_audio_fallback(output_path, estimated_dur)
