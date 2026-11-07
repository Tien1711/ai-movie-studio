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
- Con nhieu `style="..."` inline (dac biet trong template JS cua `app.js`, ~594 cho)
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
