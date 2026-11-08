# static/ — UI sau refactor (2026-11-07)

> Chi sua trong `static/` + them endpoint `GET /api/auth/status` toi thieu o `app.py` (mien auth). Khong commit/push.
> Goc: `static/index.html` 503.667 byte / 10.618 dong (commit 9445a1ed).
> Khoi phuc ban goc: `git show HEAD:static/index.html` (hoac `git checkout HEAD -- static/index.html`).

## Khong can doi app.py
- `app.py:114` mount san: `app.mount("/static", StaticFiles(directory="static"), name="static")`
- `app.py:439-440` (`GET /`) tra `FileResponse("static/index.html")` voi `Cache-Control: no-cache`.
- Vi vay `index.html` dung duong dan tuong doi `static/css/...`, `static/js/...`
  (resolve thanh `/static/css/...` khi chay qua FastAPI). Mo truc tiep bang `file://`
  se KHONG tai duoc css/js — hay chay server (xem "Kiem tra" ben duoi).

## Cau truc moi
| File | Noi dung |
|---|---|
| `index.html` | Shell: head + sidebar/topbar + 8 panel + modal (HTML giu nguyen, chi them nut collapse/theme, bo 2 nut topbar trung lap, nav-item doi sang `data-panel`/`data-action`) |
| `css/app.css` | CSS goc tach tu `<style>` (giu nguyen 100%) |
| `css/ui.css` | Lop UI moi: tokens, light theme, sidebar collapse/drawer, wizard, responsive 1024/768, skeleton, utility classes |
| `js/app.js` | JS goc tach tu `<script>` (giu nguyen id/ham/endpoint) + 5 patch (xem duoi) |
| `js/ui.js` | Tinh nang UI moi, nap sau `app.js` (defer): theme, sidebar, wizard, debounce, skeleton, visibility, fade-in |

## Patch trong js/app.js (deu co comment danh dau)
1. `renderScenes()` tach thanh `renderSceneItemHtml(s, i)` + `updateSceneCard(sceneId)`
   (chi ve lai 1 card) + render theo chunk 12 card/lan (chi khi > 24 canh, dung
   `requestIdleCallback`, token huy chunk cu). 4 caller 1-canh (`toggleVoiceMode`,
   `addCharSegment`, `removeCharSegment`, `toggleSceneChaining`) doi sang
   `updateSceneCard()` (fallback `renderScenes()` neu card chua co trong DOM).
   `onSceneContentInput` von da chi cap nhat DOM cuc bo (khong full render).
2. Polling dung khi tab an (`document.hidden`): library 30s, Quick Video 3s,
   Shot Desk 4s (`scheduleShotDeskPoll` dat co `__sdPollPaused`), compose 800ms
   (`pollComposeProgress` dat co `__sdComposePaused`). `js/ui.js` resume khi mo lai tab.
3. Tat ca `<img>` trong template JS them `loading="lazy" decoding="async"`.

4. `apiFetch()` (dau file, truoc moi cho dung): wrapper quanh `window.fetch`
   tu gan header `X-API-Key` lay tu localStorage key `'ams-api-key'` (chi gan
   khi co key), giu nguyen moi options khac, tra Response nhu fetch goc.
   TOAN BO 66 cho `fetch('/api/...')` doi sang `apiFetch` (khong con `fetch(`
   goi API nao trong app.js; EventSource cho SSE giu nguyen vi SSE mien auth).
   401 -> xoa key da luu neu key do vua bi tu choi, hien modal nhap key MOT
   lan (co `_apiKeyModalShown`), van tra Response 401 ve cho caller (khong
   tu retry, khong nuot loi). `checkStudioAuthStatus()` goi `/api/auth/status`
   khi load trang; neu server bat auth ma trinh duyet chua co key thi hien
   modal. `reloadStudioDataAfterKeySave()` goi lai loadVoices /
   loadCharacterLibrary / loadCharacters / checkApiHealth / loadLibrary.
