"""Editor operations, creative briefs and reference libraries for reviewed Muse productions."""

import asyncio
import json
import math
import os
import threading
import uuid
import zipfile
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import batch_engine as media
import batch_video_engine as batch
import composer
import story_engine
import studio_workflow as wf
from studio_styles import GROUPS

router = APIRouter()
_storage_lock = threading.RLock()


def store():
    path = batch.BATCH_DIR.parent / "_studio_editor"
    path.mkdir(exist_ok=True)
    return path


def write_json(path, value):
    tmp = path.with_name(uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class Command(BaseModel):
    action: Literal[
        "image",
        "clip",
        "speech",
        "compose",
        "comic",
        "narrated",
        "write",
        "emphasis",
        "portraits",
        "vision_qc",
        "approve_image",
        "approve_clip",
        "replace",
        "restore",
        "discard_previous",
        "edit",
    ]
    indexes: list[int] = Field(default_factory=list, max_length=200)
    version: float
    approved: bool = True
    allow_stills: bool = False
    include_subtitles: bool = True
    max_concurrent: int = Field(default=12, ge=1, le=12)
    clip_parallel_mode: Literal["sequential", "segment_parallel"] | None = None
    accept_qc: bool = False
    characters: list[str] = Field(default_factory=list, max_length=30)
    dialogue: str = Field(default="", max_length=3000)
    setting: str = Field(default="", max_length=500)
    new_segment: bool = False
    transition_out: Literal["dissolve", "cut", "match cut"] = "dissolve"
    upload: str = Field(default="", max_length=300)
    prompt: str = Field(default="", max_length=5000)
    voiceover: str = Field(default="", max_length=3000)
    voice: str = Field(default="", max_length=100)
    duration: int = Field(default=8, ge=1, le=60)


@router.post("/api/muse-studio/{vid}/actions")
def action(vid: str, req: Command):
    try:
        wf.command(vid, req)
    except (OSError, ValueError) as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@router.get("/api/muse-studio/{vid}/scenes/{index}/media/{kind}")
def scene_media(
    vid: str,
    index: int,
    kind: Literal["frame", "speech", "clip", "previous_clip", "last_frame"],
):
    video = batch.BATCH_MANAGER.get_video(vid)
    try:
        sc = wf.selected_scene(video or {"scenes": []}, index)
        path = wf.asset_path(
            vid,
            (sc.get("previous_take") or {}).get("clip")
            if kind == "previous_clip"
            else sc.get(kind),
        )
        if not path.is_file():
            raise ValueError()
    except ValueError:
        raise HTTPException(404, "Tệp chưa sẵn sàng.")
    return FileResponse(
        path,
        media_type={
            "frame": "image/webp",
            "clip": "video/mp4",
            "previous_clip": "video/mp4",
            "last_frame": "image/jpeg",
            "speech": "audio/mpeg",
        }[kind],
    )


@router.get("/api/muse-studio/{vid}/scenes/{index}/handoff")
def scene_handoff(vid: str, index: int, background: BackgroundTasks):
    import tempfile

    video = batch.BATCH_MANAGER.get_video(vid)
    if not video or not video.get("guided"):
        raise HTTPException(404, "Không tìm thấy bản dựng.")
    try:
        scene = wf.selected_scene(video, index)
    except ValueError as error:
        raise HTTPException(404, str(error)) from error
    if not scene.get("frame_approved"):
        raise HTTPException(409, "Hãy duyệt ảnh của cảnh trước khi giao.")
    if scene.get("clip"):
        first, source = scene.get("input_frame"), scene.get("reference_source")
    else:
        try:
            first, source = wf.studio_opening(video, scene)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
    metadata = {
        k: scene.get(k)
        for k in (
            "index",
            "title",
            "duration",
            "setting",
            "prompt",
            "used_prompt",
            "voiceover",
            "dialogue",
            "transition_out",
            "reference_source",
            "qc",
        )
    }
    metadata.update(
        reference_source=source,
        aspect_ratio=video["aspect_ratio"],
        fps=30,
        style=batch.BATCH_VISUAL_STYLES[video["visual_style"]]["prompt_suffix"],
        characters=[
            {
                "name": c["name"],
                "block": media.character_block_of(c),
                "voice": c.get("voice"),
            }
            for c in wf.references(video, scene)
        ],
        handoff="Match pose, props, light and screen direction over the last two and first two seconds of adjacent shots.",
    )
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as temp:
        path = Path(temp.name)
    try:
        with zipfile.ZipFile(path, "w") as archive:
            for label, token in (
                ("first_frame", first),
                ("end_frame", scene.get("last_frame")),
                ("speech", scene.get("speech")),
            ):
                if token:
                    source = wf.asset_path(vid, token)
                    filename = label + source.suffix
                    archive.write(source, filename)
                    metadata[label] = filename
            archive.writestr(
                "shot.json", json.dumps(metadata, ensure_ascii=False, indent=2)
            )
    except Exception:
        path.unlink(missing_ok=True)
        raise
    background.add_task(path.unlink, missing_ok=True)
    return FileResponse(path, filename=f"canh-{index}.zip", background=background)


@router.get("/api/muse-studio/catalog")
def catalog():
    return {
        "groups": [
            {
                "name": name,
                "styles": [
                    {"id": key, "name": label, "description": desc}
                    for key, label, desc, _ in rows
                ],
            }
            for name, rows in GROUPS
        ]
    }


class Skill(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=40000)


@router.post("/api/muse-studio/skills")
def save_skill(req: Skill):
    if not req.name.strip() or not req.text.strip():
        raise HTTPException(422, "Điền tên và nội dung skill.")
    key = uuid.uuid4().hex
    write_json(
        store() / f"skill_{key}.json",
        {"id": key, "name": req.name.strip(), "text": req.text.strip()},
    )
    return {"id": key, "name": req.name.strip()}


@router.post("/api/muse-studio/skills/import")
async def import_skill(file: Annotated[UploadFile, File()]):
    try:
        data = await file.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError("Tệp skill tối đa 8 MB.")
        filename = file.filename or "skill.txt"
        text = await asyncio.to_thread(wf.skill_text, data, filename)
        return save_skill(Skill(name=Path(filename).stem[:120], text=text))
    except (ValueError, OSError, RuntimeError, zipfile.BadZipFile) as e:
        raise HTTPException(422, str(e)) from e


@router.get("/api/muse-studio/skills")
def list_skills():
    rows = []
    for p in store().glob("skill_*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("id") and data.get("name"):
                rows.append({"id": data["id"], "name": data["name"]})
        except (OSError, ValueError, TypeError):
            continue
    return sorted(rows, key=lambda r: r["name"].casefold())


class Reference(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["character", "setting", "prop"] = "character"
    description: str = Field(default="", max_length=1500)
    avatar: str = Field(default="", max_length=300)
    voice: str = Field(default="", max_length=100)
    aliases: list[str] = Field(default_factory=list, max_length=8)


def ref_file(pid):
    if not story_engine.load_project(pid):
        raise HTTPException(404, "Không tìm thấy truyện.")
    return store() / f"refs_{pid}.json"


def library(pid):
    p = ref_file(pid)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def validated_ref(req):
    if not req.name.strip():
        raise HTTPException(422, "Nhập tên tham chiếu.")
    if req.avatar and (
        not req.avatar.startswith("/api/uploads/")
        or not media._resolve_safe_reference(req.avatar)
    ):
        raise HTTPException(422, "Cần chọn ảnh đã tải lên Studio.")
    return {**req.model_dump(), "name": req.name.strip()}


@router.get("/api/story/projects/{pid}/references")
def refs(pid: str):
    return library(pid)


@router.put("/api/story/projects/{pid}/references")
def save_reference(pid: str, req: Reference):
    entry = validated_ref(req)
    with _storage_lock:
        refs = library(pid)
        old = next(
            (
                r
                for r in refs
                if r["name"].casefold() == entry["name"].casefold()
                and r["kind"] == entry["kind"]
            ),
            None,
        )
        if old:
            old.update(entry)
        else:
            entry["id"] = uuid.uuid4().hex
            refs.append(entry)
        write_json(ref_file(pid), refs)
    return refs


@router.post("/api/story/projects/{pid}/references/extract")
async def extract_refs(pid: str):
    project = story_engine.load_project(pid)
    if not project:
        raise HTTPException(404, "Không tìm thấy truyện.")
    chapters = project.get("chapters") or []
    sample = "\n".join(
        ch["text"][: max(600, 25000 // max(1, len(chapters)))] for ch in chapters
    )[:30000]
    try:
        rows = await wf.ask_json(
            "Extract recurring characters, settings and props directly from the story. "
            "Return only a JSON array of at most 30 objects with kind (character/setting/prop), "
            "name and a concise consistent Vietnamese visual description.",
            sample,
        )
        suggestions = [Reference.model_validate(x) for x in rows[:30]]
    except Exception as e:
        raise HTTPException(502, f"Muse chưa trả thư viện hợp lệ: {e}") from e
    with _storage_lock:
        existing = library(pid)
        known = {(r["name"].casefold(), r["kind"]) for r in existing}
        for ref in suggestions:
            k = (ref.name.casefold(), ref.kind)
            if k not in known:
                existing.append(
                    {**ref.model_dump(), "avatar": "", "id": uuid.uuid4().hex}
                )
                known.add(k)
        write_json(ref_file(pid), existing)
    return existing


class Workshop(BaseModel):
    topic: str = Field(min_length=1, max_length=1500)
    minutes: int = Field(default=8, ge=1, le=20)
    seconds: Literal[6, 8, 10, 12] = 8
    skill_id: str = Field(default="", pattern=r"^(?:[a-f0-9]{32})?$")
    style: str = "cinematic"
    ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    cast_ids: list[str] = Field(default_factory=list, max_length=6)
    portraits: list[Reference] = Field(default_factory=list, max_length=6)
    voice: str = Field(default="vi-VN-HoaiMyNeural", max_length=100)
    subtitle_style: Literal[
        "cinema", "headline", "boxed", "karaoke", "word_box", "neon"
    ] = "cinema"
    subtitle_position: Literal["bottom", "middle", "top"] = "bottom"


class AssistantBrief(Workshop):
    count: int = Field(default=5, ge=1, le=12)


def create_workshop(req, cast, count=None):
    if req.style not in batch.BATCH_VISUAL_STYLES or not req.topic.strip():
        raise HTTPException(422, "Chủ đề hoặc phong cách chưa hợp lệ.")
    spec = {"topic": req.topic.strip(), "minutes": req.minutes}
    if req.skill_id:
        path = store() / f"skill_{req.skill_id}.json"
        if not path.is_file():
            raise HTTPException(404, "Skill không còn tồn tại.")
        spec.update(json.loads(path.read_text(encoding="utf-8")))
    cast = [*cast, *(validated_ref(r) for r in req.portraits)]
    if len(cast) > 6:
        raise HTTPException(422, "Chọn tối đa sáu nhân vật khoá cứng.")
    n = count if count is not None else math.ceil(req.minutes * 60 / req.seconds)
    scenes = [
        {"index": i, "duration": req.seconds, "prompt": "", "voiceover": ""}
        for i in range(1, n + 1)
    ]
    items = batch.BATCH_MANAGER.add_batch(
        [req.topic.strip()[:160]],
        scenes_count=n,
        visual_style=req.style,
        voice=req.voice,
        aspect_ratio=req.ratio,
        auto_run=False,
        scripts=[{"scenes": scenes}],
        extra={
            "guided": True,
            "render_mode": "ai_video",
            "studio_source": "muse_studio",
            "workshop": spec,
            "cast": cast,
            "subtitle_style": req.subtitle_style,
            "subtitle_position": req.subtitle_position,
        },
    )
    vid = items[0]["id"]
    wf.command(vid, Command(action="write", version=items[0]["updated_at"]))
    return {"id": vid, "scenes": n}


class ProjectSettings(BaseModel):
    voice: str = Field(min_length=1, max_length=100)
    visual_style: str
    aspect_ratio: Literal["9:16", "16:9", "1:1"]
    scenes_count: int = Field(ge=3, le=8)
    render_mode: Literal["ai_video", "illustrated"]
    subtitle_style: Literal[
        "cinema", "headline", "boxed", "karaoke", "word_box", "neon"
    ]
    subtitle_position: Literal["bottom", "middle", "top"]
    cast_ids: list[str] = Field(default_factory=list, max_length=6)


@router.put("/api/story/projects/{pid}/render-settings")
def save_project_settings(pid: str, req: ProjectSettings):
    if req.visual_style not in batch.BATCH_VISUAL_STYLES:
        raise HTTPException(422, "Phong cách không hợp lệ.")
    with _storage_lock:
        project = story_engine.load_project(pid)
        if not project:
            raise HTTPException(404, "Không tìm thấy truyện.")
        project["render_settings"] = req.model_dump()
        story_engine.save_project(project)
    return project["render_settings"]


class ChapterSettings(BaseModel):
    version: float
    voice: str = Field(min_length=1, max_length=100)
    subtitle_style: Literal[
        "cinema", "headline", "boxed", "karaoke", "word_box", "neon"
    ]
    subtitle_position: Literal["bottom", "middle", "top"]
    music: str = Field(default="", max_length=200)
    volume: float = Field(default=0.15, ge=0, le=1)
    reference_mode: Literal["chain", "approved"] = "chain"
    chain_fallback_mode: Literal["fallback_image", "stop", "independent"] = (
        "fallback_image"
    )
    color_grade: Literal[
        "none",
        "cinema_teal_orange",
        "warm_epic",
        "vibrant_3d",
        "moody_noir",
        "soft_film",
    ] = "none"
    smart_pacing: bool = True
    smart_trim: bool = False
    auto_ducking: bool = True
    transition_sfx: bool = False
    vision_qc: bool = False
    clip_parallel_mode: Literal["sequential", "segment_parallel"] = "sequential"
    watermark_text: str = Field(default="", max_length=40)


class BridgeRequest(BaseModel):
    version: float


@router.post("/api/muse-studio/{vid}/bridge-shots")
def bridge_shots(vid: str, req: BridgeRequest):
    try:
        return wf.insert_bridge_shots(vid, req.version)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.get("/api/muse-studio/{vid}/thumbnail")
def smart_thumbnail(vid: str):
    video = batch.BATCH_MANAGER.get_video(vid)
    if not video:
        raise HTTPException(404, "Không tìm thấy bản dựng.")
    d = wf.directory(vid)
    thumb = d / "thumbnail.jpg"
    final_mp4 = d / "final.mp4"
    if not thumb.is_file():
        if not final_mp4.is_file():
            raise HTTPException(404, "Thumbnail chỉ sẵn sàng sau khi dựng phim hoàn chỉnh.")
        try:
            composer.generate_smart_thumbnail(
                str(final_mp4),
                str(thumb),
                title=video.get("title") or "",
                aspect_ratio=video.get("aspect_ratio") or "9:16",
            )
        except Exception as e:
            raise HTTPException(500, f"Không thể tạo thumbnail: {e}") from e
    return FileResponse(thumb, media_type="image/jpeg")


@router.put("/api/muse-studio/{vid}/chapter-settings")
def chapter_settings(vid: str, req: ChapterSettings):
    if req.music and (Path(req.music).name != req.music or not music_file(req.music)):
        raise HTTPException(422, "Nhạc nền chưa có trong thư viện.")
    with batch._QUEUE_LOCK:
        video = batch.BATCH_MANAGER.get_video(vid)
        if (
            not video
            or not video.get("guided")
            or video.get("operation")
            or video["updated_at"] != req.version
        ):
            raise HTTPException(409, "Bản dựng đang chạy hoặc đã thay đổi.")
        if video.get("voice") != req.voice:
            for s in video["scenes"]:
                if not s.get("voice"):
                    s["speech"] = None
        batch.BATCH_MANAGER.update_video(
            vid,
            {
                **req.model_dump(exclude={"version"}),
                **wf.INVALID_FINAL,
                "status": "review",
                "scenes": video["scenes"],
            },
        )
    return {"ok": True}


def music_file(filename):
    root = (batch.BATCH_DIR.parent / "_music").resolve()
    p = (root / filename).resolve()
    if (
        p.parent == root
        and p.is_file()
        and p.suffix.lower() in {".mp3", ".wav", ".m4a", ".aac", ".ogg"}
    ):
        return p
    return None


@router.post("/api/muse-studio/{vid}/rebuild")
def rebuild(vid: str):
    v = batch.BATCH_MANAGER.get_video(vid)
    if not v or not (v.get("story_project_id") or v.get("studio_source")):
        raise HTTPException(404, "Không tìm thấy bản dựng.")
    if not all(s.get("prompt") for s in v["scenes"]):
        raise HTTPException(409, "Viết xong kịch bản trước khi dựng lại.")
    scenes = [
        {
            k: s[k]
            for k in (
                "index",
                "title",
                "prompt",
                "direction",
                "characters",
                "dialogue",
                "setting",
                "new_segment",
                "transition_out",
                "duration",
                "voiceover",
                "voice",
            )
            if k in s
        }
        for s in v["scenes"]
    ]
    meta = {
        k: v[k]
        for k in (
            "story_source",
            "reference_mode",
            "chain_fallback_mode",
            "story_project_id",
            "chapter_index",
            "studio_source",
            "cast",
            "subtitle_style",
            "subtitle_position",
            "music",
            "volume",
            "workshop",
            "color_grade",
            "smart_pacing",
            "smart_trim",
            "auto_ducking",
            "transition_sfx",
            "vision_qc",
            "clip_parallel_mode",
            "watermark_text",
        )
        if k in v
    }
    item = batch.BATCH_MANAGER.add_batch(
        [v["title"]],
        aspect_ratio=v["aspect_ratio"],
        visual_style=v["visual_style"],
        voice=v["voice"],
        scenes_count=len(scenes),
        scripts=[{"scenes": scenes}],
        auto_run=False,
        extra={**meta, "guided": True, "render_mode": "ai_video"},
    )[0]
    return {"id": item["id"]}


@router.post("/api/story/projects/{pid}/references/{rid}/draw")
async def draw_ref(pid: str, rid: str):
    entry = next((r for r in library(pid) if r["id"] == rid), None)
    if not entry:
        raise HTTPException(404, "Không có mục tham chiếu.")
    project = story_engine.load_project(pid)
    root = batch.BATCH_DIR.parent / "_uploads"
    root.mkdir(exist_ok=True)
    dest = root / (uuid.uuid4().hex + ".webp")
    _, base, key = batch.muse_connection(batch.active_muse_provider())
    try:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "ref"
            style_suffix = batch.BATCH_VISUAL_STYLES["cinematic"]["prompt_suffix"]
            primary_prompt, fallback_prompt = wf.character_design_prompts(
                entry, style_suffix
            )
            try:
                await media.generate_keyframe_image(
                    primary_prompt, base, key, str(p), "1:1"
                )
            except (RuntimeError, TimeoutError, ValueError, OSError):
                await media.generate_keyframe_image(
                    fallback_prompt, base, key, str(p), "1:1"
                )
            await asyncio.to_thread(wf.image_to_webp, p, dest)
        with _storage_lock:
            rows = library(pid)
            current = next((r for r in rows if r["id"] == rid), None)
            if current != entry:
                dest.unlink(missing_ok=True)
                raise HTTPException(409, "Mục tham chiếu đã thay đổi trong lúc vẽ.")
            current["avatar"] = "/api/uploads/" + dest.name
            write_json(ref_file(pid), rows)
        return rows
    except (OSError, RuntimeError, ValueError) as e:
        raise HTTPException(502, str(e)) from e
