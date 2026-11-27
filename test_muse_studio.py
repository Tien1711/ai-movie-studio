"""Exercise the studio's motion, review and subtitle paths without using a Muse account."""

import asyncio
import base64
import io
import json
import shutil
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import affiliate_engine
import app
import batch_engine
import batch_video_engine as batch
import composer


def test_product_instructions_in_outgoing_prompt():
    lock = affiliate_engine.build_product_dna_lock("Test product")
    prompt = (
        "Slow dolly toward the label. " + lock + " " + affiliate_engine.FIDELITY_BLOCK
    )
    shot = batch_engine.build_scene_prompt(
        {
            "prompt": prompt,
            "voiceover": "Lời kể ngoài hình.",
            "offscreen_narration": True,
        }
    )
    assert lock in shot["prompt"] and affiliate_engine.FIDELITY_BLOCK in shot["prompt"]
    assert "talking with expressive" not in shot["prompt"]


def test_every_affiliate_shot_receives_product_photo(tmp_path, monkeypatch):
    async def prefetch(*args, **kwargs):
        return {}

    references = []

    async def render(client, scene, *args, **kwargs):
        references.append(scene["reference_image"])
        return {
            "index": scene["index"],
            "status": "done",
            "last_frame_path": "generated-frame.jpg",
        }

    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"status": "ok"})
    )
    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: client_class(transport=transport, **kw)
    )
    monkeypatch.setattr(batch_engine, "prefetch_scene", prefetch)
    monkeypatch.setattr(batch_engine, "process_scene", render)
    scenes = [
        {"index": i, "reference_image": "original-product.jpg", "keep_reference": True}
        for i in range(1, 5)
    ]
    asyncio.run(
        batch_engine.run_batch(
            scenes,
            "https://muse.test",
            "fixture",
            str(tmp_path),
            chain_mode="sequential",
        )
    )
    assert references == ["original-product.jpg"] * 4


def test_ensemble_reference_contains_both_portraits_in_named_order():
    portraits = []
    for name, color in [("Mai", "red"), ("An", "blue")]:
        img = io.BytesIO()
        Image.new("RGB", (64, 64), color).save(img, "PNG")
        portraits.append(
            {
                "name": name,
                "avatar": "data:image/png;base64,"
                + base64.b64encode(img.getvalue()).decode(),
            }
        )
    uri, prompt = batch._cast_reference(portraits)
    with Image.open(io.BytesIO(base64.b64decode(uri.split(",")[1]))) as sheet:
        assert sheet.getpixel((160, 160))[0] > 220
        assert sheet.getpixel((480, 160))[2] > 220
    assert "Mai, An" in prompt


