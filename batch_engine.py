import sys
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
"""
batch_engine.py
---------------
Batch Async Engine: Xử lý hàng loạt cảnh video song song đa luồng.
Mỗi Worker độc lập: Sinh TTS + Gửi Muse2API + Poll + Tải video.
"""

import asyncio
import os
import time
import subprocess
import httpx
import edge_tts
import aiofiles
from typing import List, Dict, Any, Callable, Optional


def get_media_duration(file_path: str) -> float:
    """Đo thời lượng thực tế chính xác (giây) của file audio hoặc video bằng FFmpeg."""
    try:
        import subprocess
        import re
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        res = subprocess.run(
            [ffmpeg, "-i", file_path],
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
            errors="replace"
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", res.stderr)
        if m:
            return float(m.group(1)) * 3600 + float(m.group(2)) * 60 + float(m.group(3))
    except Exception as exc:
        print(f"[get_media_duration error] {exc}")
    return 5.0


async def generate_tts(text: str, voice: str, output_path: str) -> float:
    """
    Sinh file audio TTS qua hệ thống đa engine tts_manager (VieNeu-TTS, OmniVoice, Edge-TTS).
    Hỗ trợ voice riêng từng nhân vật (per-scene voice).
    Có cơ chế dự phòng an toàn (silent fallback) để không bao giờ làm gián đoạn việc sinh video.
    Trả về thời lượng thực tế chính xác của file âm thanh.
    """
    try:
        from tts_manager import synthesize_voice
        return await synthesize_voice(text=text, voice=voice, output_path=output_path)
    except Exception as e:
        print(f"[TTS Fallback] Lỗi tts_manager ({e}), đang tạo audio dự phòng...")
        try:
            from tts_manager import create_silent_audio_fallback
            estimated_dur = max(2.0, len(text.strip()) / 15.0)
            return create_silent_audio_fallback(output_path, estimated_dur)
        except Exception:
            estimated_dur = max(2.0, len(text.strip()) / 15.0)
            return estimated_dur


async def generate_multi_voice_tts(
    segments: list,
    output_path: str,
    tmp_dir: str,
) -> float:
    """
    Sinh audio nhiều nhân vật, mỗi đoạn 1 giọng riêng, ghép lại thành 1 file.
    segments: [{"text": "...", "voice": "vi-VN-HoaiMyNeural"}, ...]
    """
    import shutil
    import subprocess
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

    os.makedirs(tmp_dir, exist_ok=True)
    seg_files = []
    total_duration = 0.0

    for i, seg in enumerate(segments):
        seg_path = os.path.join(tmp_dir, f"seg_{i:03d}.mp3")
        dur = await generate_tts(seg["text"], seg["voice"], seg_path)
        seg_files.append(seg_path)
        total_duration += dur

    if len(seg_files) == 1:
        shutil.copy2(seg_files[0], output_path)
        actual_dur = get_media_duration(output_path)
        return actual_dur if actual_dur > 0.3 else total_duration

    # Ghép nhiều đoạn bằng ffmpeg concat (chống lỗi đường dẫn Windows)
    concat_list = os.path.join(tmp_dir, "concat.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for fp in seg_files:
            f.write(f"file '{os.path.basename(fp)}'\n")
    abs_out = os.path.abspath(output_path)
    subprocess.run(
        [FFMPEG, "-y", "-f", "concat", "-safe", "0",
         "-i", "concat.txt", "-c", "copy", abs_out],
        cwd=tmp_dir,
        check=True, capture_output=True,
    )
    try: os.remove(concat_list)
    except: pass
    for fp in seg_files:
        try: os.remove(fp)
        except: pass

    actual_dur = get_media_duration(output_path)
    return actual_dur if actual_dur > 0.3 else total_duration



import re

GENERIC_POLICY_REPLACEMENTS = [
    # 1. Smashing / Crushing / Shattering / Pulverizing
    (r"\b(?:raising\s+(?:a\s+)?massive\s+\w+\s+and\s+)?smashing\s+(?:it\s+)?down\s+onto\b", "unleashing a sweeping radiant shockwave across"),
    (r"\bsmashing\s+(?:down\s+(?:onto|on)|into|through|apart)\b", "unleashing brilliant energy shockwaves across"),
    (r"\bviolently\s+crushing\b", "firmly gripping"),
    (r"\bcrushing\s+(?:white\s+marble\s+debris|marble|stone|armor|rocks?|metal)\s+into\s+dust\b", "holding glowing crystalline fragments as magical sparks drift"),
    (r"\bcrushing\s+(?:a\s+piece\s+of\s+)?(?:shattered|broken)\b", "examining an ancient glowing"),
    (r"\bcrush(?:ing|es|ed)?\b", "channeling energy into"),
    (r"\bshatter(?:ing|s|ed)?\b", "illuminating with brilliant fractures of light"),
    (r"\bexplodes?\s+.*?\s+into\s+flying\s+shards\b", "emanates bursts of brilliant arcane light"),

    # 2. Hurling bodies / Slamming into abyss / Violent physical knockouts
    (r"\bhurling\s+(?:[a-zA-Z\s]+?)\s+through\s+the\s+air\b", "creating a dramatic gust of glowing wind and drifting embers"),
    (r"\bhurling\b", "sweeping past"),
    (r"\bslamming\s+(?:[a-zA-Z\s]+?)\s+(?:clean\s+)?off\s+the\s+(?:edge\s+of\s+the\s+)?(?:\w+)\s*(?:into\s+the\s+(?:dark\s+)?abyss\s*(?:below)?)?\b", "executing a breathtaking high-speed cinematic pass alongside the opponent"),
    (r"\bslamming\s+(?:into|onto|against)\b", "charging with blazing speed past"),
    (r"\binto\s+the\s+(?:dark\s+)?abyss\s*(?:below)?\b", "across the expansive cinematic realm"),

    # 3. Penetrating attacks / Stabbing / Impaling
    (r"\bplung(?:ing|es|ed)?\s+(?:his\s+|her\s+)?(?:glowing\s+)?(?:greatsword|sword|spear|weapon|blade|dagger|knife)\s+into\b", "raising radiant weapon toward"),
    (r"\bsplitting\s+the\s+(?:entire\s+)?(?:\w+)\s+in\s+half\b", "unleashing an ethereal dome of golden light and drifting sparks across the realm"),
    (r"\bstabb?(?:ing|s|ed)?\s*(?:into|through)?\b", "clashing dynamically against"),
    (r"\bimpales?\b", "outmaneuvers"),

    # 4. Blood / Gory / Death / Mutilation
    (r"\b(?:blood|bloody|bleeding|blood-stained)\b", "dramatic crimson rim light"),
    (r"\bdeath|dying|suicidal|corpse|dead\s+body\b", "unyielding defiant"),
    (r"\bgore|gory|severed|decapitated|slaughter|massacre|bloodbath\b", "epic cinematic duel"),
    (r"\bseverely\s+injured\b", "battle-tested warrior"),
    (r"\bleaking\s+thick\s+(?:black\s+)?oil\b", "dark metallic armor reflecting bright embers"),

    # 5. Firearms / Modern weaponry in fantasy
    (r"\bbarrel\s+of\s+(?:her\s+|his\s+)?(?:silver\s+)?gun\b", "tip of mystical scepter"),
    (r"\bholding\s+a\s+(?:silver\s+)?gun\b", "holding a mystical silver scepter"),
    (r"\bsilver\s+gun\b", "silver glowing relic"),
    (r"\bexplosions?\s+blast\b", "magical shockwaves illuminate"),
    (r"\bcamera\s+violently\s+shakes\b", "camera dynamically tracks"),

    # 6. Combat actions, archery, daggers, and direct strikes
    (r"\b(?:releasing|shoots?|firing)\s+(?:a\s+)?(?:glowing\s+)?(?:flying\s+)?arrows?\s+(?:toward|at)\b", "releasing a streak of radiant starlight soaring toward"),
    (r"\b(?:throwing|unleashes?)\s+(?:a\s+)?(?:volley\s+of\s+)?(?:silver\s+)?daggers?\b", "conjuring shimmering silver light darts"),
    (r"\bvolleys?\s+of\s+(?:silver\s+)?daggers?\b", "shimmering silver light darts"),
    (r"\bdeflect(?:ing|s|ed)?\s+(?:the\s+)?(?:flying\s+)?arrows?\b", "deflecting incoming trails of light"),
    (r"\bstrikes?\s+(?:the\s+)?(?:archer|enemy|opponent|target|assassin)\b", "sweeps past the target"),
    (r"\bcuts?\s+diagonally\b", "traces a diagonal arc of silver light"),
    (r"\bdisarming\s+([a-zA-Z\s]+?)\s+in\s+one\s+fluid\s+movement\b", r"outmaneuvering \1 with breathtaking fluid elegance"),
    (r"\bcollapses?\s+onto\s+the\s+(?:muddy\s+)?ground\s+in\s+defeat\b", "drops into a low stance on the ground"),
    (r"\bpointing\s+(?:his|her)?\s*(?:wet\s+)?(?:blade|sword)\s+at\b", "holding a glowing artifact toward"),
    (r"\bgiant\s+sabre\b", "ancient ceremonial blade"),
    (r"\bsabres?\b", "ancient ceremonial blade"),

    # 7. Restraint / Grabbing / Capturing / Choking / Hands of darkness (Chống lỗi 束缚动作)
    (r"\b(?:hands?\s+of\s+)?(?:dark\s+)?(?:mist|fog|smoke|shadows?)\s+(?:hands?\s+)?grabb?(?:ing|s|ed)?\s+([a-zA-Z\s]+?)\b", r"swirling dark mist gently encircling \1"),
    (r"\b(?:dark\s+)?(?:mist|fog|shadows?)\s+(?:reaches?\s+out\s+and\s+)?grabb?(?:ing|s|ed)?\b", "ethereal dark mist swirling gracefully around"),
    (r"\bgrabb?(?:ing|s|ed)?\s+(?:her|him|them|the\s+\w+)(?:\s+by\s+(?:the\s+)?(?:neck|throat|arm|wrist|hair))?\b", "reaching forward toward the silhouette"),
    (r"\bgrabb?(?:ing|s|ed)?\b", "reaching toward"),
    (r"\brestrain(?:ing|s|ed)?\b", "encircling in dramatic shadows"),
    (r"\bchok(?:ing|es|ed)?|strangl(?:ing|es|ed)?\b", "gasping in high cinematic tension"),
    (r"\bseiz(?:ing|es|ed)?|snatch(?:ing|es|ed)?\b", "surging dynamically toward"),
    (r"\bbound\s+by\s+(?:chains?|ropes?|dark\s+mist|shadows?)\b", "shrouded in dramatic swirling mist"),
    (r"\bclutch(?:ing|es|ed)?\s+(?:her|his|the)?\s*(?:throat|neck)\b", "looking upward in atmospheric shock"),
    (r"\btrapped\s+inside\s+(?:the\s+)?(?:mirror|glass|darkness)\b", "reflected enigmatically within the mirror"),
    (r"\bdrag(?:ging|ged|s)?\s+(?:her|him|them)\s+(?:into|down|away)\b", "gliding with camera tracking backward"),
]


def clean_script_prompt(raw_prompt: str) -> tuple[str, Optional[str]]:
    """
    Tự động chuẩn hoá prompt từ kịch bản thô:
    - Loại bỏ mốc thời gian: '0-10s:', '10-20s:'
    - Giữ lại mô tả thị giác từ '[Màu sắc: ...]'
    - Tách lời thoại trong ngoặc kép hoặc '[Lồng tiếng: ...]' để làm voiceover
    - Trả về (clean_prompt, extracted_dialogue)
    """
    p = raw_prompt.strip()
    extracted_voice = None

    # Trích xuất Màu sắc nếu có
    color_note = ""
    col_m = re.search(r"\[(?:Màu\s*sắc|Color|Tone):\s*([^\]]+)\]", p, re.IGNORECASE)
    if col_m:
        color_note = col_m.group(1).strip()

    # Bóc tách câu thoại từ [Lồng tiếng: ...] nếu có (hỗ trợ cả trong và ngoài ngoặc vuông)
    voice_match = re.search(r"\[(?:Lồng\s*tiếng|Voiceover|Thoại)[^\]]*\]\s*([^\"“\n\r]*?)\s*[:：]\s*[\"“]([^\"”\r\n]+)[\"”]", p, re.IGNORECASE)
    if voice_match:
        extracted_voice = voice_match.group(2).strip()
    else:
        voice_match_inside = re.search(r"\[(?:Lồng\s*tiếng|Voiceover|Thoại)[^\]]*?[:：]\s*[\"“]?([^\"”\]\r\n]+)[\"”]?[^\]]*\]", p, re.IGNORECASE)
        if voice_match_inside:
            extracted_voice = voice_match_inside.group(1).strip()

    # Xóa mốc thời gian và các thẻ cấu trúc [...]
    p = re.sub(r"^\s*\d+\s*[-–—]\s*\d+\s*s?\s*:\s*", "", p, flags=re.IGNORECASE)
    without_tags = re.sub(r"\[[^\]]+\]", " ", p)
    without_tags = re.sub(r"\s+", " ", without_tags).strip()

    # Bóc tách thoại trong ngoặc kép nếu chưa có extracted_voice
    quote_matches = list(re.finditer(r'["“]([^"”\n\r]+)["”]', without_tags))
    speech_cuts = []
    if quote_matches:
        for qm in quote_matches:
            speech = qm.group(1).strip()
            if not extracted_voice:
                extracted_voice = speech
            q_idx = qm.start()
            before = without_tags[:q_idx].strip()
            spk_match = re.search(r'(?:^|[,\.!\?;\n])\s*([^,.\n:"]*?)\s*(?:nói|bảo|hỏi|đáp|thì thầm|gào thét|gầm lên|ngắt quãng|lẩm bẩm|hô|quát)?[^,.\n:"]*:\s*$', before, re.IGNORECASE)
            lead_in_start = q_idx
            if spk_match:
                full_m = spk_match.group(0)
                m_idx = before.rfind(full_m)
                sep_m = re.match(r'^[,\.!\?;\n]\s*', full_m)
                sep_len = len(sep_m.group(0)) if sep_m else 0
                lead_in_start = m_idx + sep_len
            speech_cuts.append((lead_in_start, qm.end()))

    # Dựng prompt thị giác không chứa thoại
    clean_prompt = ""
    last_idx = 0
    for s_cut, e_cut in speech_cuts:
        clean_prompt += without_tags[last_idx:s_cut] + " "
        last_idx = e_cut
    clean_prompt += without_tags[last_idx:]
    clean_prompt = re.sub(r"\s+", " ", clean_prompt).strip(" ,.")

    if color_note and color_note.lower() not in clean_prompt.lower():
        clean_prompt = f"{clean_prompt}, {color_note}" if clean_prompt else color_note

    return clean_prompt, extracted_voice


def sanitize_prompt_heuristics(prompt: str, error_msg: str = "", aggressive: bool = False) -> str:
    """
    Bộ làm sạch Prompt HOÀN TOÀN TỰ ĐỘNG & ĐỘC LẬP:
    - Loại bỏ triệt để mọi từ khóa bạo lực thể xác, máu me, vũ khí sát thương, bắt giữ/trói buộc (束缚动作).
    - Tự động thay thế từ vựng tiếng Trung / tiếng Việt vi phạm thành ngôn ngữ điện ảnh an toàn.
    - Chuyển thành chuyển động máy quay (Camera Move) và ánh sáng điện ảnh an toàn 100%.
    """
    p = prompt
    for pattern, repl in GENERIC_POLICY_REPLACEMENTS:
        p = re.sub(pattern, repl, p, flags=re.IGNORECASE)

    # Từ vựng tiếng Trung / tiếng Việt thường gặp khi prompt dịch hoặc đa ngôn ngữ
    zh_vn_replaces = [
        (r"黑雾之手抓住", "黑雾轻柔环绕"),
        (r"抓住", "伸向"),
        (r"束缚", "环绕"),
        (r"掐住|勒住", "注视"),
        (r"bắt lấy|tóm lấy|chộp lấy", "vươn tay về phía"),
        (r"trói buộc|ghì chặt", "bao quanh bởi làn sương"),
        (r"đánh đập|bóp cổ", "kịch tính điện ảnh"),
    ]
    for pattern, repl in zh_vn_replaces:
        p = re.sub(pattern, repl, p)

    is_refusal_trigger = aggressive or any(k in error_msg.lower() for k in [
        "未生成video", "踩线", "束缚", "violation", "refused", "policy", "từ chối", "safety"
    ])
    if is_refusal_trigger:
        # Làm sạch quyết liệt: thay thế toàn bộ động từ tương tác thể xác bằng chuyển động máy quay
        p = re.sub(r"\b(?:kill|attack|destroy|strike|smash|hit|slam|grab|seize|restrain|drag|choke|pull)\b", "move with", p, flags=re.IGNORECASE)
        p = re.sub(r"\b(?:weapon|blade|sword|lance|spear|knife|chains?)\b", "glowing artifact", p, flags=re.IGNORECASE)
        p = re.sub(r"\b(?:violent|aggressive|deadly|brutal|bloody|dark\s+mirror-self)\b", "mysterious ethereal double", p, flags=re.IGNORECASE)
        if not re.search(r"\b(?:slow|camera|push-in|zoom|pan|tracking)\b", p, re.IGNORECASE):
            p = f"Slow cinematic push-in, {p}"

    # Đảm bảo có phong cách điện ảnh chuẩn
    if "cinematic" not in p.lower():
        p += ", cinematic lighting, 4k resolution, masterpiece"

    return re.sub(r"\s+", " ", p).strip()


async def heal_prompt_with_ai(
    prompt: str,
    error_msg: str,
    base_url: str = "http://localhost:18610",
    api_key: str = "m2a_studio_secret_2026",
    timeout: float = 6.0
) -> str:
    """
    Tối ưu prompt tự động: áp dụng bộ làm sạch an toàn cục bộ ngay lập tức,
    không thực hiện các cuộc gọi API phụ gây nghẽn và spam máy chủ.
    """
    return sanitize_prompt_heuristics(prompt, error_msg, aggressive=True)


async def poll_video_task(
    client: httpx.AsyncClient,
    task_id: str,
    base_url: str,
    headers: dict,
    scene_idx: int,
    progress_cb: Optional[Callable] = None,
) -> str:
    """
    Poll trạng thái task video thích ứng & không spam API:
    - Không giới hạn thời gian chờ quá sớm (hỗ trợ queue nhiều tài khoản).
    """
    start_time = time.time()
    max_wait_seconds = 600  # 10 phút tối đa (đảm bảo đủ thời gian render video AI & xoay vòng tài khoản)
    attempt = 0
    consecutive_net_errors = 0

    while time.time() - start_time < max_wait_seconds:
        attempt += 1
        elapsed = int(time.time() - start_time)

        if elapsed < 20:
            sleep_interval = 6
        elif elapsed < 60:
            sleep_interval = 5
        else:
            sleep_interval = 4

        await asyncio.sleep(sleep_interval)

        try:
            res = await client.get(
                f"{base_url}/v1/videos/{task_id}",
                headers=headers,
                timeout=25.0,
            )
            consecutive_net_errors = 0
            data = res.json()
            status = data.get("status", "unknown")

            if status == "completed":
                url = data.get("video_url") or data.get("url") or ""
                return f"{base_url}{url}" if url.startswith("/") else url

            if status == "failed":
                err_msg = data.get("error", "N/A")
                raise RuntimeError(
                    f"[Cảnh {scene_idx}] Muse2API báo thất bại: {err_msg}"
                )

            prog = data.get("progress")
            acc = data.get("account")
            parts = [f"Đang render... ({elapsed}s)"]
            if prog:
                parts.append(f"{prog}%")
            if acc:
                parts.append(f"acc:{acc[:6]}")
            if progress_cb:
                progress_cb(scene_idx, " • ".join(parts))

        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as net_err:
            consecutive_net_errors += 1
            if consecutive_net_errors >= 15:
                raise RuntimeError(f"[Cảnh {scene_idx}] Mất kết nối liên tục với máy chủ Muse2API ({net_err})")
            await asyncio.sleep(4)

    raise TimeoutError(f"[Cảnh {scene_idx}] Đã hết thời gian chờ tối đa ({int(max_wait_seconds/60)} phút)")


def extract_last_frame(video_path: str, output_image_path: str) -> Optional[str]:
    """
    Trích xuất khung hình cuối cùng (last frame) của video MP4 bằng FFmpeg.
    Dùng làm first_frame cho cảnh tiếp theo theo kỹ thuật First/Last Frame Chaining.
    """
    if not os.path.exists(video_path) or os.path.getsize(video_path) == 0:
        return None

    import imageio_ffmpeg
    import subprocess
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
    os.makedirs(os.path.dirname(os.path.abspath(output_image_path)), exist_ok=True)

    # Chiến lược 1: Seek tới 0.1s trước khi kết thúc video (-sseof -0.1)
    try:
        cmd1 = [
            FFMPEG, "-y",
            "-sseof", "-0.1",
            "-i", video_path,
            "-update", "1",
            "-frames:v", "1",
            "-q:v", "2",
            output_image_path
        ]
        subprocess.run(cmd1, capture_output=True)
        if os.path.exists(output_image_path) and os.path.getsize(output_image_path) > 300:
            return output_image_path
    except Exception:
        pass

    # Chiến lược 2: Đảo ngược video và lấy frame đầu tiên (-vf reverse -frames:v 1)
    try:
        cmd2 = [
            FFMPEG, "-y",
            "-i", video_path,
            "-vf", "reverse",
            "-frames:v", "1",
            "-q:v", "2",
            output_image_path
        ]
        subprocess.run(cmd2, capture_output=True)
        if os.path.exists(output_image_path) and os.path.getsize(output_image_path) > 300:
            return output_image_path
    except Exception:
        pass

def apply_style_locks(prompt: str, style_locks: Optional[Dict[str, str]] = None) -> str:
    """Ghép các khóa phong cách điện ảnh (Style, Lighting, Color grade, Camera lens) vào prompt."""
    if not style_locks:
        return prompt
    parts = [prompt.strip()]
    style = style_locks.get("style", "").strip()
    lighting = style_locks.get("lighting", "").strip()
    color = style_locks.get("color", "").strip()
    camera = style_locks.get("camera", "").strip()
    character = style_locks.get("character", "").strip()

    if style and style.lower() not in prompt.lower():
        parts.append(f"Style: {style}")
    if lighting and lighting.lower() not in prompt.lower():
        parts.append(f"Lighting: {lighting}")
    if color and color.lower() not in prompt.lower():
        parts.append(f"Color grading: {color}")
    if camera and camera.lower() not in prompt.lower():
        parts.append(f"Camera: {camera}")
    if character and character.lower() not in prompt.lower():
        parts.append(f"Character: {character}")

    return ", ".join(parts)


async def generate_keyframe_image(
    prompt: str,
    base_url: str,
    api_key: str,
    output_path: str,
    aspect_ratio: str = "9:16",
    reference_image: Optional[str] = None,
) -> Dict[str, Any]:
    """Tạo ảnh tĩnh Keyframe trước khi render video qua muse2api với polling an toàn."""
    payload: Dict[str, Any] = {
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "size": aspect_ratio,
        "model": "muse-image",
        "response_format": "url",
        "async": True,
    }
    if reference_image:
        payload["reference_image"] = reference_image

    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Gửi request tạo task async
        try:
            r = await client.post(f"{base_url}/v1/images/tasks", json=payload, headers=headers)
        except Exception:
            r = await client.post(f"{base_url}/v1/images/generations", json=payload, headers=headers)

        if r.status_code in (200, 202):
            data = r.json()
            task_id = data.get("id") or data.get("task_id")
            if task_id:
                # Poll cho tới khi hoàn tất (tối đa 180s)
                start_t = time.time()
                while time.time() - start_t < 180:
                    await asyncio.sleep(4)
                    poll_res = await client.get(f"{base_url}/v1/images/tasks/{task_id}", headers=headers)
                    if poll_res.status_code == 200:
                        pdata = poll_res.json()
                        st = pdata.get("status")
                        if st == "completed":
                            img_url = pdata.get("url") or (pdata.get("data", [{}])[0].get("url") if pdata.get("data") else None)
                            if img_url:
                                download_url = f"{base_url}{img_url}" if img_url.startswith("/") else img_url
                                img_res = await client.get(download_url, timeout=30.0)
                                img_res.raise_for_status()
                                os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
                                async with aiofiles.open(output_path, "wb") as f:
                                    await f.write(img_res.content)
                                return {"url": download_url, "local_path": output_path}
                        elif st == "failed":
                            err_msg = pdata.get("error", "Muse AI báo lỗi khi tạo ảnh")
                            raise RuntimeError(f"Lỗi sinh ảnh keyframe: {err_msg}")
                raise TimeoutError("Quá thời gian chờ tạo ảnh keyframe (180s)")
            elif "data" in data and data["data"]:
                img_url = data["data"][0].get("url")
                if img_url:
                    download_url = f"{base_url}{img_url}" if img_url.startswith("/") else img_url
                    img_res = await client.get(download_url, timeout=30.0)
                    img_res.raise_for_status()
                    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
                    async with aiofiles.open(output_path, "wb") as f:
                        await f.write(img_res.content)
                    return {"url": download_url, "local_path": output_path}

        raise RuntimeError(f"Muse2API trả về HTTP {r.status_code}: {r.text[:300]}")


async def process_scene(
    client: httpx.AsyncClient,
    scene: Dict[str, Any],
    sem: asyncio.Semaphore,
    base_url: str,
    api_key: str,
    output_dir: str,
    voice: str,
    progress_cb: Optional[Callable] = None,
    style_locks: Optional[Dict[str, str]] = None,
    voice_mode: str = "tts",
) -> Dict[str, Any]:
    """
    Pipeline xử lý 1 cảnh độc lập với cơ chế kiểm soát retry thông minh (Non-Spamming):
    - Tự động tiền kiểm & làm sạch an toàn trước khi gửi API (Pre-flight Sanitization).
    - Hỗ trợ tham chiếu Production Bible & First/Last Frame Chaining.
    - Hỗ trợ chế độ AI Voice: Đưa lời thoại vào prompt để AI tự tạo tiếng nhân vật & âm thanh gốc.
    - Nếu gặp lỗi kiểm duyệt: Chỉ thử lại TỐI ĐA 1 LẦN với prompt đã làm sạch triệt để.
    - Tuyệt đối không retry mù quáng hay lặp vô tận.
    """
    async with sem:
        idx = scene["index"]
        raw_prompt = scene.get("prompt", "")
        voiceover = scene.get("voiceover", "")
        duration = int(scene.get("duration", 5))
        size = scene.get("size", "16:9")
        scene_voice = scene.get("voice") or voice
        scene_voice_mode = scene.get("voice_mode") or voice_mode or "tts"
        is_ai_voice = bool(
            scene_voice_mode in ("ai_voice", "native", "none", "no_dub")
            or scene_voice in ("ai_voice", "native", "none", "no_dub")
        )
        voice_segments = scene.get("voice_segments") or []

        result: Dict[str, Any] = {
            "index": idx,
            "status": "processing",
            "video_path": None,
            "audio_path": None,
            "voiceover": voiceover,
            "audio_duration": duration,
            "last_frame_path": None,
            "last_frame_url": None,
            "error": None,
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        audio_path = os.path.join(output_dir, "audio", f"scene_{idx:03d}.mp3") if not is_ai_voice else None
        video_path = os.path.join(output_dir, "scenes", f"scene_{idx:03d}.mp4")
        tmp_seg_dir = os.path.join(output_dir, "audio", f"segs_{idx:03d}")

        original_prompt = scene.get("original_prompt") or raw_prompt
        # Tự động bóc tách kịch bản thô (loại bỏ [Màu sắc], [Âm thanh], [Lồng tiếng])
        cleaned_prompt, ext_voice = clean_script_prompt(raw_prompt)
        if not voiceover and ext_voice:
            voiceover = ext_voice
            result["voiceover"] = voiceover

        # Áp dụng khóa phong cách (Style, Lighting, Color, Camera)
        styled_prompt = apply_style_locks(cleaned_prompt, style_locks)
        enhanced_prompt = styled_prompt

        # Tự động tiền kiểm tra & làm sạch an toàn
        prompt = sanitize_prompt_heuristics(styled_prompt)

        spk_names = []
        if voice_segments:
            for seg in voice_segments:
                cname = (seg.get("character") or seg.get("speaker") or "").strip()
                if cname and cname not in spk_names:
                    spk_names.append(cname)
        if not spk_names and scene.get("character"):
            spk_names.append(scene.get("character").strip())

        p_lower = prompt.lower()
        has_dialogue = bool(voiceover or voice_segments)

        if is_ai_voice:
            # 🤖 CHẾ ĐỘ ĐƯA THOẠI VÀO PROMPT ĐỂ AI TỰ SINH TIẾNG NHÂN VẬT & ÂM THANH
            if voice_segments and len(voice_segments) > 0:
                dialogue_parts = []
                for seg in voice_segments:
                    cname = (seg.get("character") or seg.get("speaker") or "").strip()
                    txt = (seg.get("text") or "").strip()
                    if cname and txt:
                        dialogue_parts.append(f"{cname} says '{txt}'")
                    elif txt:
                        dialogue_parts.append(f"character says '{txt}'")
                dialogue_clause = ", ".join(dialogue_parts)
                prompt = f"{prompt}, audible sound design and native speech: characters speaking naturally with synchronized realistic vocal audio and clear ambient atmosphere: {dialogue_clause}"
            elif voiceover:
                spk_clause = f"the character {spk_names[0]}" if spk_names else "the character"
                prompt = f"{prompt}, audible sound design and native speech: {spk_clause} speaking with natural voice '{voiceover}', synchronized realistic vocal audio and clear ambient atmosphere"
            else:
                prompt = f"{prompt}, realistic synchronized sound design, clear natural ambient audio and Foley sound effects"
        else:
            # 🎙️ CHẾ ĐỘ LỒNG TIẾNG TTS: Bổ sung chỉ thị khẩu hình (Lip-sync Articulation)
            if has_dialogue and not any(k in p_lower for k in ["lip movement", "mouth movement", "speaking dialogue", "lip sync", "khớp môi", "articulation", "talking with"]):
                spk_clause = f"the character {', '.join(spk_names)}" if spk_names else "the character"
                prompt = f"{prompt}, medium shot of {spk_clause} talking with expressive natural synchronized mouth movement and realistic lip articulation speaking Vietnamese dialogue"

        if prompt != raw_prompt:
            scene["prompt"] = prompt

        healed_prompt = scene.get("healed_prompt")
        result["original_prompt"] = original_prompt
        result["enhanced_prompt"] = enhanced_prompt
        result["healed_prompt"] = healed_prompt
        result["used_prompt"] = prompt

        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            try:
                if attempt > 1:
                    if progress_cb:
                        progress_cb(idx, f"🔄 Đang thử lại lần {attempt}/{max_attempts}...")
                    await asyncio.sleep(6 * attempt)
                else:
                    if progress_cb:
                        msg_init = "Đang gửi tới muse2api & đưa thoại vào prompt (AI Voice)..." if is_ai_voice else "Đang gửi tới muse2api & sinh TTS..."
                        progress_cb(idx, msg_init)

                # 1. Sinh TTS (Bỏ qua khi ở chế độ AI Voice)
                if is_ai_voice:
                    tts_task = None
                else:
                    if not os.path.exists(audio_path):
                        if voice_segments and len(voice_segments) > 0:
                            tts_task = asyncio.create_task(
                                generate_multi_voice_tts(voice_segments, audio_path, tmp_seg_dir)
                            )
                        else:
                            tts_task = asyncio.create_task(
                                generate_tts(voiceover, scene_voice, audio_path)
                            )
                    else:
                        tts_task = None

                payload = {"prompt": prompt, "duration": duration, "size": size, "timeout": 600}
                ref_img = scene.get("reference_image") or scene.get("image")
                if ref_img:
                    ref_str = str(ref_img).strip()
                    if ref_str.startswith("/api/uploads/"):
                        upload_path = os.path.join("workspace", "_uploads", os.path.basename(ref_str))
                        if os.path.exists(upload_path):
                            import base64
                            import mimetypes
                            m = mimetypes.guess_type(upload_path)[0] or "image/png"
                            with open(upload_path, "rb") as f:
                                payload["reference_image"] = f"data:{m};base64,{base64.b64encode(f.read()).decode('ascii')}"
                        else:
                            payload["reference_image"] = f"http://localhost:8000{ref_str}"
                    elif ref_str.startswith("/api/shot-desk/projects/"):
                        parts = ref_str.replace("/api/shot-desk/projects/", "").split("/")
                        if len(parts) >= 2:
                            proj_id = parts[0]
                            rel_sub = "/".join(parts[1:])
                            desk_file = os.path.join("workspace", proj_id, rel_sub)
                            if os.path.exists(desk_file):
                                import base64, mimetypes
                                m = mimetypes.guess_type(desk_file)[0] or "image/jpeg"
                                with open(desk_file, "rb") as f:
                                    payload["reference_image"] = f"data:{m};base64,{base64.b64encode(f.read()).decode('ascii')}"
                    elif os.path.exists(ref_str) and os.path.isfile(ref_str):
                        import base64
                        import mimetypes
                        m = mimetypes.guess_type(ref_str)[0] or "image/jpeg"
                        with open(ref_str, "rb") as f:
                            payload["reference_image"] = f"data:{m};base64,{base64.b64encode(f.read()).decode('ascii')}"
                    elif ref_str.startswith(("http://", "https://", "data:")):
                        payload["reference_image"] = ref_str
                    else:
                        print(f"[batch_engine] Bỏ qua đường dẫn reference_image không tồn tại hoặc không hợp lệ: {ref_str[:80]}")

                res = await client.post(
                    f"{base_url}/v1/videos",
                    json=payload,
                    headers=headers,
                    timeout=60.0,
                )
                res.raise_for_status()
                task_id = res.json().get("id", "")
                if not task_id:
                    raise ValueError("Muse2API không trả về task_id hợp lệ")

                if progress_cb:
                    progress_cb(idx, f"⏳ Task ID: {task_id[:12]}... đang render")

                # 2. Poll kết quả
                video_url = await poll_video_task(
                    client, task_id, base_url, headers, idx, progress_cb
                )

                if tts_task:
                    audio_duration = await tts_task
                else:
                    if is_ai_voice:
                        text_len = len(voiceover) if voiceover else sum(len(s.get("text", "")) for s in voice_segments)
                        audio_duration = max(float(duration), min(10.0, max(2.5, text_len / 3.0)))
                    else:
                        audio_duration = max(2.0, len(voiceover) / 3.0)

                if progress_cb:
                    progress_cb(idx, "📥 Đang tải video về...")

                r = await client.get(video_url, timeout=120.0)
                r.raise_for_status()
                if len(r.content) < 100:
                    raise RuntimeError(f"File video tải về từ Muse quá nhỏ hoặc rỗng ({len(r.content)} bytes)")
                raw_video_path = os.path.join(output_dir, "scenes", f"scene_{idx:03d}_raw.mp4")
                async with aiofiles.open(raw_video_path, "wb") as f:
                    await f.write(r.content)

                # Bảo vệ chống video tích lũy/kéo dài: tự động cắt phần đuôi mới sinh nếu video bị dài hơn thời lượng
                expected_dur = max(float(duration), float(audio_duration or 0.0))
                try:
                    raw_dur = get_media_duration(raw_video_path)
                    if raw_dur > (expected_dur + 0.3):
                        offset = max(0.0, raw_dur - expected_dur)
                        temp_raw = raw_video_path + ".trim.mp4"
                        import imageio_ffmpeg
                        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
                        subprocess.run([
                            ffmpeg_exe, "-y",
                            "-ss", f"{offset:.3f}",
                            "-i", raw_video_path,
                            "-t", f"{expected_dur:.3f}",
                            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                            "-pix_fmt", "yuv420p",
                            "-c:a", "aac", "-b:a", "192k",
                            temp_raw
                        ], check=True, capture_output=True)
                        if os.path.exists(temp_raw) and os.path.getsize(temp_raw) > 500:
                            os.replace(temp_raw, raw_video_path)
                except Exception as trim_err:
                    print(f"[batch_engine Warning] Trimming raw video scene {idx} failed: {trim_err}")

                # Ghép ngay audio thoại vào video cảnh để bản preview có tiếng ngay lập tức
                final_dir = os.path.join(output_dir, "final")
                os.makedirs(final_dir, exist_ok=True)
                clip_path = os.path.join(final_dir, f"clip_{idx:03d}.mp4")

                try:
                    from composer import compose_scene_video
                    if progress_cb:
                        msg_sync = "🎬 Đang chuẩn hóa âm thanh & giọng gốc AI..." if is_ai_voice else "🎬 Đang đồng bộ lời thoại & âm thanh..."
                        progress_cb(idx, msg_sync)

                    has_real_audio = bool(audio_path and os.path.exists(audio_path) and os.path.getsize(audio_path) > 500)
                    await asyncio.to_thread(
                        compose_scene_video,
                        scene_video=raw_video_path,
                        scene_audio=audio_path if has_real_audio else None,
                        output=video_path,
                        audio_duration=audio_duration,
                        target_duration=expected_dur,
                        aspect_ratio=size,
                    )
                    import shutil
                    shutil.copy2(video_path, clip_path)
                except Exception as comp_err:
                    print(f"[batch_engine Warning] Ghép âm thanh ngay cho scene {idx} thất bại: {comp_err}")
                    import shutil
                    if not os.path.exists(video_path):
                        shutil.copy2(raw_video_path, video_path)
                    if not os.path.exists(clip_path):
                        shutil.copy2(video_path, clip_path)

                # 3. Trích xuất khung hình cuối cùng (Last Frame Extraction)
                last_frame_path = os.path.join(output_dir, "scenes", f"scene_{idx:03d}_last_frame.jpg")
                extracted_frame = extract_last_frame(video_path, last_frame_path)
                job_id = os.path.basename(output_dir)
                last_frame_url = f"/api/jobs/{job_id}/last-frame/{idx}" if extracted_frame else None

                full_voiceover = voiceover
                if voice_segments:
                    full_voiceover = " | ".join(
                        f"{seg.get('character', '')}: {seg.get('text', '')}" if seg.get('character') else seg.get('text', '')
                        for seg in voice_segments
                    )

                result.update(
                    {
                        "status": "done",
                        "video_path": video_path,
                        "raw_video_path": raw_video_path,
                        "audio_path": audio_path,
                        "audio_duration": audio_duration,
                        "voiceover": full_voiceover,
                        "last_frame_path": extracted_frame,
                        "last_frame_url": last_frame_url,
                        "error": None,
                    }
                )

                if progress_cb:
                    extra_data = {
                        "status": "done",
                        "video_url": f"/api/jobs/{job_id}/scenes/{idx}/video",
                        "last_frame_url": last_frame_url,
                        "prompt": scene.get("prompt", ""),
                        "voiceover": full_voiceover,
                        "error": None,
                    }
                    msg_text = "✅ Hoàn thành (Đã trích xuất Last Frame)" if extracted_frame else "✅ Hoàn thành!"
                    try:
                        progress_cb(idx, msg_text, extra_data)
                    except TypeError:
                        progress_cb(idx, msg_text)
                break

            except Exception as e:
                clean_err = str(e).replace('\n', ' ')
                if "Server disconnected" in clean_err or "Connection reset" in clean_err:
                    clean_err = "Mất kết nối tạm thời với server Muse"

                # Lỗi xác thực nghiêm trọng: dừng ngay
                if "401" in clean_err or "Unauthorized" in clean_err or "Forbidden" in clean_err:
                    result["status"] = "error"
                    result["error"] = "Lỗi xác thực API Key với Muse2API"
                    if progress_cb:
                        progress_cb(idx, "❌ Lỗi xác thực API Key")
                    break

                # Nhận diện nếu Muse AI từ chối nội dung
                is_prompt_refusal = any(k in clean_err for k in [
                    "模型未生成媒体", "仅返回文本", "没法直接生成", "做不了", 
                    "抱歉", "换个方向", "不带血腥", "去掉", "换个思路", "无法直接生成", "policy"
                ])

                # Nhận diện lỗi tải trang hoặc mạng bận tạm thời của muse.ai
                is_transient_error = any(k in clean_err for k in [
                    "页面加载超时", "timeout", "timed out", "502", "503", "504", 
                    "Mất kết nối", "Connection reset", "aborted", "10053"
                ])

                # Nếu bị lỗi ở lần 1, thử lại ĐÚNG 1 LẦN sau khoảng nghỉ
                if attempt == 1:
                    if is_prompt_refusal:
                        if progress_cb:
                            progress_cb(idx, "🪄 Muse từ chối nội dung. Đang tự động làm sạch prompt...")
                        healed_prompt = sanitize_prompt_heuristics(prompt, clean_err, aggressive=True)
                        if healed_prompt != prompt:
                            prompt = healed_prompt
                            scene["prompt"] = healed_prompt
                            result["revised_prompt"] = healed_prompt
                            if progress_cb:
                                progress_cb(idx, "✨ Thử lại lần 2 với prompt an toàn...")
                            continue
                    elif is_transient_error:
                        if progress_cb:
                            progress_cb(idx, "⏳ Muse2API đang bận tải trang, tự động thử lại lần 2...")
                        await asyncio.sleep(8)
                        continue

                # Đã thử hết số lần hoặc lỗi không thể khắc phục -> DỪNG NGAY
                result["status"] = "error"
                result["error"] = clean_err
                result["prompt"] = prompt
                if is_prompt_refusal:
                    result["suggested_prompt"] = sanitize_prompt_heuristics(prompt, clean_err, aggressive=True)
                if progress_cb:
                    err_extra = {
                        "status": "error",
                        "error": clean_err,
                        "prompt": prompt,
                        "suggested_prompt": result.get("suggested_prompt", ""),
                        "is_refusal": is_prompt_refusal,
                    }
                    try:
                        progress_cb(idx, f"❌ Thất bại: {clean_err[:60]}", err_extra)
                    except TypeError:
                        progress_cb(idx, f"❌ Thất bại: {clean_err[:60]}")
                break

        return result


async def run_batch(
    scenes: List[Dict[str, Any]],
    base_url: str,
    api_key: str,
    output_dir: str,
    voice: str = "vi-VN-HoaiMyNeural",
    max_concurrent: int = 6,
    progress_cb: Optional[Callable] = None,
    chain_mode: str = "none",
    chain_fallback_mode: str = "independent",
    style_locks: Optional[Dict[str, str]] = None,
    start_scene_idx: int = 1,
    voice_mode: str = "tts",
) -> List[Dict[str, Any]]:
    """
    Chạy hàng loạt tất cả các cảnh với hỗ trợ kỹ thuật First/Last Frame Chaining & Fallback:
    - chain_mode == 'sequential': Chạy theo chuỗi nối tiếp.
    - chain_mode == 'none': Chạy song song (nếu cảnh nào có chain_from_previous=True thì đợi cảnh trước).
    - chain_fallback_mode: 'independent' (render độc lập), 'fallback_image' (dùng keyframe), 'stop' (tạm dừng).
    - start_scene_idx: Cho phép chạy lại từ cảnh N mà không làm mất cảnh 1..N-1 đã hoàn thành.
    """
    os.makedirs(os.path.join(output_dir, "audio"), exist_ok=True)
    os.makedirs(os.path.join(output_dir, "scenes"), exist_ok=True)
    os.makedirs(os.path.join(output_dir, "final"), exist_ok=True)

    sem = asyncio.Semaphore(max_concurrent)
    sorted_scenes = sorted(scenes, key=lambda x: x["index"])

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Pre-check health của base_url trước khi bắn batch
        try:
            r = await client.get(f"{base_url}/healthz", timeout=5.0)
        except Exception:
            try:
                r = await client.get(f"{base_url}/admin/status", timeout=5.0)
            except Exception:
                return [
                    {
                        "index": s["index"],
                        "status": "error",
                        "error": f"Không thể kết nối tới Muse2API tại {base_url}. Vui lòng kiểm tra server.",
                        "video_path": None,
                        "audio_path": None,
                        "voiceover": s.get("voiceover", ""),
                        "audio_duration": int(s.get("duration", 5)),
                    }
                    for s in scenes
                ]

        # ── Kỹ thuật Chaining: Tuần tự toàn bộ phim (Seamless Sequential Chaining) ──
        if chain_mode == "sequential":
            results = []
            prev_last_frame = None
            for s in sorted_scenes:
                idx = s["index"]
                if idx < start_scene_idx and s.get("status") == "done" and s.get("video_path"):
                    results.append(s)
                    if s.get("last_frame_path"):
                        prev_last_frame = s["last_frame_path"]
                    continue

                if prev_last_frame:
                    s["reference_image"] = prev_last_frame
                    if progress_cb:
                        progress_cb(idx, f"🔗 Khóa Last Frame Cảnh {idx-1} ➔ Đang đặt làm First Frame...")
                elif idx > 1:
                    if chain_fallback_mode == "fallback_image" and (s.get("keyframe_path") or s.get("character_reference")):
                        s["reference_image"] = s.get("keyframe_path") or s.get("character_reference")
                        if progress_cb:
                            progress_cb(idx, f"🖼️ Cảnh {idx-1} không có frame ➔ Sử dụng ảnh Keyframe dự phòng")
                    else:
                        s["reference_image"] = None

                res = await process_scene(
                    client, s, sem, base_url, api_key,
                    output_dir, voice, progress_cb, style_locks=style_locks,
                    voice_mode=voice_mode,
                )
                results.append(res)
                if res.get("status") == "done" and res.get("last_frame_path"):
                    prev_last_frame = res["last_frame_path"]
                else:
                    prev_last_frame = None
            results.sort(key=lambda x: x["index"])
            return results

        # ── Kỹ thuật Chaining theo từng phân cảnh (Selective Chaining with Async Events) ──
        done_events: Dict[int, asyncio.Event] = {s["index"]: asyncio.Event() for s in sorted_scenes}
        results_map: Dict[int, Dict[str, Any]] = {}

        # Khởi tạo trạng thái cho các cảnh trước start_scene_idx đã xong
        for s in sorted_scenes:
            idx = s["index"]
            if idx < start_scene_idx and s.get("status") == "done" and s.get("video_path"):
                results_map[idx] = s
                done_events[idx].set()

        async def worker_with_chain(s: Dict[str, Any], order_idx: int):
            idx = s["index"]
            if idx < start_scene_idx and s.get("status") == "done" and s.get("video_path"):
                return s

            # Giãn cách nhẹ (staggered launch) 1.5s giữa các cảnh để tránh sốc tải đồng thời lên Chrome CDP / muse.ai
            if not s.get("chain_from_previous") and order_idx > 0:
                stagger = min(order_idx * 1.5, 6.0)
                await asyncio.sleep(stagger)

            # Nếu cảnh này yêu cầu nối tiếp cảnh trước đó
            if s.get("chain_from_previous") and (idx - 1) in done_events:
                if progress_cb:
                    progress_cb(idx, f"⏳ Đang đợi Cảnh {idx-1} hoàn thành để lấy khung hình cuối...")
                await done_events[idx - 1].wait()
                prev_res = results_map.get(idx - 1)
                if prev_res and prev_res.get("last_frame_path"):
                    s["reference_image"] = prev_res["last_frame_path"]
                    if progress_cb:
                        progress_cb(idx, f"🔗 Đã nhận Last Frame Cảnh {idx-1} ➔ Bắt đầu render First Frame")
                else:
                    # Xử lý khi cảnh trước bị lỗi
                    if chain_fallback_mode == "stop":
                        err_msg = f"Cảnh {idx-1} bị lỗi, tạm dừng Cảnh {idx} theo cấu hình chờ."
                        if progress_cb:
                            progress_cb(idx, f"⛔ {err_msg}")
                        res = {
                            "index": idx,
                            "status": "error",
                            "error": err_msg,
                            "prompt": s.get("prompt"),
                            "video_path": None,
                            "audio_path": None,
                            "voiceover": s.get("voiceover", ""),
                        }
                        results_map[idx] = res
                        done_events[idx].set()
                        return res
                    elif chain_fallback_mode == "fallback_image" and (s.get("keyframe_path") or s.get("character_reference")):
                        fallback_ref = s.get("keyframe_path") or s.get("character_reference")
                        s["reference_image"] = fallback_ref
                        if progress_cb:
                            progress_cb(idx, f"🖼️ Cảnh {idx-1} lỗi ➔ Dùng ảnh Keyframe/Character dự phòng cho Cảnh {idx}")
                    else:
                        s["reference_image"] = None
                        if progress_cb:
                            progress_cb(idx, f"⚡ Cảnh {idx-1} lỗi ➔ Tự động chuyển Cảnh {idx} sang render Text-to-Video độc lập")

            res = await process_scene(
                client, s, sem, base_url, api_key,
                output_dir, voice, progress_cb, style_locks=style_locks,
                voice_mode=voice_mode,
            )
            results_map[idx] = res
            done_events[idx].set()
            return res

        tasks = [worker_with_chain(s, oi) for oi, s in enumerate(sorted_scenes)]
        results = await asyncio.gather(*tasks, return_exceptions=False)

    # Sắp xếp đúng thứ tự
    results.sort(key=lambda x: x["index"])
    return results
