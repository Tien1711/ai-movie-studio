"""Tái hiện rò rỉ nhân vật giữa các phim — chạy trên code ĐÃ SỬA phải PASS 100%.

Thiết kế mới (scope per-film):
- Thư viện toàn cục chỉ là nơi lưu mẫu; nhân vật chỉ áp dụng cho phim
  khi đã GÁN vào phim (addFilmCharacter). detect/auto-link chỉ quét
  filmCharacters, không quét thư viện.
- "Phim mới" (newFilm/resetFilmState) reset sạch toàn bộ trạng thái phim.
- loadProjectByName/import khôi phục TUYỆT ĐỐI (kể cả field rỗng).
- voice_mode + script_text round-trip qua save/load/export (audit #1/#2).
- Nạp sample dùng chung resetFilmState (audit #7).

Kịch bản:
1. Tạo nhân vật toàn cục "Luc Tran REPRO" trong thư viện.
2. Phim A: lock + voice_mode ai_voice + script_text, GÁN nhân vật vào
   phim, thêm cảnh nhắc tên -> auto-link avatar + giọng (theo phim).
3. Lưu project A -> kiểm tra characters/lock/voice_mode/script_text.
4. Bấm "Phim mới" -> form reset sạch, thư viện toàn cục vẫn còn nhân vật.
5. Phim mới: thêm cảnh nhắc tên nhân vật -> KHÔNG bị đóng dấu (chưa gán).
6. Lưu phim B (rỗng) -> mở lại B: lock/voice_mode/script_text sạch
   (khôi phục tuyệt đối); mở lại A: khôi phục đủ.
7. Payload tạo phim (lúc đang ở phim A) chỉ mang nhân vật của phim A.
8. Nạp sample hội thoại -> form reset sạch (lock/nhân vật phim rỗng).

Chạy: /tmp/studio-venv2/bin/python scripts/repro_character_leak.py
  (tự khởi động server trên cổng 8131, tự dọn project/nhân vật test sau khi chạy)
Thoát 0 = KHÔNG còn rò rỉ; thoát 1 = còn rò rỉ.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
PORT = 8131
BASE = f"http://127.0.0.1:{PORT}"

MARKER_LOCK = "MARKER_BLOCK_A solitary swordsman in dark hanfu robe"
SCRIPT_A = "Kich ban tho phim A REPRO: Luc Tran rut kiem."
CHAR_NAME = "Luc Tran REPRO"
CHAR_AVATAR = "https://example.com/luc-tran-repro.png"
CHAR_VOICE = "vi-VN-NamMinhNeural"


def http(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    data = json.dumps(body).encode() if body is not None else None
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait_server(proc, timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            raise RuntimeError(f"server chết sớm, exit={proc.returncode}")
        try:
            status, _ = http("GET", "/api/health")
            if status == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("server không lên sau 40s")


def main():
    env = {k: v for k, v in os.environ.items()
           if k not in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                        "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")}
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(PORT)],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    char_id = None
    try:
        wait_server(proc)
        # Dọn dư từ lần chạy trước (nếu có)
        http("DELETE", "/api/projects/repro_film_a")
        http("DELETE", "/api/projects/repro_film_b")
        _, chars = http("GET", "/api/characters")
        for c in chars if isinstance(chars, list) else []:
            if c.get("name") == CHAR_NAME:
                http("DELETE", f"/api/characters/{c['id']}")

        # 1. Tạo nhân vật toàn cục
        status, created = http("POST", "/api/characters", {
            "name": CHAR_NAME, "aliases": ["Luc Tran"],
            "voice": CHAR_VOICE, "avatar": CHAR_AVATAR,
            "description": MARKER_LOCK})
        assert status == 200, f"tạo nhân vật lỗi {status}: {created}"
        char_id = created["character"]["id"]

        from playwright.sync_api import sync_playwright
        import os as _os
        _chrome = _os.path.expanduser(
            "~/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome")
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=_chrome)
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_timeout(1500)
            page.evaluate("loadCharacters()")
            page.wait_for_function(
                "() => (typeof charactersList !== 'undefined' && charactersList.length > 0)")

            # 2. Phim A: lock + voice_mode + script_text + GÁN nhân vật
            page.evaluate(
                """(args) => {
                  document.getElementById('projectTitle').value = 'Phim A REPRO';
                  document.getElementById('lockCharacter').value = args.lock;
                  document.getElementById('rawScriptInput').value = args.script;
                  document.getElementById('voiceModeSelect').value = 'ai_voice';
                  addFilmCharacter(args.charId);
                }""", {"lock": MARKER_LOCK, "script": SCRIPT_A, "charId": char_id})
            page.evaluate(
                """() => addScene('Luc Tran REPRO rut kiem giua rung truc', '', 5,
                                  '16:9', 'Canh 1', '', [], '', '', false, 'front')""")
            page.wait_for_timeout(800)
            scene_a = page.evaluate("() => { const s = scenes.find(x => x.prompt.includes('rut kiem giua rung truc')); return s ? {ref: s.reference_image, voice: s.voice} : null; }")
            check("Phim A: canh tu dong gan avatar nhan vat DA GAN vao phim",
                  scene_a and scene_a["ref"] == CHAR_AVATAR, f"ref={scene_a}")
            check("Phim A: canh tu dong gan giong nhan vat DA GAN vao phim",
                  scene_a and scene_a["voice"] == CHAR_VOICE, f"voice={scene_a}")
            film_chars_a = page.evaluate("() => filmCharacters.length")
            check("Phim A: nhan vat da gan (filmCharacters=1)", film_chars_a == 1,
                  f"n={film_chars_a}")

            # 3. Lưu project A (qua UI, xử lý hộp prompt)
            page.once("dialog", lambda d: d.accept("repro_film_a"))
            page.evaluate("() => saveCurrentProject()")
            page.wait_for_timeout(1200)
            _, proj_a = http("GET", "/api/projects/repro_film_a")
            check("Project A luu duoc characters",
                  isinstance(proj_a.get("characters"), list) and len(proj_a["characters"]) >= 1,
                  f"characters={proj_a.get('characters')}")
            check("Project A luu duoc lockCharacter",
                  (proj_a.get("style_locks") or {}).get("character") == MARKER_LOCK,
                  f"lock={(proj_a.get('style_locks') or {}).get('character')}")
            check("Project A luu duoc voice_mode=ai_voice (audit #1)",
                  proj_a.get("voice_mode") == "ai_voice", f"voice_mode={proj_a.get('voice_mode')}")
            check("Project A luu duoc script_text (audit #2)",
                  proj_a.get("script_text") == SCRIPT_A, f"script_text={proj_a.get('script_text')!r}")

            # 4. Nút "Phim mới" reset sạch, thư viện toàn cục còn nguyên
            has_btn = page.evaluate("() => !!document.getElementById('btnNewFilm')")
            check("Co nut 'Phim moi' trong panel Tao phim", has_btn)
            page.once("dialog", lambda d: d.accept())
            page.evaluate("() => newFilm()")
            page.wait_for_timeout(800)
            st = page.evaluate(
                """() => ({
                  title: document.getElementById('projectTitle').value,
                  lock: document.getElementById('lockCharacter').value,
                  script: document.getElementById('rawScriptInput').value,
                  voiceMode: document.getElementById('voiceModeSelect').value,
                  scenes: scenes.length,
                  filmChars: (typeof filmCharacters !== 'undefined') ? filmCharacters.length : -1,
                  libChars: (typeof charactersList !== 'undefined') ? charactersList.length : -1,
                })""")
            check("Phim moi: reset sach tieu de/lock/kich ban/giong/canh/nhan vat phim",
                  st["title"] == "" and st["lock"] == "" and st["script"] == ""
                  and st["voiceMode"] == "tts" and st["scenes"] == 0
                  and st["filmChars"] == 0, f"{st}")
            check("Phim moi: thu vien nhan vat toan cuc KHONG bi xoa",
                  st["libChars"] >= 1, f"libChars={st['libChars']}")
            page.screenshot(path="/tmp/repro_after_new_film.png")

            # 5. Phim mới: cảnh nhắc tên nhưng CHƯA gán nhân vật -> không đóng dấu
            page.evaluate(
                """() => addScene('Luc Tran REPRO di cho mua rau', '', 5,
                                  '16:9', 'Canh 1', '', [], '', '', false, 'front')""")
            page.wait_for_timeout(800)
            scene_b = page.evaluate("() => { const s = scenes.find(x => x.prompt.includes('di cho mua rau')); return s ? {ref: s.reference_image, voice: s.voice} : null; }")
            check("Phim moi: canh moi KHONG bi dong dau avatar nhan vat cu",
                  not scene_b or not scene_b["ref"], f"ref={scene_b}")
            check("Phim moi: canh moi KHONG bi gan giong nhan vat cu",
                  not scene_b or not scene_b["voice"], f"voice={scene_b}")

            # 6. Lưu phim B (rỗng) rồi mở lại B -> sạch; mở lại A -> đủ
            page.once("dialog", lambda d: d.accept("repro_film_b"))
            page.evaluate("() => saveCurrentProject()")
            page.wait_for_timeout(1200)
            page.evaluate("() => loadProjectByName('repro_film_b')")
            page.wait_for_timeout(1200)
            st_b = page.evaluate(
                """() => ({
                  lock: document.getElementById('lockCharacter').value,
                  script: document.getElementById('rawScriptInput').value,
                  voiceMode: document.getElementById('voiceModeSelect').value,
                  filmChars: filmCharacters.length,
                })""")
            check("Mo lai phim B: lock/voice_mode/script_text/nhan vat sach (audit #4)",
                  st_b["lock"] == "" and st_b["voiceMode"] == "tts"
                  and st_b["script"] == "" and st_b["filmChars"] == 0, f"{st_b}")
            page.evaluate("() => loadProjectByName('repro_film_a')")
            page.wait_for_timeout(1200)
            st_a = page.evaluate(
                """() => ({
                  lock: document.getElementById('lockCharacter').value,
                  script: document.getElementById('rawScriptInput').value,
                  voiceMode: document.getElementById('voiceModeSelect').value,
                  filmChars: filmCharacters.length,
                  filmCharName: filmCharacters[0] ? filmCharacters[0].name : null,
                })""")
            check("Mo lai phim A: lock/voice_mode/script_text/nhan vat khoi phuc du",
                  st_a["lock"] == MARKER_LOCK and st_a["voiceMode"] == "ai_voice"
                  and st_a["script"] == SCRIPT_A and st_a["filmChars"] == 1
                  and st_a["filmCharName"] == CHAR_NAME, f"{st_a}")
            page.screenshot(path="/tmp/repro_film_a_restored.png")

            # 7. Payload tạo phim chỉ mang nhân vật của phim hiện tại (phim A)
            payload = page.evaluate("() => buildMoviePayload()")
            check("Payload tao phim co key characters",
                  "characters" in payload, f"keys={sorted(payload.keys())}")
            if "characters" in payload:
                names = [c.get("name") for c in payload["characters"]]
                check("Payload chi mang nhan vat cua phim hien tai (phim A)",
                      names == [CHAR_NAME], f"names={names}")

            # 8. Nạp sample hội thoại -> form reset sạch (audit #7)
            page.evaluate("() => loadDialogueSample()")
            page.wait_for_timeout(800)
            st_s = page.evaluate(
                """() => ({
                  title: document.getElementById('projectTitle').value,
                  lock: document.getElementById('lockCharacter').value,
                  script: document.getElementById('rawScriptInput').value,
                  voiceMode: document.getElementById('voiceModeSelect').value,
                  filmChars: filmCharacters.length,
                  scenes: scenes.length,
                })""")
            check("Nap sample: form reset sach, tieu de = sample",
                  st_s["title"] == "Gặp Gỡ Định Mệnh" and st_s["lock"] == ""
                  and st_s["script"] == "" and st_s["voiceMode"] == "tts"
                  and st_s["filmChars"] == 0 and st_s["scenes"] > 0, f"{st_s}")

            check("Khong co loi JS trong qua trinh chay", len(errors) == 0,
                  f"errors={errors}")
            browser.close()

        # Dọn
        http("DELETE", "/api/projects/repro_film_a")
        http("DELETE", "/api/projects/repro_film_b")
        if char_id:
            http("DELETE", f"/api/characters/{char_id}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    failed = [c for c in checks if not c[1]]
    print(f"\nKết quả: {len(checks) - len(failed)}/{len(checks)} PASS")
    if failed:
        print("CÒN RÒ RỈ:")
        for name, _, detail in failed:
            print(f"  - {name} {detail}")
        return 1
    print("KHÔNG còn rò rỉ.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