@pytest.mark.parametrize("guided", [False, True])
def test_muse_draft_motion_and_review(tmp_path, monkeypatch, guided):
    root = tmp_path / "workspace"
    store = root / "_batch_videos"
    store.mkdir(parents=True)
    monkeypatch.setattr(batch, "BATCH_DIR", store)
    monkeypatch.setattr(app, "BATCH_DIR", store)
    monkeypatch.setattr(batch, "QUEUE_FILE", store / "queue.json")
    monkeypatch.setattr(batch, "QUEUE_BAK_FILE", store / "backup.json")
    monkeypatch.setattr(batch_engine, "_WORKSPACE_ROOT", root)
    monkeypatch.setattr(batch, "start_batch_worker", lambda: None)
    monkeypatch.setattr(
        batch, "muse_connection", lambda _: ("browser", "https://muse.test", "test-key")
    )
    reference = "data:image/png;base64,iVBORw0KGgo="
    monkeypatch.setattr(
        app,
        "load_characters",
        lambda: [
            {
                "id": "mai",
                "name": "Mai",
                "description": "A green coat and a black braid",
                "avatar": reference,
            }
        ],
    )
    flags = ["-hide_banner", "-loglevel", "error", "-y"]
    src = tmp_path / "source.mp4"
    sound = tmp_path / "speech.mp3"
    subprocess.run(
        [
            composer.FFMPEG,
            *flags,
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x240:r=30:d=5",
            "-c:v",
            "libx264",
            str(src),
        ],
        check=True,
    )
    subprocess.run(
        [composer.FFMPEG, *flags, "-f", "lavfi", "-i", "sine=duration=1", str(sound)],
        check=True,
    )
    sent = []

    png = io.BytesIO()
    Image.new("RGB", (100, 100), "gold").save(png, "PNG")

    def provider(request):
        if request.url.path == "/v1/images/tasks":
            return httpx.Response(200, json={"data": [{"url": "/frame.png"}]})
        if request.url.path == "/frame.png":
            return httpx.Response(
                200, content=png.getvalue(), headers={"content-type": "image/png"}
            )
        if request.url.path == "/v1/videos" and request.method == "POST":
            sent.append(json.loads(request.content))
            return httpx.Response(202, json={"id": "fixture"})
        if request.url.path == "/v1/videos/fixture":
            return httpx.Response(
                200, json={"status": "done", "video_url": "https://muse.test/media.mp4"}
            )
        assert request.url.path == "/media.mp4"
        return httpx.Response(
            200, content=src.read_bytes(), headers={"content-type": "video/mp4"}
        )

    async def tts(text, voice, path):
        shutil.copy2(sound, path)
        return 1.0, False, ""

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client_class(transport=httpx.MockTransport(provider), **kw),
    )
    monkeypatch.setattr(batch_engine, "generate_tts_with_status", tts)
    monkeypatch.setattr(
        batch,
        "_generate_scene_image",
        lambda **kw: (_ for _ in ()).throw(AssertionError("slideshow")),
    )
    client = TestClient(app.app)
    draft = {
        "guided": guided,
        "title": "Test studio",
        "aspect_ratio": "16:9",
        "cast_ids": ["mai"],
        "subtitle_style": "karaoke",
        "subtitle_position": "top",
        "scenes": [
            {"prompt": "Mai opens the door", "voiceover": "Mai mở cửa.", "duration": 5, "setting": "Hall, morning"},
            {
                "prompt": "Camera follows the footsteps",
                "voiceover": "Bước chân tiến gần.",
                "setting": "Hall, morning",
                "duration": 5,
            },
        ],
    }
    result = client.post("/api/muse-studio/drafts", json=draft)
    assert result.status_code == 200, result.text
    vid = result.json()["id"]
    assert not sent and batch.BATCH_MANAGER.get_video(vid)["status"] == "draft"
    draft["title"] = "Phim đã biên tập"
    assert client.put(f"/api/muse-studio/drafts/{vid}", json=draft).is_success
    if guided:

        def act(action, indexes, **extra):
            item = batch.BATCH_MANAGER.get_video(vid)
            return client.post(
                f"/api/muse-studio/{vid}/actions",
                json={
                    "action": action,
                    "indexes": indexes,
                    "version": item["updated_at"],
                    **extra,
                },
            )

        assert act("clip", [1]).status_code == 409
        assert act("image", [1, 2]).is_success
        batch._process_single_video(vid)
        assert act("approve_image", [1, 2]).is_success
        assert act("clip", [1, 2]).is_success
        batch._process_single_video(vid)
        assert len(sent) == 2
        for idx, sent_shot in enumerate(sent, 1):
            source = "1/media/frame" if idx == 1 else "1/media/last_frame"
            photo = client.get(f"/api/muse-studio/{vid}/scenes/{source}").content
            assert base64.b64decode(sent_shot["reference_image"].split(",")[1]) == photo
        import zipfile

        bundle = client.get(f"/api/muse-studio/{vid}/scenes/2/handoff")
        assert bundle.is_success
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as z:
            handoff = json.loads(z.read("shot.json"))
            assert (
                handoff["reference_source"] == "last_frame"
                and z.read(handoff["first_frame"]) == photo
            )
        assert act("narrated", []).status_code == 409
        assert act("approve_clip", [1, 2]).is_success
        assert act("narrated", []).is_success
        batch._process_single_video(vid)
        item = batch.BATCH_MANAGER.get_video(vid)
        assert item["status"] == "completed", item["error"]
        assert (
            item["inspection"]["width"] == 1920
            and item["inspection"]["lufs"] is not None
        )
        assert client.get(f"/api/batch-videos/{vid}/video").content[4:8] == b"ftyp"
        assert "Bước chân" in client.get(f"/api/batch-videos/{vid}/subtitles").text
        assert act("compose", [], include_subtitles=False).is_success
        batch._process_single_video(vid)
        clean = batch.BATCH_MANAGER.get_video(vid)
        assert clean["status"] == "completed", clean["error"]
        assert clean["include_subtitles"] is False and clean["srt_url"] is None
        assert not (store / vid / "subtitles.srt").exists()
        assert act("approve_image", [1], approved=False).is_success
        assert not batch.BATCH_MANAGER.get_video(vid)["scenes"][0]["clip"]
        assert batch.BATCH_MANAGER.get_video(vid)["scenes"][1]["stale_join"]
        assert not act("approve_clip", [2]).is_success
        assert client.get(f"/api/batch-videos/{vid}/video").status_code == 409
        assert client.get(
            f"/api/muse-studio/{vid}/scenes/1/media/previous_clip"
        ).is_success
        assert act("restore", [1]).is_success
        restored = batch.BATCH_MANAGER.get_video(vid)["scenes"][0]
        assert (
            restored["clip"] and restored["frame_approved"] and not restored["approved"]
        )
        return
    assert client.post(f"/api/batch-videos/{vid}/run").is_success
    batch._process_single_video(vid)
    item = batch.BATCH_MANAGER.get_video(vid)
    assert item["status"] == "completed", item["error"]
    assert item["title"] == draft["title"]
    assert client.put(f"/api/muse-studio/drafts/{vid}", json=draft).status_code == 409
    assert len(sent) == 2 and sent[0]["reference_image"] == reference
    assert sent[1]["reference_image"].startswith("data:image/jpeg;")
    assert "A green coat and a black braid" in sent[0]["prompt"]
    assert all(s["video_status"] == "done" for s in item["scenes"])
    assert client.get(f"/api/muse-studio/{vid}/scenes/1/clip").is_success
    assert client.get(f"/api/batch-videos/{vid}/video").content[4:8] == b"ftyp"
    assert "Bước chân" in client.get(f"/api/batch-videos/{vid}/subtitles").text
    assert client.put(
        f"/api/muse-studio/{vid}/scenes/2/review", json={"approved": True}
    ).is_success
    assert batch.BATCH_MANAGER.get_video(vid)["scenes"][1]["approved"]


