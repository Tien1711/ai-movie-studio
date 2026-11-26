"""Persistent review gates for MuseStudio. Only the batch worker performs media generation."""

import asyncio
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path

from PIL import Image, ImageOps

import batch_engine as media
import batch_video_engine as batch
import composer
import qc
from consistency import opening_reference, resolve_scene_cast, run_consistency_checks

BUSY = {"queued", "running"}
ASYNC = {
    "image",
    "clip",
    "speech",
    "compose",
    "narrated",
    "write",
    "emphasis",
    "portraits",
}
INVALID_FINAL = {"final_video_url": None, "srt_url": None, "inspection": None}


def directory(vid):
    if not batch.is_safe_video_id(vid):
        raise ValueError("Mã bản dựng không hợp lệ.")
    return batch.BATCH_DIR / vid


def selected_scene(video, index):
    for scene in video["scenes"]:
        if scene["index"] == index:
            return scene
    raise ValueError(f"Không tìm thấy cảnh {index}.")


def asset_path(vid, token):
    if not re.fullmatch(r"[a-f0-9]{32}\.(webp|mp4|mp3|jpg)", token or ""):
        raise ValueError("Tệp chưa sẵn sàng.")
    return directory(vid) / "media" / token


def new_asset(vid, ext):
    path = asset_path(vid, uuid.uuid4().hex + "." + ext)
    path.parent.mkdir(exist_ok=True)
    return path


def image_to_webp(source, dest):
    with Image.open(source) as im:
        if im.width * im.height > 48_000_000:
            raise ValueError("Ảnh quá lớn để dùng làm ảnh cảnh.")
        ImageOps.exif_transpose(im).convert("RGB").save(dest, "WEBP", lossless=True)


def take(s):
    return {
        k: s.get(k)
        for k in (
            "clip",
            "used_prompt",
            "clip_frame",
            "frame",
            "frame_approved",
            "last_frame",
            "reference_source",
            "input_frame",
            "accepted_join",
            "video_status",
            "prompt",
            "direction",
            "voiceover",
            "voice",
            "duration",
            "setting",
            "characters",
            "dialogue",
            "speech",
            "new_segment",
            "transition_out",
            "image_prompt",
            "anchors",
            "qc",
            "qc_accepted",
        )
    }


def refresh_joins(scenes):
    previous = {}
    for scene in sorted(scenes, key=lambda s: s["index"]):
        if (
            scene.get("clip")
            and scene.get("reference_source") == "last_frame"
            and scene.get("input_frame")
        ):
            current = previous.get("last_frame") or ""
            stale = (
                scene["input_frame"] != current
                and scene.get("accepted_join") != current
            )
            if stale:
                scene["approved"] = False
            scene["stale_join"] = stale
        else:
            scene["stale_join"] = False
        previous = scene


def prune_takes(vid, scenes):
    """Only the UUID assets owned by this production are eligible for removal."""
    folder = directory(vid) / "media"
    if not folder.is_dir():
        return
    retained = {
        item.get(k)
        for s in scenes
        for item in (s, s.get("previous_take") or {})
        for k in ("frame", "clip", "last_frame", "speech", "input_frame")
    }
    for p in folder.iterdir():
        if (
            re.fullmatch(r"[a-f0-9]{32}\.(webp|jpg|mp3|mp4)", p.name)
            and p.name not in retained
        ):
            try:
                p.unlink(missing_ok=True)
            except PermissionError:
                pass  # A preview may still have this file open on Windows.


def invalidate_clip(s):
    if s.get("clip"):
        s["previous_take"] = take(s)
    s.update(
        clip=None,
        approved=False,
        video_status=None,
        used_prompt=None,
        last_frame=None,
        qc={},
        qc_accepted=False,
    )


def set_image(s, name):
    invalidate_clip(s)
    s.update(
        frame=name, frame_approved=False, anchors=[], image_prompt=None, error=None
    )


def can_act(s, action):
    if action == "image":
        return bool(s.get("prompt"))
    if action in ("speech", "emphasis"):
        return bool(s.get("voiceover"))
    if action == "approve_image":
        return bool(s.get("frame"))
    if action == "clip":
        return bool(s.get("frame") and s.get("frame_approved"))
    if action == "approve_clip":
        return bool(s.get("clip") and s.get("frame_approved"))
    return True