5. Modal nhap key `#apiKeyModal` (them vao `index.html`, an mac dinh) +
   muc "Khoa API Studio" trong panel Cai dat (`#studioApiKeyInput`,
   `#studioApiKeyStatus`, nut Luu / Xoa key); logic o cuoi `js/ui.js`
   (`window.showApiKeyModal`, `window.refreshStudioApiKeyStatus`).
   Luu y bao mat: key luu o localStorage — chi dung tool ca nhan / trinh
   duyet tin cay, KHONG dung chung may cong cong.


## Bo cuc moi
- **Sidebar**: nut ☰ o topbar de thu gon (desktop: chi icon + tooltip `data-label`,
  luu `localStorage 'ams-sidebar'`); <=1024px thanh drawer truot + overlay;
  <=768px thanh bottom-nav (icon + label nho). Nav-item dung event delegation
  (`data-panel`/`data-action`) thay cho `onclick` rieng le.
- **Topbar**: chi giu trang thai Muse2API + CTA chinh Shot Desk + nut theme 🌙/☀️
  + nut ☰ + Cai dat. Bo nut "Lam moi" (da co check luc load + trong Cai dat)
  va "Sheet Prompt" (da co trong sidebar).
- **Tao phim -> wizard 3 buoc** (gom card co san bang JS, khong doi HTML):
  1 Thong tin phim · 2 Danh sach canh (Beat + Sample + Scene list) · 3 Render.
  Nut "Bat dau San xuat Hang Loat" (`startMovieCreation()`) thanh sticky bottom.
- **Shot Desk**: filter pills (`#sdFilterPills` + `setShotDeskFilter`) va timeline
  mini-map (`#sdCinemaMiniMapWrap`) von da co san — chi nang cap style (pill active,
  grid `auto-fill minmax(180px,1fr)`), o tim kiem debounce 180ms.
- **Theme**: dark mac dinh; toggle 🌙/☀️ luu `localStorage 'ams-theme'`.
  Light theme doi bien CSS; mot so mau cung (`#fff`) trong template JS van dang dark.
- **Thu vien**: skeleton rows trong luc `loadLibrary()` chay.
- **Font**: bo Inter + giam weight — con Plus Jakarta Sans 600/700/800 (body font goc)
  + JetBrains Mono 400/600.

## Performance
- `index.html` nho lai rat nhieu; css/js tach file de trinh duyet cache rieng
  (luu y: `GET /` tra `no-cache` cho chinh index.html, con `/static/*` thi FastAPI
  mac dinh cho cache theo `StaticFiles` — muon cache manh hon thi them header o app.py,
  ngoai pham vi task nay).
- Panel khac chi init khi mo (`showPanel` von da lazy-load library/characters/
  shot-desk/affiliate); `ui.js` danh dau them class `panel-loaded`.
- Anh lazy + fade-in khi cuon toi (IntersectionObserver, rootMargin 200px).
- Render canh theo chunk + cap nhat 1 card (xem patch 1).

## Responsive
- 1024px (moi): sidebar 220px -> drawer; form cols-3/4 -> 2 cot; topbar cuon ngang,
  an nut phu (chi giu theme/collapse).
- 768px (mo rong @media co san): sidebar -> bottom-nav; form ve 1 cot; an text logo;
  grid Shot Desk xuong minmax(140px,1fr). (Ban goc chi co 2 @media: 900px va 768px.)

## Kiem tra
```bash
node --check static/js/app.js && node --check static/js/ui.js
cd <repo> && /tmp/studio-venv/bin/python -m uvicorn app:app --port 8123
# mo http://localhost:8123/ -> bam qua 8 panel, doi theme, thu sidebar, wizard 3 buoc,
# Shot Desk filter + tim kiem, Thu vien (skeleton), an tab vai giay roi mo lai (poll resume)
curl -s -o /dev/null -w "%{http_code} %{size_download}\n" http://localhost:8123/static/js/app.js
```
Khong co trinh duyet headless trong VM nay nen chua chay kiem tra UI bang browser that —
can kiem tra thu cong theo checklist tren truoc khi dua len production.