def test_workshop_resumes_script_and_locks_cast(tmp_path, monkeypatch):
    import zipfile

    import studio_workflow as workflow

    store = tmp_path / "_batch_videos"
    store.mkdir()
    for name, path in (
        ("BATCH_DIR", store),
        ("QUEUE_FILE", store / "queue.json"),
        ("QUEUE_BAK_FILE", store / "backup.json"),
    ):
        monkeypatch.setattr(batch, name, path)
    monkeypatch.setattr(batch, "start_batch_worker", lambda: None)
    client = TestClient(app.app)
    catalog = client.get("/api/muse-studio/catalog").json()
    assert [len(g["styles"]) for g in catalog["groups"]] == [5, 6, 5, 4]

    brief = "Giữ hướng nhìn và trang phục của Mai xuyên suốt mọi cảnh trong phim."

    def archive(name, text):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as target:
            target.writestr(name, text)
        return data.getvalue()

    for filename, body in [
        ("brief.txt", brief.encode()),
        ("brief.md", brief.encode()),
        (
            "brief.docx",
            archive("word/document.xml", "<w:p><w:t>" + brief + "</w:t></w:p>"),
        ),
        ("brief.zip", archive("example/SKILL.md", brief)),
    ]:
        resp = client.post(
            "/api/muse-studio/skills/import", files={"file": (filename, body)}
        )
        assert resp.is_success, resp.text
    skill_id = resp.json()["id"]

    groups = []

    async def writer(system, user):
        payload = json.loads(user)
        assert payload["skill"] == brief
        indexes = payload["requested_scene_numbers"]
        groups.append(indexes)
        if len(groups) == 2:
            raise ValueError("Lỗi giữa chừng")
        rows = [
            {
                "index": i,
                "title": "Cảnh " + str(i),
                "prompt": "Mai mở cửa.",
                "voiceover": "Mai trở về.",
            }
            for i in indexes
        ]
        if indexes[0] == 1:
            return {
                "characters": [{"name": "Mai", "description": "Tóc đen, áo nâu"}],
                "scenes": rows,
            }
        assert payload["characters"][0]["name"] == "Mai" and payload["preceding_beats"]
        return rows

    monkeypatch.setattr(workflow, "ask_json", writer)
    data = client.post(
        "/api/muse-studio/workshop",
        json={"topic": "Đường về", "minutes": 1, "seconds": 6, "skill_id": skill_id},
    )
    assert data.is_success, data.text
    vid = data.json()["id"]
    assert data.json()["scenes"] == 10
    batch._process_single_video(vid)
    first = batch.BATCH_MANAGER.get_video(vid)
    assert len([s for s in first["scenes"] if s["prompt"]]) == 6 and first["error"]
    endpoint = f"/api/muse-studio/{vid}/actions"
    req = {"action": "write", "version": first["updated_at"]}
    assert client.post(endpoint, json=req).is_success
    batch._process_single_video(vid)
    second = batch.BATCH_MANAGER.get_video(vid)
    assert all(s["prompt"] for s in second["scenes"]) and not second["error"]
    assert groups[1] == groups[2] == [7, 8, 9, 10]
    assert client.post(endpoint, json=req).status_code == 409
    version = second["updated_at"]
    assert (
        client.post(
            endpoint, json={"action": "image", "indexes": [1], "version": version}
        ).status_code
        == 409
    )

    async def draw(prompt, base, key, target, size):
        assert "Tóc đen, áo nâu" in prompt
        Image.new("RGB", (32, 32), "blue").save(target, "PNG")

    monkeypatch.setattr(batch_engine, "generate_keyframe_image", draw)
    assert client.post(
        endpoint, json={"action": "portraits", "version": version}
    ).is_success
    batch._process_single_video(vid)
    current = batch.BATCH_MANAGER.get_video(vid)
    assert (
        current["cast"][0]["avatar"].startswith("/api/uploads/")
        and not current["error"]
    )
    assert client.post(
        endpoint,
        json={"action": "image", "indexes": [1], "version": current["updated_at"]},
    ).is_success