def command(vid, req):
    with batch._QUEUE_LOCK:
        video = batch.BATCH_MANAGER.get_video(vid)
        if not video or not video.get("guided"):
            raise ValueError("Bản dựng này không dùng quy trình duyệt ảnh.")
        if video["status"] in BUSY or video.get("operation"):
            raise ValueError("Bản dựng đang xử lý. Hãy chờ thao tác hiện tại hoàn tất.")
        if req.version != video.get("updated_at"):
            raise ValueError(
                "Bản dựng đã thay đổi. Hãy kiểm tra lại phiên bản đang hiển thị."
            )
        action = req.action
        scenes = video["scenes"]
        chosen = [selected_scene(video, i) for i in sorted(set(req.indexes))]
        if action in ("image", "clip", "speech", "emphasis"):
            for scene in chosen:
                resolve_scene_cast(scene, video.get("cast", []))
        if action in {"compose", "narrated"}:
            if not all(
                s.get("approved")
                or (
                    req.allow_stills
                    and action == "narrated"
                    and s.get("frame_approved")
                    and not s.get("clip")
                )
                for s in scenes
            ):
                raise ValueError("Hãy duyệt đủ các cảnh dùng để xuất phim.")
        elif action == "portraits":
            if not any(
                c.get("auto_cast") and not c.get("avatar")
                for c in video.get("cast", [])
            ):
                raise ValueError("Đã đủ ảnh nhân vật.")
        elif action == "image" and any(
            c.get("auto_cast") and not c.get("avatar") for c in video.get("cast", [])
        ):
            raise ValueError("Vẽ ảnh nhân vật trước khi tạo ảnh phân cảnh.")
        elif action == "write":
            if not video.get("workshop"):
                raise ValueError("Bản dựng không có yêu cầu viết theo skill.")
            chosen = [s for s in scenes if not s.get("prompt")]
        elif not chosen or not all(can_act(s, action) for s in chosen):
            raise ValueError(
                "Cảnh chưa đủ điều kiện. Duyệt đúng ảnh trước khi tạo clip."
            )
        updates = {"scenes": scenes, "error": None}
        if action in ("clip", "image"):
            warnings = [
                c["message"] + " " + c.get("suggestion", "")
                for c in run_consistency_checks(
                    {
                        "scenes": scenes,
                        "characters": video.get("cast") or [],
                        "chain_mode": "sequential",
                        "voice": video.get("voice"),
                    }
                )["checks"]
                if c["level"] == "warning"
            ]
            for shot in chosen:
                for person in references(video, shot):
                    if not media.character_block_of(person):
                        raise ValueError(
                            f"Cảnh {shot['index']}: {person['name']} thiếu mô tả nhận dạng."
                        )
                if (
                    len(shot.get("dialogue") or shot.get("voiceover") or "")
                    > 15 * shot["duration"]
                ):
                    warnings.append(
                        f"Cảnh {shot['index']}: lời đọc có thể dài hơn hình."
                    )
            updates["continuity_warnings"] = list(dict.fromkeys(warnings))
        if action in ASYNC:
            if action == "write" and not chosen:
                raise ValueError("Đã viết đủ các cảnh.")
            updates.update(
                status="queued",
                current_step="Đang chờ xử lý",
                operation={
                    "action": action,
                    "pending": [s["index"] for s in chosen],
                    "active": [],
                    "allow_stills": req.allow_stills,
                    "errors": [],
                },
            )
        else:
            for s in chosen:
                if action == "approve_image":
                    if not req.approved and s.get("frame_approved"):
                        invalidate_clip(s)
                    s["frame_approved"] = req.approved
                elif action == "approve_clip":
                    if (
                        req.approved
                        and (s.get("qc") or {}).get("passed") is False
                        and not (req.accept_qc and len(chosen) == 1)
                    ):
                        raise ValueError(
                            "Cảnh có lỗi QC. Xem lại clip và xác nhận riêng cảnh này."
                        )
                    if req.approved and s.get("stale_join"):
                        if not req.accept_qc or len(chosen) != 1:
                            raise ValueError(
                                "Khung kết cảnh trước đã thay đổi. Xem lại mối nối rồi duyệt riêng."
                            )
                        prev = next(
                            (r for r in scenes if r["index"] == s["index"] - 1), {}
                        )
                        s["accepted_join"] = prev.get("last_frame") or ""
                    s["approved"] = req.approved
                    s["qc_accepted"] = bool(
                        req.approved
                        and req.accept_qc
                        and (s.get("qc") or {}).get("passed") is False
                    )
                elif action == "discard_previous":
                    s.pop("previous_take", None)
                elif action == "restore":
                    old = s.get("previous_take")
                    if not old:
                        raise ValueError("Chưa có phiên bản cũ.")
                    current = take(s)
                    s.update(old, previous_take=current, approved=False)
                    s["error"] = None
                elif action == "replace":
                    path = media._resolve_safe_reference(req.upload)
                    if not req.upload.startswith("/api/uploads/") or not path:
                        raise ValueError("Chọn ảnh đã tải lên Studio.")
                    dest = new_asset(vid, "webp")
                    image_to_webp(path, dest)
                    set_image(s, dest.name)
                elif action == "edit":
                    if not req.prompt.strip() or not req.voiceover.strip():
                        raise ValueError("Điền cả prompt và lời kể.")
                    metadata = req.model_dump(
                        include={
                            "characters",
                            "setting",
                            "new_segment",
                            "transition_out",
                            "dialogue",
                        },
                        exclude_unset=True,
                    )
                    resolve_scene_cast({**s, **metadata}, video.get("cast", []))
                    if "dialogue" in metadata and metadata["dialogue"] != s.get(
                        "dialogue"
                    ):
                        s["speech"] = None
                    changed = any(
                        s.get(k) != val
                        for k, val in metadata.items()
                        if k != "transition_out" and (s.get(k) or val)
                    )
                    if (
                        changed
                        or req.prompt.strip() != s.get("prompt")
                        or req.duration != s.get("duration")
                    ):
                        set_image(s, None)
                    if req.voiceover != s.get("voiceover") or req.voice != s.get(
                        "voice"
                    ):
                        s["speech"] = None
                    s.update(
                        **metadata,
                        prompt=req.prompt.strip(),
                        direction=req.prompt.strip(),
                        voiceover=req.voiceover.strip(),
                        duration=req.duration,
                        voice=req.voice,
                        error=None,
                    )
            refresh_joins(scenes)
            if action != "discard_previous":
                updates.update(
                    INVALID_FINAL, status="review", current_step="Đang biên tập"
                )
        batch.BATCH_MANAGER.update_video(vid, updates)
        if action not in ASYNC:
            prune_takes(vid, scenes)
    if action in ASYNC:
        batch.start_batch_worker()