## TODO / Chua lam (ly do)
- Con `style="..."` inline: dot redesign 2026-11-08 da dua 898 -> 417 cho (xem muc Redesign o cuoi file); phan con lai chu yeu la trang thai display, do rong dong % va mau 1 lan trong template JS
  va `onclick="..."` (~250 cho): chi doi nhung cho chac chan (nav sidebar, 1 so
  class lap lai o body). Doi hang loat trong template JS de vo `esc()`/handler
  va kho kiem chung khong co browser — nen lam theo dot rieng co test UI.
- Light theme chua phu het mau cung trong template JS (chu trang tren nen sang
  o 1 so card) — can doi template sang dung bien CSS moi hoan thien.
- `index.html` van giu toan bo 8 panel + modal trong DOM tu luc load (lazy that su
  kieu fetch HTML rieng tung panel doi backend ho tro partial — ngoai pham vi).
- Chua co test UI tu dong (repo khong co Playwright/jsdom); can them truoc khi
  refactor sau rong hon.

## Xac thuc X-API-Key (frontend)
- **Bat auth tren server:** dat bien moi truong `STUDIO_API_KEY=<key-ngau-nhien>`
  khi chay uvicorn (KHONG commit key vao repo, KHONG ghi vao `.env.example`).
  Khi chua dat, auth TAT — moi thu chay nhu cu, khong can key.
- **Kiem tra server co bat auth khong:** `GET /api/auth/status` (mien auth) tra
  `{"auth_required": true/false}` — khong tra key hay secret nao.
- **Nhap key tren giao dien:** mo trang, neu server bat auth ma trinh duyet chua
  co key thi modal "Nhap Khoa API Studio" tu hien; hoac vao panel Cai dat >
  muc "Khoa API Studio (X-API-Key)" nhap key roi bam Luu key. Key luu o
  localStorage key `'ams-api-key'`, chi gui toi chinh server Studio nay qua
  header `X-API-Key` (xem `apiFetch()` trong `js/app.js`). Sau khi luu, giao
  dien tu goi lai cac ham load chinh (giong, nhan vat, trang thai API, thu vien).
- **Xoa key / dang xuat:** panel Cai dat > "Xoa key / Dang xuat" (xoa
  localStorage). Neu key cu bi server tu choi (401), frontend tu xoa key do
  va hien lai modal nhap key mot lan.
- **SSE** (`/api/jobs/{id}/sse`) van mien auth vi EventSource khong gui duoc
  custom header — giu nguyen, chi lo tien do/log cua job_id.
- **Kiem tra nhanh:**
  ```bash
  STUDIO_API_KEY=<khoa-test-tam-thoi> uvicorn app:app --port 8123
  curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8123/api/jobs            # -> 401
  curl -s -H "X-API-Key: <khoa-test-tam-thoi>" http://localhost:8123/api/jobs       # -> 200
  curl -s http://localhost:8123/api/auth/status                                        # -> {"auth_required":true}
  ```
  (Chi dung key tam thoi trong luc kiem tra; khong ghi key that vao file nao trong repo.)

## Timeline Editor (2026-11-07, chua commit)
- Timeline strip tren danh sach canh (panel Tao phim): o ti le theo duration,
  bam o de xem truoc + cuon toi card. Tong thoi luong + badge "phim da ghep cu".
- Keo-tha card bang HTML5 DnD (da co san) + vach chi vi tri tha + nut len/xuong;
  thu tu moi luu xuong server: POST /api/jobs/{id}/scenes/reorder {order:[...]}
  (hoan vi 1..N; 400 neu sai, 409 neu job dang chay; doi ten file clip theo
  chi so moi, danh dau final_stale). Loi -> hoan lai thu tu cu tren UI.