def test_ask_json_parses_browser_dom_prose_and_favicon_not_404(monkeypatch):
    import studio_workflow as workflow

    client = TestClient(app.app)
    fav = client.get("/favicon.ico")
    assert fav.status_code == 200
    assert "image/svg+xml" in fav.headers["content-type"]

    # 1. Muse Browser DOM innerText with code-block toolbar ("json\nCopy\n") + NBSP + prose
    browser_dom_reply = (
        "Dưới đây là kịch bản phân cảnh [1, 2]:\n"
        "json\nCopy\n"
        '{\u00a0"characters": [{"name": "Trưng Trắc", "description": "Áo giáp đồng, tóc búi cao"}],\n'
        '  "scenes": [\n'
        '    {"index": "1", "title": "Mê Linh rực lửa", "prompt": "Wide shot", "voiceover": "Mùa xuân năm 40.", "characters": ["Trưng Trắc", "Nghĩa quân quần chúng"],},\n'
        "  ]\n"
        "}\n"
        "Bạn có muốn viết tiếp cảnh sau không?"
    )
    parsed = workflow.parse_llm_json(browser_dom_reply)
    assert isinstance(parsed, dict)
    assert parsed["characters"][0]["name"] == "Trưng Trắc"
    assert parsed["scenes"][0]["title"] == "Mê Linh rực lửa"

    # 2. Empty or pure-prose reply raises clear Vietnamese ValueError instead of JSONDecodeError
    with pytest.raises(ValueError, match="phản hồi rỗng"):
        workflow.parse_llm_json("   \u200b ")
    with pytest.raises(ValueError, match="định dạng JSON kịch bản"):
        workflow.parse_llm_json("Xin lỗi, hệ thống đang bận không thể tạo kịch bản.")

    # 3. End-to-end ask_json + write_beats with noisy Browser reply & unregistered crowd extra
    monkeypatch.setattr(
        batch, "muse_connection", lambda _: ("browser", "https://muse.test", "test-key")
    )
    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client_class(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": browser_dom_reply,
                                }
                            }
                        ]
                    },
                )
            ),
            **kw,
        ),
    )
    video = {
        "id": "bv_test01",
        "visual_style": "cinematic",
        "workshop": {"topic": "Khởi nghĩa Hai Bà Trưng"},
        "cast": [],
        "scenes": [{"index": 1, "duration": 8, "prompt": "", "voiceover": ""}],
    }
    asyncio.run(workflow.write_beats(video, [1]))
    assert video["cast"][0]["name"] == "Trưng Trắc"
    assert video["scenes"][0]["characters"] == ["Trưng Trắc"]
    assert video["scenes"][0]["title"] == "Mê Linh rực lửa"