def skill_text(data, filename):
    import story_engine

    if filename.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            matches = [
                i for i in z.infolist() if Path(i.filename).name.upper() == "SKILL.MD"
            ]
            if len(matches) != 1 or matches[0].file_size > 100_000:
                raise ValueError("ZIP cần chứa một SKILL.md dưới 100 KB.")
            data, filename = z.read(matches[0]), "SKILL.md"
    result = story_engine.parse_text_bytes(data, filename).strip()
    if not result or len(result) > 40000:
        raise ValueError("Nội dung skill phải từ 1 đến 40.000 ký tự.")
    return result


def parse_llm_json(raw: str):
    """Extract a balanced JSON object or array from noisy LLM / Browser DOM replies."""
    cleaned = (
        (raw or "")
        .replace("\ufeff", "")
        .replace("\u200b", "")
        .replace("\u00a0", " ")
        .strip()
    )
    if not cleaned:
        raise ValueError(
            "Muse trả về phản hồi rỗng. Hãy bấm 'Viết tiếp kịch bản' để thử lại."
        )

    stripped = re.sub(
        r"^`{3}(?:json)?\s*|\s*`{3}$", "", cleaned, flags=re.IGNORECASE
    ).strip()
    try:
        direct = json.loads(stripped)
        if isinstance(direct, (dict, list)):
            return direct
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    best = None
    best_span = -1

    for match in re.finditer(r"`{3}(?:json)?\s*([\s\S]*?)`{3}", cleaned, re.IGNORECASE):
        block = match.group(1).strip()
        for candidate in (block, re.sub(r",\s*([}\]])", r"\1", block)):
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, (dict, list)) and len(candidate) > best_span:
                    best = parsed
                    best_span = len(candidate)
            except json.JSONDecodeError:
                continue

    if best is not None:
        return best

    def _scan_text(text: str):
        nonlocal best, best_span
        pos = 0
        length = len(text)
        while pos < length:
            starts = [i for i in (text.find("{", pos), text.find("[", pos)) if i != -1]
            if not starts:
                break
            start = min(starts)
            try:
                parsed, end = decoder.raw_decode(text, start)
                span = end - start
                if isinstance(parsed, (dict, list)) and span > best_span:
                    best = parsed
                    best_span = span
                pos = max(start + 1, end)
                continue
            except json.JSONDecodeError:
                pass
            pos = start + 1

    _scan_text(cleaned)
    relaxed = re.sub(r",\s*([}\]])", r"\1", cleaned)
    if relaxed != cleaned:
        _scan_text(relaxed)
    if best is not None:
        return best

    snippet = re.sub(r"\s+", " ", cleaned)[:120]
    raise ValueError(
        "Muse chưa trả đúng định dạng JSON kịch bản "
        f"(nhận được: {snippet!r}). Hãy bấm 'Viết tiếp kịch bản' để thử lại."
    )