- Xem truoc: bam vao card -> video clip neu co, neu khong thi keyframe, neu
  khong thi prompt + trang thai. Poll trang thai job 4s de khoa keo-tha khi
  job dang chay.
- Render lai 1 shot: nut 🔄 tren card -> dialog sua prompt/voiceover/duration
  (nhan vat/giong/anh giu nguyen theo khoa). POST /api/jobs/{id}/scenes/{idx}/rerender
  (409 neu job/scene dang render); clip cu luu sang previous/ (giu toi da 1 ban).
  So sanh A/B tren card: GET .../previous-video; POST .../restore-previous
  (hoan doi clip + prompt) / .../discard-previous (giu ban moi). Khong tu ghep
  lai ca phim: final_stale=true, hien "can ghep lai".
- Code: backend app.py (endpoint + helper _scene_file_relpaths/_rename_scene_files/
  _save_previous_clip/_swap_previous_clip, single_scene_pipeline them
  recompose_final=False); frontend chu yeu static/js/ui.js (window.TimelineEditor),
  app.js chi them 2 hook trong renderSceneItemHtml; CSS static/css/ui.css (.tl-*).
  Test: test_timeline_editor.py (5 test).
- Gioi han: chua ap dung keo-tha cho Shot Desk project (chi panel Tao phim);
  chua kiem tra bang trinh duyet that (VM khong co headless browser); reorder
  doi ten file clip tren dia nen job dang co final video se bi danh dau cu.
## Affiliate continuity — chain sequential + Product DNA lock + 10s chuẩn
- **Render nối tiếp:** `/api/affiliate/render` luôn đặt `chain_mode="sequential"` cho
  `MovieRequest` affiliate (xem `app.py: api_render_affiliate`). Cảnh sau dùng last
  frame cảnh trước làm first frame, giữ tay/sản phẩm/bối cảnh đồng nhất.
  Trade-off: render tuần tự nên wall-time ≈ tổng thời gian từng cảnh (5 cảnh ≈ 5 lần
  1 cảnh), chậm hơn song song — đổi lại video bán hàng không bị trôi sản phẩm.
  Cảnh 1 giữ nguyên `reference_image` sản phẩm user đưa vào; cảnh sau bị ghi đè
  bằng last frame (chủ đích). Nếu 1 cảnh lỗi, cảnh sau render độc lập
  (`chain_fallback_mode="independent"` mặc định).
- **Product DNA lock:** `build_product_dna_lock()` trong `affiliate_engine.py` dựng
  khối `[PRODUCT CONTINUITY LOCK — ...]` từ tên sản phẩm (EN + giữ tên gốc),
  tính năng chính, neo liên tục theo kiểu quay (tay/bàn/bối cảnh/KOC) và style_tag
  ngành hàng; dán nguyên văn vào cuối prompt MỌI cảnh (local + AI: LLM được yêu
  cầu dán nguyên văn, hậu xử lý append lại nếu thiếu). `validate_affiliate_script()`
  kiểm tra cảnh nào thiếu lock/tên sản phẩm.
- **Chuẩn 10s:** mọi cảnh affiliate `duration=10` (đúng clip API 10s trả về) —
  script local, hậu xử lý AI và render đều ép 10 kể cả khi script cũ gửi duration
  khác. Clip được dùng trọn vẹn, không cắt đuôi, không loop. Thoại mỗi cảnh nên
  25–30 từ tiếng Việt để đọc vừa 10s; thoại dài hơn thì CHỈ cảnh báo trong
  `marketing_notes`, không cắt lặng lẽ — khi đó compose vẫn kéo dài cảnh theo độ
  dài audio (clip loop cho đủ) như cơ chế cũ.
