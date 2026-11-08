# UI Redesign — Inventory ID (2026-11-08, nhanh feature/ui-redesign)

Quet tu `static/js/app.js` + `static/js/ui.js`: moi `getElementById`, `querySelector(#...)`, `querySelectorAll(#...)`.
Quy tac redesign: KHONG doi ten/xoa/trung bat ky ID nao JS dang dung.

- Tong ID JS tham chieu: **225**
- Co trong `index.html`: **190** (gom 3 ID moi them: `topbarProjectName`, `topbarCreateBtn`, `globalStylePreset`)
- Khong co trong HTML: **35** — tat ca deu do JS tu tao dong (`innerHTML`/`createElement`) hoac duoc guard (`?.`/`if (el)`), da kiem chung tung ID truoc khi sua.

## ID doi ten: KHONG CO

Khong ID nao bi doi ten. 2 ID cu chet (`movieTitle`, `aspectRatio`) khong con duoc tham chieu sau khi sua `sendAffiliateToMainStudio` sang `projectTitle`/`globalAspectRatio` (xem README muc bug fix).

## ID moi them (chi UI, khong anh huong logic cu)

| ID | Vi tri | Muc dich |
|---|---|---|
| `topbarProjectName` | topbar | Chip ten project hien tai, dong bo tu `#projectTitle` (ui.js) |
| `topbarCreateBtn` | topbar | Nut San xuat chinh, chi hien khi `body[data-panel="create"]`, goi `startMovieCreation()` |
| `globalStylePreset` | panel-create, `<input type="hidden">` | Khoi phuc element JS `aiStandardizeScript()` doc o `app.js:1381` (truoc day `null.value` -> TypeError) |

## ID khong co trong HTML (JS tu tao / guarded)

| ID | Cach xu ly trong JS |
|---|---|
| `globalToastContainer` | `showToast()` tu tao neu chua co |
| `museNotRunningNote` | guarded `if (settingsNote)` o `checkApiHealth()` |
| `promptSheetModal` | `openPromptSheetModal()` tu tao neu chua co |
| `sdAbCompareContainer` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdAbDivider` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdAbOverlay` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdAbRangeSlider` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdEditComplianceInput` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdEditDurationInput` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdEditLipSyncInput` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdEditModalEl` | chi dung `?.remove()` |
| `sdEditPromptInput` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdEditSpeakerInput` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdEditTransitionInput` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdEditVoiceInput` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdEditVoiceModeInput` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdEditVoiceNote` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdEditVoiceoverInput` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdFilterEmptyMsg` | `applyShotDeskFilter()` tu tao neu chua co |
| `sdHotkeysModalEl` | chi dung `?.remove()` |
| `sdLoopToggleBtn` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdNoteSavedHint` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdProjApplyAllShots` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdProjDefaultMode` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdProjDefaultVoice` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdProjVoiceModalEl` | chi dung `?.remove()` |
| `sdReviewDecisionArea` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdReviewDeskAudioEl` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdReviewVideoPlayer` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdReviewWaveWidget` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdSeqCounter` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdSeqShotBadge` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdSeqShotTitle` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdSeqVideo` | tao dong trong template `innerHTML` cua Shot Desk (review/edit/sequential/A-B compare) |
| `sdSequentialModal` | tao/xoa dong trong sequential player |