async def ask_json(system, user):
    import httpx

    _, base, key = batch.muse_connection(batch.active_muse_provider())
    if not key:
        raise ValueError("Chưa cấu hình khóa kết nối Muse. Kiểm tra mục Cài đặt Muse.")
    async with httpx.AsyncClient(timeout=150) as client:
        try:
            res = await client.post(
                f"{base}/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": "muse-spark",
                    "timeout": 120,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
            res.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ValueError(
                "Muse phản hồi quá thời gian chờ. Hãy bấm 'Viết tiếp kịch bản' để thử lại."
            ) from exc
        except httpx.HTTPStatusError as exc:
            detail = ""
            try:
                body = exc.response.json()
                if isinstance(body, dict):
                    detail = str(body.get("error") or body.get("detail") or "").strip()
            except ValueError:
                detail = ""
            raise ValueError(
                detail
                or f"Muse trả lỗi HTTP {exc.response.status_code}. Hãy kiểm tra tab Muse và thử lại."
            ) from exc
        payload = res.json()
        raw = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return parse_llm_json(raw)


async def write_beats(v, indexes):
    spec = v["workshop"]
    previous = [
        s["voiceover"]
        for s in v["scenes"]
        if s.get("prompt") and s["index"] < indexes[0]
    ]
    payload = {
        "topic": spec["topic"],
        "skill": spec.get("text", ""),
        "total_scenes": len(v["scenes"]),
        "requested_scene_numbers": indexes,
        "seconds_per_scene": v["scenes"][0]["duration"],
        "style": batch.BATCH_VISUAL_STYLES[v["visual_style"]]["prompt_suffix"],
        "characters": [
            {"name": c["name"], "description": c.get("description", "")}
            for c in v.get("cast", [])
        ],
        "preceding_setting": next(
            (
                s.get("setting")
                for s in reversed(v["scenes"][: indexes[0] - 1])
                if s.get("setting")
            ),
            "",
        ),
        "opening": previous[:1],
        "preceding_beats": previous[-4:],
    }
    infer_cast = indexes[0] == 1 and not v.get("cast")
    response = (
        "Output ONLY raw valid JSON with no markdown code fences, no intro text, and no explanation. "
        "Return a JSON object with characters (up to six recurring characters with name and description "
        "of face, hair, costume and stable identifying features) and scenes (array of requested scenes). "
        if infer_cast
        else "Output ONLY raw valid JSON with no markdown code fences, no intro text, and no explanation. "
        "Return only a JSON array of requested scenes. "
    )
    rows = await ask_json(
        response
        + "Write a continuous film following the supplied creative brief and skill. "
        "Each scene object in order must contain index, title, "
        "prompt (visual action, camera, matching named characters), voiceover (Vietnamese, fits the seconds). "
        "Also return characters (names actually visible), setting (place and story time, repeated verbatim until it changes), "
        "new_segment (boolean), transition_out (cut or dissolve). Continue the preceding beats. "
        "Use exactly the supplied or newly registered character names.",
        json.dumps(payload, ensure_ascii=False),
    )
    cast = []
    if infer_cast:
        if not isinstance(rows, dict):
            raise ValueError("Muse trả kịch bản mở đầu không đúng cấu trúc JSON object.")
        characters = rows.get("characters")
        if not isinstance(characters, list):
            raise ValueError("Muse trả hồ sơ nhân vật không hợp lệ.")
        for character in characters[:6]:
            if not isinstance(character, dict):
                raise ValueError("Muse trả hồ sơ nhân vật không hợp lệ.")
            name, description = character.get("name"), character.get("description")
            if (
                not isinstance(name, str)
                or not isinstance(description, str)
                or not name.strip()
                or not description.strip()
            ):
                raise ValueError("Hồ sơ nhân vật thiếu tên hoặc mô tả.")
            cast.append(
                {
                    "id": uuid.uuid4().hex,
                    "kind": "character",
                    "name": name.strip()[:100],
                    "description": description.strip()[:1500],
                    "auto_cast": True,
                }
            )
        rows = rows.get("scenes")
    elif isinstance(rows, dict) and isinstance(rows.get("scenes"), list):
        rows = rows["scenes"]
    if not isinstance(rows, list) or len(rows) != len(indexes):
        raise ValueError(
            "Muse trả sai số cảnh. Có thể viết tiếp từ nhóm chưa hoàn thành."
        )
    normalized_indexes = []
    for pos, r in enumerate(rows):
        if not isinstance(r, dict):
            raise ValueError("Dữ liệu phân cảnh không hợp lệ.")
        raw_idx = r.get("index")
        if isinstance(raw_idx, str) and raw_idx.strip().isdigit():
            raw_idx = int(raw_idx.strip())
            r["index"] = raw_idx
        normalized_indexes.append(raw_idx)
    if normalized_indexes != indexes:
        raise ValueError(
            "Muse trả sai số cảnh. Có thể viết tiếp từ nhóm chưa hoàn thành."
        )
    for r in rows:
        for k in ("title", "prompt", "voiceover"):
            if not isinstance(r.get(k), str) or not 0 < len(r[k].strip()) <= 3000:
                raise ValueError("Kịch bản trả về thiếu tiêu đề, prompt hoặc lời kể.")
    if infer_cast:
        v["cast"] = cast
    registered_aliases = {
        str(alias).strip().casefold()
        for c in v.get("cast", [])
        if c.get("kind", "character") == "character"
        for alias in (c.get("id"), c.get("name"), *(c.get("aliases") or []))
        if alias
    }
    for r in rows:
        target = selected_scene(v, r["index"])
        names = r.get("characters", [])
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ValueError("Danh sách nhân vật của cảnh không hợp lệ.")
        names = [
            n.strip()
            for n in names
            if n.strip() and n.strip().casefold() in registered_aliases
        ]
        resolve_scene_cast({"characters": names}, v.get("cast", []))
        target.update(
            characters=names,
            setting=str(r.get("setting") or "")[:500],
            new_segment=r.get("new_segment") is True,
            transition_out="cut" if r.get("transition_out") == "cut" else "dissolve",
            title=r["title"],
            prompt=r["prompt"],
            direction=r["prompt"],
            voiceover=r["voiceover"],
            error=None,
        )


_VI_SAFE_VISUAL_REPLACEMENTS = (
    (r"kẻ cai trị tàn bạo|tên quan tàn bạo", "quan lại cổ trang uy nghiêm"),
    (r"tàn bạo|hung ác|khát máu|đẫm máu|sát khí|giết chóc|chém giết", "uy nghiêm kịch tính"),
    (r"nham hiểm|độc ác|xảo quyệt", "sắc sảo"),
    (r"nữ chiến binh", "nữ hiệp cổ trang"),
    (r"chiến binh", "tráng sĩ cổ trang"),
    (r"nữ tướng", "nữ thủ lĩnh cổ trang"),
    (r"cầm thanh kiếm dài|thanh kiếm dài|thanh kiếm|thanh gươm|gươm báu|kiếm báu", "trượng ngọc lễ nghi cổ trang"),
    (r"cầm thanh đao lớn|thanh đao lớn|đại đao|thanh đao", "trường côn cổ trang chạm khắc"),
    (r"cầm cây giáo dài đầu đồng|cầm cây giáo dài|cây giáo dài|ngọn giáo|giáo đồng|giáo tre", "cờ lệnh cổ trang thêu hoa văn"),
    (r"\b(?:kiếm|gươm|đao|giáo mác|vũ khí)\b", "pháp khí cổ trang"),
)


def sanitize_visual_traits(text: str, *, aggressive: bool = False) -> str:
    """Soften weapon/violence & ID-portrait triggers while keeping visual traits intact."""
    cleaned = (text or "").strip()
    for pattern, repl in _VI_SAFE_VISUAL_REPLACEMENTS:
        cleaned = re.sub(pattern, repl, cleaned, flags=re.IGNORECASE)
    if aggressive:
        cleaned = media.sanitize_prompt_heuristics(cleaned, "refused", aggressive=True)
    return re.sub(r"\s+", " ", cleaned).strip(" .")


def character_design_prompts(character: dict, style_suffix: str) -> tuple[str, str]:
    """Build primary & scenic fallback character reference prompts avoiding ID-portrait blocks."""
    kind = character.get("kind", "character")
    raw_desc = character.get("description") or character.get("name") or ""
    traits = sanitize_visual_traits(raw_desc, aggressive=False)
    safe_traits = sanitize_visual_traits(raw_desc, aggressive=True)
    if kind != "character":
        primary = (
            f"Cinematic concept art of a {kind} in a film world. "
            f"{traits}. {style_suffix}. Clear atmospheric lighting, no text, no watermark."
        )
        fallback = (
            f"Wide cinematic film still showing {safe_traits}. "
            f"{style_suffix}. Rich environment detail, soft natural lighting, no text, no watermark."
        )
        return primary, fallback
    # Avoid "Single ... portrait" + "One clearly visible face" + real historical proper names,
    # which trip Muse AI's real-person single-face portrait guardrail.
    primary = (
        "Cinematic medium three-quarter character concept art of a fictional film hero "
        "standing calmly in a serene traditional courtyard with soft depth of field. "
        f"Visual appearance and traditional costume: {traits}. "
        f"{style_suffix}. "
        "Head-to-waist framing showing hairstyle, facial expression, and full outfit clearly, "
        "warm atmospheric lighting, no text, no watermark."
    )
    fallback = (
        "Cinematic medium-wide film scene of a fictional animated character standing heroically "
        "by a misty river and lush green hills at sunrise. "
        f"Character attire and visual features: {safe_traits}. "
        f"{style_suffix}. "
        "Expressive storytelling composition, clear view of character design and costume, "
        "peaceful epic atmosphere, no text, no watermark."
    )
    return primary, fallback


async def draw_cast(v, progress):
    _, base, key = batch.muse_connection(batch.active_muse_provider())
    uploads = batch.BATCH_DIR.parent / "_uploads"
    uploads.mkdir(exist_ok=True)
    style_suffix = batch.BATCH_VISUAL_STYLES[v["visual_style"]]["prompt_suffix"]
    for index, character in enumerate(v["cast"], 1):
        if character.get("avatar") or not character.get("auto_cast"):
            continue
        if not batch.BATCH_MANAGER.get_video(v["id"]):
            raise asyncio.CancelledError()
        progress(index, "Vẽ tạo hình nhân vật " + character["name"])
        dest = uploads / (uuid.uuid4().hex + ".webp")
        primary_prompt, fallback_prompt = character_design_prompts(
            character, style_suffix
        )
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "portrait"
            try:
                await media.generate_keyframe_image(
                    primary_prompt, base, key, str(source), "1:1"
                )
            except (RuntimeError, TimeoutError, ValueError, OSError) as first_err:
                if not batch.BATCH_MANAGER.get_video(v["id"]):
                    raise asyncio.CancelledError()
                err_text = str(first_err).lower()
                transient_dom = any(
                    k in err_text
                    for k in ("tải lại lượt chat", "ô chat muse", "chưa xác nhận đã gửi")
                )
                retry_prompt = primary_prompt if transient_dom else fallback_prompt
                progress(
                    index,
                    (
                        "Thử gửi lại tạo hình nhân vật "
                        if transient_dom
                        else "Đổi góc cảnh điện ảnh cho nhân vật "
                    )
                    + character["name"],
                )
                await media.generate_keyframe_image(
                    retry_prompt, base, key, str(source), "1:1"
                )
            await asyncio.to_thread(image_to_webp, source, dest)
        character["avatar"] = "/api/uploads/" + dest.name
        batch.BATCH_MANAGER.update_video(v["id"], {"cast": v["cast"]})


def studio_opening(video, scene):
    previous = next(
        (s for s in video["scenes"] if s["index"] == scene["index"] - 1), None
    )
    last = None
    if (
        previous
        and not previous.get("error")
        and previous.get("last_frame")
        and (previous.get("qc") or {}).get("passed", True)
    ):
        token = previous["last_frame"]
        if asset_path(video["id"], token).is_file():
            last = token
    return opening_reference(
        scene,
        previous,
        last,
        scene.get("frame"),
        video.get("chain_fallback_mode", "fallback_image"),
        retry=bool(scene.get("clip_generations"))
        or video.get("reference_mode") == "approved",
    )


def references(v, s):
    library = v.get("cast") or []
    text = f"{s['prompt']} {s['voiceover']} {s.get('setting', '')}"
    found = media.match_film_characters(text, library)
    if s.get("characters"):
        people, _ = resolve_scene_cast(s, library)
        return people + [r for r in found if r.get("kind") in ("setting", "prop")]
    return found


async def produce(v, s, action, work, progress):
    vid, idx = v["id"], s["index"]
    if action == "image":
        cast = references(v, s)
        ref, guide = await asyncio.to_thread(batch._cast_reference, cast)
        if ref and ref.startswith(("http://", "https://")):
            ref = await asyncio.to_thread(media._browser_image_from_url, ref)
        elif ref:
            ref = batch._resolve_character_ref_data_uri(ref)
            if not ref:
                raise ValueError("Không đọc được ảnh tham chiếu đã chọn.")
        style = batch.BATCH_VISUAL_STYLES[v["visual_style"]]["prompt_suffix"]
        identities = "\n".join(
            f"{c['name']}: {media.character_block_of(c)}" for c in cast
        )
        prompt = f"Cinematic film scene stillframe. {s['prompt']}\n{identities}\n{style}\n{s.get('setting', '')}\n{guide}"
        _, base, key = batch.muse_connection(batch.active_muse_provider())
        try:
            await media.generate_keyframe_image(
                prompt, base, key, str(work / "generated"), v["aspect_ratio"], ref
            )
        except (RuntimeError, TimeoutError, ValueError, OSError):
            if batch._BatchCancellation(vid).is_set():
                raise asyncio.CancelledError()
            progress(idx, "Làm mềm mô tả cảnh để tạo lại ảnh mở đầu")
            safe_action = sanitize_visual_traits(s["prompt"], aggressive=True)
            safe_identities = "\n".join(
                f"{c['name']}: {sanitize_visual_traits(media.character_block_of(c), aggressive=False)}"
                for c in cast
            )
            prompt = (
                f"Cinematic wide-medium storytelling frame. {safe_action}\n"
                f"{safe_identities}\n{style}\n{s.get('setting', '')}\n{guide}"
            )
            await media.generate_keyframe_image(
                prompt, base, key, str(work / "generated"), v["aspect_ratio"], ref
            )
        dest = new_asset(vid, "webp")
        image_to_webp(work / "generated", dest)
        set_image(s, dest.name)
        s["anchors"] = [c["name"] for c in cast if c.get("avatar")]
        s["image_prompt"] = prompt
        s["image_generations"] = s.get("image_generations", 0) + 1
    elif action in ("speech", "emphasis"):
        if action == "emphasis":
            revised = await ask_json(
                "Return a JSON object with a text field. Add natural Vietnamese narration pauses "
                "using punctuation only. Preserve every original word in order.",
                s["voiceover"],
            )
            text = revised["text"]
            if re.findall(r"\w+", text.casefold()) != re.findall(
                r"\w+", s["voiceover"].casefold()
            ):
                raise ValueError("Muse đã đổi lời kể. Studio giữ nguyên bản gốc.")
        else:
            text = s["voiceover"]
        speech = new_asset(vid, "mp3")
        _, segments = resolve_scene_cast(s, v.get("cast", []))
        if segments:
            text = " ".join(seg["text"] for seg in segments)
            _, failed, why = await media.generate_multi_voice_tts_with_status(
                segments, str(speech), str(work / "voices")
            )
        else:
            _, failed, why = await media.generate_tts_with_status(
                text, s.get("voice") or v["voice"], str(speech)
            )
        if failed:
            raise ValueError(why or "Không tạo được lời kể.")
        s.update(speech=speech.name, voiceover=text)
    elif action == "clip":
        if not can_act(s, "clip"):
            raise ValueError("Ảnh chưa duyệt.")
        ref = asset_path(vid, s["frame"])
        if not ref.is_file():
            raise ValueError("Ảnh đã duyệt không còn trên đĩa.")
        clip = new_asset(vid, "mp4")
        token, source = studio_opening(v, s)
        first = str(asset_path(vid, token)) if token else None
        world = "\n".join(
            media.character_block_of(r)
            for r in references(v, s)
            if r.get("kind") in ("setting", "prop")
        )
        shot = {
            **s,
            "world_description": world,
            "approved_frame": first or "",
            "raw_studio": True,
            "reference_source": source,
        }
        used = await batch._render_story_clip(
            v, shot, idx, work, clip, batch._BatchCancellation(vid), progress
        )
        last_frame = new_asset(vid, "jpg")
        extracted = await asyncio.to_thread(
            media.extract_last_frame, str(clip), str(last_frame)
        )
        report = await asyncio.to_thread(
            qc.qc_clip, str(clip), expected_duration=s["duration"]
        )
        if s.get("clip"):
            s["previous_take"] = take(s)
        s.update(
            qc=report,
            qc_accepted=False,
            reference_source=source,
            input_frame=Path(first).name if first else None,
            accepted_join=None,
            last_frame=last_frame.name if extracted else None,
            clip=clip.name,
            approved=False,
            video_status="done",
            used_prompt=used,
            clip_frame=s["frame"],
            clip_generations=s.get("clip_generations", 0) + 1,
        )
    refresh_joins(v["scenes"])
    s["error"] = None


def inspect_final(path):
    info = qc.probe_clip(str(path))
    proc = subprocess.run(
        [
            composer.FFMPEG,
            "-nostats",
            "-i",
            str(path),
            "-vn",
            "-af",
            "loudnorm=print_format=json",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        check=False,
        timeout=600,
    )
    output = proc.stderr.decode(errors="replace")
    m = re.search(r'\{\s*"input_i".*?\}', output, re.DOTALL)
    if proc.returncode == 0 and m:
        levels = json.loads(m.group())
        for name, field in (("lufs", "input_i"), ("peak", "input_tp")):
            val = float(levels[field])
            if math.isfinite(val):
                info[name] = val
    return info


async def export(v, op, work, progress):
    vid = v["id"]
    clips, durations = [], []
    narrated = op["action"] == "narrated"
    for s in v["scenes"]:
        if batch._BatchCancellation(vid).is_set():
            raise asyncio.CancelledError()
        idx = s["index"]
        progress(idx, "Chuẩn hóa cảnh để ghép phim")
        out = work / f"clip_{idx}.mp4"
        speech = None
        if narrated:
            if not s.get("speech"):
                await produce(v, s, "speech", work, progress)
            speech = asset_path(vid, s["speech"])
        if (
            s.get("approved")
            and s.get("clip_frame") == s.get("frame")
            and s.get("frame_approved")
        ):
            source = asset_path(vid, s["clip"])
        elif (
            narrated
            and op["allow_stills"]
            and s.get("frame_approved")
            and not s.get("clip")
        ):
            dur = max(s["duration"], media.get_media_duration(str(speech)))
            source = work / f"still_{idx}.mp4"
            if not batch._convert_image_and_audio_to_clip(
                asset_path(vid, s["frame"]), None, dur, v["aspect_ratio"], source
            ):
                raise ValueError(f"Lỗi dựng ảnh cảnh {idx}.")
        else:
            raise ValueError(f"Cảnh {idx} chưa được duyệt.")
        await asyncio.to_thread(
            composer.compose_scene_video,
            str(source),
            str(speech) if speech else None,
            str(out),
            target_duration=s["duration"],
            aspect_ratio=v["aspect_ratio"],
            quality="1080p",
        )
        width, height = qc.expected_resolution_for_size(v["aspect_ratio"], "1080p")
        target_duration = max(
            s["duration"], media.get_media_duration(str(speech)) if speech else 0
        )
        result = await asyncio.to_thread(
            qc.qc_clip, str(out), target_duration, width, height, composer.TARGET_FPS
        )
        if not result["passed"] and not s.get("qc_accepted"):
            s.update(qc=result, approved=False)
            batch.BATCH_MANAGER.update_video(
                vid, {"scenes": v["scenes"], **INVALID_FINAL}
            )
            raise ValueError(
                f"Cảnh {idx} không đạt QC trước khi ghép: "
                + ", ".join(result["issues"])
            )
        clips.append(str(out))
        durations.append(media.get_media_duration(str(out)))
    merged = work / "merged.mp4"
    transitions = [s.get("transition_out") or "dissolve" for s in v["scenes"][:-1]]
    offsets = composer.compute_timeline_offsets(clips, transitions, 0.75)
    await asyncio.to_thread(
        composer.merge_clips_advanced,
        clips,
        transitions,
        0.75,
        str(merged),
        "1080p",
        v["aspect_ratio"],
    )
    srt = work / "subtitles.srt"
    composer.create_srt(
        v["scenes"],
        str(srt),
        clip_durations=durations,
        media_paths=clips,
        timeline_offsets=offsets,
    )
    final = merged
    if narrated:
        final = work / "captioned.mp4"
        await asyncio.to_thread(
            composer.burn_subtitles,
            str(merged),
            str(srt),
            str(final),
            font_size=12,
            style=v.get("subtitle_style", "cinema"),
            position=v.get("subtitle_position", "bottom"),
            raise_on_error=True,
        )
    if narrated and v.get("music"):
        from studio_routes import music_file

        track = music_file(v["music"])
        if not track:
            raise ValueError("Nhạc nền đã chọn không còn trên đĩa.")
        mixed = work / "mixed.mp4"
        await asyncio.to_thread(
            composer.add_background_music,
            str(final),
            str(track),
            str(mixed),
            v.get("volume", 0.15),
        )
        final = mixed
    inspection = await asyncio.to_thread(inspect_final, final)
    if not batch.BATCH_MANAGER.get_video(vid):
        raise asyncio.CancelledError()
    for s, clip, duration in zip(v["scenes"], clips, durations):
        idx = s["index"]
        s["actual_duration"] = duration
        os.replace(clip, directory(vid) / "clips" / f"clip_{idx}.mp4")
        if s.get("speech"):
            shutil.copy2(
                asset_path(vid, s["speech"]),
                directory(vid) / "audio" / f"scene_{idx:03d}.mp3",
            )
        if s.get("frame"):
            images = directory(vid) / "images"
            images.mkdir(exist_ok=True)
            shutil.copy2(asset_path(vid, s["frame"]), images / f"scene_{idx}.webp")
    os.replace(final, directory(vid) / "final.mp4")
    os.replace(srt, directory(vid) / "subtitles.srt")
    return inspection


def run(video):
    vid = video["id"]
    op = video.get("operation")
    manager = batch.BATCH_MANAGER
    if not op:
        manager.update_video(
            vid, {"status": "review", "current_step": "Chờ duyệt phân cảnh"}
        )
        return

    def progress(i, message, extra=None):
        manager.update_video(vid, {"current_step": f"Cảnh {i} · {message}"})

    manager.update_video(vid, {"status": "running"})
    try:
        if op["action"] in ("compose", "narrated"):
            with tempfile.TemporaryDirectory(dir=directory(vid)) as tmp:
                report = asyncio.run(export(video, op, Path(tmp), progress))
            manager.update_video(
                vid,
                {
                    "status": "completed",
                    "progress": 100,
                    "current_step": "Đã xuất phim",
                    "inspection": report,
                    "scenes": video["scenes"],
                    "operation": None,
                    "error": None,
                    "final_video_url": f"/api/batch-videos/{vid}/video",
                    "srt_url": f"/api/batch-videos/{vid}/subtitles",
                },
            )
            return
        # An interrupted provider request may already have consumed a generation.
        # Require an explicit retry for those scenes instead of submitting twice.
        if op.get("active"):
            raise ValueError(
                "Lượt trước bị gián đoạn. Kiểm tra cảnh đang làm và chạy lại."
            )
        if op["action"] == "portraits":
            op["active"] = ["portraits"]
            manager.update_video(vid, {"operation": op})
            asyncio.run(draw_cast(video, progress))
        while op["pending"]:
            indexes = op["pending"][: 6 if op["action"] == "write" else 1]
            op["active"] = indexes
            if not manager.update_video(
                vid, {"operation": op, "current_step": f"Đang xử lý cảnh {indexes[0]}"}
            ):
                return
            try:
                with tempfile.TemporaryDirectory(dir=directory(vid)) as tmp:
                    if op["action"] == "write":
                        asyncio.run(write_beats(video, indexes))
                    else:
                        asyncio.run(
                            produce(
                                video,
                                selected_scene(video, indexes[0]),
                                op["action"],
                                Path(tmp),
                                progress,
                            )
                        )
            except Exception as error:  # noqa: BLE001 — isolate provider failures to these scenes
                for idx in indexes:
                    selected_scene(video, idx)["error"] = str(error)
                op["errors"].append(f"Cảnh {indexes[0]}: {error}")
                if op["action"] == "write" or (
                    op["action"] == "clip"
                    and video.get("chain_fallback_mode") == "stop"
                ):
                    break
            del op["pending"][: len(indexes)]
            op["active"] = []
            manager.update_video(
                vid,
                {
                    **INVALID_FINAL,
                    "operation": op,
                    "cast": video.get("cast", []),
                    "scenes": video["scenes"],
                },
            )
        manager.update_video(
            vid,
            {
                **INVALID_FINAL,
                "operation": None,
                "scenes": video["scenes"],
                "status": "review",
                "error": "\n".join(op["errors"]) or None,
                "current_step": "Chờ duyệt phân cảnh",
            },
        )
        with batch._QUEUE_LOCK:
            latest = manager.get_video(vid)
            if latest and not latest.get("operation"):
                prune_takes(vid, latest["scenes"])
    except asyncio.CancelledError:
        return
    except Exception as error:  # noqa: BLE001 — always release the queue after worker failure
        manager.update_video(
            vid,
            {
                "operation": None,
                "status": "review",
                "error": str(error),
                "current_step": "Cần kiểm tra",
            },
        )