- **UI:** ô thời lượng từng cảnh affiliate cố định 10s (không sửa được). Khi job có
  video cuối, card video affiliate nổi bật ở đầu panel affiliate và Batch Matrix
  (panel Monitor) tự thu gọn thành dải tiến độ compact (pill ✅/❌/▶️/⌛ từng cảnh),
  bấm "Mở rộng" để xem lại từng cảnh; job mới bắt đầu thì mở lại đầy đủ.
## Đồng nhất tối đa (Consistency Max, 2026-11-07)

- Toggle "🎯 Đồng nhất tối đa" trong panel Tạo phim (cạnh mục Chaining): tự BẬT khi phim có từ 6 cảnh trở lên, vẫn tắt được bằng tay (sau khi bật/tắt tay thì không tự đổi nữa).
- Khi bật: `POST /api/movies` chạy preflight (`consistency.py`) trước khi tạo job — có lỗi thì trả 422 kèm danh sách check, không tạo job; chỉ cảnh báo thì vẫn tạo job và ép `chain_mode="sequential"` (ghi log rõ đã override). Nút "🔍 Kiểm tra đồng nhất" gọi `POST /api/consistency-check` (không tạo job) và hiện báo cáo trong modal (lỗi đỏ, cảnh báo vàng, gợi ý); nút "Mở Hồ sơ nhân vật" nhảy sang panel Characters để sửa lock. Tool không tự sửa prompt/thoại của user.
- Endpoint `POST /api/jobs/{job_id}/consistency-check` kiểm tra lại req đã lưu của 1 job.
- Neo lại nhân vật: trong chuỗi sequential, cứ mỗi 8 scene (mốc 8/16/24...) thì dùng ảnh keyframe/character reference thay cho Last Frame để reset drift (`CONSISTENCY_ANCHOR_EVERY = 8` trong `consistency.py`); scene không có ảnh neo thì giữ nguyên Last Frame.
- Giới hạn: phát hiện shot cầu nối chỉ là heuristic từ khóa thời gian/địa điểm (sáng/trưa/chiều/tối/đêm, trong nhà/ngoài trời, danh từ địa điểm), chưa hiểu ngữ nghĩa sâu nên có thể báo thừa/thiếu — chỉ dùng làm gợi ý.
---

## Tốc độ render (2026-11-08)

- **Timing từng giai đoạn:** mỗi job có `job["timing"] = {render_s, poll_s, tts_s, compose_s, total_s}` (lưu cùng `job_state.json`, trả về trong `GET /api/jobs/{id}`). `render_s` = wall time chạy batch, `poll_s`/`tts_s` = tổng thời gian chờ API / sinh TTS (hai số này chồng lấn nhau vì TTS chạy song song với poll, và chồng lấn giữa các scene ở chế độ song song — dùng để soi bottleneck, không cộng vào tổng). Panel Monitor hiện bảng nhỏ "Render X.Xs · Poll API X.Xs · TTS X.Xs · Ghép X.Xs · Tổng X.Xs" khi có số liệu.
- **Ghép single-pass:** `merge_clips_advanced` mặc định ghép toàn bộ timeline bằng 1 lần gọi ffmpeg (chuỗi xfade cho dissolve/fade, concat cho bước cut; audio acrossfade/concat tương ứng). `COMPOSE_MODE=pairwise` ép dùng nhánh pairwise cũ (để benchmark đối chiếu); single-pass lỗi thì tự fallback pairwise. Benchmark: `scripts/bench_compose.py` (tự sinh clip, không cần API ngoài).
- **Cache TTS:** `batch_engine.generate_tts` cache file audio theo hash SHA256 (text chuẩn hóa + voice + speed) tại `workspace/_tts_cache/`, tối đa 500 file hoặc 500MB (cái nào chạm trước), dọn LRU theo mtime. Không cache text rỗng, voice đặc biệt (`ai_voice`/`native`/`none`/`no_dub`) hay audio fallback im lặng.
- **Giới hạn đã đo:** bottleneck lớn nhất của phim dài vẫn là chờ API video ngoài (poll) — ghép/xfade và TTS chỉ là phần nhỏ; xem số `poll_s` trên Monitor để xác nhận với từng job thật.
---

