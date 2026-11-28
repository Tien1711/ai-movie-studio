"""Persistent review gates for MuseStudio. Only the batch worker performs media generation."""

import asyncio
import base64
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
import comic_pipeline
import composer
import qc
from consistency import (
    build_bridge_scenes_for_jumps,
    opening_reference,
    partition_clip_segments,
    resolve_scene_cast,
    run_consistency_checks,
)
from muse_connection import current_muse_affinity, muse_capacity_from_accounts
from muse_voice import REMOTE_DELIVERY, narrator_text, remote_speaker_names, scene_voice_prompt, voice_turn_blocks

BUSY = {"queued", "running"}
ASYNC = {
    "comic",
    "image",
    "clip",
    "speech",
    "compose",
    "narrated",
    "write",
    "emphasis",
    "portraits",
    "vision_qc",
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
        for k in ("frame", "clip", "last_frame", "speech", "input_frame", "comic_panel", "turn_reference")
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
    if action == "speech":
        return bool(s.get("voiceover") or s.get("dialogue") or s.get("comic_lines"))
    if action == "emphasis":
        return bool(s.get("voiceover"))
    if action in ("approve_image", "vision_qc"):
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
            native = action == "clip" and video.get("clip_audio_mode") == "native"
            for scene in chosen:
                try:
                    resolve_scene_cast(
                        scene, video.get("cast", []), require_tts_voice=action in ("speech", "emphasis")
                    )
                    if native:
                        scene_voice_prompt(scene, video.get("cast", []), video.get("muse_narrator"))
                except ValueError as error:
                    raise ValueError(f"Cảnh {scene['index']}: {error}") from error
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
            if action == "narrated" and video.get("timing_mode") == "strict_clip":
                for s in scenes:
                    if s.get("speech") and (s.get("speech_duration") or 0) > float(s["duration"]) * 1.35 + 0.02:
                        raise ValueError(f"Cảnh {s['index']}: lời đọc dài {s['speech_duration']:.2f}s. Hãy tăng thời lượng hoặc chọn khớp theo lời đọc.")
        elif action == "comic":
            if not video.get("comic") or batch.active_muse_provider() != "browser":
                raise ValueError("Chọn Muse Browser để tiếp tục dựng chương truyện tranh.")
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
                if action == "clip" and video.get("clip_audio_mode") == "native":
                    blocks = voice_turn_blocks(shot, video.get("cast", []), video.get("muse_narrator"))
                    if len(blocks) > 1:
                        warnings.append(
                            f"Cảnh {shot['index']}: có {len(blocks)} lượt thoại khác nhau. "
                            "Dùng Tách lượt thoại Muse nếu cần tránh lẫn người nói hoặc sai thứ tự."
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
                    "include_subtitles": req.include_subtitles,
                    "max_concurrent": req.max_concurrent,
                    "clip_parallel_mode": req.clip_parallel_mode or video.get("clip_parallel_mode", "sequential"),
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
                    if old.get("before_voice_split"):
                        raise ValueError("Bản này thuộc cả cảnh trước khi tách thoại và chỉ dùng để so sánh.")
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
                    if not req.prompt.strip():
                        raise ValueError("Điền mô tả hình ảnh của cảnh.")
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
                    resolve_scene_cast({**s, **metadata}, video.get("cast", []), require_tts_voice=False)
                    if "dialogue" in metadata and metadata["dialogue"] != s.get(
                        "dialogue"
                    ):
                        s["speech"] = None
                        s["speech_duration"] = None
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
                        s["speech_duration"] = None
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


async def ask_json(system, user, reference_image=None, response_parser=parse_llm_json):
    import httpx

    _, base, key = batch.muse_connection(batch.active_muse_provider())
    if not key:
        raise ValueError("Chưa cấu hình khóa kết nối Muse. Kiểm tra mục Cài đặt Muse.")
    affinity = current_muse_affinity()
    async with httpx.AsyncClient(timeout=150) as client:
        try:
            res = await client.post(
                f"{base}/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": "muse-spark",
                    **({"reference_image": reference_image} if reference_image else {}),
                    **({"affinity": affinity} if affinity else {}),
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
        raw = ((payload.get("choices") or [{}])[0].get("message") or {}).get(
            "content"
        ) or ""
        return response_parser(raw)


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
        "prompt (visual action, camera, lighting and expressions matching named characters. Only an on-screen "
        "speaker moves their lips. For a device or off-screen voice, frame the device or a silent listener), "
        "voiceover (Vietnamese 3rd-person off-screen narrator line that fits the seconds), "
        "dialogue (empty for narration-only beats, otherwise one 'Character Name [source]: short spoken line' "
        "per line in speaking order. Source is on_screen, offscreen, phone, radio, speaker, or thought. "
        "Use 'speaker' for a loudspeaker. Keep a single source per scene when channels change). "
        "Also return characters (names actually visible), setting (place and story time, repeated verbatim until it changes), "
        "new_segment (boolean), transition_out (cut or dissolve). Continue the preceding beats. "
        "Use exactly the supplied or newly registered character names.",
        json.dumps(payload, ensure_ascii=False),
    )
    cast = []
    if infer_cast:
        if not isinstance(rows, dict):
            raise ValueError(
                "Muse trả kịch bản mở đầu không đúng cấu trúc JSON object."
            )
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
        raw_dialogue = str(r.get("dialogue") or "").strip()[:3000]
        clean_dialogue = ""
        if raw_dialogue:
            valid_lines = []
            for dl in raw_dialogue.splitlines():
                dl = dl.strip()
                if not dl:
                    continue
                _, sep, _ = dl.partition(":")
                if not sep:
                    continue
                try:
                    _, (line,) = resolve_scene_cast(
                        {"dialogue": dl}, v.get("cast", []), require_tts_voice=False
                    )
                except ValueError:
                    continue
                source = f" [{line['delivery']}]" if line.get("delivery") else ""
                valid_lines.append(f"{line['character']}{source}: {line['text']}")
            clean_dialogue = "\n".join(valid_lines)
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
            dialogue=clean_dialogue,
            error=None,
        )


_VI_SAFE_VISUAL_REPLACEMENTS = (
    (r"kẻ cai trị tàn bạo|tên quan tàn bạo", "quan lại cổ trang uy nghiêm"),
    (
        r"tàn bạo|hung ác|khát máu|đẫm máu|sát khí|giết chóc|chém giết",
        "uy nghiêm kịch tính",
    ),
    (r"nham hiểm|độc ác|xảo quyệt", "sắc sảo"),
    (r"nữ chiến binh", "nữ hiệp cổ trang"),
    (r"chiến binh", "tráng sĩ cổ trang"),
    (r"nữ tướng", "nữ thủ lĩnh cổ trang"),
    (
        r"cầm thanh kiếm dài|thanh kiếm dài|thanh kiếm|thanh gươm|gươm báu|kiếm báu",
        "trượng ngọc lễ nghi cổ trang",
    ),
    (
        r"cầm thanh đao lớn|thanh đao lớn|đại đao|thanh đao",
        "trường côn cổ trang chạm khắc",
    ),
    (
        r"cầm cây giáo dài đầu đồng|cầm cây giáo dài|cây giáo dài|ngọn giáo|giáo đồng|giáo tre",
        "cờ lệnh cổ trang thêu hoa văn",
    ),
    (r"\b(?:kiếm|gươm|đao|giáo mác|vũ khí)\b", "pháp khí cổ trang"),
)


_EN_SAFE_VISUAL_REPLACEMENTS = (
    (r"\b(?:jeweled\s+|bronze\s+|long\s+)?swords?\b|\bblades?\b|\bspears?\b|\bweapons?\b", "ornate golden ceremonial scroll"),
    (r"\b(?:crimson\s+silk\s+|ancient\s+|bronze\s+)?armor\b", "embroidered crimson and gold royal silk tunic"),
    (r"\b(?:woman\s+)?warriors?\b|\bsoldiers?\b", "noble traditional heroine"),
    (r"\bdrawing\s+her\s+ornate\s+golden\s+ceremonial\s+scroll\b", "unfurling her ornate golden ceremonial scroll"),
)


def sanitize_visual_traits(text: str, *, aggressive: bool = False) -> str:
    """Soften weapon/violence & ID-portrait triggers while keeping visual traits intact."""
    cleaned = (text or "").strip()
    for pattern, repl in _VI_SAFE_VISUAL_REPLACEMENTS:
        cleaned = re.sub(pattern, repl, cleaned, flags=re.IGNORECASE)
    if aggressive:
        for pattern, repl in _EN_SAFE_VISUAL_REPLACEMENTS:
            cleaned = re.sub(pattern, repl, cleaned, flags=re.IGNORECASE)
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
    pending_chars = [
        (index, character)
        for index, character in enumerate(v["cast"], 1)
        if not character.get("avatar") and character.get("auto_cast")
    ]
    if not pending_chars:
        return
    workers = await image_workers(len(pending_chars)) if len(pending_chars) > 1 else 1
    sem = asyncio.Semaphore(workers)

    async def _draw_one(index, character):
        async with sem:
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
                        for k in (
                            "tải lại lượt chat",
                            "ô chat muse",
                            "chưa xác nhận đã gửi",
                        )
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

    if workers <= 1:
        for index, character in pending_chars:
            await _draw_one(index, character)
    else:
        await asyncio.gather(*(_draw_one(index, character) for index, character in pending_chars))


def studio_opening(video, scene):
    clip_mode = (video.get("operation") or {}).get("clip_parallel_mode") or video.get(
        "clip_parallel_mode", "sequential"
    )
    if clip_mode == "full_parallel" and scene.get("frame"):
        return scene["frame"], "anchor"
    previous = next(
        (s for s in video["scenes"] if s["index"] == scene["index"] - 1), None
    )
    setting = (scene.get("setting") or "").strip().casefold()
    previous_setting = ((previous or {}).get("setting") or "").strip().casefold()
    cast_changed = False
    if previous and video.get("cast") and (scene.get("characters") or scene.get("dialogue")):
        def cast_at(shot):
            return {c["name"] for c in references(video, shot) if c.get("kind", "character") == "character"}

        cast_changed = bool(cast_at(scene) - cast_at(previous))
    if scene.get("frame_approved") and scene.get("frame") and (
        not setting or setting != previous_setting or cast_changed
    ):
        return scene["frame"], "anchor"
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
    if s.get("characters") or (s.get("dialogue") and library):
        people, _ = resolve_scene_cast(s, library, require_tts_voice=False)
        found = people + [r for r in found if r.get("kind") in ("setting", "prop")]
    if library and s.get("dialogue"):
        remote = remote_speaker_names(library, s)
        return [person for person in found if person["name"] not in remote]
    return found


async def produce(v, s, action, work, progress):
    vid, idx = v["id"], s["index"]
    if action == "image" and s.get("comic_panel"):
        await color_comic_panel(v, s, work, progress)
        return
    if action == "image":
        cast = references(v, s)
        ref, guide = await asyncio.to_thread(batch._cast_reference, cast)
        if s.get("turn_reference") and (original := asset_path(vid, s["turn_reference"])).is_file():
            ref = str(original)
            guide = "Use the attachment for identity, costume and colors only. Follow the new camera framing requested for this shot."
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
        visual_report = None
        if v.get("vision_qc") and batch.active_muse_provider() == "browser":
            progress(idx, "AI Vision chấm điểm đồng nhất ảnh")
            visual_report = await qc.evaluate_visual_consistency(
                str(dest), ref, s.get("prompt", ""), ask_json
            )
            if not visual_report.get("passed"):
                progress(
                    idx,
                    f"Ảnh đạt {visual_report.get('score')}/100 — tự vẽ lại tăng độ đồng nhất",
                )
                retry_path = work / "generated_retry"
                issues_hint = "; ".join(visual_report.get("issues") or [])
                reinforced = (
                    f"{prompt}\nCRITICAL FIX: strictly match reference face, hairstyle, and costume; "
                    f"fix these flaws: {issues_hint or 'character drift, distorted hands, stray text'}. No text, no watermark."
                )
                try:
                    await media.generate_keyframe_image(
                        reinforced, base, key, str(retry_path), v["aspect_ratio"], ref
                    )
                    retry_dest = new_asset(vid, "webp")
                    image_to_webp(retry_path, retry_dest)
                    retry_report = await qc.evaluate_visual_consistency(
                        str(retry_dest), ref, s.get("prompt", ""), ask_json
                    )
                    if (retry_report.get("score") or 0) >= (visual_report.get("score") or 0):
                        dest.unlink(missing_ok=True)
                        dest = retry_dest
                        visual_report = retry_report
                    else:
                        retry_dest.unlink(missing_ok=True)
                except Exception:
                    pass
        set_image(s, dest.name)
        s["anchors"] = [c["name"] for c in cast if c.get("avatar")]
        s["image_prompt"] = prompt
        s["image_generations"] = s.get("image_generations", 0) + 1
        if visual_report is not None:
            s["visual_qc"] = visual_report
    elif action == "vision_qc":
        if batch.active_muse_provider() != "browser":
            raise ValueError(
                "AI Vision QC cần chọn nhà cung cấp Muse Browser để đính kèm ảnh chấm điểm trực tiếp."
            )
        frame_path = asset_path(vid, s.get("frame"))
        if not frame_path.is_file():
            raise ValueError("Chưa có ảnh mở đầu để chấm điểm.")
        cast = references(v, s)
        ref, _ = await asyncio.to_thread(batch._cast_reference, cast)
        if ref and ref.startswith(("http://", "https://")):
            ref = await asyncio.to_thread(media._browser_image_from_url, ref)
        elif ref:
            ref = batch._resolve_character_ref_data_uri(ref)
        progress(idx, "AI Vision đang chấm điểm đồng nhất")
        s["visual_qc"] = await qc.evaluate_visual_consistency(
            str(frame_path), ref, s.get("prompt", ""), ask_json
        )
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
        narrator_voice = s.get("voice") or v["voice"]
        has_dialogue = bool((s.get("dialogue") or "").strip())
        segments = []
        if (comic_segments := comic_speech_segments(v, s)) is not None:
            segments = comic_segments
        elif has_dialogue:
            try:
                _, segments = resolve_scene_cast(s, v.get("cast", []))
            except ValueError:
                # Cho phép thoại nhân vật fallback về giọng kể nếu nhân vật chưa khóa giọng riêng
                for line in (s.get("dialogue") or "").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    marker, sep, words = line.partition(":")
                    spoken = words.strip() if sep else line
                    if spoken:
                        segments.append({
                            "character": marker.strip() if sep else "Character",
                            "voice": narrator_voice,
                            "text": spoken,
                        })
        if segments:
            # Nếu cảnh có cả lời kể ngôi thứ 3 VÀ thoại nhân vật riêng biệt -> đọc nối tiếp: Giọng kể -> Giọng nhân vật
            dlg_joined = " ".join(seg["text"] for seg in segments).strip()
            narr_clean = (text or "").strip()
            combined_segments = []
            if narr_clean and narr_clean.casefold() != dlg_joined.casefold():
                combined_segments.append({
                    "character": "Narrator",
                    "voice": narrator_voice,
                    "text": narr_clean,
                })
            combined_segments.extend(segments)
            _, failed, why = await media.generate_multi_voice_tts_with_status(
                combined_segments, str(speech), str(work / f"voices_{idx}")
            )
            if failed:
                raise ValueError(why or "Không tạo được lời kể và thoại nhân vật.")
            # Giữ nguyên s["voiceover"] (không ghi đè lời kể ngôi thứ 3 bằng thoại nhân vật)
            sp_dur = round(float(await asyncio.to_thread(media.get_media_duration, str(speech)) or 0.0), 2)
            s.update(speech=speech.name, speech_duration=sp_dur or None,
                     voiceover=text if comic_segments is not None else text or dlg_joined)
        else:
            _, failed, why = await media.generate_tts_with_status(
                text, narrator_voice, str(speech)
            )
            if failed:
                raise ValueError(why or "Không tạo được lời kể.")
            sp_dur = round(float(await asyncio.to_thread(media.get_media_duration, str(speech)) or 0.0), 2)
            s.update(speech=speech.name, speech_duration=sp_dur or None, voiceover=text)
    elif action == "clip":
        if not can_act(s, "clip"):
            raise ValueError("Ảnh chưa duyệt.")
        ref = asset_path(vid, s["frame"])
        if not ref.is_file():
            raise ValueError("Ảnh đã duyệt không còn trên đĩa.")
        audio_mode = v.get("clip_audio_mode", "auto")
        if audio_mode == "narrated" and (s.get("voiceover") or s.get("dialogue")):
            if not s.get("speech") or not asset_path(vid, s["speech"]).is_file():
                await produce(v, s, "speech", work, progress)
            measured = await asyncio.to_thread(media.get_media_duration, str(asset_path(vid, s["speech"])))
            if not measured:
                raise ValueError(f"Cảnh {idx}: chưa đo được thời lượng lời đọc.")
            s["speech_duration"] = measured
            limit = float(s["duration"]) if v.get("timing_mode") == "strict_clip" else 12
            if measured > limit + 0.02:
                raise ValueError(f"Cảnh {idx}: lời đọc dài {measured:.2f}s, vượt {limit:g}s. Hãy tăng thời lượng, rút lời hoặc tách cảnh.")
        if audio_mode != "native" and (v.get("timing_mode") == "auto_expand_clip" or
                                       (audio_mode == "narrated" and v.get("timing_mode") != "strict_clip")):
            rec_dur = recommended_clip_duration(s)
            if rec_dur > int(s.get("duration") or 5):
                s["duration"] = rec_dur
        clip = new_asset(vid, "mp4")
        token, source = studio_opening(v, s)
        first = str(asset_path(vid, token)) if token else None
        world = "\n".join(
            media.character_block_of(r)
            for r in references(v, s)
            if r.get("kind") in ("setting", "prop")
        )
        if s.get("comic_panel"):
            setting = v.get("comic_settings", {}).get((s.get("setting") or "").casefold())
            if setting:
                world = f"Setting {setting['name']}: {setting['colors']}"
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
        if audio_mode == "native" and not report.get("has_audio"):
            report["passed"] = False
            report["issues"].append("Clip Muse thiếu track âm thanh gốc.")
        if s.get("comic_panel") and report["passed"]:
            try:
                if not extracted:
                    raise ValueError("Không lấy được khung cuối của clip.")
                review = await review_comic_image(
                    [ref, last_frame],
                    "Compare the left approved color frame to the right final video frame. "
                    "Check that visible faces, garments and their base colors stay consistent. "
                    "Allow movement, lighting, a camera cut, and characters moving out of view.",
                    comic_palette(v, s),
                )
            except ValueError as error:
                review = {"passed": False, "issues": [str(error)]}
            s["comic_clip_visual_qc"] = review
            if not review["passed"]:
                report["passed"] = False
                report["issues"].extend(review["issues"])
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
    narrated = op["action"] == "narrated"
    timing_mode = v.get("timing_mode") or "match_speech"
    strict_clip = timing_mode == "strict_clip"
    scenes = v["scenes"]
    width, height = qc.expected_resolution_for_size(v["aspect_ratio"], "1080p")
    prep_workers = min(len(scenes) or 1, max(2, min(4, (os.cpu_count() or 4) // 2)))
    sem = asyncio.Semaphore(prep_workers)
    done_count = 0

    async def _prepare_scene(s):
        nonlocal done_count
        async with sem:
            if batch._BatchCancellation(vid).is_set():
                raise asyncio.CancelledError()
            idx = s["index"]
            progress(idx, f"Chuẩn hóa cảnh {idx}/{len(scenes)} ({prep_workers} luồng song song)")
            out = work / f"clip_{idx}.mp4"
            speech = None
            speech_dur = 0.0
            if narrated and (s.get("speech") or s.get("voiceover") or s.get("dialogue") or s.get("comic_lines")):
                if not s.get("speech"):
                    await produce(v, s, "speech", work, progress)
                speech = asset_path(vid, s["speech"])
                speech_dur = float(await asyncio.to_thread(media.get_media_duration, str(speech)) or 0.0)
                if speech_dur > 0:
                    s["speech_duration"] = round(speech_dur, 2)
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
                dur = float(s["duration"]) if strict_clip else max(s["duration"], speech_dur)
                source = work / f"still_{idx}.mp4"
                if not await asyncio.to_thread(
                    batch._convert_image_and_audio_to_clip,
                    asset_path(vid, s["frame"]), None, dur, v["aspect_ratio"], source
                ):
                    raise ValueError(f"Lỗi dựng ảnh cảnh {idx}.")
            else:
                raise ValueError(f"Cảnh {idx} chưa được duyệt.")
            target_seconds = float(s["duration"])
            if not narrated:
                target_seconds = await asyncio.to_thread(media.get_media_duration, str(source))
                if not target_seconds or (v.get("clip_audio_mode") == "native" and not await asyncio.to_thread(composer.check_video_has_audio, str(source))):
                    raise ValueError(f"Cảnh {idx}: clip Muse thiếu thời lượng hoặc track âm thanh gốc. Hãy kiểm tra clip trước khi ghép.")
            if strict_clip and speech_dur > target_seconds * 1.35 + 0.02:
                raise ValueError(f"Cảnh {idx}: lời đọc {speech_dur:.2f}s vượt khả năng của clip {target_seconds:g}s. Hãy chọn khớp theo lời đọc hoặc rút lời trước khi xuất.")
            spd = 1.0
            if strict_clip and speech_dur > float(s["duration"]) * 1.01:
                spd = min(1.35, speech_dur / float(s["duration"]))
            await asyncio.to_thread(
                composer.compose_scene_video,
                str(source),
                str(speech) if speech else None,
                str(out),
                target_duration=target_seconds,
                normalize_dialogue=not narrated and bool(
                    (s.get("dialogue") or "").strip() or (
                        v.get("clip_audio_mode") == "native" and narrator_text(s, v.get("muse_narrator"))
                    )
                ),
                aspect_ratio=v["aspect_ratio"],
                quality="1080p",
                ambience_volume=(
                    0.0
                    if (s.get("dialogue") or "").strip()
                    else (
                        0.18
                        if s.get("clip")
                        and (
                            batch.STORY_AMBIENCE_DIRECTION.rstrip(".") in (s.get("used_prompt") or "")
                            or "Pure environmental Foley" in (s.get("used_prompt") or "")
                            or "Generate audible location ambience" in (s.get("used_prompt") or "")
                        )
                        else 0.0
                    )
                ),
                smart_pacing=v.get("smart_pacing", True),
                smart_trim=v.get("smart_trim", False),
                color_grade=v.get("color_grade", "none"),
                watermark_text=v.get("watermark_text", ""),
                timing_mode=timing_mode,
                threads=max(1, (os.cpu_count() or 4) // max(1, prep_workers)),
            )
            target_duration = (
                target_seconds
                if strict_clip or not narrated
                else max(s["duration"], speech_dur if speech else 0)
            )
            already_qcd = bool((s.get("qc") or {}).get("passed") or s.get("qc_accepted"))
            if not already_qcd:
                result = await asyncio.to_thread(
                    qc.qc_clip, str(out), target_duration, width, height, composer.TARGET_FPS
                )
                if not result["passed"]:
                    s.update(qc=result, approved=False)
                    batch.BATCH_MANAGER.update_video(
                        vid, {"scenes": v["scenes"], **INVALID_FINAL}
                    )
                    raise ValueError(
                        f"Cảnh {idx} không đạt QC trước khi ghép: "
                        + ", ".join(result["issues"])
                    )
            out_dur = await asyncio.to_thread(media.get_media_duration, str(out))
            done_count += 1
            progress(idx, f"Đã chuẩn hóa xong {done_count}/{len(scenes)} cảnh")
            return str(out), out_dur, str(speech or out), spd

    prepared = await asyncio.gather(*(_prepare_scene(s) for s in scenes))
    clips = [item[0] for item in prepared]
    durations = [item[1] for item in prepared]
    speech_sources = [item[2] for item in prepared]
    speed_factors = [item[3] for item in prepared]
    merged = work / "merged.mp4"
    transitions = [
        (s.get("transition_out") or "dissolve") if narrated else "cut"
        for s in v["scenes"][:-1]
    ]
    bridge_duration = 0.75 if narrated else 0.0
    for i, scene in enumerate(scenes[:-1]):
        if narrated and durations[i] - (scene.get("speech_duration") or 0) / speed_factors[i] < bridge_duration:
            transitions[i] = "cut"
    offsets = composer.compute_timeline_offsets(clips, transitions, bridge_duration)
    await asyncio.to_thread(
        composer.merge_clips_advanced,
        clips,
        transitions,
        bridge_duration,
        str(merged),
        "1080p",
        v["aspect_ratio"],
    )
    srt = work / "subtitles.srt"
    include_subtitles = op.get("include_subtitles", True)
    if include_subtitles:
        srt_scenes = []
        for s in v["scenes"]:
            if not narrated and v.get("clip_audio_mode") == "native":
                _, lines = resolve_scene_cast(s, v.get("cast", []), require_tts_voice=False)
                paragraphs = [narrator_text(s, v.get("muse_narrator")), *(line["text"] for line in lines)]
                srt_scenes.append({**s, "voiceover": " ".join(filter(None, paragraphs))})
                continue
            vo = (s.get("voiceover") or "").strip()
            if (segments := comic_speech_segments(v, s)) is not None:
                spoken = " ".join([vo, *(segment["text"] for segment in segments)]).strip()
                srt_scenes.append({**s, "voiceover": spoken})
                continue
            dlg_raw = (s.get("dialogue") or "").strip()
            if dlg_raw:
                spoken_lines = []
                for dl in dlg_raw.splitlines():
                    dl = dl.strip()
                    if not dl:
                        continue
                    _, sep, words = dl.partition(":")
                    spoken = words.strip() if sep else dl
                    if spoken:
                        spoken_lines.append(spoken)
                dlg_text = " ".join(spoken_lines).strip()
                if dlg_text and dlg_text.casefold() not in vo.casefold():
                    vo = f"{vo} {dlg_text}".strip() if vo else dlg_text
            srt_scenes.append({**s, "voiceover": vo})
        composer.create_srt(
            srt_scenes,
            str(srt),
            clip_durations=durations,
            media_paths=speech_sources,
            speed_factors=speed_factors,
            timeline_offsets=offsets,
        )
    final = merged
    if narrated and v.get("transition_sfx", False) and len(offsets) > 1:
        dissolve_offsets = [
            offsets[i + 1]
            for i, tr in enumerate(transitions)
            if tr == "dissolve" and i + 1 < len(offsets)
        ]
        if dissolve_offsets:
            with_sfx = work / "with_sfx.mp4"
            await asyncio.to_thread(
                composer.add_transition_sfx, str(final), dissolve_offsets, str(with_sfx)
            )
            final = with_sfx
    if narrated and include_subtitles:
        word_timings = composer.collect_timeline_word_timings(
            speech_sources, offsets, speed_factors=speed_factors if strict_clip else None
        )
        final_cap = work / "captioned.mp4"
        await asyncio.to_thread(
            composer.burn_subtitles,
            str(final),
            str(srt),
            str(final_cap),
            font_size=12,
            style=v.get("subtitle_style", "cinema"),
            position=v.get("subtitle_position", "bottom"),
            raise_on_error=True,
            word_timings=word_timings or None,
        )
        final = final_cap
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
            auto_ducking=v.get("auto_ducking", True),
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
    try:
        # Dùng merged.mp4 (trước bước burn_subtitles) để Smart Thumbnail sắc nét không bị dính chữ phụ đề ở chân khung hình
        thumb_source = str(merged) if merged.is_file() else str(directory(vid) / "final.mp4")
        await asyncio.to_thread(
            composer.generate_smart_thumbnail,
            thumb_source,
            str(directory(vid) / "thumbnail.jpg"),
            title=v.get("title") or "",
            aspect_ratio=v.get("aspect_ratio") or "16:9",
        )
    except Exception:
        pass
    target_srt = directory(vid) / "subtitles.srt"
    if include_subtitles:
        os.replace(srt, target_srt)
    else:
        target_srt.unlink(missing_ok=True)
    return inspection


async def image_workers(max_concurrent=12):
    import httpx

    provider, url, key = batch.muse_connection(batch.active_muse_provider())
    if provider not in ("muse2api", "browser") or not key:
        return 1
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(
                f"{url}/admin/accounts", headers={"Authorization": f"Bearer {key}"}
            )
            response.raise_for_status()
            data = response.json()
        return muse_capacity_from_accounts(provider, data, max_concurrent)
    except Exception:  # noqa: BLE001 — fallback cleanly when capacity probe is unavailable
        return 1


async def produce_images(video, indexes, progress):
    async def draw(index):
        scene = selected_scene(video, index)
        try:
            with tempfile.TemporaryDirectory(dir=directory(video["id"])) as tmp:
                await produce(video, scene, "image", Path(tmp), progress)
        except Exception as error:  # noqa: BLE001 — retain success for other images
            scene["error"] = str(error)
            return f"Cảnh {index}: {error}"
        return None

    results = await asyncio.gather(*(draw(index) for index in indexes))
    return [error for error in results if error]


def comic_speech_segments(video, scene):
    lines = scene.get("comic_lines")
    if lines is None:
        return None
    original_dialogue = "\n".join(
        f"{line['speaker']}: {line['text']}" for line in lines if line["speaker"]
    )
    if (scene.get("dialogue") or "") != original_dialogue:
        return None  # Respect manual edits to the script.
    _, spoken = resolve_scene_cast(scene, video.get("cast", []))
    named = iter(spoken)
    narrator_voice = scene.get("voice") or video["voice"]
    return [
        next(named) if line["speaker"] else
        {"character": "Narrator", "voice": narrator_voice, "text": line["text"]}
        for line in lines
    ]


def comic_palette(video, scene):
    names = set(scene.get("characters", []))
    cast = comic_pipeline.palette_text(c for c in video.get("cast", []) if c["name"] in names)
    setting = video.get("comic_settings", {}).get((scene.get("setting") or "").casefold())
    return f"{cast}\nSetting {setting['name']}: {setting['colors']}" if setting else cast


def comic_page_file(vid, number, url):
    folder = directory(vid) / "comic_pages"
    folder.mkdir(exist_ok=True)
    path = folder / f"{number:03d}.webp"
    if not path.is_file():
        data = media._browser_image_from_url(url)
        with Image.open(io.BytesIO(base64.b64decode(data.split(",", 1)[1]))) as image:
            if image.width * image.height > 48_000_000:
                raise ValueError(f"Ảnh trang {number} quá lớn.")
            staged = path.with_suffix(".tmp")
            image.convert("RGB").save(staged, "WEBP", lossless=True)
            os.replace(staged, path)
    return path


async def analyze_comic_pages(video, progress):
    vid = video["id"]
    manager = batch.BATCH_MANAGER
    cancel = batch._BatchCancellation(vid)
    pages = video.get("comic_pages")
    if pages is None:
        # Older jobs used one placeholder per page. Preserve work already rendered.
        pages = [s["page_url"] for s in video["scenes"]]
        completed = next(
            (i for i, s in enumerate(video["scenes"]) if not s.get("frame")), len(pages)
        )
    else:
        completed = video.get("comic_pages_done", 0)
    scenes = [
        s for i, s in enumerate(video["scenes"], 1)
        if s.get("comic_page", i) <= completed
    ]
    cast = video.get("cast", [])
    settings = video.get("comic_settings", {})
    for number, url in enumerate(pages[completed:], completed + 1):
        if cancel.is_set():
            raise asyncio.CancelledError()
        progress(number, f"Đọc và tách khung, tách thoại trang {number}/{len(pages)}")
        source = await asyncio.to_thread(comic_page_file, vid, number, url)
        reference = await asyncio.to_thread(comic_pipeline.load_reference, [source])
        rows = await ask_json(
            comic_pipeline.PAGE_INSTRUCTIONS,
            comic_pipeline.page_context(number, video.get("title", ""), cast, settings),
            reference_image=reference,
            response_parser=comic_pipeline.parse_vision_rows,
        )
        if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"Muse chưa xác định được đầy đủ các khung ở trang {number}.")
        with Image.open(source) as page:
            requests = comic_pipeline.profiles_needed(rows, cast, settings, comic_pipeline.is_colored(page))
            profiles = []
            if any(requests.values()):
                profiles = await ask_json(
                    comic_pipeline.PROFILE_INSTRUCTIONS,
                    json.dumps({"chapter": video.get("title", ""), **requests}, ensure_ascii=False),
                    reference_image=reference,
                    response_parser=comic_pipeline.parse_vision_rows,
                )
                if not isinstance(profiles, list) or any(not isinstance(profile, dict) for profile in profiles):
                    raise ValueError(f"Trang {number}: Muse chưa trả đúng hồ sơ màu.")
            response = {
                "panels": rows,
                "characters": [profile for profile in profiles if profile.get("kind") == "character"],
                "settings": [profile for profile in profiles if profile.get("kind") == "setting"],
            }
            panels, cast, settings = comic_pipeline.parse_page(
                response, page, number, cast, settings, video["voice"]
            )
        for panel, scene in panels:
            panel_asset = new_asset(vid, "webp")
            panel.save(panel_asset, "WEBP", lossless=True)
            scenes.append({
                **scene, "index": len(scenes) + 1, "page_url": url, "comic_panel": panel_asset.name,
            })
        updates = dict(
            scenes=scenes, scenes_count=len(scenes), cast=cast,
            comic_pages=pages, comic_pages_done=number, comic_settings=settings,
        )
        if cancel.is_set() or not manager.update_video(vid, updates):
            raise asyncio.CancelledError()
        video.update(updates)


async def review_comic_image(paths, instruction, locks):
    reference = await asyncio.to_thread(comic_pipeline.load_reference, paths)
    response = await ask_json(
        "You are reviewing comic-to-video continuity. " + instruction +
        " Return only JSON with integer score from 0 to 100 and an issues array of concise Vietnamese "
        "descriptions of actual mismatches. Any changed costume color or different face is a major mismatch.",
        locks, reference_image=reference,
    )
    if (
        not isinstance(response, dict) or type(response.get("score")) is not int
        or not 0 <= response["score"] <= 100 or not isinstance(response.get("issues"), list)
    ):
        raise ValueError("Muse chưa trả kết quả kiểm tra màu và nhân vật hợp lệ.")
    issues = [comic_pipeline.text(issue, 180) for issue in response["issues"] if isinstance(issue, str) and issue.strip()]
    passed = response["score"] >= qc.VISUAL_QC_PASS_SCORE and not issues
    if not passed and not issues:
        issues = ["Mức độ khớp nhân vật hoặc bảng màu dưới ngưỡng duyệt."]
    return {"score": response["score"], "passed": passed, "issues": issues}


async def color_comic_panel(video, scene, work, progress):
    vid, idx = video["id"], scene["index"]
    source = asset_path(vid, scene["comic_panel"])
    locks = comic_palette(video, scene)
    wanted = set(scene.get("characters", []))
    if video.get("cast") and scene.get("dialogue"):
        wanted -= remote_speaker_names(video["cast"], scene)
    guides = []
    portraits = [person for person in video.get("cast", []) if person["name"] in wanted and batch.character_portrait(person)]
    if portraits:
        ref, order = await asyncio.to_thread(batch._cast_reference, portraits)
        if ref.startswith(("http://", "https://")):
            ref = await asyncio.to_thread(media._browser_image_from_url, ref)
        else:
            ref = batch._resolve_character_ref_data_uri(ref)
        if not ref:
            raise ValueError(f"Cảnh {idx}: không đọc được ảnh đã khóa của nhân vật.")
        contact = work / "cast-reference"
        contact.write_bytes(base64.b64decode(ref.split(",", 1)[1]))
        guides.append(contact)
        wanted -= {person["name"] for person in portraits}
    else:
        order = ""
    for prior in video["scenes"]:
        if prior["index"] >= idx or not wanted or len(guides) >= 3:
            break
        shared = wanted.intersection(prior.get("characters", []))
        if shared and prior.get("comic_panel") and prior.get("frame_approved") and prior.get("frame"):
            candidate = asset_path(vid, prior["frame"])
            if candidate.is_file():
                guides.append(candidate)
                wanted -= shared
    guide_instruction = (
        " Additional images to its right are identity/color guides only. Never copy their framing or action. "
        + order
        if guides else ""
    )
    color_instruction = (
        "Match all source colors visible in the original panel, including garment colors and background."
        if scene["comic_source_color"] else
        "Color the grayscale ink drawing using the locked character palette and a coherent setting palette."
    )
    cutaway = scene.get("muse_voice_turn") in REMOTE_DELIVERY
    closeup = scene.get("muse_voice_turn") == "on_screen"
    composition = (
        "The first image supplies the setting, art and colors. Use the new framing requested for this shot."
        if cutaway or closeup else "The first image is the target panel. Keep its composition, pose, expression, setting, and props."
    )
    prompt = (
        "Produce one clean full-color keyframe in the original manga's drawing style. "
        + composition + " "
        "Reconstruct artwork behind speech bubbles and remove all lettering, balloons and borders. "
        + guide_instruction + " " + color_instruction + "\n"
        + scene["prompt"] + "\n" + locks
    )
    reference = await asyncio.to_thread(comic_pipeline.load_reference, [source, *guides])
    _, url, key = batch.muse_connection(batch.active_muse_provider())
    generated = work / "colored"
    progress(idx, "Tạo khung màu và đối chiếu trang phục")
    await media.generate_keyframe_image(prompt, url, key, str(generated), video["aspect_ratio"], reference)
    frame = new_asset(vid, "webp")
    await asyncio.to_thread(image_to_webp, generated, frame)
    review_instruction = (
        "The last image is an intentional cutaway. Match the setting and base colors of the first panel. "
        "Reject visible mouths, stray lettering or mismatched character colors."
        if cutaway else
        "The last image is an intentional close-up. Match the named speaker's face, hair and clothing "
        "to the reference images. Reject additional visible mouths and stray lettering."
        if closeup else
        "Compare the last image to the first, target panel. Keep the target composition and characters. "
        "Any intervening images are previously approved character/color references. "
        "Check locked clothing colors, facial identity, missing characters, and remaining balloons or text."
    )
    review = await review_comic_image(
        [source, *guides, frame],
        review_instruction + " " + color_instruction,
        locks,
    )
    set_image(scene, frame.name)
    scene.update(frame_approved=review["passed"], visual_qc=review)
    batch.BATCH_MANAGER.update_video(vid, {"scenes": video["scenes"]})
    if not review["passed"]:
        raise ValueError(f"Cảnh {idx}: khung màu cần duyệt lại. " + " · ".join(review["issues"]))


async def render_comic(video, operation, work, progress):
    await analyze_comic_pages(video, progress)
    manager = batch.BATCH_MANAGER
    vid = video["id"]
    cancel = batch._BatchCancellation(vid)
    for scene in video["scenes"]:
        index = scene["index"]
        if cancel.is_set():
            raise asyncio.CancelledError()
        if scene.get("comic_panel") and not scene.get("frame_approved"):
            await color_comic_panel(video, scene, work, progress)
        if not scene.get("clip"):
            with tempfile.TemporaryDirectory(dir=directory(vid)) as temp:
                await produce(video, scene, "clip", Path(temp), progress)
            manager.update_video(vid, {"scenes": video["scenes"]})
        if (scene.get("qc") or {}).get("passed") is False and not scene.get("qc_accepted"):
            raise ValueError(f"Cảnh {index} cần xem lại màu, nhân vật hoặc lỗi kỹ thuật trước khi xuất.")
        scene["approved"] = True
        manager.update_video(vid, {"scenes": video["scenes"]})
    action = "compose" if video.get("clip_audio_mode") == "native" else "narrated"
    return await export(video, {**operation, "action": action}, work, progress)


async def produce_clip_segments(video, segments, workers, progress):
    """Chạy song song nhiều phân đoạn clip độc lập (mỗi phân đoạn chạy tuần tự để giữ Last Frame)."""
    sem = asyncio.Semaphore(max(1, workers))
    errors: list[str] = []
    vid = video["id"]

    async def _run_segment(seg_indexes):
        async with sem:
            for idx in seg_indexes:
                if batch._BatchCancellation(vid).is_set():
                    return
                scene = selected_scene(video, idx)
                try:
                    with tempfile.TemporaryDirectory(dir=directory(vid)) as tmp:
                        await produce(video, scene, "clip", Path(tmp), progress)
                except Exception as error:  # noqa: BLE001 — isolate segment errors
                    scene["error"] = str(error)
                    errors.append(f"Cảnh {idx}: {error}")
                    if video.get("chain_fallback_mode") == "stop":
                        break
                finally:
                    batch.BATCH_MANAGER.update_video(
                        vid, {**INVALID_FINAL, "scenes": video["scenes"]}
                    )

    await asyncio.gather(*(_run_segment(seg) for seg in segments))
    return errors


def recommended_clip_duration(scene: dict) -> int:
    """Tính thời lượng clip AI (5/6/8/10/12s) vừa đủ cho lời kể + thoại của cảnh để không lặp clip."""
    measured = float(scene.get("speech_duration") or 0.0)
    if measured > 0.3:
        needed = measured
    else:
        vo = (scene.get("voiceover") or "").strip()
        dlg = (scene.get("dialogue") or "").strip()
        spoken_parts = [vo] if vo else []
        if dlg:
            dlg_words = " ".join(
                (ln.partition(":")[2] if ":" in ln else ln).strip()
                for ln in dlg.splitlines()
                if ln.strip()
            ).strip()
            if dlg_words and dlg_words.casefold() not in vo.casefold():
                spoken_parts.append(dlg_words)
        words = len(" ".join(spoken_parts).split())
        needed = (words / 2.6 + 0.4) if words else float(scene.get("duration") or 5)
    for bucket in (5, 6, 8, 10, 12):
        if bucket >= needed - 0.01:
            return bucket
    return 12


def fit_scene_durations(vid: str, version: float) -> dict:
    """Tự nâng duration các cảnh chưa dựng clip lên mốc 5/6/8/10/12s phù hợp với thời lượng lời đọc."""
    with batch._QUEUE_LOCK:
        video = batch.BATCH_MANAGER.get_video(vid)
        if not video or not video.get("guided"):
            raise ValueError("Không tìm thấy bản dựng.")
        if video["status"] in BUSY or video.get("operation"):
            raise ValueError("Bản dựng đang xử lý. Hãy chờ xong trước khi điều chỉnh thời lượng.")
        if version != video.get("updated_at"):
            raise ValueError("Bản dựng đã thay đổi. Hãy tải lại trước khi điều chỉnh.")
        updated = 0
        for s in video["scenes"]:
            if s.get("clip"):
                continue
            cur = int(s.get("duration") or 5)
            rec = recommended_clip_duration(s)
            if rec > cur:
                s["duration"] = rec
                updated += 1
        if updated:
            batch.BATCH_MANAGER.update_video(
                vid, {**INVALID_FINAL, "scenes": video["scenes"]}
            )
        return {"updated": updated, "scenes": len(video["scenes"])}


def split_muse_voice_turns(vid: str, index: int, version: float) -> dict:
    with batch._QUEUE_LOCK:
        video = batch.BATCH_MANAGER.get_video(vid)
        if not video or not video.get("guided") or video.get("clip_audio_mode") != "native":
            raise ValueError("Chọn chế độ dùng giọng Muse để tách lượt thoại.")
        if video["status"] in BUSY or video.get("operation") or video.get("updated_at") != version:
            raise ValueError("Bản dựng đang chạy hoặc đã thay đổi. Hãy tải lại.")
        scene = selected_scene(video, index)
        cast = video.get("cast", [])
        narrator = video.get("muse_narrator")
        scene_voice_prompt(scene, cast, narrator)
        blocks = voice_turn_blocks(scene, cast, narrator)
        if len(blocks) < 2:
            raise ValueError("Cảnh này chỉ có tối đa một lượt thoại.")
        original_take = take(scene) if scene.get("clip") else scene.get("previous_take")
        shots = []
        for i, block in enumerate(blocks):
            delivery, speaker = block["delivery"], block["speaker"]
            voiceover = block["voiceover"] or (scene.get("voiceover", "") if not narrator and i == 0 else "")
            dialogue_text = " ".join(line.partition(":")[2] for line in block["dialogue"])
            words = len(f"{block['voiceover']} {dialogue_text}".split())
            shot = dict(
                scene, voiceover=voiceover, dialogue="\n".join(block["dialogue"]),
                title=f"{scene.get('title') or f'Cảnh {index}'} · Lượt {i + 1}",
                duration=max(5, min(60, math.ceil(words / 2.6 + 0.6))),
                new_segment=True, transition_out="cut",
                speech=None, speech_duration=None, actual_duration=None, error=None,
                muse_voice_turn=delivery,
            )
            if delivery in REMOTE_DELIVERY:
                device = (
                    f" A detail shot of the {REMOTE_DELIVERY[delivery]} is also suitable."
                    if delivery != "offscreen" else ""
                )
                shot["prompt"] += (
                    " Camera framing for this turn overrides earlier framing: cut away to the listener's "
                    f"hands or back. Keep every mouth outside the frame. No visible speaking.{device}"
                )
            elif delivery == "on_screen":
                shot["characters"] = [speaker]
                shot["prompt"] += (
                    f" Camera framing for this turn overrides earlier framing: close-up of {speaker} "
                    "speaking. Keep other people's faces outside the frame."
                )
            else:
                shot["prompt"] += " Show silent physical acting. All visible mouths stay closed for this turn."
            if delivery in REMOTE_DELIVERY or delivery == "on_screen":
                shot["turn_reference"] = scene.get("frame") or scene.get("turn_reference")
                set_image(shot, None)
            else:
                invalidate_clip(shot)
            if i:
                shot.pop("previous_take", None)
                shot.pop("comic_lines", None)
            elif original_take:
                shot["previous_take"] = {**original_take, "before_voice_split": True}
            shot["direction"] = shot["prompt"]
            shots.append(shot)
        shots[-1]["transition_out"] = scene.get("transition_out") or "cut"
        spare = max(0, int(scene["duration"]) - sum(shot["duration"] for shot in shots))
        shots[-1]["duration"] += spare
        scenes = []
        for item in video["scenes"]:
            scenes.extend(shots if item["index"] == index else [dict(item)])
        for number, item in enumerate(scenes, 1):
            item["index"] = number
        refresh_joins(scenes)
        batch.BATCH_MANAGER.update_video(
            vid, {**INVALID_FINAL, "scenes": scenes, "scenes_count": len(scenes), "status": "review"}
        )
        return {"turns": len(shots), "scenes": len(scenes)}


def insert_bridge_shots(vid: str, version: float) -> dict:
    """Tự động chèn các cảnh cầu nối 4s vào những vị trí nhảy thời gian/địa điểm."""
    with batch._QUEUE_LOCK:
        video = batch.BATCH_MANAGER.get_video(vid)
        if not video or not video.get("guided"):
            raise ValueError("Không tìm thấy bản dựng.")
        if video["status"] in BUSY or video.get("operation"):
            raise ValueError("Bản dựng đang xử lý. Hãy chờ hoàn tất trước khi chèn cảnh cầu nối.")
        if version != video.get("updated_at"):
            raise ValueError("Bản dựng đã thay đổi. Hãy tải lại trước khi chèn cảnh.")
        updated_scenes, inserted = build_bridge_scenes_for_jumps(video)
        if inserted == 0:
            return {"inserted": 0, "scenes": len(updated_scenes)}
        refresh_joins(updated_scenes)
        warnings = [
            c["message"] + " " + c.get("suggestion", "")
            for c in run_consistency_checks({
                "scenes": updated_scenes,
                "characters": video.get("cast") or [],
                "chain_mode": "sequential",
                "voice": video.get("voice"),
            })["checks"]
            if c["level"] == "warning"
        ]
        batch.BATCH_MANAGER.update_video(
            vid,
            {
                **INVALID_FINAL,
                "scenes": updated_scenes,
                "scenes_count": len(updated_scenes),
                "continuity_warnings": list(dict.fromkeys(warnings)),
            },
        )
        return {"inserted": inserted, "scenes": len(updated_scenes)}


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
        if op["action"] == "comic":
            if op.get("active"):
                raise ValueError("Lượt dựng bị gián đoạn. Bấm Tiếp tục dựng truyện tranh.")
            if batch.active_muse_provider() != "browser":
                raise ValueError("Chọn Muse Browser để dựng chương truyện tranh.")
            op["active"] = ["comic"]
            manager.update_video(vid, {"operation": op})
        if op["action"] in ("compose", "narrated", "comic"):
            with tempfile.TemporaryDirectory(dir=directory(vid)) as tmp:
                render = render_comic if op["action"] == "comic" else export
                report = asyncio.run(render(video, op, Path(tmp), progress))
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
                    "srt_url": f"/api/batch-videos/{vid}/subtitles" if op.get("include_subtitles", True) else None,
                    "include_subtitles": op.get("include_subtitles", True),
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
        clip_mode = op.get("clip_parallel_mode", video.get("clip_parallel_mode", "sequential"))
        if (
            op["action"] == "clip"
            and op["pending"]
            and clip_mode in ("segment_parallel", "full_parallel")
        ):
            clip_workers = asyncio.run(image_workers(op.get("max_concurrent", 12)))
            if clip_mode == "full_parallel":
                segments = [[idx] for idx in op["pending"]]
                step_label = f"Đang dựng song song toàn bộ {len(segments)} cảnh ({clip_workers} luồng · bỏ nối đuôi)"
            else:
                segments = partition_clip_segments(
                    video["scenes"],
                    op["pending"],
                    reference_mode=video.get("reference_mode", "chain"),
                )
                step_label = f"Đang dựng song song {len(segments)} phân đoạn ({clip_workers} luồng)"
            if clip_workers > 1 and len(segments) > 1:
                op["active"] = list(op["pending"])
                if not manager.update_video(
                    vid,
                    {
                        "operation": op,
                        "current_step": step_label,
                    },
                ):
                    return
                op["errors"].extend(
                    asyncio.run(
                        produce_clip_segments(video, segments, clip_workers, progress)
                    )
                )
                op["pending"] = []
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
        chunk_size = 6 if op["action"] == "write" else 1
        if op["action"] == "image":
            chunk_size = asyncio.run(image_workers(op.get("max_concurrent", 12)))
        while op["pending"]:
            if batch._BatchCancellation(vid).is_set():
                return
            indexes = op["pending"][:chunk_size]
            op["active"] = indexes
            if not manager.update_video(
                vid,
                {
                    "operation": op,
                    "current_step": "Đang xử lý cảnh " + ", ".join(map(str, indexes)),
                },
            ):
                return
            try:
                if op["action"] == "image":
                    op["errors"].extend(
                        asyncio.run(produce_images(video, indexes, progress))
                    )
                elif op["action"] == "write":
                    asyncio.run(write_beats(video, indexes))
                else:
                    with tempfile.TemporaryDirectory(dir=directory(vid)) as tmp:
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