def test_draw_cast_avoids_portrait_filter_and_retries_scenic_fallback(
    tmp_path, monkeypatch
):
    import studio_workflow as workflow

    store = tmp_path / "_batch_videos"
    store.mkdir()
    monkeypatch.setattr(batch, "BATCH_DIR", store)
    monkeypatch.setattr(batch, "QUEUE_FILE", store / "queue.json")
    monkeypatch.setattr(batch, "QUEUE_BAK_FILE", store / "backup.json")
    monkeypatch.setattr(
        batch, "muse_connection", lambda _: ("browser", "https://muse.test", "test-key")
    )

    items = batch.BATCH_MANAGER.add_batch(
        ["Tạo video cuộc khởi nghĩa Hai Bà Trưng"],
        visual_style="3d_pixar",
        auto_run=False,
        extra={
            "guided": True,
            "cast": [
                {
                    "id": "trung_trac",
                    "kind": "character",
                    "name": "Trưng Trắc",
                    "description": (
                        "Chị cả, nữ tướng oai phong. Gương mặt trái xoan, tóc đen búi cao "
                        "cài trâm vàng hình phượng. Mặc áo giáp đỏ thẫm viền vàng, cầm thanh kiếm dài."
                    ),
                    "auto_cast": True,
                }
            ],
        },
    )
    video = items[0]
    attempts = []

    async def fake_generate(prompt, base, key, target, size):
        attempts.append(prompt)
        assert "Single canonical character portrait" not in prompt
        assert "One clearly visible face" not in prompt
        assert "Trưng Trắc" not in prompt
        assert "thanh kiếm dài" not in prompt
        assert "Gương mặt trái xoan" in prompt
        assert "áo giáp đỏ thẫm viền vàng" in prompt
        if len(attempts) == 1:
            raise RuntimeError(
                "Muse chưa tạo được tệp media (I couldn't generate that portrait)"
            )
        Image.new("RGB", (48, 48), "crimson").save(target, "PNG")

    monkeypatch.setattr(batch_engine, "generate_keyframe_image", fake_generate)
    asyncio.run(workflow.draw_cast(video, lambda *_: None))
    assert len(attempts) == 2
    assert "misty river" in attempts[1]
    assert video["cast"][0]["avatar"].startswith("/api/uploads/")


def test_images_use_enabled_muse2api_accounts_and_isolate_failure(
    tmp_path, monkeypatch
):
    import studio_workflow as workflow

    def response(request):
        assert request.url.path == "/admin/accounts"
        return httpx.Response(200, json={"accounts": [{}, {}, {}, {"enabled": False}]})

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client(transport=httpx.MockTransport(response), **kw),
    )
    monkeypatch.setattr(
        batch, "muse_connection", lambda _: ("muse2api", "https://muse.test", "fixture")
    )
    assert asyncio.run(workflow.image_workers(6)) == 3
    assert asyncio.run(workflow.image_workers(2)) == 2
    monkeypatch.setattr(
        batch, "muse_connection", lambda _: ("browser", "https://muse.test", "fixture")
    )
    assert asyncio.run(workflow.image_workers()) == 1

    def pool_response(request):
        assert request.url.path == "/admin/accounts"
        return httpx.Response(
            200,
            json={"pool": True, "ready": True, "accounts": [{}, {}, {}]},
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client(transport=httpx.MockTransport(pool_response), **kw),
    )
    assert asyncio.run(workflow.image_workers(6)) == 3
    assert asyncio.run(workflow.image_workers(2)) == 2

    async def render():
        all_started = asyncio.Event()
        started = []

        async def produce(video, scene, action, work, progress):
            started.append(work)
            if len(started) == 3:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=2)
            if scene["index"] == 2:
                raise ValueError("lỗi mô phỏng")
            scene["frame"] = "ready"

        monkeypatch.setattr(workflow, "directory", lambda _: tmp_path)
        monkeypatch.setattr(workflow, "produce", produce)
        video = {"id": "fixture", "scenes": [{"index": i} for i in (1, 2, 3)]}
        errors = await workflow.produce_images(video, [1, 2, 3], None)
        assert len(set(started)) == 3
        assert errors == ["Cảnh 2: lỗi mô phỏng"]
        assert video["scenes"][0]["frame"] == video["scenes"][2]["frame"] == "ready"

    asyncio.run(render())


def test_studio_uses_approved_frame_when_continuity_is_unspecified():
    import studio_workflow as workflow

    previous = {"index": 1, "setting": "Làng — sáng", "last_frame": "previous.jpg"}
    current = {"index": 2, "frame": "approved.webp", "frame_approved": True}
    video = {"id": "fixture", "scenes": [previous, current]}
    assert workflow.studio_opening(video, current) == ("approved.webp", "anchor")
    current["setting"] = "Sân tập — sáng"
    assert workflow.studio_opening(video, current) == ("approved.webp", "anchor")