## Redesign 2026-11-08 — desktop-first (nhanh `feature/ui-redesign`)

> Chi sua `static/` (HTML/CSS/JS), khong sua backend, khong commit/push.
> Inventory ID day du: `../docs-ui-redesign-inventory.md` (goc repo).

### Dinh huong (doi so voi ban goc: desktop-first, khong mobile-first)
- Uu tien man hinh lon (1440px+, toi uu 1920x1080): bo cuc rong nhieu cot,
  mat do cao nhung thoang, it cuon. Mobile chi giu muc toi thieu: khong vo
  layout, van dung duoc (drawer <=1024px + bottom-nav <=768px cua PR #1 giu
  nguyen, khong mo rong).
- Accent DUY NHAT indigo `#6366f1` (light: `#4f46e5`); cac mau sky/violet/pink
  cu (`#38bdf8`, `#a855f7`, `#c084fc`, `#ec4899`, `rgba(56,189,248,…)`,
  `rgba(168,85,247,…)`) da doi het sang ho indigo trong `css/app.css`,
  `css/ui.css`, `index.html`, `js/app.js`. Xanh/vang/do chi con la mau ngu
  nghia trang thai; mau hong/xanh la trong `CHAR_COLORS` (avatar nhan vat)
  va bang mau loai canh affiliate (data) duoc giu lai co chu dich.
- Dark-first sau hon (`#0a0a0c`), light theme day du (cung accent).

### Bo cuc moi
- **Topbar (60px):** logo 1 mau, chip ten project hien tai (`#topbarProjectName`,
  dong bo tu o Ten du an), trang thai Muse2API, nut **🚀 San xuat** chi hien
  o panel Tao phim (`body[data-panel="create"]`, goi `startMovieCreation()`).
- **Sidebar (272px, thu gon duoc):** icon + nhan day du, muc active co vach
  accent trai + nen accent mo; moi muc co ghi chu phim tat (`Alt+C/A/K/N/M/Q/L/O/P`).
- **Tao phim (wizard 3 buoc giu nguyen):** them tieu de panel + nut Luu/Mo
  Project; buoc 1 chia 2 cot **(1) Thong tin phim | (2) Nhan vat & khoa** vs
  **(3) Am thanh & giong doc | (4) Xuat phim** (`>=1440px`); Style Locks +
  Chaining gom vao muc **Nang cao** (`<details>`, mac dinh thu gon); nut
  Phan ra nhip giu ngoai; Truong bat buoc co dau `*` (Ten du an, Ten san
  pham affiliate); buoc 2: cong cu trai 400px + danh sach canh phai rong;
  nut San xuat to, dinh day (`position: sticky`, `Ctrl+Enter`).
- **Monitor:** 2 cot `>=1440px` (job/audit/matrix trai, log + video + clips
  phai, cot phai dinh). **Video Nhanh:** form trai, ket qua + lich su phai
  (`>=1280px`). **Cai dat:** 2 card canh nhau (`>=1100px`); sua luon loi
  cu: card dau tien thieu the dong `</div>`.
- **Thu vien:** bang dac, header dinh khi cuon, hang hover ro, cot tien do
  co thanh tien trinh; empty state moi. **Affiliate:** banner + badge 1 mau
  accent, nut tao/render dung `.btn-primary`/`.btn-success` (bo gradient
  hong/tim). **Nhan vat:** luoi card giu nguyen, avatar dung class.
- **Empty state** dung chung (`.empty-state`): Monitor, Affiliate, Scene
  list (placeholder trong `#sceneList`, JS `renderScenes()` se thay bang
  danh sach that), Thu vien (hang `Chua co job nao`).
- **Toast:** chuyen tu `bottom-right` sang **top-right** (duoi topbar) de
  khong che nut San xuat dinh day; style chuyen sang class
  (`.toast-container`, `.toast`, `.toast-error`, `.toast-warning`).
- **Focus/hover/ban phim:** `:focus-visible` outline accent 2px toan app;
  nut co hover nang nhe; phim tat moi (xem duoi).

### Phim tat moi (them o cuoi `js/ui.js`, khong dung so)
- `Alt+C` Tao phim · `Alt+A` Affiliate · `Alt+K` Shot Desk · `Alt+N` Nhan vat ·
  `Alt+M` Monitor · `Alt+Q` Video Nhanh · `Alt+L` Thu vien · `Alt+O` Cai dat ·
  `Alt+P` Sheet Prompt Mau · `Ctrl+Enter` (hoac `Cmd+Enter`) San xuat khi
  dang o panel Tao phim. Khong bat phim khi dang go trong input/textarea/select.
- Ly do dung Alt+CHU: `app.js` da bat phim `1/2/3` tran de doi subtab Shot Desk
  (khong kiem tra modifier), nen phim so se kich hoat ca hai handler.
  `Alt+D` tranh dung vi Chrome dung de focus thanh dia chi.

### Thay doi JS (toi thieu, co ghi chu trong code)
- `js/ui.js`: wizard nav chen sau `.panel-head` (truoc day chen dau panel);
  skeleton thu vien + spacer wizard dung class; them dong bo ten project
  len topbar + `body[data-panel]` + phim tat tren.
- `js/app.js`: `showToast()` dung class thay `cssText`; `loadLibrary()` dung
  class (`.job-id`, `.job-prog`, `.job-meta`, `.job-actions`, `.empty-row`);
  empty-filter Shot Desk dung class `.sd-empty-msg`.
- **Bug fix kem theo (khong phai redesign):**
  1. `sendAffiliateToMainStudio()` dung `movieTitle`/`aspectRatio` (ID da
     khong con trong HTML tu truoc) -> doi sang `projectTitle` /
     `globalAspectRatio`; truoc day nut "Nap Vao Studio" am tham khong dat
     ten/ti le phim.
  2. Them `<input type="hidden" id="globalStylePreset" value="">` (panel Tao
     phim): `aiStandardizeScript()` doc `.value` khong guard o `app.js:1381`
     -> truoc day bam "AI Tu Dong Chuyen The" se vo `TypeError`.
- **ID doi ten: KHONG CO.** Xem `../docs-ui-redesign-inventory.md`.

### Inline style: 898 -> 417 (cung cach dem `grep -o 'style="[^"]*"'`)
- Chuyen 437 cho sang utility class (`.fx`, `.fx-g*`, `.u-fs*`, `.u-c-*`,
  `.u-mt*`, `.u-mb*`, `.u-p-*`, …) o cuoi `css/ui.css`, bang script co
  denylist (khong dung vao style trang thai `display:none/block`, style
  dong `${...}`, element JS dang toggle, nut co class `.btn` khi doi
  display). Con lai 417 cho, chu yeu: trang thai display do JS toggle
  (52), do rong dong `%` (42), mau nen/gradient rieng le trong template JS
  (124, gom mau loai canh + mau trang thai), style 1 lan khong lap
  (position/size px). `onclick="..."` giu nguyen toan bo (khong doi co che
  goi ham).

### Kiem chung da chay (2026-11-08, so that)
- `node --check` ca 2 file JS: OK. `python -m py_compile app.py`: OK.
- `pytest` (venv `/tmp/studio-venv2`, bo 2 file loi san): **43 passed**.
  `test_bottom_sub.py` + `test_sub.py` loi collection ca tren HEAD goc
  (thieu `workspace/_shot_desk_projects/.../final`) — loi co san, khong phai
  do redesign. (Chay server that can `env -u ..._PROXY ... -u NO_PROXY`
  vi `NO_PROXY` chua `[::1]` lam `httpx.AsyncClient()` vo o lifespan —
  van de moi truong, co san truoc redesign.)
- Server that: `/` 200, `css/app.css` 200, `css/ui.css` 200, `js/app.js` 200,
  `js/ui.js` 200; HTML tra ve giong het file; script doi chieu: **190/225
  ID JS co trong HTML, 0 ID trung**; 35 ID con lai do JS tu tao dong hoac
  guarded (xem inventory). `/api/health` tra `studio:true`
  (muse2api:false do chua cau hinh key — dung nhu moi truong test).
- HTML parser: 0 loi can bang tag (ca loi thieu `</div>` cu o panel Cai dat
  da sua). CSS: so `{`/`}` can bang ca 2 file.
- **CHUA kiem chung bang trinh duyet that:** VM khong co Chromium/Firefox/
  Playwright (`which chromium`, `python -m playwright` deu khong co) —
  khong the chup anh man hinh hay click thu. Can kiem tra tay theo
  checklist duoi truoc khi merge.

### Checklist kiem tra tay tren trinh duyet that (cho Tien)
Desktop 1920x1080 (Chrome/Edge) + laptop 1440x900:
- [ ] Mo trang: sidebar day du nhan, topbar co chip ten project, nut San xuat
      chi hien o panel Tao phim; doi ten project -> chip doi theo.
- [ ] Tao phim: buoc 1 hien 2 cot (thong tin/khoa trai, am thanh/xuat phai);
      mo muc Nang cao thay Style Locks + Chaining; buoc 2: cong cu trai,
      danh sach canh phai; nut San xuat dinh day khi cuon.
- [ ] Alt+C/A/K/N/M/Q/L/O/P chuyen panel; Ctrl+Enter o panel Tao phim goi
      san xuat (chi bam khi muon chay that).
- [ ] Monitor (mo job cu): cot phai (log/video) dinh khi cuon; Thu vien:
      header bang dinh, hang hover, nut tai/xem/xoa.
- [ ] Shot Desk: mo project mau, doi subtab 1/2/3, loc pill, mo Review Desk;
      phim `1/2/3` cu van doi subtab (khong bi phim tat moi pha).
- [ ] Affiliate: nhap ten san pham -> nut tao kich ban (mau indigo); mo
      Sheet Prompt Mau (Alt+P) dong/mo binh thuong.
- [ ] Dark/light: bam 🌙/☀️, tai lai trang van nho theme; light khong chu
      trang tren nen sang o card chinh.
- [ ] Ban phim: Tab di chuyen qua cac nut/field thay duoc outline accent;
      modal API key mo bang Escape dong duoc.
- [ ] Nhan vat: mo form them nhan vat, anh dai dien hien dung.
- Mobile (Android, Chrome): mo trang, bam ☰ mo drawer, chon muc, drawer
  dong; khong tran ngang; khong can bottom-nav cung dung duoc.
- [ ] Kiem tra chuc nang cu khong doi: Tao phim 1-2 canh that (neu co key),
      Video Nhanh 1 clip, luu/mo project JSON.

### Luu y merge
- Nhanh nay se **conflict voi PR #2 (timeline), #3 (affiliate), #4
  (consistency-max)** vi cung sua `index.html`/`js/app.js`/`css/*`.
  Thu tu goi y: merge PR #2/#3/#4 truoc (hoac quyet dinh bo), roi rebase
  nhanh nay va giai conflict theo tung file — phan CSS utility + HTML
  structure cua redesign nen giu, phan logic moi cua cac PR kia giu
  nguyen, chi map lai class/ID neu can. Khong merge thang khi chua chay
  checklist trinh duyet that o tren.
- Han che con biet: mau chu `#fff` tren nen toi trong 1 so template JS
  (card ket qua affiliate, modal) van cung; light theme chua phu het mau
  cung trong template JS (ghi tu PR #1, chua giai quyet o dot nay).
