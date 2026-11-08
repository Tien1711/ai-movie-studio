/* AI Movie Studio — app logic (tach tu static/index.html; giu nguyen id/ham/endpoint).
   Patch refactor 2026-11-07: renderSceneItemHtml/updateSceneCard + chunked renderScenes,
   polling dung khi tab an, img loading=lazy. Xem static/README.md. */
// ── Config & State ──────────────────────────────────────────────────────
const CFG_KEY = 'aiMovieStudio_cfg';
let cfg = {
  apiUrl: 'http://localhost:18610',
  apiKey: 'm2a_your_secret_key',
  concurrent: 12,
  aspectRatio: '16:9',
};
let scenes = [];   // { id, title, prompt, voiceover, duration, size }
let sceneIdCounter = 0;
let activeJobId = null;
let sseSource = null;

// ── apiFetch: fetch() dùng chung cho mọi gọi /api/* ─────────────────────
// Tự gắn header X-API-Key lấy từ localStorage key 'ams-api-key' (chỉ gắn
// khi có key). Giữ nguyên mọi options khác, trả Response như fetch gốc.
// BẢO MẬT: key lưu ở localStorage nên chỉ dùng cho tool cá nhân / trình
// duyệt tin cậy, KHÔNG dùng chung máy công cộng. SSE (/api/jobs/{id}/sse)
// vẫn miễn auth nên EventSource giữ nguyên, không qua apiFetch.
const STUDIO_API_KEY_STORAGE = 'ams-api-key';
let _apiKeyModalShown = false; // chống mở modal nhập key lặp trong cùng phiên

function getStudioApiKey() {
  try { return localStorage.getItem(STUDIO_API_KEY_STORAGE) || ''; } catch { return ''; }
}

// UI (static/js/ui.js) gọi hàm này sau khi lưu key để modal 401 hiện lại
// được nếu key mới cũng bị từ chối.
function resetApiKeyModalFlag() { _apiKeyModalShown = false; }

async function apiFetch(url, options = {}) {
  const key = getStudioApiKey();
  const headers = new Headers(options.headers || {});
  if (key) headers.set('X-API-Key', key);
  const res = await window.fetch(url, { ...options, headers });
  if (res.status === 401) {
    // Key đã lưu (nếu có) vừa bị server từ chối -> xóa để lần sau nhập lại.
    if (key) { try { localStorage.removeItem(STUDIO_API_KEY_STORAGE); } catch {} }
    // Hiện modal nhập key MỘT lần; vẫn trả Response 401 về cho caller
    // (không tự retry vô hạn, không nuốt lỗi).
    if (!_apiKeyModalShown && typeof window.showApiKeyModal === 'function') {
      _apiKeyModalShown = true;
      window.showApiKeyModal();
    }
    showToast('Cần nhập Khóa API Studio (X-API-Key) để tiếp tục.', 'warning');
  }
  return res;
}

// Kiểm tra server có bắt buộc X-API-Key không; nếu có mà trình duyệt chưa
// lưu key thì hiện modal nhập key ngay khi load trang (không chặn các load
// khác). /api/auth/status được miễn auth nên luôn trả 200.
async function checkStudioAuthStatus() {
  try {
    const r = await apiFetch('/api/auth/status');
    const data = await r.json();
    if (data && data.auth_required && !getStudioApiKey()) {
      // setTimeout: init() chạy trong lúc app.js parse, ui.js (định nghĩa
      // window.showApiKeyModal) chạy ngay sau đó — đợi 1 tick cho chắc.
      setTimeout(() => { if (typeof window.showApiKeyModal === 'function') window.showApiKeyModal(); }, 0);
    }
  } catch { /* lỗi mạng: bỏ qua, các load khác tự báo lỗi */ }
}

// Gọi lại các hàm load chính sau khi lưu key mới (không reload cả trang).
async function reloadStudioDataAfterKeySave() {
  _apiKeyModalShown = false;
  try { if (typeof loadVoices === 'function') await loadVoices(); } catch {}
  try { if (typeof loadCharacterLibrary === 'function') await loadCharacterLibrary(); } catch {}
  try { if (typeof loadCharacters === 'function') await loadCharacters(); } catch {}
  try { checkApiHealth(); } catch {}
  try {
    if (document.getElementById('panel-library')?.classList.contains('active')) loadLibrary();
  } catch {}
}

function showToast(msg, type = 'info', duration = 3000) {
  if (!msg) return;
  let toastContainer = document.getElementById('globalToastContainer');
  if (!toastContainer) {
    toastContainer = document.createElement('div');
    toastContainer.id = 'globalToastContainer';
    toastContainer.className = 'toast-container'; // vi tri/style o css/ui.css (.toast-container)
    document.body.appendChild(toastContainer);
  }

  const toast = document.createElement('div');
  toast.className = 'toast' + (type === 'error' ? ' toast-error' : type === 'warning' ? ' toast-warning' : '');
  const icon = type === 'error' ? '❌' : type === 'warning' ? '⚠️' : '✨';
  const cleanMsg = (typeof esc === 'function') ? esc(msg) : String(msg).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  toast.innerHTML = `<span>${icon}</span><span class="toast-msg">${cleanMsg}</span>`;
  toastContainer.appendChild(toast);

  requestAnimationFrame(() => {
    toast.style.opacity = '1';
    toast.style.transform = 'translateY(0)';
  });

  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(-8px)';
    setTimeout(() => { toast.remove(); }, 250);
  }, duration);
}

function applyGlobalAspectRatio(val, forceAll = false) {
  if (!val) return;
  cfg.aspectRatio = val;
  try {
    const saved = JSON.parse(localStorage.getItem(CFG_KEY) || '{}');
    saved.aspectRatio = val;
    localStorage.setItem(CFG_KEY, JSON.stringify(saved));
  } catch {}

  const sel = document.getElementById('globalAspectRatio');
  if (sel && sel.value !== val) sel.value = val;

  if (forceAll) {
    syncScenesFromDOM();
    scenes.forEach(s => {
      s.size = val;
    });
    renderScenes();
    const btn = event?.target;
    if (btn && btn.tagName === 'BUTTON') {
      const orig = btn.textContent;
      btn.textContent = '✅ Đã đổi xong!';
      setTimeout(() => { btn.textContent = orig; }, 1500);
    }
  }
}

function updateConcurrent(val) {
  const n = Math.max(1, Math.min(30, parseInt(val) || 1));
  cfg.concurrent = n;
  const el1 = document.getElementById('maxConcurrent');
  const el2 = document.getElementById('cfgConcurrent');
  const side = document.getElementById('sidebarConcurrent');
  if (el1 && el1.value != n) el1.value = n;
  if (el2 && el2.value != n) el2.value = n;
  if (side) side.textContent = `Concurrent: ${n}`;
  try {
    const saved = JSON.parse(localStorage.getItem(CFG_KEY) || '{}');
    saved.concurrent = n;
    localStorage.setItem(CFG_KEY, JSON.stringify(saved));
  } catch {}
}

function loadCfg() {
  try {
    const saved = JSON.parse(localStorage.getItem(CFG_KEY) || '{}');
    Object.assign(cfg, saved);
  } catch {}
  if (!cfg.concurrent || cfg.concurrent < 1) cfg.concurrent = 6;
  if (!cfg.aspectRatio) cfg.aspectRatio = '16:9';
  document.getElementById('cfgApiUrl').value = cfg.apiUrl;
  document.getElementById('cfgApiKey').value = cfg.apiKey;
  document.getElementById('cfgConcurrent').value = cfg.concurrent;
  document.getElementById('maxConcurrent').value = cfg.concurrent;
  document.getElementById('sidebarConcurrent').textContent = `Concurrent: ${cfg.concurrent}`;
  const ratioEl = document.getElementById('globalAspectRatio');
  if (ratioEl && cfg.aspectRatio) ratioEl.value = cfg.aspectRatio;
}

function saveConfig() {
  cfg.apiUrl = document.getElementById('cfgApiUrl').value.trim();
  cfg.apiKey = document.getElementById('cfgApiKey').value.trim();
  const c = parseInt(document.getElementById('cfgConcurrent').value) || 12;
  updateConcurrent(c);
  document.getElementById('cfgTestResult').textContent = '✅ Đã lưu cấu hình!';
  setTimeout(() => { document.getElementById('cfgTestResult').textContent = ''; }, 2000);
}

// ── Navigation ──────────────────────────────────────────────────────────
function showPanel(name) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById(`panel-${name}`)?.classList.add('active');
  document.getElementById(`nav-${name}`)?.classList.add('active');
  if (name === 'library') loadLibrary();
  if (name === 'characters') loadCharacters();
  if (name === 'shot-desk') initShotDeskStudio();
  if (name === 'affiliate') initAffiliateStudio();
}

// ── API Health ──────────────────────────────────────────────────────────
async function checkApiHealth() {
  const bar = document.getElementById('museStatusBar');
  const txt = document.getElementById('museStatusText');
  const badge = document.getElementById('museAccountsBadge');
  const resEl = document.getElementById('cfgTestResult');

  txt.textContent = 'Muse2API: Dang kiem tra...';
  bar.className = 'muse-status-bar warn';
  badge.style.display = 'none';

  try {
    const r = await apiFetch('/api/health');
    const data = await r.json();

    if (data.muse2api) {
      window.currentAccountsCount = data.accounts || 1;
      bar.className = 'muse-status-bar ok';
      txt.textContent = 'Muse2API: Đã kết nối';
      badge.textContent = data.accounts + ' tài khoản';
      badge.style.display = 'inline';
      if (resEl) resEl.textContent = 'Đã kết nối - ' + data.accounts + ' tài khoản hoạt động';
      const hint = document.getElementById('concurrentHint');
      if (hint) {
        hint.textContent = `⚡ Có ${data.accounts} tài khoản Muse — Tự động chia đều chạy song song qua ${data.accounts} tài khoản!`;
      }

      // Tự động cập nhật input Render song song và cấu hình nếu chưa chỉnh thủ công
      const maxConcInput = document.getElementById('maxConcurrent');
      if (maxConcInput && (!maxConcInput.value || parseInt(maxConcInput.value) < data.accounts)) {
        maxConcInput.value = data.accounts;
        maxConcInput.max = Math.max(16, data.accounts);
        updateConcurrent(data.accounts);
      }

      // Cập nhật select Shot Desk
      const sdSelect = document.getElementById('sdConcurrentSelect');
      if (sdSelect) {
        let optFound = false;
        for (let opt of sdSelect.options) {
          if (parseInt(opt.value) === data.accounts) {
            opt.selected = true;
            optFound = true;
            break;
          }
        }
        if (!optFound) {
          const newOpt = new Option(`⚡ ${data.accounts} luồng (Toàn bộ ${data.accounts} tài khoản)`, data.accounts, true, true);
          sdSelect.add(newOpt, 0);
        }
      }
    } else {
      bar.className = 'muse-status-bar fail';
      txt.textContent = 'Muse2API: Chua chay';
      badge.style.display = 'none';
      if (resEl) resEl.textContent = 'LOI: ' + (data.muse_error || 'Khong ket noi duoc');
      // Hiển thị hướng dẫn khởi động
      const settingsNote = document.getElementById('museNotRunningNote');
      if (settingsNote) settingsNote.style.display = 'block';
    }
  } catch (e) {
    bar.className = 'muse-status-bar fail';
    txt.textContent = 'Studio: Loi ket noi';
  }
}

// ── Scene management ────────────────────────────────────────────────────
let VOICES = [];  // Loaded from /api/voices
// Màu avatar cho nhân vật (luân phiên)
const CHAR_COLORS = ['#818cf8','#f472b6','#34d399','#fbbf24','#a78bfa','#fb923c','#e879f9'];

async function loadVoices() {
  try {
    const r = await apiFetch('/api/voices');
    VOICES = await r.json();
    populateAllVoiceDropdowns();
  } catch (err) {
    console.error("loadVoices error:", err);
    VOICES = [];
  }
}

function populateAllVoiceDropdowns() {
  // 1. Dropdown chính ở giao diện Studio
  const vSel = document.getElementById('voiceSelect');
  if (vSel && VOICES.length > 0) {
    const curVal = vSel.value || 'vieneu:TrucLy';
    vSel.innerHTML = voiceSelectHtml(curVal);
    if ([...vSel.options].some(o => o.value === curVal)) {
      vSel.value = curVal;
    }
  }

  // 2. Dropdown giọng nhân vật ở Quản lý nhân vật (Character Library)
  const charSel = document.getElementById('charVoice');
  if (charSel && VOICES.length > 0) {
    const curChar = charSel.value || 'vieneu:TrucLy';
    charSel.innerHTML = voiceSelectHtml(curChar);
    if ([...charSel.options].some(o => o.value === curChar)) {
      charSel.value = curChar;
    }
  }

  // 4. Dropdown giọng tiếp thị Affiliate
  const affSel = document.getElementById('affiliateVoiceSelect');
  if (affSel && VOICES.length > 0) {
    const curAff = affSel.value || 'vieneu:AdamBua';
    affSel.innerHTML = voiceSelectHtml(curAff);
    if ([...affSel.options].some(o => o.value === curAff)) {
      affSel.value = curAff;
    }
  }

  // 3. Dropdown các cảnh hiện có trên màn hình Studio
  document.querySelectorAll('.scene-voice-sel').forEach(sel => {
    const cur = sel.value;
    sel.innerHTML = `<option value="">-- Dùng giọng mặc định toàn phim --</option>` + voiceSelectHtml(cur);
    if (cur) sel.value = cur;
  });
}

function voiceSelectHtml(selectedId = '') {
  const grouped = {};
  VOICES.forEach(v => {
    if (!grouped[v.lang]) grouped[v.lang] = [];
    grouped[v.lang].push(v);
  });
  const langName = { 
    ai: '🤖 Giọng Gốc AI (Muse Native Speech)', 
    vieneu: '⚡ VieNeu-TTS (On-Device Neural Tiếng Việt)',
    omnivoice: '🌐 OmniVoice (k2-fsa Đa Ngôn Ngữ & Voice Design)',
    vi: '☁️ Edge-TTS Tiếng Việt (Cloud)', 
    en: '🇬🇧 Tiếng Anh (English)', 
    zh: '🇨🇳 Tiếng Trung (Chinese)', 
    ja: '🇯🇵 Tiếng Nhật (Japanese)', 
    ko: '🇰🇷 Tiếng Hàn (Korean)', 
    th: '🇹🇭 Tiếng Thái (Thai)' 
  };
  return Object.entries(grouped).map(([lang, voices]) =>
    `<optgroup label="${langName[lang]||lang}">${
      voices.map(v => {
        if (v.lang === 'ai') return `<option value="${v.id}" ${v.id===selectedId?'selected':''}>🤖 ${v.label}</option>`;
        const prefix = v.gender === 'female' ? '👩 Nữ' : (v.gender === 'male' ? '👨 Nam' : '🎙️');
        return `<option value="${v.id}" ${v.id===selectedId?'selected':''}>${prefix} - ${v.label}</option>`;
      }).join('')
    }</optgroup>`
  ).join('');
}

function onGlobalVoiceModeChanged() {
  const mode = document.getElementById('voiceModeSelect')?.value || 'tts';
  const notice = document.getElementById('aiVoiceNoticeBar');
  const vSel = document.getElementById('voiceSelect');
  const vLbl = document.getElementById('voiceSelectLabel');
  if (notice) notice.style.display = (mode === 'ai_voice') ? 'flex' : 'none';
  if (mode === 'ai_voice') {
    if (vSel && vSel.value !== 'ai_voice') vSel.value = 'ai_voice';
    if (vLbl) vLbl.textContent = 'Âm thanh AI';
  } else {
    if (vSel && vSel.value === 'ai_voice') vSel.value = 'vi-VN-HoaiMyNeural';
    if (vLbl) vLbl.textContent = 'Giọng TTS mặc định';
  }
}

function onVoiceSelectChanged() {
  const val = document.getElementById('voiceSelect')?.value;
  const mSel = document.getElementById('voiceModeSelect');
  if (val === 'ai_voice') {
    if (mSel) mSel.value = 'ai_voice';
    onGlobalVoiceModeChanged();
  } else if (mSel && mSel.value === 'ai_voice') {
    mSel.value = 'tts';
    onGlobalVoiceModeChanged();
  }
}

function addScene(prompt = '', voiceover = '', duration = 5, size = '', title = '', voice = '', voiceSegments = [], reference_image = '', reference_image_name = '', chain_from_previous = false, sheet_angle = 'front') {
  const id = ++sceneIdCounter;
  const globalRatio = document.getElementById('globalAspectRatio')?.value || cfg.aspectRatio || '16:9';
  const finalSize = size || globalRatio;
  const newScene = {
    id,
    title: title || `Cảnh ${scenes.length + 1}`,
    prompt,
    voiceover,
    voice,          // Per-scene voice override
    voiceSegments,  // Multi-character voice segments
    voiceMode: voiceSegments.length > 0 ? 'multi' : 'single',
    duration,
    size: finalSize,
    reference_image, // Ảnh tham chiếu nhân vật (URL hoặc Base64 Data URL)
    reference_image_name: reference_image_name || (reference_image ? 'Ảnh tham chiếu' : ''),
    chain_from_previous: chain_from_previous || false,
    sheet_angle: sheet_angle || 'front',
  };
  if (typeof autoLinkCharacterToScene === 'function') {
    autoLinkCharacterToScene(newScene);
  }
  scenes.push(newScene);
  renderScenes();
}

function removeScene(id) {
  scenes = scenes.filter(s => s.id !== id);
  renderScenes();
}

function toggleVoiceMode(sceneId, mode) {
  syncScenesFromDOM();
  const s = scenes.find(s => s.id === sceneId);
  if (s) s.voiceMode = mode;
  if (!updateSceneCard(sceneId)) renderScenes();
}

function addCharSegment(sceneId) {
  syncScenesFromDOM();
  const s = scenes.find(s => s.id === sceneId);
  if (!s) return;
  const defVoice = VOICES[0]?.id || 'vi-VN-HoaiMyNeural';
  s.voiceSegments = s.voiceSegments || [];
  s.voiceSegments.push({ character: 'Nhân vật ' + (s.voiceSegments.length + 1), text: '', voice: defVoice });
  if (!updateSceneCard(sceneId)) renderScenes();
}

function removeCharSegment(sceneId, segIdx) {
  syncScenesFromDOM();
  const s = scenes.find(s => s.id === sceneId);
  if (!s) return;
  s.voiceSegments.splice(segIdx, 1);
  if (!updateSceneCard(sceneId)) renderScenes();
}

function getSceneFromDOM(id) {
  const el = document.getElementById(`scene-${id}`);
  if (!el) return null;

  const voiceMode = el.querySelector('.voice-tab.active')?.dataset.mode || 'single';

  // Lấy voice segments
  const voiceSegments = [];
  el.querySelectorAll('.char-seg-item').forEach(seg => {
    voiceSegments.push({
      character: seg.querySelector('.seg-char-name')?.value || '',
      text: seg.querySelector('.seg-text')?.value || '',
      voice: seg.querySelector('.seg-voice')?.value || '',
    });
  });

  const chainChk = el.querySelector('.scene-chain-chk');
  const chain_from_previous = chainChk ? chainChk.checked : false;
  const sheet_angle = el.querySelector('.scene-sheet-angle-val')?.value || 'front';

  return {
    prompt:               el.querySelector('.scene-prompt')?.value || '',
    voiceover:            el.querySelector('.scene-voice')?.value || '',
    voice:                el.querySelector('.scene-voice-sel')?.value || '',
    duration:             parseInt(el.querySelector('.scene-duration')?.value) || 5,
    size:                 el.querySelector('.scene-size')?.value || '16:9',
    title:                el.querySelector('.scene-title-input')?.value || '',
    reference_image:      el.querySelector('.scene-ref-img-val')?.value.trim() || el.querySelector('.scene-ref-img')?.value.trim() || '',
    reference_image_name: el.querySelector('.scene-ref-img-name')?.value || '',
    voiceMode,
    voiceSegments,
    chain_from_previous,
    sheet_angle,
  };
}

function syncScenesFromDOM() {
  scenes = scenes.map(s => {
    const vals = getSceneFromDOM(s.id);
    return vals ? { ...s, ...vals } : s;
  });
}

function renderSceneItemHtml(s, i) {

    const vm = s.voiceMode || 'single';
    const segs = s.voiceSegments || [];
    const defVoice = VOICES[0]?.id || 'vi-VN-HoaiMyNeural';

    // Character segments HTML
    const segsHtml = segs.map((seg, si) => {
      const color = CHAR_COLORS[si % CHAR_COLORS.length];
      const gender = VOICES.find(v=>v.id===seg.voice)?.gender || 'female';
      return `
        <div class="char-seg-item">
          <div>
            <div class="fx fx-ac fx-g5 u-mb4">
              <div class="dot" style="background:${color};width:10px;height:10px;border-radius:50%;flex-shrink:0;"></div>
              <span class="gender-badge ${gender}">${gender==='female'?'Nu':'Nam'}</span>
            </div>
            <input class="seg-char-name" style="width:100%;background:var(--bg0);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:4px 6px;font-size:12px;"
              value="${esc(seg.character)}" placeholder="Ten nhan vat...">
          </div>
          <div>
            <textarea class="seg-text" rows="2" style="width:100%;background:var(--bg0);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:4px 6px;font-size:12px;resize:vertical;"
              placeholder="Loi thoai cua nhan vat...">${esc(seg.text)}</textarea>
          </div>
          <div>
            <div class="fx fx-g4 fx-ac">
              <select class="seg-voice" style="width:100%;background:var(--bg0);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:4px 6px;font-size:12px;">
                ${voiceSelectHtml(seg.voice)}
              </select>
              <button type="button" class="btn btn-ghost btn-sm u-p-3-6" title="Nghe thử giọng nhân vật này" onclick="testPlayVoice(this.previousElementSibling.value, this.closest('.char-seg-item').querySelector('.seg-text').value)">🔊</button>
            </div>
          </div>
          <div class="fx fx-ac fx-jc-c">
            <button class="btn btn-ghost btn-sm u-c-red u-p-4-8" onclick="removeCharSegment(${s.id},${si})">✕</button>
          </div>
        </div>`;
    }).join('');

    // Render Reference Image Upload / Preview component
    let refImgHtml = '';
    const hasRef = !!(s.reference_image && s.reference_image.trim());
    const refName = s.reference_image_name || (hasRef ? 'Ảnh tham chiếu' : '');

    const libOptions = characterLibrary && characterLibrary.length > 0
      ? `<select class="btn btn-ghost btn-sm" style="font-size:11px;padding:3px 6px;max-width:150px;background:var(--bg0);color:var(--text);border:1px solid var(--border);" onchange="applyRecentCharImage(${s.id}, this.value); this.selectedIndex=0;">
           <option value="">📂 Dùng lại ảnh cũ...</option>
           ${characterLibrary.map(c => `<option value="${esc(c.url)}">${esc(c.filename)}</option>`).join('')}
         </select>`
      : '';

    if (hasRef) {
      refImgHtml = `
        <div class="ref-img-preview-box">
          <img loading="lazy" decoding="async" src="${esc(s.reference_image)}" class="ref-img-thumb" alt="Tham chiếu nhân vật" onclick="triggerSceneUpload(${s.id})" title="Bấm để đổi ảnh khác">
          <div class="fx-f1">
            <div class="u-fs12 u-fw600 u-c-accent fx fx-ac fx-g6">
              <span>🔒 Đã khóa nhân vật theo ảnh này</span>
            </div>
            <div style="font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px;">
              ${esc(refName)}
            </div>
          </div>
          <div class="fx fx-g6 fx-ac">
            ${libOptions}
            <button type="button" class="btn btn-ghost btn-sm u-fs11 u-p-4-8" onclick="triggerSceneUpload(${s.id})">🔄 Đổi ảnh</button>
            <button type="button" class="btn btn-ghost btn-sm u-fs11 u-p-4-8 u-c-red" onclick="clearSceneImage(${s.id})">🗑️ Xóa</button>
          </div>
          <input type="hidden" class="scene-ref-img-val" value="${esc(s.reference_image)}">
          <input type="hidden" class="scene-ref-img-name" value="${esc(refName)}">
          <input type="file" id="scene-file-${s.id}" accept="image/*" style="display:none;" onchange="handleSceneFileUpload(${s.id}, this)">
        </div>`;
    } else {
      refImgHtml = `
        <div class="ref-img-upload-zone" id="ref-zone-${s.id}"
             onclick="triggerSceneUpload(${s.id})"
             ondragover="handleDragOver(event, ${s.id})"
             ondragleave="handleDragLeave(event, ${s.id})"
             ondrop="handleSceneDrop(event, ${s.id})">
          <div class="fx fx-ac fx-g10">
            <div style="width:36px;height:36px;border-radius:6px;background:var(--bg2);display:flex;align-items:center;justify-content:center;font-size:18px;">📁</div>
            <div>
              <div class="u-fs12 u-fw600 u-c-text">
                <span class="u-c-accent">Tải ảnh nhân vật từ máy tính</span> hoặc kéo thả vào đây
              </div>
              <div class="u-fs11 u-c-muted u-mt2">Hỗ trợ PNG, JPG, WEBP (Để khóa khuôn mặt / trang phục) hoặc dán Clipboard (Ctrl+V)</div>
            </div>
          </div>
          <div class="fx fx-g6 fx-ac" onclick="event.stopPropagation();">
            ${libOptions}
            <button type="button" class="btn btn-ghost btn-sm u-fs11 u-p-3-8" onclick="promptImageUrl(${s.id})">🌐 Dán link</button>
          </div>
          <input type="hidden" class="scene-ref-img-val" value="">
          <input type="hidden" class="scene-ref-img-name" value="">
          <input type="file" id="scene-file-${s.id}" accept="image/*" style="display:none;" onchange="handleSceneFileUpload(${s.id}, this)">
        </div>`;
    }

    const matchedChar = (typeof detectCharactersInScene === 'function') ? detectCharactersInScene(s) : null;
    let charMatchBannerHtml = '';
    if (matchedChar) {
      const sheet = matchedChar.character_sheet || {};
      const angles = [
        { key: 'front', label: '👤 Chính diện' },
        { key: 'side', label: '↗️ Góc nghiêng' },
        { key: 'closeup', label: '🔍 Cận mặt' },
        { key: 'fullbody', label: '🧍 Toàn thân' }
      ];
      const curAngle = s.sheet_angle || 'front';
      const angleButtonsHtml = angles.map(ang => {
        const hasImg = !!sheet[ang.key];
        const isSel = curAngle === ang.key;
        return `
          <button type="button" class="btn btn-ghost btn-sm"
            style="font-size:10.5px;padding:2px 7px;${isSel ? 'border-color:var(--accent);color:var(--accent);background:rgba(99,102,241,0.14);font-weight:700;' : ''}${!hasImg ? 'opacity:0.6;' : ''}"
            onclick="applyCharSheetAngle(${s.id}, '${matchedChar.id}', '${ang.key}')"
            title="${hasImg ? `Khóa góc ${ang.label} từ Character Sheet` : `Góc này chưa có ảnh trong Sheet - bấm để tạo hoặc dùng ảnh avatar`}">
            ${ang.label} ${hasImg ? '✓' : ''}
          </button>`;
      }).join('');

      charMatchBannerHtml = `
        <div class="char-match-banner" id="scene-char-match-${s.id}">
          <div style="width:36px;height:36px;border-radius:50%;overflow:hidden;background:var(--bg2);flex-shrink:0;border:2px solid var(--accent);display:flex;align-items:center;justify-content:center;">
            ${matchedChar.avatar ? `<img loading="lazy" decoding="async" src="${esc(matchedChar.avatar)}" style="width:100%;height:100%;object-fit:cover;">` : `<span class="u-fs18">👤</span>`}
          </div>
          <div class="fx-f1">
            <div class="u-fs125 u-fw700 u-c-text fx fx-ac fx-g6 fx-wrap">
              <span>🎯 Đã nhận diện nhân vật: <b class="u-c-accent">${esc(matchedChar.name)}</b></span>
              <span class="pill running u-fs10 u-p-1-6">Tự động tham chiếu</span>
            </div>
            <div class="u-fs11 u-c-muted u-mt2">
              🎙️ Giọng: <b>${esc(matchedChar.voice || 'Mặc định')}</b> ${matchedChar.description ? `• Tạo hình: ${esc(matchedChar.description.slice(0, 50))}...` : ''}
            </div>
            <div class="fx fx-ac fx-g5 fx-wrap u-mt5">
              <span class="u-fs11 u-c-muted u-fw600">Góc quay Bible:</span>
              ${angleButtonsHtml}
            </div>
          </div>
          <div class="fx fx-g5 fx-ac fx-wrap">
            ${matchedChar.avatar ? `
              <button type="button" class="btn btn-ghost btn-sm u-fs11 u-p-2-7 u-c-accent" onclick="applyCharToScene(${s.id}, '${matchedChar.id}', 'image')" title="Khóa ảnh tham chiếu này cho cảnh">🔒 Khóa ảnh</button>
            ` : ''}
            ${matchedChar.description ? `
              <button type="button" class="btn btn-ghost btn-sm u-fs11 u-p-2-7 u-c-green" onclick="applyCharToScene(${s.id}, '${matchedChar.id}', 'desc')" title="Chèn mô tả tạo hình vào Prompt">➕ Thêm mô tả</button>
            ` : ''}
            <button type="button" class="btn btn-ghost btn-sm u-fs11 u-p-2-7" onclick="applyCharToScene(${s.id}, '${matchedChar.id}', 'all')" title="Đồng bộ ảnh, giọng nói và mô tả">⚡ Áp dụng tất cả</button>
          </div>
        </div>`;
    } else {
      charMatchBannerHtml = `<div id="scene-char-match-${s.id}"></div>`;
    }

    const chainBarHtml = i > 0 ? `
      <div class="scene-chain-bar">
        <label style="display:flex;align-items:center;gap:6px;font-size:12px;font-weight:600;color:var(--yellow);cursor:pointer;margin-bottom:0;" title="Dùng khung hình cuối cùng (last frame) của cảnh trước làm khung hình đầu (first frame) của cảnh này">
          <input type="checkbox" class="scene-chain-chk u-ac-yellow" ${s.chain_from_previous ? 'checked' : ''} onchange="toggleSceneChaining(${s.id}, this.checked)">
          <span>🔗 Nối tiếp khung hình từ Cảnh ${i} (First/Last Frame Chaining)</span>
        </label>
        <span class="scene-chain-badge">Khóa chuyển động liền mạch</span>
      </div>` : '';

    return `
    <div class="scene-item" id="scene-${s.id}" draggable="true" ondragstart="handleSceneDragStart(event, ${s.id})" ondragover="handleSceneDragOver(event)" ondrop="handleSceneDropReorder(event, ${s.id})">
      <input type="hidden" class="scene-sheet-angle-val" value="${esc(s.sheet_angle || 'front')}">
      <div class="scene-item-header">
        <div class="drag-handle" title="Kéo thả để đổi thứ tự cảnh">⋮⋮</div>
        <div class="scene-num">${i + 1}</div>
        <input class="scene-title-input" value="${esc(s.title)}" placeholder="Tiêu đề cảnh..." oninput="onSceneContentInput(${s.id})">
        <div style="display:flex;gap:6px;margin-left:auto;">
          ${i > 0 ? `<button class="btn btn-ghost btn-sm" onclick="moveScene(${s.id},-1)">^</button>` : ''}
          ${i < scenes.length - 1 ? `<button class="btn btn-ghost btn-sm" onclick="moveScene(${s.id},1)">v</button>` : ''}
          ${window.TimelineEditor ? window.TimelineEditor.headerButtonsHtml(s, i) : ''}
          <button class="btn btn-ghost btn-sm u-c-red" onclick="removeScene(${s.id})">x</button>
        </div>
      </div>

      ${window.TimelineEditor ? window.TimelineEditor.cardExtraHtml(s, i) : ''}
      ${chainBarHtml}

      ${charMatchBannerHtml}

      <!-- Keyframe Preview Box (Nếu đã sinh ảnh keyframe) -->
      <div id="scene-keyframe-box-${s.id}" style="${s.keyframe_url ? 'display:block;' : 'display:none;'}margin-bottom:8px;">
        ${s.keyframe_url ? `
          <div class="keyframe-preview-card">
            <img loading="lazy" decoding="async" src="${esc(s.keyframe_url)}" class="keyframe-preview-thumb" alt="Keyframe Preview" onclick="window.open('${esc(s.keyframe_url)}')">
            <div class="fx-f1">
              <div class="u-fs12 u-fw700 u-c-accent">🖼️ Keyframe xem trước (Đã tạo bằng Muse Image)</div>
              <div class="u-fs11 u-c-muted u-mt2">Xem trước góc máy và tạo hình trước khi render video</div>
            </div>
            <div class="fx fx-g6">
              <button type="button" class="btn btn-primary btn-sm u-fs11" onclick="applyKeyframeAsRef(${s.id}, '${esc(s.keyframe_url)}')">🔒 Dùng làm ảnh tham chiếu</button>
              <button type="button" class="btn btn-ghost btn-sm u-fs11" onclick="closeSceneKeyframeBox(${s.id})">✕</button>
            </div>
          </div>
        ` : ''}
      </div>

      <div class="form-row u-mb8">
        <div>
          <div class="fx fx-jc-sb fx-ac u-mb4 fx-wrap fx-g6">
            <label class="u-mb0 u-fw600 u-c-text">🎬 Prompt mô tả cảnh quay</label>
            <div class="fx fx-g4 fx-wrap">
              <button type="button" class="btn btn-ghost btn-sm" style="font-size:11px;padding:2px 8px;color:var(--accent2);border:1px solid rgba(129,140,248,0.35);" onclick="previewSceneKeyframe(${s.id})" title="Sinh ảnh mẫu (Keyframe) bằng Muse Image trước khi tốn lượt render video">🖼️ Xem Keyframe</button>
              <button type="button" class="btn btn-ghost btn-sm u-fs11 u-p-2-8 u-c-yellow" onclick="sanitizePromptDirect(${s.id})" title="Tự động loại bỏ các từ khóa bạo lực, máu me có nguy cơ bị Muse AI từ chối">🛡️ Làm sạch từ cấm</button>
              <button type="button" class="btn btn-ghost btn-sm u-fs11 u-p-2-8 u-c-accent" onclick="autoEnhancePrompt(${s.id})">✨ Chuẩn hóa Điện ảnh</button>
            </div>
          </div>
          
          <!-- Thanh chọn nhanh các thông số đạo diễn -->
          <div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px;background:rgba(0,0,0,0.25);padding:6px;border-radius:6px;border:1px solid var(--border);">
            <select class="u-fs11 u-p-2-6 u-w-auto" onchange="insertTag(${s.id}, this.value); this.selectedIndex=0;">
              <option value="">🎥 Góc máy (Shot & Angle)...</option>
              <option value="Cinematic wide drone shot,">Flycam / Toàn cảnh (Wide Drone)</option>
              <option value="Cinematic close-up shot, 85mm lens,">Cận cảnh (Close-up 85mm)</option>
              <option value="Dramatic low angle shot looking up,">Góc thấp quyền lực (Low Angle)</option>
              <option value="Over-the-shoulder shot,">Góc qua vai đối thoại</option>
              <option value="Extreme close-up on eyes, macro lens,">Đặc tả ánh mắt (Macro Eye)</option>
              <option value="Dutch angle tilted shot, dynamic,">Góc nghiêng kịch tính (Dutch Angle)</option>
            </select>

            <select class="u-fs11 u-p-2-6 u-w-auto" onchange="insertTag(${s.id}, this.value); this.selectedIndex=0;">
              <option value="">💡 Ánh sáng (Lighting)...</option>
              <option value="Golden hour sunset sunlight, warm glow,">Hoàng hôn vàng (Golden Hour)</option>
              <option value="Volumetric lighting with fog and god rays,">Tia sáng xuyên sương mù (God Rays)</option>
              <option value="Cyberpunk neon rim lighting, high contrast,">Đèn viền Neon tương phản</option>
              <option value="Moody dramatic moonlight, dark shadows,">Ánh trăng huyền bí (Moonlight)</option>
              <option value="Studio softbox lighting, soft shadows,">Ánh sáng studio mịn màng</option>
              <option value="Eerie candle light flickering, dark atmosphere,">Ánh nến lung linh mờ ảo</option>
            </select>

            <select class="u-fs11 u-p-2-6 u-w-auto" onchange="insertTag(${s.id}, this.value); this.selectedIndex=0;">
              <option value="">🔄 Chuyển động (Movement)...</option>
              <option value="camera slowly zooming in,">Camera zoom từ từ vào chủ thể</option>
              <option value="camera slowly panning right,">Camera lia ngang sang phải</option>
              <option value="smooth tracking shot following subject,">Camera bám theo chuyển động</option>
              <option value="smooth orbit camera rotating around,">Camera quay vòng cung 360</option>
              <option value="slow motion 120fps, floating particles,">Chuyển động chậm (Slow-motion)</option>
              <option value="fpv drone fast fly through,">FPV bay lướt tốc độ cao</option>
            </select>

            <select class="u-fs11 u-p-2-6 u-w-auto" onchange="insertTag(${s.id}, this.value); this.selectedIndex=0;">
              <option value="">🎞️ Chất lượng (Cinema Look)...</option>
              <option value="photorealistic 8k, 35mm film grain, masterpiece.">35mm Film Grain chuẩn điện ảnh</option>
              <option value="anamorphic lens, cinematic bokeh, 4k.">Ống kính Anamorphic xóa phông</option>
              <option value="Unreal Engine 5 render, hyper-detailed.">Đồ họa 3D Unreal Engine 5</option>
              <option value="IMAX documentary quality, ultra-sharp.">Chuẩn tài liệu IMAX sắc nét</option>
            </select>
          </div>

          <textarea class="scene-prompt" rows="3" oninput="onSceneContentInput(${s.id})" placeholder="Nhập ý tưởng hoặc click các thông số điện ảnh phía trên để gắn trực tiếp vào prompt...">${esc(s.prompt)}</textarea>
        </div>
      </div>

      <div class="form-row cols-2 u-mb10">
        <div>
          <label>Thoi luong video (giay)</label>
          <select class="scene-duration">
            ${[5,6,8,10].map(d => `<option value="${d}" ${d==s.duration?'selected':''}>${d}s</option>`).join('')}
          </select>
        </div>
        <div>
          <label>Ti le khung hinh</label>
          <select class="scene-size">
            ${['16:9','9:16','1:1','21:9','4:3'].map(sz => `<option value="${sz}" ${sz==s.size?'selected':''}>${sz}</option>`).join('')}
          </select>
        </div>
      </div>

      <!-- Reference Image for Character Lock -->
      <div class="form-row u-mb10">
        <div class="u-w100">
          <div class="fx fx-jc-sb fx-ac u-mb4">
            <label class="u-mb0 u-fw600 u-c-text">🖼️ Ảnh tham chiếu nhân vật (Character Lock / First Frame)</label>
            <span class="u-fs11 u-c-muted">Tùy chọn: Để trống nếu là cảnh phong cảnh hoặc nhân vật phụ không cần khóa mặt</span>
          </div>
          ${refImgHtml}
        </div>
      </div>

      <!-- Voice mode tabs -->
      <label style="font-size:12px;color:var(--muted);font-weight:500;margin-bottom:6px;display:block;">Giọng thoại / Thuyết minh</label>
      <div class="voice-mode-tabs">
        <div class="voice-tab ${vm==='single'?'active':''}" data-mode="single" onclick="toggleVoiceMode(${s.id},'single')">Một giọng</div>
        <div class="voice-tab ${vm==='multi'?'active':''}" data-mode="multi" onclick="toggleVoiceMode(${s.id},'multi')">Nhiều nhân vật</div>
      </div>

      ${vm === 'single' ? `
        <div class="form-row cols-2 u-mb0">
          <div>
            <label>Lời thuyết minh</label>
            <input class="scene-voice" type="text" value="${esc(s.voiceover)}" placeholder="Nội dung đọc..." oninput="onSceneContentInput(${s.id})">
          </div>
          <div>
            <div class="fx fx-jc-sb fx-ac">
              <label class="u-mb0">Giọng đọc riêng cảnh này</label>
              <button type="button" class="btn btn-ghost btn-sm u-fs11 u-p-1-6 u-c-accent" onclick="testPlayVoice(document.querySelector('#scene-${s.id} .scene-voice-sel').value || document.getElementById('voiceSelect').value)">🔊 Nghe thử</button>
            </div>
            <select class="scene-voice-sel u-mt4">
              <option value="">-- Dùng giọng mặc định toàn phim --</option>
              ${voiceSelectHtml(s.voice)}
            </select>
          </div>
        </div>
      ` : `
        <input class="scene-voice" type="hidden" value="">
        <select class="scene-voice-sel" style="display:none;"></select>
        <div class="char-seg-list">${segsHtml}</div>
        <button class="btn btn-ghost btn-sm u-mt8 u-w100" onclick="addCharSegment(${s.id})">＋ Thêm nhân vật</button>
      `}
    </div>`;
  }

 // Cap nhat 1 card canh ma khong ve lai toan bo danh sach (giu trang thai cac card khac).
 // Tra ve false neu card khong co trong DOM (vd: dang render theo chunk) -> caller dung renderScenes().
function updateSceneCard(sceneId) {
  const idx = scenes.findIndex(s => s.id === sceneId);
  if (idx < 0) return false;
  const el = document.getElementById(`scene-${sceneId}`);
  if (!el) return false;
  const tpl = document.createElement('template');
  tpl.innerHTML = renderSceneItemHtml(scenes[idx], idx).trim();
  const next = tpl.content.firstElementChild;
  if (!next) return false;
  el.replaceWith(next);
  return true;
}

function renderScenes() {
  const list = document.getElementById('sceneList');
  if (!list) return;
  document.getElementById('sceneCountLabel').textContent =
    `Danh sach phan canh (${scenes.length} canh)`;

  const html = scenes.map((s, i) => renderSceneItemHtml(s, i));
  const token = (window.__renderScenesToken || 0) + 1;
  window.__renderScenesToken = token;
  if (scenes.length <= 24) {
    list.innerHTML = html.join('');
    return;
  }
  // Danh sach dai: ve 12 card dau ngay, cac chunk sau ve lan luot khi trinh duyet ranh
  // (token huy chunk cu neu co renderScenes() moi chen vao giua chung).
  const CHUNK = 12;
  list.innerHTML = html.slice(0, CHUNK).join('');
  let next = CHUNK;
  const schedule = (fn) => {
    if (window.requestIdleCallback) window.requestIdleCallback(fn, { timeout: 120 });
    else setTimeout(fn, 0);
  };
  const step = () => {
    if (token !== window.__renderScenesToken) return;
    const cur = document.getElementById('sceneList');
    if (!cur) return;
    const tpl = document.createElement('template');
    tpl.innerHTML = html.slice(next, next + CHUNK).join('');
    cur.append(...tpl.content.childNodes);
    next += CHUNK;
    if (next < html.length) schedule(step);
  };
  schedule(step);
}


function moveScene(id, dir) {
  const idx = scenes.findIndex(s => s.id === id);
  if (idx < 0) return;
  const newIdx = idx + dir;
  if (newIdx < 0 || newIdx >= scenes.length) return;
  // Sync trước khi move
  syncScenesFromDOM();
  [scenes[idx], scenes[newIdx]] = [scenes[newIdx], scenes[idx]];
  renderScenes();
}

function insertTag(sceneId, tag) {
  if (!tag) return;
  const el = document.querySelector(`#scene-${sceneId} .scene-prompt`);
  if (!el) return;
  let cur = el.value.trim();
  if (cur.length > 0 && !cur.endsWith(',') && !cur.endsWith('.')) {
    cur += ', ';
  } else if (cur.length > 0) {
    cur += ' ';
  }
  el.value = cur + tag + ' ';
  syncScenesFromDOM();
}

function autoEnhancePrompt(sceneId) {
  const el = document.querySelector(`#scene-${sceneId} .scene-prompt`);
  if (!el) return;
  let cur = el.value.trim();
  if (!cur) {
    alert("Vui lòng nhập ý tưởng cơ bản trước khi bấm chuẩn hóa!");
    return;
  }

  // Tiền tố điện ảnh nếu chưa có
  if (!cur.toLowerCase().includes("cinematic") && !cur.toLowerCase().includes("shot")) {
    cur = "Cinematic shot, " + cur;
  }
  // Thêm chuyển động máy nếu chưa có
  if (!cur.toLowerCase().includes("camera") && !cur.toLowerCase().includes("zoom") && !cur.toLowerCase().includes("pan")) {
    cur += ", camera slowly zooming in";
  }
  // Thêm ánh sáng nếu chưa có
  if (!cur.toLowerCase().includes("light") && !cur.toLowerCase().includes("sun") && !cur.toLowerCase().includes("glow")) {
    cur += ", dramatic volumetric lighting";
  }
  // Thêm chất lượng phim nếu chưa có
  if (!cur.toLowerCase().includes("4k") && !cur.toLowerCase().includes("8k") && !cur.toLowerCase().includes("film")) {
    cur += ", 35mm film grain, photorealistic 8k, masterpiece.";
  }

  el.value = cur;
  syncScenesFromDOM();
}

function clearAll() {
  if (scenes.length && !confirm('Xóa toàn bộ cảnh?')) return;
  scenes = [];
  sceneIdCounter = 0;
  renderScenes();
}

function loadDialogueSample() {
  clearAll();
  document.getElementById('projectTitle').value = "Gặp Gỡ Định Mệnh";
  // Cảnh 1: Đối thoại 2 nhân vật Nam và Nữ
  addScene(
    "Cinematic close-up of a handsome young man standing in autumn rain, looking surprised, soft cinematic lighting, 4k",
    "",
    5,
    "16:9",
    "Cảnh 1: Cuộc chạm trán",
    "",
    [
      { character: "Minh (Nam)", text: "Nàng thực sự đã quay trở lại sao?", voice: "vi-VN-NamMinhNeural" },
      { character: "Lan (Nữ)", text: "Ta chưa từng rời đi, chỉ là chàng không nhận ra thôi.", voice: "vi-VN-HoaiMyNeural" }
    ]
  );
  // Cảnh 2: Khung cảnh hoàng hôn và lời hứa
  addScene(
    "Cinematic wide shot of two silhouettes standing on a bridge at sunset, river reflecting warm golden light, cinematic masterpiece",
    "",
    5,
    "16:9",
    "Cảnh 2: Lời ước hẹn",
    "",
    [
      { character: "Minh (Nam)", text: "Từ nay về sau, ta sẽ cùng nàng đi hết nhân gian này.", voice: "vi-VN-NamMinhNeural" },
      { character: "Lan (Nữ)", text: "Dù phong ba bão táp, thiếp cũng nguyện theo chàng.", voice: "vi-VN-HoaiMyNeural" }
    ]
  );
}

function loadTiktokSample() {
  clearAll();
  document.getElementById('projectTitle').value = "Bí Ẩn Vũ Trụ Cyberpunk";
  document.getElementById('exportMode').value = "full";
  addScene(
    "Vertical 9:16 shot, neon cyberpunk city in rain, flying cars, glowing holographic billboards, ultra realistic 4k",
    "Chào mừng bạn đến với Neo Sài Gòn năm 2088, nơi công nghệ thay đổi mọi quy luật.",
    5,
    "9:16",
    "Cảnh 1: Thành phố tương lai",
    "vi-VN-NamMinhNeural"
  );
  addScene(
    "Vertical 9:16 shot, cyborg girl with glowing blue eyes turning head toward camera, dramatic neon lighting, cinematic",
    "Và tại đây, ranh giới giữa con người và trí tuệ nhân tạo đã hoàn toàn biến mất.",
    5,
    "9:16",
    "Cảnh 2: Thức tỉnh",
    "vi-VN-HoaiMyNeural"
  );
}

function loadSampleStoryboard() {
  clearAll();
  document.getElementById('projectTitle').value = "Huyền Thoại Kiếm Sĩ";
  const defRatio = document.getElementById('globalAspectRatio')?.value || cfg.aspectRatio || '16:9';
  const samples = [
    { prompt: 'Cinematic wide drone shot of ancient mountain castle at golden hour, cherry blossoms falling, 4k', voiceover: 'Tại vùng đất xa xôi, nơi mây trắng vờn quanh những ngọn núi huyền bí...', duration: 5, size: defRatio },
    { prompt: 'Lương Phong, a solitary swordsman in weathered conical hat and dark hanfu walks through misty bamboo forest, slow motion, dramatic lighting', voiceover: 'Lương Phong cô độc bước đi trong màn sương mù dày đặc của rừng trúc.', duration: 5, size: defRatio },
    { prompt: 'Close-up of ancient silver katana blade reflecting moonlight, water droplets, bokeh background, cinematic', voiceover: 'Ánh trăng chiếu qua từng giọt nước trên lưỡi kiếm sắc bén như gương.', duration: 5, size: defRatio },
    { prompt: 'Epic battle scene: Lương Phong clashes swords with fierce assassin on mountain cliff at sunset, sparks flying, 4k', voiceover: 'Lương Phong đối đầu sinh tử trên đỉnh vách núi cheo leo.', duration: 6, size: defRatio },
    { prompt: 'Lương Phong stands victorious on cliff overlooking vast valley at sunrise, cape flowing in wind', voiceover: 'Và cuối cùng, Lương Phong nhìn xuống thung lũng rộng lớn trong bình minh rực rỡ.', duration: 5, size: defRatio },
  ];
  samples.forEach((s, i) => addScene(s.prompt, s.voiceover, s.duration, s.size, `Cảnh ${i + 1}`));
}

function loadOneMinuteSample() {
  clearAll();
  document.getElementById('projectTitle').value = "Kẻ Du Hành Thời Gian 2099";
  const defRatio = document.getElementById('globalAspectRatio')?.value || cfg.aspectRatio || '16:9';
  const scenes60s = [
    {
      title: "Cảnh 1: Bình Minh Tương Lai",
      prompt: "Cinematic epic wide establishing aerial drone shot gliding over futuristic cyberpunk metropolis at twilight, towering neon skyscrapers piercing through misty clouds, flying spinners cruising between holographic billboards, warm cyan and amber volumetric lighting, shot on ARRI Alexa 35, anamorphic lens, 8k resolution, masterpiece, no text, no watermark",
      voiceover: "Năm 2099, khi dòng thời gian bị bẻ cong, thành phố này chính là nơi lưu giữ những ký ức cuối cùng của nhân loại.",
      duration: 8,
      size: defRatio,
      voice: "vi-VN-NamMinhNeural"
    },
    {
      title: "Cảnh 2: Kẻ Lạ Mặt Xuất Hiện",
      prompt: "Cinematic medium tracking shot of a weary time-traveler named Minh wearing a worn dark trench coat with subtle glowing cybernetic collar, walking slowly through a rainy neon alley, steam rising from grates, wet asphalt reflections, camera tracking backward smoothly, shallow depth of field, 8k, photorealistic, masterpiece, no text, no watermark",
      voiceover: "Nếu thiết bị Chrono này đếm ngược về con số không, toàn bộ thực tại sẽ tan biến.",
      duration: 8,
      size: defRatio,
      voice: "vi-VN-NamMinhNeural"
    },
    {
      title: "Cảnh 3: Cuộc Chạm Trán Định Mệnh",
      prompt: "Cinematic medium over-the-shoulder shot, a cybernetic female operative named Lan in sleek matte black armor with radiant blue visor standing atop metal staircase, rain dripping from her visor, neon rim lighting casting sharp blue highlights, intense gaze, cinematic bokeh, masterpiece, no text, no watermark",
      voiceover: "Minh, anh không thể tiếp tục can thiệp vào dòng thời gian nữa. Dừng lại đi!",
      duration: 7,
      size: defRatio,
      voice: "vi-VN-HoaiMyNeural"
    },
    {
      title: "Cảnh 4: Lời Cảnh Báo Nghẹt Thở",
      prompt: "Cinematic intense close-up shot of Minh looking up with determination, rain droplets trickling down his face, neon reflections dancing in his eyes, background neon city lights blurred into beautiful anamorphic bokeh, 85mm portrait lens, photorealistic 8k, masterpiece, no text, no watermark",
      voiceover: "Em không hiểu sao, Lan? Nếu tôi không khởi động lại dòng thời gian, ngày mai em sẽ không bao giờ tồn tại!",
      duration: 8,
      size: defRatio,
      voice: "vi-VN-NamMinhNeural"
    },
    {
      title: "Cảnh 5: Kích Hoạt Thiết Bị Thời Gian",
      prompt: "Cinematic dynamic low-angle shot, Minh quickly pulls out a glowing hexagonal Chrono device, blue energetic plasma sparks pulsing between his fingers, holographic temporal rings expanding outward, volumetric blue lighting illuminating his face, high-speed camera movement, masterpiece, no text, no watermark",
      voiceover: "Đừng chạm vào lõi phản ứng! Nó sắp quá tải rồi!",
      duration: 8,
      size: defRatio,
      voice: "vi-VN-HoaiMyNeural"
    },
    {
      title: "Cảnh 6: Bùng Nổ Sóng Năng Lượng",
      prompt: "Epic cinematic wide shot, a massive dome of brilliant golden and cyan temporal energy erupting between the two characters in the alleyway, rain droplets freezing mid-air in slow motion, holographic shockwave distorting space around them, awe-inspiring VFX, ARRI Alexa 35, 8k, masterpiece, no text, no watermark",
      voiceover: "Tin tôi một lần này thôi... Giữ chặt lấy tay tôi!",
      duration: 7,
      size: defRatio,
      voice: "vi-VN-NamMinhNeural"
    },
    {
      title: "Cảnh 7: Khoảnh Khắc Lắng Đọng",
      prompt: "Cinematic emotional close-up shot of Lan reaching out her robotic gloved hand, her glowing visor retracting to reveal her emotional tearful eyes, golden light illuminating her face amidst swirling frozen sparks, 85mm lens, shallow depth of field, photorealistic 8k, masterpiece, no text, no watermark",
      voiceover: "Dù ở bất kỳ dòng thời gian nào... em cũng sẽ tìm thấy anh.",
      duration: 7,
      size: defRatio,
      voice: "vi-VN-HoaiMyNeural"
    },
    {
      title: "Cảnh 8: Cánh Cổng Khởi Đầu Mới",
      prompt: "Cinematic wide silhouette shot of Minh and Lan holding hands as the golden temporal vortex swallows the alleyway, camera sweeping upward into the neon sky filled with swirling starlight, peaceful yet awe-inspiring resolution, cinematic lighting, 8k, masterpiece, no text, no watermark",
      voiceover: "Dòng thời gian bắt đầu viết lại. Và cuộc hành trình xuyên không gian... chính thức bắt đầu.",
      duration: 7,
      size: defRatio,
      voice: "vi-VN-NamMinhNeural"
    }
  ];
  scenes60s.forEach(s => addScene(s.prompt, s.voiceover, s.duration, s.size, s.title, s.voice));
  if (typeof showToast === 'function') {
    showToast("⏱️ Đã nạp kịch bản phim 1 phút (60 giây - 8 cảnh) hoàn chỉnh!");
  }
}

function importFromText() {
  const el = document.getElementById('importTextArea');
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

function parseAndAddScenes() {
  const raw = document.getElementById('rawScriptInput').value.trim();
  if (!raw) return;

  // 1. Nếu là định dạng Markdown chuyên nghiệp (có SHOT và PROMPT:)
  if (raw.includes("SHOT ") && raw.includes("PROMPT:")) {
    parseProfessionalMarkdownScript(raw);
    document.getElementById('importTextArea').style.display = 'none';
    document.getElementById('rawScriptInput').value = '';
    return;
  }

  // 2. Bộ phân tách toàn năng (Universal Script Parser): hỗ trợ timeline 0-10s:, Cảnh 1:, Prompt|Thoại, văn bản tự do
  parseUniversalScript(raw);
  document.getElementById('importTextArea').style.display = 'none';
  document.getElementById('rawScriptInput').value = '';
}

// ── Bộ nhận diện giới tính nhân vật thông minh (Không hard-code) ──────────
const GENDER_FEMALE_TITLES = [
  "cô", "cô nương", "nàng", "tiểu thư", "bà", "chị", "mẹ", "công chúa", 
  "nữ hoàng", "nữ tướng", "thiếu nữ", "thánh nữ", "muội", "mỹ nhân", "mỹ nữ",
  "miss", "mrs", "ms", "lady", "girl", "woman", "queen", "princess", "actress", "witch"
];

const GENDER_MALE_TITLES = [
  "chàng", "anh", "chú", "bác", "ông", "cậu", "bố", "cha", "hoàng tử", "vua", 
  "công tử", "thiếu gia", "đại ca", "sư huynh", "huynh", "hắn", "gã", "tên", 
  "kiếm khách", "sát thủ", "đại hiệp", "chủ tịch", "tổng tài", "tướng quân", "đại úy", "thầy",
  "mr", "sir", "lord", "king", "prince", "boy", "man", "actor", "wizard", "warrior"
];

const GENDER_FEMALE_LEXICON = new Set([
  "thị", "mai", "lan", "cúc", "trúc", "đào", "hoa", "hương", "nga", "thảo", "hà", 
  "linh", "ngọc", "trang", "trâm", "my", "huyền", "vy", "chi", "phương", "oanh", 
  "yến", "dung", "hằng", "nhung", "thúy", "thùy", "diệu", "bích", "liên", "quỳnh", 
  "tuyết", "nhi", "hạnh", "phụng", "loan", "kiều", "nhàn", "ngân", "vân", "thu", 
  "xuân", "sen", "đan", "trân", "như", "ly", "châu", "mơ", "hường", "tú", "an",
  "mary", "anna", "sarah", "emma", "natasha", "alice", "lucy", "sophia", "olivia", 
  "mia", "charlotte", "amelia", "elena", "lisa", "jessica", "linda", "barbara", 
  "susan", "hannah", "claire", "diana", "rachel", "rose", "daisy", "lily", "grace", "chloe"
]);

const GENDER_MALE_LEXICON = new Set([
  "văn", "hữu", "đức", "quốc", "minh", "tuấn", "hùng", "dũng", "long", "thắng", 
  "hải", "sơn", "phong", "quân", "thành", "cường", "trần", "phúc", "khang", "kiên", 
  "trọng", "khoa", "bảo", "tùng", "huy", "trung", "đạt", "nguyên", "hoàng", "vũ", 
  "bình", "thạch", "giang", "tài", "tiến", "trí", "thiện", "phát", "bách", "lâm", 
  "nam", "hiệp", "chính", "thao", "tân", "bằng", "quang", "khôi", "việt",
  "john", "tony", "peter", "james", "robert", "michael", "william", "david", "richard", 
  "thomas", "charles", "daniel", "matthew", "anthony", "donald", "paul", "andrew", 
  "joshua", "kevin", "brian", "george", "edward", "jason", "ryan", "jacob", "gary", 
  "eric", "justin", "scott", "brandon", "frank", "samuel", "patrick", "alex", "jack", 
  "adam", "henry", "ethan", "carl", "arthur", "austin", "noah", "bruce", "logan", "alan"
]);

function detectCharacterVoice(characterName, contextText = "", registry = {}) {
  const normName = (characterName || "").trim().toLowerCase();
  const femaleVoice = "vi-VN-HoaiMyNeural";
  const maleVoice = "vi-VN-NamMinhNeural";

  if (!normName || normName === "nhân vật" || normName === "người dẫn truyện" || normName === "narrator") {
    return femaleVoice;
  }

  // 1. Kiểm tra bộ nhớ nhân vật xuyên suốt các cảnh trong phim (Đảm bảo 1 nhân vật luôn giữ 1 giọng)
  if (registry && registry[normName]) {
    return registry[normName];
  }

  const normContext = (contextText || "").toLowerCase();

  // 2. Phân tích ngữ cảnh trực tiếp trong câu thoại và mô tả cảnh xung quanh
  if (/\b(?:giọng\s*nữ|tiếng\s*nữ|nữ\s*tính|nữ\s*run\s*rẩy|female\s*voice)\b/i.test(normContext)) {
    if (registry) registry[normName] = femaleVoice;
    return femaleVoice;
  }
  if (/\b(?:giọng\s*nam|tiếng\s*nam|nam\s*tính|nam\s*trầm|nam\s*khàn|nam\s*gầm|male\s*voice)\b/i.test(normContext)) {
    if (registry) registry[normName] = maleVoice;
    return maleVoice;
  }

  // Tìm danh xưng hoặc chức danh trong ngữ cảnh và trong tên
  for (const t of GENDER_FEMALE_TITLES) {
    if (normName.startsWith(t + " ") || normName.includes(" " + t + " ") || normName.endsWith(" " + t)) {
      if (registry) registry[normName] = femaleVoice;
      return femaleVoice;
    }
    const pat = new RegExp(`(?:\\b${t}\\s+${normName}\\b|\\b${normName}\\s*,?\\s*(?:là\\s+)?(?:một\\s+)?${t}\\b)`, "i");
    if (pat.test(normContext)) {
      if (registry) registry[normName] = femaleVoice;
      return femaleVoice;
    }
  }

  for (const t of GENDER_MALE_TITLES) {
    if (normName.startsWith(t + " ") || normName.includes(" " + t + " ") || normName.endsWith(" " + t)) {
      if (registry) registry[normName] = maleVoice;
      return maleVoice;
    }
    const pat = new RegExp(`(?:\\b${t}\\s+${normName}\\b|\\b${normName}\\s*,?\\s*(?:là\\s+)?(?:một\\s+)?${t}\\b)`, "i");
    if (pat.test(normContext)) {
      if (registry) registry[normName] = maleVoice;
      return maleVoice;
    }
  }

  // 3. Phân tích ngữ âm cấu trúc tên (Tên đệm, Tên chính)
  const words = normName.split(/[\s_-]+/).filter(Boolean);
  const givenName = words[words.length - 1];
  const middleName = words.length > 1 ? words[words.length - 2] : "";

  if (middleName === "thị" || words.includes("thị")) {
    if (registry) registry[normName] = femaleVoice;
    return femaleVoice;
  }
  if (middleName === "văn" || words.includes("văn")) {
    if (registry) registry[normName] = maleVoice;
    return maleVoice;
  }

  let femaleScore = 0;
  let maleScore = 0;

  for (const w of words) {
    if (GENDER_FEMALE_LEXICON.has(w)) femaleScore += (w === givenName ? 3 : 1);
    if (GENDER_MALE_LEXICON.has(w)) maleScore += (w === givenName ? 3 : 1);
  }

  if (femaleScore > maleScore) {
    if (registry) registry[normName] = femaleVoice;
    return femaleVoice;
  }
  if (maleScore > femaleScore) {
    if (registry) registry[normName] = maleVoice;
    return maleVoice;
  }

  // 4. Phân tích đuôi tên quốc tế
  if (/(?:a|ia|ina|ie|ette|elle|y)$/.test(givenName)) {
    if (registry) registry[normName] = femaleVoice;
    return femaleVoice;
  }
  if (/(?:o|os|er|or|us|on|an|en|rk|rt|ck|ke|ld|hn|th)$/.test(givenName)) {
    if (registry) registry[normName] = maleVoice;
    return maleVoice;
  }

  // 5. Mặc định
  if (registry) registry[normName] = femaleVoice;
  return femaleVoice;
}

function parseUniversalScript(text) {
  clearAll();
  const globalPreset = document.getElementById('globalStylePreset')?.value.trim() || '';
  const globalRatio = document.getElementById('globalAspectRatio')?.value || cfg.aspectRatio || "16:9";
  const characterVoiceRegistry = {};

  // Phân đoạn kịch bản
  let blocks = [];
  if (/(?:^|\n)\s*\d+\s*[-–—]\s*\d+\s*s?\s*:/i.test(text)) {
    // Định dạng mốc thời gian (0-10s:, 10-20s:)
    blocks = text.split(/(?=(?:^|\n)\s*\d+\s*[-–—]\s*\d+\s*s?\s*:)/i);
  } else if (/(?:^|\n)\s*(?:cảnh|shot|hồi|phân cảnh|scene)\s*\d+/i.test(text)) {
    // Định dạng Cảnh 1, Shot 1
    blocks = text.split(/(?=(?:^|\n)\s*(?:cảnh|shot|hồi|phân cảnh|scene)\s*\d+)/i);
  } else if (text.includes("\n\n")) {
    blocks = text.split(/\n\s*\n+/);
  } else {
    blocks = text.split('\n');
  }

  blocks = blocks.map(b => b.trim()).filter(Boolean);
  if (blocks.length === 0 && text.trim()) blocks = [text.trim()];

  blocks.forEach((block, idx) => {
    // Hỗ trợ dạng đơn giản có dấu pipe |
    if (block.includes('|') && !block.includes('0-10s')) {
      const parts = block.split('|').map(p => p.trim());
      let pr = parts[0] || '';
      const vo = parts[1] || '';
      if (pr && globalPreset && !pr.toLowerCase().includes("arri alexa")) {
        pr = pr + (pr.endsWith('.') || pr.endsWith(',') ? ' ' : ', ') + globalPreset;
      }
      addScene(pr, vo, 5, globalRatio, `Cảnh ${scenes.length + 1}`);
      return;
    }

    let clean = block;

    // 1. Trích xuất thời lượng
    let duration = 5;
    const rangeM = clean.match(/(?:^|\n)\s*(\d+)\s*[-–—]\s*(\d+)\s*s?\s*:/i);
    let title = `Cảnh ${idx + 1}`;
    if (rangeM) {
      const s = parseInt(rangeM[1]);
      const e = parseInt(rangeM[2]);
      if (!isNaN(s) && !isNaN(e) && e > s) {
        duration = Math.min(10, Math.max(3, e - s));
        title = `Cảnh ${idx + 1} (${s}-${e}s)`;
      }
      clean = clean.replace(rangeM[0], ' ').trim();
    } else {
      const durM = clean.match(/duration:\s*(\d+)s?/i);
      if (durM) {
        duration = Math.min(10, Math.max(3, parseInt(durM[1])));
        clean = clean.replace(durM[0], ' ').trim();
      }
    }

    // Xóa header Cảnh/Shot cũ nếu có
    clean = clean.replace(/^(?:#\s*)?(?:🎥\s*)?(?:cảnh|shot|hồi|phân cảnh|scene)\s*[\d\.]+[\s–—:.-]*/i, '').trim();

    // 2. Trích xuất Màu sắc / Ánh sáng
    let colorNote = "";
    const colorM = clean.match(/\[Màu sắc:\s*([^\]]+)\]/i);
    if (colorM) {
      colorNote = colorM[1].trim();
    }

    // Xóa các tag cấu trúc [...]
    const withoutTags = clean.replace(/\[[^\]]+\]/g, ' ').replace(/\s+/g, ' ').trim();

    // 3. Bóc tách câu thoại & tên nhân vật theo mệnh đề cận kề
    const quoteMatches = [...withoutTags.matchAll(/["“]([^"”\n\r]+)["”]/g)];
    const voiceSegments = [];
    const speechCuts = [];

    for (const qm of quoteMatches) {
      const speech = qm[1].trim();
      const qIdx = qm.index;
      const before = withoutTags.substring(0, qIdx).trim();

      // Chỉ tìm trong mệnh đề cuối cùng ngay trước dấu hai chấm và ngoặc kép (không xuyên qua dấu câu)
      const spkMatch = before.match(/(?:^|[,\.!\?;\n])\s*([^,.\n:"]*?)\s*(?:nói|bảo|hỏi|đáp|thì thầm|gào thét|gầm lên|ngắt quãng|lẩm bẩm|hô|quát)?[^,.\n:"]*:\s*$/i);

      let speaker = "Nhân vật";
      let leadInStart = qIdx;

      if (spkMatch) {
        const fullMatch = spkMatch[0];
        const matchIndexInBefore = before.lastIndexOf(fullMatch);
        const sepMatch = fullMatch.match(/^[,\.!\?;\n]\s*/);
        const sepLen = sepMatch ? sepMatch[0].length : 0;
        leadInStart = matchIndexInBefore + sepLen;

        const clause = before.substring(leadInStart);
        const nameM = clause.match(/([A-ZÀ-Ỹ][a-zà-ỹ\w]+(?:\s+[A-ZÀ-Ỹ][a-zà-ỹ\w]+){0,3})/);
        if (nameM) {
          speaker = nameM[1].trim();
        }
      }

      speechCuts.push({ start: leadInStart, end: qIdx + qm[0].length });

      // Nhận diện giọng chuẩn xác tự động (không hard-code tên)
      const charName = speaker || (voiceSegments.length === 0 ? "Nhân vật" : `Nhân vật ${voiceSegments.length + 1}`);
      const voice = detectCharacterVoice(charName, clean, characterVoiceRegistry);

      voiceSegments.push({
        character: charName,
        text: speech,
        voice: voice
      });
    }

    // 4. Trích xuất Visual Prompt đầy đủ
    let visualPrompt = "";
    let lastIdx = 0;
    for (const cut of speechCuts) {
      visualPrompt += withoutTags.substring(lastIdx, cut.start) + " ";
      lastIdx = cut.end;
    }
    visualPrompt += withoutTags.substring(lastIdx);
    visualPrompt = visualPrompt.replace(/PROMPT:\s*/gi, ' ')
      .replace(/Duration:\s*\d+s?/gi, ' ')
      .replace(/🎙️\s*Hội thoại[^:]*:/gi, ' ')
      .replace(/\s+/g, ' ')
      .replace(/^[,.\s]+|[,.\s]+$/g, '').trim();

    if (colorNote && !visualPrompt.toLowerCase().includes(colorNote.toLowerCase())) {
      visualPrompt = `${visualPrompt}, ${colorNote}`;
    }

    // Gắn Global Style Master Preset
    if (globalPreset && visualPrompt) {
      if (!visualPrompt.toLowerCase().includes("arri alexa") && !visualPrompt.toLowerCase().includes("zeiss")) {
        visualPrompt = visualPrompt + (visualPrompt.endsWith('.') || visualPrompt.endsWith(',') ? ' ' : ', ') + globalPreset;
      }
    }

    let voiceover = voiceSegments.map(s => s.character && s.character !== "Nhân vật" ? `${s.character}: "${s.text}"` : s.text).join(" | ");
    if (voiceSegments.length === 1 && (!voiceSegments[0].character || voiceSegments[0].character === "Nhân vật")) {
      voiceover = voiceSegments[0].text;
    }

    if (visualPrompt) {
      addScene(
        visualPrompt,
        voiceover,
        duration,
        globalRatio,
        title,
        "",
        voiceSegments
      );
    }
  });

  showToast(`⚡ Đã tự động bóc tách thành công ${scenes.length} phân cảnh!`);
}


async function aiStandardizeScript() {
  const raw = document.getElementById('rawScriptInput').value.trim();
  if (!raw) {
    alert("Vui lòng dán nội dung kịch bản hoặc ý tưởng trước!");
    return;
  }

  const btn = document.getElementById('btnAiStandardize');
  btn.disabled = true;
  btn.textContent = '⏳ AI đang chuyển thể sang chuẩn điện ảnh...';

  try {
    const stylePreset = document.getElementById('globalStylePreset').value.trim();
    const res = await apiFetch('/api/ai/convert-script', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ raw_script: raw, style_preset: stylePreset }),
    });

    const data = await res.json();
    if (data.error) throw new Error(data.error);

    // Cập nhật nội dung đã chuẩn hoá vào ô và tự động bóc tách thành các cảnh
    document.getElementById('rawScriptInput').value = data.standardized_script;
    parseProfessionalMarkdownScript(data.standardized_script);
    document.getElementById('importTextArea').style.display = 'none';
  } catch (err) {
    alert("Lỗi khi AI chuẩn hóa kịch bản: " + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '🤖 Dùng AI Tự Động Chuyển Thể Sang Chuẩn Studio';
  }
}

function parseProfessionalMarkdownScript(text) {
  clearAll();
  const characterVoiceRegistry = {};

  // 1. Trích xuất Tiêu đề phim nếu có
  const titleMatch = text.match(/(?:#\s*)?🎬\s*([^\n\r]+)/i) || text.match(/#\s*([^\n\r]+)/);
  if (titleMatch) {
    document.getElementById('projectTitle').value = titleMatch[1].replace(/–.*/, '').trim();
  }

  // 2. Chia theo từng SHOT (chấp nhận cả có dấu # hoặc không có dấu #)
  const shotBlocks = text.split(/(?=(?:#\s*)?(?:🎥\s*)?SHOT\s+[\d\.]+)/i);

  shotBlocks.forEach((block) => {
    if (!block.trim() || !block.toUpperCase().includes("PROMPT")) return;

    // Lấy tiêu đề Shot (ví dụ: SHOT 1.1 – BƯỚC CHÂN CỦA BÓNG TỐI)
    const shotTitleMatch = block.match(/(?:#\s*)?(?:🎥\s*)?(SHOT\s+[^\n\r]+)/i);
    const shotTitle = shotTitleMatch ? shotTitleMatch[1].trim() : `Cảnh ${scenes.length + 1}`;

    // Lấy Duration (thời lượng, ví dụ: Duration: 7s hoặc **Duration:** 8s)
    const durMatch = block.match(/Duration:\s*(\d+)s/i);
    const duration = durMatch ? parseInt(durMatch[1]) : 5;

    // Lấy PROMPT: bắt từ PROMPT: đến khi gặp phần Hội thoại hoặc hết block
    let prompt = "";
    const promptMatch = block.match(/PROMPT:\s*([\s\S]*?)(?=(?:\r?\n\s*(?:🎙️|\*\*🎙️|Hội thoại|HỘI THOẠI|VOICEOVER|SFX|MUSIC|\n\n\n|$)))/i);
    if (promptMatch) {
      prompt = promptMatch[1].replace(/```(?:\w+)?/g, '').trim();
    }

    // Lấy các câu thoại tiếng Việt
    const voiceSegments = [];
    const dialogueSectionMatch = block.match(/(?:🎙️|Hội thoại tiếng Việt|Hội thoại|VOICEOVER)[\s\S]*?(?=\*\*🎭|\*\*🎵|🎭|🎵|---|#|$)/i);
    
    if (dialogueSectionMatch) {
      const dialogueText = dialogueSectionMatch[0];
      // Bắt các dòng thoại dạng:
      // TÊN (V.O): "câu thoại"
      // > **TÊN:** "câu thoại"
      // TÊN: "câu thoại"
      const lines = dialogueText.split('\n');
      for (const line of lines) {
        const clean = line.replace(/^[>\s*-]+/, '').trim();
        const lineM = clean.match(/^\*{0,2}([^:*"\n]+?)(?:\([^\)]*\))?\*{0,2}\s*:\s*["“]?([^"”\n\r]+)["”]??$/);
        if (lineM) {
          const charName = lineM[1].replace(/[*_]/g, '').trim();
          const speech = lineM[2].trim();
          if (speech && !charName.toLowerCase().includes("hội thoại") && !charName.toLowerCase().includes("duration")) {
            const charVoice = detectCharacterVoice(charName, block, characterVoiceRegistry);
            voiceSegments.push({
              character: charName,
              text: speech,
              voice: charVoice
            });
          }
        }
      }
    }

    let simpleVoiceover = "";
    if (voiceSegments.length === 0) {
      // Nếu không bắt được tên nhân vật cụ thể, lấy nội dung thoại bất kỳ sau chữ Hội thoại
      if (dialogueSectionMatch) {
        const textLines = dialogueSectionMatch[0].split('\n')
          .map(l => l.replace(/^[>*\s🎙️-]+/, '').trim())
          .filter(l => l.length > 5 && !l.toLowerCase().includes("hội thoại"));
        simpleVoiceover = textLines.join(" ");
      }
    }

    if (prompt) {
      // Tự động kết hợp với Prompt Quy chuẩn Toàn phim (Global Style Master Preset)
      const globalPreset = document.getElementById('globalStylePreset')?.value.trim() || '';
      let finalPrompt = prompt;
      if (globalPreset) {
        // Nếu prompt chưa chứa các từ khoá trong preset thì ghép thêm vào cuối
        if (!finalPrompt.toLowerCase().includes("arri alexa") && !finalPrompt.toLowerCase().includes("zeiss")) {
          if (!finalPrompt.endsWith('.') && !finalPrompt.endsWith(',')) finalPrompt += ', ';
          else finalPrompt += ' ';
          finalPrompt += globalPreset;
        }
      }

      const globalRatio = document.getElementById('globalAspectRatio')?.value || cfg.aspectRatio || "16:9";
      addScene(
        finalPrompt,
        simpleVoiceover,
        duration,
        globalRatio,
        shotTitle,
        "",
        voiceSegments
      );
    }
  });

  if (scenes.length > 0) {
    alert(`🎉 Đã nhận diện thành công ${scenes.length} cảnh từ kịch bản!`);
  } else {
    alert("Không tìm thấy cảnh nào hợp lệ. Vui lòng kiểm tra lại cấu trúc kịch bản.");
  }
}

// ── Start Movie Creation ────────────────────────────────────────────────
// (Refactor 2026-11-07, Consistency Max) Payload tách riêng thành
// buildMoviePayload() để nút "Kiểm tra đồng nhất" (ui.js) dùng chung
// đúng 1 bản payload với startMovieCreation, tránh lệch nhau.
function buildMoviePayload() {
  const concurrent = parseInt(document.getElementById('maxConcurrent').value) || cfg.concurrent || 12;
  cfg.concurrent = concurrent;

  const globalVoice = document.getElementById('voiceSelect')?.value || 'vi-VN-HoaiMyNeural';
  const globalVoiceMode = document.getElementById('voiceModeSelect')?.value || (globalVoice === 'ai_voice' ? 'ai_voice' : 'tts');

  const payload = {
    title: document.getElementById('projectTitle').value.trim() || 'movie',
    voice: globalVoice,
    voice_mode: globalVoiceMode,
    font_size: parseInt(document.getElementById('fontSize').value) || 22,
    aspect_ratio: document.getElementById('globalAspectRatio')?.value || cfg.aspectRatio || '16:9',
    export_mode: document.getElementById('exportMode').value,
    max_concurrent: concurrent,
    chain_mode: document.getElementById('chainMode')?.value || 'none',
    consistency_mode: document.getElementById('consistencyMax')?.checked ? 'max' : 'standard',
    chain_fallback_mode: document.getElementById('chainFallbackMode')?.value || 'independent',
    bg_music_path: document.getElementById('bgMusicSelect')?.value || null,
    music_volume: parseFloat(document.getElementById('musicVolume')?.value || '0.25'),
    voice_volume: parseFloat(document.getElementById('voiceVolume')?.value || '1.0'),
    subtitle_mode: document.getElementById('subtitleMode')?.value || 'burn_in',
    quality: document.getElementById('exportQuality')?.value || '720p',
    transition: document.getElementById('sceneTransition')?.value || 'none',
    style_locks: {
      style: document.getElementById('lockStyle')?.value || '',
      lighting: document.getElementById('lockLighting')?.value || '',
      color: document.getElementById('lockColor')?.value || '',
      camera: document.getElementById('lockCamera')?.value || '',
      character: document.getElementById('lockCharacter')?.value || '',
    },
    scenes: scenes.map((s, i) => ({
      index: i + 1,
      prompt: s.prompt,
      voiceover: s.voiceover || '',
      voice: s.voice || '',
      voice_mode: s.voice_mode || (s.voice === 'ai_voice' ? 'ai_voice' : globalVoiceMode),
      reference_image: s.reference_image || null,
      voice_segments: (s.voiceMode === 'multi' && s.voiceSegments && s.voiceSegments.length > 0)
        ? s.voiceSegments
        : [],
      duration: s.duration,
      size: s.size,
      chain_from_previous: !!s.chain_from_previous,
      sheet_angle: s.sheet_angle || 'front',
    })),
  };
  return payload;
}

async function startMovieCreation() {
  syncScenesFromDOM();

  if (scenes.length === 0) {
    alert('Vui lòng thêm ít nhất 1 cảnh!');
    return;
  }

  const payload = buildMoviePayload();

  // Consistency Max: kiểm tra trước ở client để báo lỗi nhanh; server vẫn
  // kiểm tra lại và trả 422 nếu có lỗi (không tin hoàn toàn client).
  if (payload.consistency_mode === 'max' && typeof window.runConsistencyPreflight === 'function') {
    const pre = await window.runConsistencyPreflight(payload);
    if (pre && !pre.passed) {
      if (typeof window.showConsistencyReport === 'function') {
        window.showConsistencyReport(pre, { allowProceed: false });
      } else {
        alert('Preflight đồng nhất chưa đạt: ' + (pre.checks || []).filter(c => c.level === 'error').map(c => c.message).join(' | '));
      }
      return;
    }
  }

  document.getElementById('btnCreateMovie').disabled = true;
  document.getElementById('createStatus').textContent = '⏳ Đang gửi yêu cầu...';

  try {
    const res = await apiFetch('/api/movies', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    // Server trả 422 kèm danh sách check khi preflight lỗi -> hiện báo cáo
    // thay vì alert chung chung.
    if (res.status === 422 && data.checks && typeof window.showConsistencyReport === 'function') {
      window.showConsistencyReport({ passed: false, errors: data.errors || 0, warnings: data.warnings || 0, checks: data.checks }, { allowProceed: false });
      return;
    }
    if (data.error) throw new Error(data.error);

    activeJobId = data.job_id;
    document.getElementById('createStatus').textContent = `✅ Job ${activeJobId} đã bắt đầu!`;

    // Chuyển sang panel Monitor
    showPanel('monitor');
    startMonitoring(activeJobId, payload.title, scenes.length);

  } catch (e) {
    alert('Lỗi: ' + e.message);
  } finally {
    document.getElementById('btnCreateMovie').disabled = false;
    document.getElementById('createStatus').textContent = '';
  }
}

// ── Monitor / SSE with Auto-Reconnect ────────────────────────────────────
let sseReconnectAttempts = 0;
let sseReconnectTimer = null;

function setSseStatus(status, text) {
  const badge = document.getElementById('monSseBadge');
  const txt = document.getElementById('monSseText');
  if (!badge || !txt) return;
  badge.className = `sse-badge ${status}`;
  txt.textContent = text;
}

function startMonitoring(jobId, title, totalScenes) {
  document.getElementById('monitorEmpty').style.display = 'none';
  document.getElementById('monitorContent').style.display = 'block';
  document.getElementById('monJobTitle').textContent = `🎬 Đang sản xuất: "${title}"`;
  document.getElementById('monJobId').textContent = `ID: ${jobId}`;
  document.getElementById('outputCard').style.display = 'none';
  document.getElementById('clipsCard').style.display = 'none';
  document.getElementById('sceneAuditCard').style.display = 'none';
  document.getElementById('logConsole').innerHTML = '';
  const cancelBtn = document.getElementById('btnCancelJob');
  if (cancelBtn) cancelBtn.style.display = 'inline-flex';

  // Tạo khung task matrix
  const matrix = document.getElementById('taskMatrix');
  matrix.innerHTML = '';
  // Affiliate continuity 2026-11-07: job mới bắt đầu thì mở lại matrix đầy đủ.
  window.__matrixAutoCollapsedForJob = null;
  if (typeof window.setTaskMatrixCollapsed === 'function') window.setTaskMatrixCollapsed(false);
  for (let i = 1; i <= totalScenes; i++) {
    const defaultPrompt = (scenes[i - 1] ? scenes[i - 1].prompt : '');
    const defaultVoice = (scenes[i - 1] ? scenes[i - 1].voiceover : '');
    matrix.innerHTML += `
      <div class="task-cell queued" id="cell-${i}" data-status="queued">
        <div class="cell-header">
          <div class="cell-title">
            <span class="cell-icon">⌛</span>
            <span>Cảnh ${i}</span>
          </div>
          <span class="pill queued">⌛ Đang chờ</span>
        </div>
        <div class="cell-media" style="background:rgba(255,255,255,0.02);border:1px dashed var(--border);color:var(--muted);font-size:12px;">
          ⌛ Đang xếp hàng chờ xử lý...
        </div>
        <div class="cell-prompt-box">
          <div class="cell-prompt-label">
            <span>📝 Prompt:</span>
          </div>
          <div class="cell-prompt-text u-c-muted">${esc(defaultPrompt || 'Đang chuẩn bị...')}</div>
          ${defaultVoice ? `<div class="cell-voiceover u-c-muted">🎙️ Lời thoại: ${esc(defaultVoice)}</div>` : ''}
        </div>
      </div>`;
  }

  // Khởi tạo SSE
  sseReconnectAttempts = 0;
  if (sseReconnectTimer) { clearTimeout(sseReconnectTimer); sseReconnectTimer = null; }
  initSSE(jobId);
}

function initSSE(jobId) {
  if (sseSource) { sseSource.close(); sseSource = null; }
  setSseStatus('reconnecting', sseReconnectAttempts > 0 ? `Đang kết nối lại (${sseReconnectAttempts})...` : 'Đang kết nối...');

  sseSource = new EventSource(`/api/jobs/${jobId}/sse`);

  sseSource.onopen = () => {
    sseReconnectAttempts = 0;
    setSseStatus('live', 'Trực tiếp');
  };

  sseSource.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      updateMonitor(data, jobId);
      if (data.status === 'done' || data.status === 'cancelled' || (data.status === 'error' && !data.is_retrying)) {
        sseSource.close();
        sseSource = null;
        setSseStatus('offline', 'Đã hoàn tất');
      }
    } catch (err) {
      console.warn('[SSE parse err]', err);
    }
  };

  sseSource.onerror = () => {
    if (sseSource) { sseSource.close(); sseSource = null; }
    if (sseReconnectAttempts < 6) {
      sseReconnectAttempts++;
      const delay = Math.min(1500 * Math.pow(1.4, sseReconnectAttempts), 10000);
      setSseStatus('reconnecting', `Mất kết nối. Thử lại sau ${Math.round(delay/1000)}s...`);
      sseReconnectTimer = setTimeout(() => initSSE(jobId), delay);
    } else {
      setSseStatus('offline', 'Dự phòng HTTP Polling');
      pollJobFallback(jobId);
    }
  };
}

async function pollJobFallback(jobId) {
  while (true) {
    await new Promise(r => setTimeout(r, 3000));
    try {
      const res = await apiFetch(`/api/jobs/${jobId}`);
      const data = await res.json();
      updateMonitor(data, jobId);
      if (data.status === 'done' || data.status === 'error' || data.status === 'cancelled') break;
    } catch {}
  }
}

function updateMonitor(data, jobId) {
  const bar = document.getElementById('monProgressBar');
  const lbl = document.getElementById('monProgressLabel');
  const pill = document.getElementById('monJobPill');

  bar.style.width = data.progress + '%';
  lbl.textContent = data.progress + '%';

  if (data.status === 'done') {
    pill.className = 'pill done';
    pill.textContent = '✅ Hoàn thành';
    document.getElementById('btnCancelJob').style.display = 'none';
  } else if (data.status === 'error') {
    pill.className = 'pill error';
    pill.textContent = '❌ Có lỗi';
    document.getElementById('btnCancelJob').style.display = 'none';
  } else if (data.status === 'audit_pending') {
    pill.className = 'pill';
    pill.style.background = 'rgba(251,191,36,0.15)';
    pill.style.color = 'var(--yellow)';
    pill.textContent = '⚠️ Chờ kiểm định cảnh';
    document.getElementById('btnCancelJob').style.display = 'none';
  } else if (data.status === 'cancelled') {
    pill.className = 'pill error';
    pill.textContent = '🛑 Đã dừng';
    document.getElementById('btnCancelJob').style.display = 'none';
  } else {
    pill.className = 'pill running';
    pill.textContent = '⚙️ Đang chạy';
    document.getElementById('btnCancelJob').style.display = 'inline-flex';
  }

  // Pre-Export Scene Audit Card: Hiển thị bảng kiểm định nếu có audit
  if (data.audit || data.status === 'audit_pending') {
    renderSceneAuditCard(data.audit, data, jobId);
  }

  // Hiển thị nút Retry các cảnh bị lỗi nếu có
  const retryBtn = document.getElementById('btnRetryFailed');
  if (data.failed_scenes && data.failed_scenes.length > 0) {
    retryBtn.style.display = 'inline-flex';
    if (data.is_retrying) {
      retryBtn.disabled = true;
      retryBtn.innerHTML = '⏳ Đang render lại các cảnh lỗi...';
    } else {
      retryBtn.disabled = false;
      retryBtn.innerHTML = `🔄 Render lại cảnh bị lỗi (<span id="failedCountBadge">${data.failed_scenes.length}</span>)`;
    }
  } else {
    retryBtn.style.display = 'none';
  }

  // Update live scene cards
  if (data.scenes) {
    Object.entries(data.scenes).forEach(([idx, info]) => {
      renderSceneMonitorCard(idx, info, data, jobId);
    });
  }

  // Logs
  if (data.logs && data.logs.length) {
    const con = document.getElementById('logConsole');
    data.logs.forEach(l => {
      con.innerHTML += `<p>${esc(l)}</p>`;
    });
    con.scrollTop = con.scrollHeight;
  }

  // Final video
  if (data.final_video) {
    const card = document.getElementById('outputCard');
    const wasHidden = (card.style.display === 'none');
    card.style.display = 'block';
    const player = document.getElementById('finalPlayer');
    if (player.src !== location.origin + data.final_video) {
      player.src = data.final_video;
      player.play().catch(() => {});
    }
    const dlUrl = data.final_video.replace('/video/', '/download/');
    document.getElementById('dlBtn').href = dlUrl;
    const srtBtn = document.getElementById('dlSrtBtn');
    if (srtBtn) {
      srtBtn.style.display = 'inline-flex';
      srtBtn.href = `/api/jobs/${jobId}/subtitles`;
    }
    showAffiliateFinishedVideo(data.final_video, data.title || (data.req && data.req.title) || "Video Tiếp Thị", `/api/jobs/${jobId}/subtitles`);
    if (wasHidden) {
      card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    // Affiliate continuity 2026-11-07: có video cuối thì thu gọn Batch Matrix
    // thành dải tiến độ compact (chỉ 1 lần/job; user mở lại bằng nút toggle).
    if (!window.__matrixAutoCollapsedForJob || window.__matrixAutoCollapsedForJob !== jobId) {
      window.__matrixAutoCollapsedForJob = jobId;
      if (typeof window.setTaskMatrixCollapsed === 'function') window.setTaskMatrixCollapsed(true);
    }
  }

  // Clips
  if (data.clips && data.clips.length) {
    const clipsCard = document.getElementById('clipsCard');
    clipsCard.style.display = 'block';
    const grid = document.getElementById('clipsGrid');
    if (grid.children.length !== data.clips.length) {
      grid.innerHTML = data.clips.map((url, i) => `
        <div class="clip-card">
          <video src="${url}" controls muted></video>
          <div class="clip-card-footer">
            <span class="u-fs12 u-c-muted">Clip ${i+1}</span>
            <a class="btn btn-ghost btn-sm" href="${url.replace('/clips/', '/clips/')}" download>⬇️</a>
          </div>
        </div>`).join('');
    }
  }
}

function renderSceneMonitorCard(idx, info, data, jobId) {
  let cell = document.getElementById(`cell-${idx}`);
  if (!cell) {
    const matrix = document.getElementById('taskMatrix');
    if (!matrix) return;
    cell = document.createElement('div');
    cell.id = `cell-${idx}`;
    cell.className = 'task-cell queued';
    matrix.appendChild(cell);
  }

  const currentStatus = info.status || 'queued';
  const icons = { queued: '⌛', running: '⚙️', done: '✅', error: '❌' };
  const statusLabels = {
    queued: '⌛ Đang chờ',
    running: '⚙️ Đang xử lý',
    done: '✅ Hoàn thành',
    error: '❌ Bị lỗi'
  };

  const prevStatus = cell.getAttribute('data-status');
  const prevVideo = cell.getAttribute('data-video');
  const videoUrl = info.video_url || (currentStatus === 'done' ? `/api/jobs/${jobId}/scenes/${idx}/video` : '');

  // Tránh render lại gián đoạn nếu video đang phát
  if (currentStatus === 'done' && prevStatus === 'done' && prevVideo === videoUrl) {
    return;
  }

  // Tránh ghi đè nếu người dùng đang nhập text vào textarea khi có lỗi
  if (currentStatus === 'error' && prevStatus === 'error') {
    const activeTextarea = document.getElementById(`edit-prompt-${idx}`);
    if (activeTextarea && document.activeElement === activeTextarea) {
      return;
    }
  }

  // Nếu đang running và đã render trạng thái running, chỉ cập nhật message chữ
  if (currentStatus === 'running' && prevStatus === 'running') {
    const runningMsgEl = cell.querySelector('.cell-running-msg');
    if (runningMsgEl) {
      runningMsgEl.textContent = info.msg || 'Đang render video...';
      return;
    }
  }

  cell.className = `task-cell ${currentStatus}`;
  cell.setAttribute('data-status', currentStatus);
  if (videoUrl) cell.setAttribute('data-video', videoUrl);

  const promptText = info.prompt || (scenes[idx - 1] ? scenes[idx - 1].prompt : '') || '';
  const voiceoverText = info.voiceover || (scenes[idx - 1] ? scenes[idx - 1].voiceover : '') || '';
  const lf = (data && data.last_frames && (data.last_frames[idx] || data.last_frames[String(idx)])) || info.last_frame_url;

  let mediaHtml = '';
  if (currentStatus === 'done' && videoUrl) {
    mediaHtml = `
      <div class="cell-media">
        <video src="${videoUrl}" controls playsinline preload="metadata"></video>
        ${lf ? `
          <div style="position:absolute;bottom:6px;right:6px;background:rgba(0,0,0,0.75);backdrop-filter:blur(4px);padding:3px 8px;border-radius:4px;display:flex;align-items:center;gap:4px;border:1px solid rgba(251,191,36,0.6);cursor:pointer;" onclick="window.open('${esc(lf)}')">
            <span class="u-fs105 u-c-yellow u-fw600">🔗 Last Frame</span>
          </div>
        ` : ''}
      </div>
    `;
  } else if (currentStatus === 'running') {
    mediaHtml = `
      <div class="cell-media" style="background:rgba(251,191,36,0.05);border:1px dashed rgba(251,191,36,0.3);flex-direction:column;gap:8px;">
        <div style="font-size:26px;animation:spin 2s linear infinite;">⚙️</div>
        <div class="cell-running-msg" style="font-size:12px;color:var(--yellow);font-weight:600;padding:0 12px;text-align:center;">${esc(info.msg || 'Đang sinh video...')}</div>
        <button class="btn btn-ghost btn-sm" style="font-size:11px;color:var(--yellow);border:1px solid rgba(251,191,36,0.35);padding:2px 8px;margin-top:2px;" onclick="retrySingleSceneDirect(${idx}, event)" title="Buộc hủy chờ và gửi render lại cảnh này ngay lập tức">
          🔄 Buộc thử lại cảnh này
        </button>
      </div>
    `;
  } else if (currentStatus === 'error') {
    mediaHtml = `
      <div style="background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.3);border-radius:8px;padding:10px;">
        <div class="u-c-red u-fs12 u-fw700 u-mb4 fx fx-ac fx-g6">
          <span>⚠️ Chi tiết lỗi:</span>
        </div>
        <div style="font-size:11.5px;color:var(--text);word-break:break-word;max-height:60px;overflow-y:auto;line-height:1.4;background:rgba(0,0,0,0.3);padding:6px 8px;border-radius:4px;">
          ${esc(info.error || info.msg || 'Lỗi không xác định')}
        </div>
      </div>
    `;
  } else {
    // queued
    mediaHtml = `
      <div class="cell-media" style="background:rgba(255,255,255,0.02);border:1px dashed var(--border);color:var(--muted);font-size:12px;">
        ⌛ Đang xếp hàng chờ xử lý...
      </div>
    `;
  }

  let promptAndActionsHtml = '';
  if (currentStatus === 'error') {
    promptAndActionsHtml = `
      <div class="fx fx-col fx-g6 u-mt2">
        <label class="u-fs115 u-fw700 u-c-yellow u-mb0">✏️ Chỉnh sửa Prompt cho Cảnh ${idx}:</label>
        <textarea id="edit-prompt-${idx}" rows="3" style="width:100%;font-size:12px;padding:6px 8px;border-radius:6px;background:var(--bg1);border:1px solid var(--border);color:var(--text);resize:vertical;font-family:inherit;" placeholder="Nhập prompt thay thế an toàn hơn...">${esc(promptText)}</textarea>
        <div class="fx fx-g8 fx-jc-fe u-mt2">
          <button class="btn btn-ghost btn-sm" style="font-size:11px;color:var(--yellow);border:1px solid rgba(251,191,36,0.35);background:rgba(251,191,36,0.08);" onclick="aiFixScenePromptDirect(${idx}, event)" title="Dùng AI phân tích lý do từ chối và tự động viết lại prompt an toàn chuẩn Hollywood">
            🪄 AI Sửa Prompt
          </button>
          <button class="btn btn-primary btn-sm u-fs11" onclick="retrySingleSceneDirect(${idx}, event)">
            🔄 Tạo lại cảnh này
          </button>
        </div>
      </div>
    `;
  } else if (currentStatus === 'done') {
    promptAndActionsHtml = `
      <div class="cell-prompt-box">
        <div class="cell-prompt-label">
          <span>📝 Prompt thị giác:</span>
          <button class="btn btn-ghost btn-sm u-fs105 u-p-1-6 u-c-muted" onclick="toggleScenePromptEdit(${idx})">✏️ Sửa & Tạo lại</button>
        </div>
        <div id="scene-prompt-view-${idx}" class="cell-prompt-text">
          ${esc(promptText || 'Không có mô tả prompt')}
        </div>
        <div id="scene-prompt-edit-box-${idx}" style="display:none;flex-direction:column;gap:6px;margin-top:6px;">
          <textarea id="edit-prompt-${idx}" rows="2" style="width:100%;font-size:11.5px;padding:6px;border-radius:6px;background:var(--bg1);border:1px solid var(--border);color:var(--text);font-family:inherit;">${esc(promptText)}</textarea>
          <div class="fx fx-g6 fx-jc-fe">
            <button class="btn btn-ghost btn-sm u-fs105 u-p-2-8" onclick="toggleScenePromptEdit(${idx})">Đóng</button>
            <button class="btn btn-primary btn-sm u-fs105 u-p-2-8" onclick="retrySingleSceneDirect(${idx}, event)">🔄 Tạo lại cảnh này</button>
          </div>
        </div>
        ${voiceoverText ? `<div class="cell-voiceover">🎙️ Lời thoại: ${esc(voiceoverText)}</div>` : ''}
      </div>
    `;
  } else {
    promptAndActionsHtml = `
      <div class="cell-prompt-box">
        <div class="cell-prompt-label">
          <span>📝 Prompt:</span>
          <span class="u-fs105 u-c-muted">${info.msg || ''}</span>
        </div>
        <div class="cell-prompt-text u-c-muted">
          ${esc(promptText || 'Đang chờ xử lý...')}
        </div>
        ${voiceoverText ? `<div class="cell-voiceover u-c-muted">🎙️ Lời thoại: ${esc(voiceoverText)}</div>` : ''}
      </div>
    `;
  }

  cell.innerHTML = `
    <div class="cell-header">
      <div class="cell-title">
        <span class="cell-icon">${icons[currentStatus] || '❓'}</span>
        <span>Cảnh ${idx}</span>
      </div>
      <span class="pill ${currentStatus}">${statusLabels[currentStatus] || currentStatus}</span>
    </div>
    ${mediaHtml}
    ${promptAndActionsHtml}
  `;
}

function toggleScenePromptEdit(idx) {
  const viewEl = document.getElementById(`scene-prompt-view-${idx}`);
  const editBox = document.getElementById(`scene-prompt-edit-box-${idx}`);
  if (!viewEl || !editBox) return;
  const isEditing = editBox.style.display !== 'none';
  if (isEditing) {
    editBox.style.display = 'none';
    viewEl.style.display = 'block';
  } else {
    editBox.style.display = 'flex';
    viewEl.style.display = 'none';
  }
}

async function retrySingleSceneDirect(idx, event) {
  if (event) event.stopPropagation();
  if (!activeJobId) {
    alert("Không tìm thấy job đang hoạt động!");
    return;
  }
  const editArea = document.getElementById(`edit-prompt-${idx}`);
  const updatedPrompt = editArea ? editArea.value.trim() : '';

  const cell = document.getElementById(`cell-${idx}`);
  if (cell) {
    cell.className = 'task-cell running';
    cell.setAttribute('data-status', 'running');
    cell.innerHTML = `
      <div class="cell-header">
        <div class="cell-title">
          <span class="cell-icon">⚙️</span>
          <span>Cảnh ${idx}</span>
        </div>
        <span class="pill running">⚙️ Đang xử lý</span>
      </div>
      <div class="cell-media" style="background:rgba(251,191,36,0.05);border:1px dashed rgba(251,191,36,0.3);flex-direction:column;gap:8px;">
        <div style="font-size:26px;animation:spin 2s linear infinite;">⚙️</div>
        <div class="u-fs12 u-c-yellow u-fw600">Đang khởi chạy render lại...</div>
      </div>
    `;
  }

  addLog(`🔄 [Cảnh ${idx}] Đang gửi yêu cầu render lại độc lập...`, 'info');

  try {
    const res = await apiFetch(`/api/jobs/${activeJobId}/scenes/${idx}/retry`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: updatedPrompt || undefined }),
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    addLog(`🚀 [Cảnh ${idx}] ${data.message}`, 'info');

    // Mở lại fallback poll nếu SSE đang đóng
    if (!sseSource) {
      pollJobFallback(activeJobId);
    }
  } catch (err) {
    alert(`Lỗi khi tạo lại cảnh ${idx}: ` + err.message);
    if (!sseSource) pollJobFallback(activeJobId);
  }
}

async function aiFixScenePromptDirect(idx, event) {
  if (event) event.stopPropagation();
  if (!activeJobId) return;

  const editArea = document.getElementById(`edit-prompt-${idx}`);
  const currentPrompt = editArea ? editArea.value.trim() : (scenes[idx - 1]?.prompt || '');

  const cell = document.getElementById(`cell-${idx}`);
  let errMsg = '';
  if (cell) {
    const mediaErr = cell.querySelector('.cell-media') || cell;
    if (mediaErr) errMsg = mediaErr.innerText;
  }

  addLog(`🪄 [Cảnh ${idx}] Đang phân tích lỗi và tạo prompt an toàn điện ảnh...`, 'info');

  try {
    const res = await apiFetch('/api/ai/fix-prompt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prompt: currentPrompt,
        error: errMsg,
        job_id: activeJobId,
        scene_index: idx,
      })
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    if (editArea) {
      editArea.value = data.fixed_prompt;
      editArea.style.borderColor = 'var(--yellow)';
      editArea.style.boxShadow = '0 0 8px rgba(251,191,36,0.3)';
    }

    if (scenes[idx - 1]) {
      scenes[idx - 1].prompt = data.fixed_prompt;
    }

    addLog(`✨ [Cảnh ${idx}] Prompt đã được AI tối ưu an toàn! Bạn có thể xem lại hoặc bấm 'Tạo lại cảnh này'.`, 'info');
  } catch (err) {
    alert(`Lỗi khi AI sửa prompt: ` + err.message);
  }
}

function addLog(msg, type = 'info') {
  const con = document.getElementById('logConsole');
  con.innerHTML += `<p>${esc(msg)}</p>`;
  con.scrollTop = con.scrollHeight;
}

async function retryFailedScenes() {
  if (!activeJobId) return;
  const btn = document.getElementById('btnRetryFailed');
  if (btn.disabled) return;
  const autoFix = document.getElementById('chkAutoFixPrompt')?.checked ?? true;
  btn.disabled = true;
  btn.textContent = '⏳ Đang gửi yêu cầu...';

  try {
    const res = await apiFetch(`/api/jobs/${activeJobId}/retry-failed?auto_fix=${autoFix}`, { method: 'POST' });
    const data = await res.json();
    if (data.error) {
      alert('Thông báo: ' + data.error);
      btn.disabled = false;
      btn.innerHTML = '🔄 Render lại cảnh bị lỗi';
      return;
    }
    btn.textContent = '⏳ Đang render lại các cảnh lỗi...';
    addLog(`🔄 ${data.message}`, 'info');

    // Mở lại SSE hoặc fallback poll để theo dõi tiến trình
    if (!sseSource) {
      pollJobFallback(activeJobId);
    }
  } catch (e) {
    alert('Lỗi khi render lại cảnh: ' + e.message);
    btn.disabled = false;
    btn.innerHTML = '🔄 Render lại cảnh bị lỗi';
  }
}

function sanitizePromptDirect(sceneId) {
  const el = document.querySelector(`#scene-${sceneId} .scene-prompt`);
  if (!el) return;
  const oldPrompt = el.value.trim();
  if (!oldPrompt) return;

  const replacements = [
    [/\b(?:blood|bloody|bleeding|blood-stained)\b/gi, "dramatic red rim light"],
    [/\bleaking\s+thick\s+(?:black\s+)?oil\s*(?:\([^)]*\))?/gi, "dark metallic armor reflecting bright embers"],
    [/\bseverely\s+injured\b/gi, "battle-tested warrior"],
    [/\bdying\s+flame\b/gi, "flickering mystic flame"],
    [/\bdeath|dying|suicidal|corpse\b/gi, "unyielding defiant"],
    [/\bgore|gory|severed|decapitated|slaughter\b/gi, "epic cinematic duel"],
    [/\bviolently\s+crushing\b/gi, "firmly holding"],
    [/\bcrushing\s+a\s+piece\s+of\s+shattered\b/gi, "examining an ancient glowing"],
    [/\bviolently\s+stomps?\s*(?:on)?\b/gi, "advances with thunderous steps toward"],
    [/\bfleeing\s+pawns\b/gi, "advancing pawns"],
    [/\bmagical\s+explosions?\s+blast\b/gi, "brilliant magical shockwaves illuminate"],
    [/\bcamera\s+violently\s+shakes\b/gi, "camera dynamically tracks"],
    [/\bbarrel\s+of\s+(?:her\s+)?(?:silver\s+)?gun\b/gi, "tip of her silver scepter"],
    [/\bholding\s+a\s+silver\s+gun\b/gi, "holding a mystical silver scepter"],
    [/\bsilver\s+gun\b/gi, "silver glowing artifact"]
  ];

  let newPrompt = oldPrompt;
  let count = 0;
  replacements.forEach(([re, rep]) => {
    if (re.test(newPrompt)) {
      newPrompt = newPrompt.replace(re, rep);
      count++;
    }
  });

  if (count > 0) {
    el.value = newPrompt;
    syncScenesFromDOM();
    alert(`🛡️ Đã thay thế ${count} yếu tố nhạy cảm thành ngôn ngữ điện ảnh an toàn!`);
  } else {
    alert("✅ Prompt này an toàn, không chứa từ khóa bạo lực hay vi phạm chính sách!");
  }
}

// ── Voice Preview Player ────────────────────────────────────────────────
let previewAudio = null;
let currentPreviewBtn = null;

async function testPlayVoice(voiceId, customText = '', triggerBtn = null) {
  if (!voiceId) return;

  const btn = triggerBtn || (window.event?.target?.closest('button'));

  // Nếu đang phát chính audio này và người dùng bấm lại -> Dừng phát
  if (previewAudio && !previewAudio.paused && currentPreviewBtn === btn) {
    try {
      previewAudio.pause();
      previewAudio.currentTime = 0;
    } catch (_) {}
    previewAudio = null;
    if (btn) {
      btn.innerHTML = btn.dataset.origHtml || '🔊 Nghe thử';
      btn.disabled = false;
    }
    currentPreviewBtn = null;
    return;
  }

  // Dừng bài đang phát trước đó (nếu có)
  if (previewAudio) {
    try {
      previewAudio.pause();
      previewAudio.currentTime = 0;
    } catch (_) {}
    previewAudio = null;
    if (currentPreviewBtn && currentPreviewBtn !== btn) {
      currentPreviewBtn.innerHTML = currentPreviewBtn.dataset.origHtml || '🔊 Nghe thử';
      currentPreviewBtn.disabled = false;
    }
  }

  // Cập nhật trạng thái loading cho nút bấm để tránh người dùng click dồn dập
  if (btn) {
    if (!btn.dataset.origHtml) btn.dataset.origHtml = btn.innerHTML;
    btn.innerHTML = '⏳ Đang tải...';
    btn.disabled = true;
    currentPreviewBtn = btn;
  }

  try {
    const url = `/api/voices/preview?voice=${encodeURIComponent(voiceId)}&text=${encodeURIComponent(customText || '')}&_t=${Date.now()}`;
    const audio = new Audio(url);
    previewAudio = audio;

    audio.oncanplay = () => {
      if (btn && currentPreviewBtn === btn) {
        btn.disabled = false;
      }
    };

    audio.onplaying = () => {
      if (btn && currentPreviewBtn === btn) {
        btn.innerHTML = '⏹️ Dừng';
        btn.disabled = false;
      }
    };

    audio.onended = () => {
      if (btn && currentPreviewBtn === btn) {
        btn.innerHTML = btn.dataset.origHtml || '🔊 Nghe thử';
        btn.disabled = false;
      }
      if (previewAudio === audio) previewAudio = null;
      if (currentPreviewBtn === btn) currentPreviewBtn = null;
    };

    audio.onerror = () => {
      if (btn && currentPreviewBtn === btn) {
        btn.innerHTML = btn.dataset.origHtml || '🔊 Nghe thử';
        btn.disabled = false;
      }
      if (previewAudio === audio) previewAudio = null;
      if (currentPreviewBtn === btn) currentPreviewBtn = null;
    };

    await audio.play();
  } catch (err) {
    // Bỏ qua AbortError khi pause() ngắt play() do người dùng chuyển giọng hoặc dừng phát
    if (err.name === 'AbortError') {
      return;
    }
    console.warn("Không thể phát giọng đọc thử:", err);
    if (btn && currentPreviewBtn === btn) {
      btn.innerHTML = btn.dataset.origHtml || '🔊 Nghe thử';
      btn.disabled = false;
      currentPreviewBtn = null;
    }
  }
}

// ── Library ─────────────────────────────────────────────────────────────
async function loadLibrary() {
  try {
    const res = await apiFetch('/api/jobs');
    const jobs = await res.json();
    const tbody = document.getElementById('jobsBody');
    if (!jobs.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="6">Chưa có job nào — tạo phim mới để bắt đầu.</td></tr>';
      return;
    }
    tbody.innerHTML = jobs.map(j => `
      <tr>
        <td class="job-id">${j.id}</td>
        <td><span class="pill ${j.status}">${statusIcon(j.status)} ${j.status}</span></td>
        <td class="job-prog">
          <div class="prog-wrap">
            <div class="prog-bar" style="width:${j.progress}%"></div>
          </div>
          <span class="job-meta">${j.progress}%</span>
        </td>
        <td>${j.total} cảnh</td>
        <td class="job-meta">${timeAgo(j.created_at)}</td>
        <td>
          <div class="job-actions">
            ${j.final_video ? `<a class="btn btn-ghost btn-sm" href="${j.final_video.replace('/video/','/download/')}">⬇️</a>
              <button class="btn btn-ghost btn-sm" onclick="watchJob('${j.id}')">▶️</button>` : ''}
            <button class="btn btn-ghost btn-sm u-c-red" onclick="deleteJob('${j.id}')">🗑️</button>
          </div>
        </td>
      </tr>
    `).join('');
  } catch (e) {
    console.error(e);
  }
}

function statusIcon(s) {
  return { running: '⚙️', done: '✅', error: '❌' }[s] || '❓';
}

function timeAgo(ts) {
  const d = Math.floor((Date.now() / 1000) - ts);
  if (d < 60) return `${d}s trước`;
  if (d < 3600) return `${Math.floor(d/60)}m trước`;
  return `${Math.floor(d/3600)}h trước`;
}

async function deleteJob(id) {
  if (!confirm(`Xoá job ${id}?`)) return;
  await apiFetch(`/api/jobs/${id}`, { method: 'DELETE' });
  loadLibrary();
}

async function watchJob(id) {
  const res = await apiFetch(`/api/jobs/${id}`);
  const data = await res.json();
  showPanel('monitor');
  document.getElementById('monitorEmpty').style.display = 'none';
  document.getElementById('monitorContent').style.display = 'block';
  document.getElementById('monJobTitle').textContent = `🎬 Job: ${id}`;
  document.getElementById('monJobId').textContent = `ID: ${id}`;
  updateMonitor(data, id);
}

// ── Reference Image Upload & Character Library ───────────────────────────
let characterLibrary = [];

async function loadCharacterLibrary() {
  try {
    const res = await apiFetch('/api/character-library');
    if (res.ok) {
      characterLibrary = await res.json();
    }
  } catch (e) {
    console.warn("Could not load character library:", e);
  }
}

function triggerSceneUpload(sceneId) {
  const inp = document.getElementById(`scene-file-${sceneId}`);
  if (inp) inp.click();
}

function handleDragOver(e, sceneId) {
  e.preventDefault();
  e.stopPropagation();
  const el = document.getElementById(`ref-zone-${sceneId}`);
  if (el) el.classList.add('dragover');
}

function handleDragLeave(e, sceneId) {
  e.preventDefault();
  e.stopPropagation();
  const el = document.getElementById(`ref-zone-${sceneId}`);
  if (el) el.classList.remove('dragover');
}

function handleSceneDrop(e, sceneId) {
  e.preventDefault();
  e.stopPropagation();
  const el = document.getElementById(`ref-zone-${sceneId}`);
  if (el) el.classList.remove('dragover');
  if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]) {
    processImageFile(sceneId, e.dataTransfer.files[0]);
  }
}

function handleSceneFileUpload(sceneId, input) {
  if (input.files && input.files[0]) {
    processImageFile(sceneId, input.files[0]);
  }
}

function processImageFile(sceneId, file) {
  if (!file.type.startsWith('image/')) {
    alert("Vui lòng chọn file hình ảnh (PNG, JPG, WEBP)!");
    return;
  }
  const reader = new FileReader();
  reader.onload = async (e) => {
    const dataUrl = e.target.result;
    syncScenesFromDOM();
    const s = scenes.find(sc => sc.id === sceneId);
    if (s) {
      s.reference_image = dataUrl;
      s.reference_image_name = file.name;
    }
    renderScenes();

    // Tải lên backend để lưu trữ vào thư viện nhân vật
    try {
      const formData = new FormData();
      formData.append('file', file);
      const res = await apiFetch('/api/upload-image', { method: 'POST', body: formData });
      if (res.ok) {
        await loadCharacterLibrary();
      }
    } catch (err) {
      console.warn("Upload error:", err);
    }
  };
  reader.readAsDataURL(file);
}

function clearSceneImage(sceneId) {
  syncScenesFromDOM();
  const s = scenes.find(sc => sc.id === sceneId);
  if (s) {
    s.reference_image = '';
    s.reference_image_name = '';
  }
  renderScenes();
}

function promptImageUrl(sceneId) {
  const url = prompt("Nhập đường link hình ảnh (URL):");
  if (url && url.trim()) {
    syncScenesFromDOM();
    const s = scenes.find(sc => sc.id === sceneId);
    if (s) {
      s.reference_image = url.trim();
      s.reference_image_name = url.trim().split('/').pop() || 'Ảnh Web';
    }
    renderScenes();
  }
}

function applyRecentCharImage(sceneId, url) {
  if (!url) return;
  syncScenesFromDOM();
  const s = scenes.find(sc => sc.id === sceneId);
  const found = characterLibrary.find(c => c.url === url);
  if (s) {
    s.reference_image = url;
    s.reference_image_name = found ? found.filename : url.split('/').pop();
  }
  renderScenes();
}

// Hỗ trợ dán ảnh trực tiếp từ Clipboard (Ctrl + V)
document.addEventListener('paste', (e) => {
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  for (let i = 0; i < items.length; i++) {
    if (items[i].type.indexOf('image') !== -1) {
      const file = items[i].getAsFile();
      const hoveredScene = document.querySelector('.scene-item:hover');
      let targetId = null;
      if (hoveredScene) {
        targetId = parseInt(hoveredScene.id.replace('scene-', ''));
      } else {
        const emptyScene = scenes.find(s => !s.reference_image);
        if (emptyScene) targetId = emptyScene.id;
      }
      if (targetId) {
        processImageFile(targetId, file);
        e.preventDefault();
      }
      break;
    }
  }
});

// ── Utils ───────────────────────────────────────────────────────────────
function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Character Profiles Studio & Recognition Engine ───────────────────────
let charactersList = [];
let sceneInputTimer = null;

async function loadCharacters() {
  try {
    const res = await apiFetch('/api/characters');
    if (res.ok) {
      charactersList = await res.json();
      renderCharacterGrid();
      populateCharVoiceDropdown();
    }
  } catch (e) {
    console.warn("Could not load characters:", e);
  }
}

function populateCharVoiceDropdown() {
  const sel = document.getElementById('charVoice');
  if (sel && (!sel.options || sel.options.length === 0)) {
    sel.innerHTML = voiceSelectHtml('vi-VN-HoaiMyNeural');
  }
}

function renderCharacterGrid() {
  const grid = document.getElementById('charListGrid');
  if (!grid) return;

  if (!charactersList || charactersList.length === 0) {
    grid.innerHTML = `
      <div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--muted);background:var(--bg0);border-radius:8px;border:1px dashed var(--border);">
        <div style="font-size:36px;margin-bottom:8px;">👥</div>
        <div class="u-fs14 u-fw600 u-c-text">Chưa có hồ sơ nhân vật nào</div>
        <div class="u-fs12 u-mt4">Bấm "➕ Thêm nhân vật mới" phía trên để tạo nhân vật đầu tiên!</div>
      </div>`;
    return;
  }

  grid.innerHTML = charactersList.map(c => {
    const aliasHtml = (c.aliases || []).map(a => `<span class="char-alias-tag">${esc(a)}</span>`).join('');
    const voiceObj = VOICES.find(v => v.id === c.voice);
    const voiceLabel = voiceObj ? `${voiceObj.gender === 'female' ? 'Nữ' : 'Nam'} - ${voiceObj.label}` : (c.voice || 'Mặc định');

    return `
      <div class="char-card" id="char-card-${c.id}">
        <div class="char-card-header">
          ${c.avatar ? `
            <img loading="lazy" decoding="async" src="${esc(c.avatar)}" class="char-card-avatar" alt="${esc(c.name)}">
          ` : `
            <div class="char-card-avatar fx fx-ac fx-jc-c u-fs24">👤</div>
          `}
          <div class="char-card-info">
            <div class="char-card-name">${esc(c.name)}</div>
            <div class="char-card-voice fx fx-ac fx-g6">
              <span>🎙️ ${esc(voiceLabel)}</span>
              <button type="button" class="btn btn-ghost btn-sm" style="font-size:10px;padding:1px 5px;" onclick="testPlayVoice('${esc(c.voice)}')">🔊</button>
            </div>
          </div>
        </div>

        ${c.description ? `
          <div class="char-card-desc" title="${esc(c.description)}">
            <b>Mô tả:</b> ${esc(c.description)}
          </div>
        ` : ''}

        ${aliasHtml ? `
          <div class="char-card-aliases">
            <span style="font-size:11px;color:var(--muted);align-self:center;">Từ khóa:</span>
            ${aliasHtml}
          </div>
        ` : ''}

        <!-- Production Bible 4-Angle Gallery -->
        <div class="char-sheet-box">
          <div class="char-sheet-header">
            <div class="char-sheet-title">
              <span>📐</span> <span>Production Bible (4 góc)</span>
            </div>
            <button type="button" class="char-sheet-all-btn" onclick="generateFullCharacterSheet('${c.id}', 'all')" title="Dùng AI sinh trọn bộ 4 góc dựa trên ảnh gốc và mô tả">
              🎨 Sinh cả 4 góc
            </button>
          </div>
          <div class="char-sheet-grid">
            ${['front', 'side', 'closeup', 'fullbody'].map(ang => {
              const labels = { front: 'Chính diện', side: 'Góc nghiêng', closeup: 'Cận mặt', fullbody: 'Toàn thân' };
              const icons = { front: '👤', side: '↗️', closeup: '🔍', fullbody: '🧍' };
              const imgUrl = (c.character_sheet && c.character_sheet[ang]) || (ang === 'front' ? c.avatar : '');
              return `
                <div class="char-sheet-slot ${imgUrl ? 'has-img' : ''}">
                  <div class="char-sheet-label">${icons[ang]} ${labels[ang]}</div>
                  <div class="char-sheet-preview">
                    ${imgUrl ? `
                      <img loading="lazy" decoding="async" src="${esc(imgUrl)}" class="char-sheet-thumb" title="Click để xem lớn" onclick="window.open('${esc(imgUrl)}')">
                    ` : `
                      <div class="char-sheet-empty">
                        <span class="char-sheet-empty-icon">${icons[ang]}</span>
                        <span class="char-sheet-empty-text">Chưa có</span>
                      </div>
                    `}
                  </div>
                  <div class="char-sheet-btn-row">
                    ${imgUrl ? `
                      <button type="button" class="btn btn-ghost btn-sm char-sheet-action-btn" onclick="generateFullCharacterSheet('${c.id}', '${ang}')" title="Sinh lại góc này bằng AI">🪄 Lại</button>
                      <button type="button" class="btn btn-ghost btn-sm char-sheet-icon-btn" onclick="promptSetSheetAngle('${c.id}', '${ang}')" title="Nhập URL ảnh">🌐</button>
                    ` : `
                      <button type="button" class="btn btn-primary btn-sm char-sheet-action-btn" onclick="generateFullCharacterSheet('${c.id}', '${ang}')" title="Sinh góc này bằng AI">🪄 Sinh</button>
                      <button type="button" class="btn btn-ghost btn-sm char-sheet-icon-btn" onclick="promptSetSheetAngle('${c.id}', '${ang}')" title="Nhập URL ảnh">🌐</button>
                    `}
                  </div>
                </div>
              `;
            }).join('')}
          </div>
        </div>

        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:auto;padding-top:10px;border-top:1px solid var(--border);">
          <button class="btn btn-ghost btn-sm u-fs11" onclick="linkCharacterToAllMatchingScenes('${c.id}')" title="Quét và đồng bộ cho các cảnh có tên này">🎯 Đồng bộ vào cảnh</button>
          <button class="btn btn-ghost btn-sm u-fs11" onclick="editCharacter('${c.id}')">✏️ Sửa</button>
          <button class="btn btn-ghost btn-sm u-fs11 u-c-red" onclick="deleteCharacter('${c.id}')">🗑️ Xóa</button>
        </div>
      </div>
    `;
  }).join('');
}

function openNewCharModal() {
  document.getElementById('charFormTitle').textContent = '➕ Thêm nhân vật mới';
  document.getElementById('charEditId').value = '';
  document.getElementById('charName').value = '';
  document.getElementById('charAliases').value = '';
  document.getElementById('charAvatarUrl').value = '';
  document.getElementById('charDescription').value = '';
  updateCharAvatarPreview('');
  populateCharVoiceDropdown();
  document.getElementById('charFormCard').style.display = 'block';
  document.getElementById('charName').focus();
}

function closeCharForm() {
  document.getElementById('charFormCard').style.display = 'none';
}

function updateCharAvatarPreview(url) {
  const img = document.getElementById('charAvatarImg');
  const empty = document.getElementById('charAvatarEmpty');
  if (url && url.trim()) {
    img.src = url.trim();
    img.style.display = 'block';
    empty.style.display = 'none';
  } else {
    img.src = '';
    img.style.display = 'none';
    empty.style.display = 'inline';
  }
}

async function handleCharAvatarUpload(input) {
  if (input.files && input.files[0]) {
    const file = input.files[0];
    const formData = new FormData();
    formData.append('file', file);
    try {
      const res = await apiFetch('/api/upload-image', { method: 'POST', body: formData });
      if (res.ok) {
        const data = await res.json();
        document.getElementById('charAvatarUrl').value = data.url;
        updateCharAvatarPreview(data.url);
        await loadCharacterLibrary();
      } else {
        alert("Không thể tải ảnh lên server!");
      }
    } catch (e) {
      alert("Lỗi upload: " + e.message);
    }
  }
}

function editCharacter(charId) {
  const c = charactersList.find(ch => ch.id === charId);
  if (!c) return;

  populateCharVoiceDropdown();
  document.getElementById('charFormTitle').textContent = `✏️ Chỉnh sửa hồ sơ: ${c.name}`;
  document.getElementById('charEditId').value = c.id;
  document.getElementById('charName').value = c.name || '';
  document.getElementById('charAliases').value = (c.aliases || []).join(', ');
  document.getElementById('charVoice').value = c.voice || 'vi-VN-HoaiMyNeural';
  document.getElementById('charAvatarUrl').value = c.avatar || '';
  document.getElementById('charDescription').value = c.description || '';
  updateCharAvatarPreview(c.avatar || '');
  document.getElementById('charFormCard').style.display = 'block';
  document.getElementById('charFormCard').scrollIntoView({ behavior: 'smooth' });
}

async function saveCharacterFromForm() {
  const name = document.getElementById('charName').value.trim();
  if (!name) {
    alert("Vui lòng nhập tên nhân vật!");
    document.getElementById('charName').focus();
    return;
  }

  const id = document.getElementById('charEditId').value.trim() || undefined;
  const rawAliases = document.getElementById('charAliases').value.split(',');
  const aliases = rawAliases.map(a => a.trim()).filter(a => a.length > 0);
  const voice = document.getElementById('charVoice').value;
  const avatar = document.getElementById('charAvatarUrl').value.trim();
  const description = document.getElementById('charDescription').value.trim();

  const payload = { id, name, aliases, voice, avatar, description };

  try {
    const res = await apiFetch('/api/characters', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (res.ok) {
      await loadCharacters();
      closeCharForm();
      // Re-scan scenes to update character badges
      syncScenesFromDOM();
      scenes.forEach(s => autoLinkCharacterToScene(s));
      renderScenes();
    } else {
      const err = await res.json();
      alert("Lỗi lưu nhân vật: " + (err.detail || err.error || "Không thể lưu"));
    }
  } catch (e) {
    alert("Lỗi kết nối: " + e.message);
  }
}

async function deleteCharacter(charId) {
  const c = charactersList.find(ch => ch.id === charId);
  const cName = c ? c.name : 'nhân vật này';
  if (!confirm(`Bạn có chắc chắn muốn xóa hồ sơ "${cName}"?`)) return;

  try {
    const res = await apiFetch(`/api/characters/${charId}`, { method: 'DELETE' });
    if (res.ok) {
      await loadCharacters();
      syncScenesFromDOM();
      renderScenes();
    } else {
      alert("Không thể xóa nhân vật!");
    }
  } catch (e) {
    alert("Lỗi xóa nhân vật: " + e.message);
  }
}

function linkCharacterToAllMatchingScenes(charId) {
  const c = charactersList.find(ch => ch.id === charId);
  if (!c) return;

  syncScenesFromDOM();
  let count = 0;
  scenes.forEach(s => {
    const fullText = `${s.title} ${s.prompt} ${s.voiceover}`.toLowerCase();
    const isMatched = (c.name && fullText.includes(c.name.toLowerCase())) ||
                      (c.aliases && c.aliases.some(a => fullText.includes(a.toLowerCase())));
    if (isMatched) {
      if (c.avatar) {
        s.reference_image = c.avatar;
        s.reference_image_name = `Khóa: ${c.name}`;
      }
      if (c.voice) {
        s.voice = c.voice;
      }
      count++;
    }
  });

  renderScenes();
  alert(`✅ Đã đồng bộ hồ sơ "${c.name}" vào ${count} cảnh phù hợp!`);
}

// ── Real-time Scene Matching Logic ──────────────────────────────────────
function detectCharactersInScene(scene) {
  if (!charactersList || charactersList.length === 0) return null;

  let segText = '';
  if (scene.voiceSegments && scene.voiceSegments.length > 0) {
    segText = scene.voiceSegments.map(v => (v.character || '') + ' ' + (v.text || '')).join(' ');
  }
  const fullText = `${scene.title || ''} ${scene.prompt || ''} ${scene.voiceover || ''} ${segText}`.toLowerCase();
  if (!fullText.trim()) return null;

  const sorted = [...charactersList].sort((a, b) => {
    const maxA = Math.max(a.name.length, ...(a.aliases || []).map(al => al.length));
    const maxB = Math.max(b.name.length, ...(b.aliases || []).map(al => al.length));
    return maxB - maxA;
  });

  for (const ch of sorted) {
    if (ch.name && fullText.includes(ch.name.toLowerCase())) {
      return ch;
    }
    if (ch.aliases && ch.aliases.length > 0) {
      for (const al of ch.aliases) {
        if (al && al.trim() && fullText.includes(al.trim().toLowerCase())) {
          return ch;
        }
      }
    }
  }
  return null;
}

function autoLinkCharacterToScene(scene) {
  const ch = detectCharactersInScene(scene);
  if (ch) {
    if (!scene.reference_image && ch.avatar) {
      scene.reference_image = ch.avatar;
      scene.reference_image_name = `[Tự động] Khóa: ${ch.name}`;
    }
    if (!scene.voice && ch.voice) {
      scene.voice = ch.voice;
    }
  }
  return ch;
}

function applyCharToScene(sceneId, charId, mode) {
  syncScenesFromDOM();
  const s = scenes.find(sc => sc.id === sceneId);
  const ch = charactersList.find(c => c.id === charId);
  if (!s || !ch) return;

  if (mode === 'image' || mode === 'all') {
    if (ch.avatar) {
      s.reference_image = ch.avatar;
      s.reference_image_name = `Khóa: ${ch.name}`;
    }
  }

  if (mode === 'desc' || mode === 'all') {
    if (ch.description) {
      let cur = s.prompt.trim();
      if (!cur.toLowerCase().includes(ch.name.toLowerCase())) {
        cur = `${ch.name}, ${cur}`;
      }
      if (!cur.toLowerCase().includes(ch.description.slice(0, 20).toLowerCase())) {
        cur += cur.endsWith('.') ? ` ${ch.description}` : `, ${ch.description}`;
      }
      s.prompt = cur;
    }
  }

  if (mode === 'all') {
    if (ch.voice) {
      s.voice = ch.voice;
    }
  }

  renderScenes();
}

function onSceneContentInput(sceneId) {
  clearTimeout(sceneInputTimer);
  sceneInputTimer = setTimeout(() => {
    const el = document.getElementById(`scene-${sceneId}`);
    if (!el) return;
    const s = scenes.find(item => item.id === sceneId);
    if (!s) return;

    s.prompt = el.querySelector('.scene-prompt')?.value || '';
    s.voiceover = el.querySelector('.scene-voice')?.value || '';
    s.title = el.querySelector('.scene-title-input')?.value || '';

    const matchedChar = detectCharactersInScene(s);
    const container = document.getElementById(`scene-char-match-${sceneId}`);
    if (!container) return;

    if (matchedChar) {
      if (!s.reference_image && matchedChar.avatar) {
        s.reference_image = matchedChar.avatar;
        s.reference_image_name = `[Tự động] Khóa: ${matchedChar.name}`;
        renderScenes();
        return;
      }
      if (!s.voice && matchedChar.voice) {
        const vSel = el.querySelector('.scene-voice-sel');
        if (vSel) vSel.value = matchedChar.voice;
        s.voice = matchedChar.voice;
      }

      const sheet = matchedChar.character_sheet || {};
      const angles = [
        { key: 'front', label: '👤 Chính diện' },
        { key: 'side', label: '↗️ Góc nghiêng' },
        { key: 'closeup', label: '🔍 Cận mặt' },
        { key: 'fullbody', label: '🧍 Toàn thân' }
      ];
      const curAngle = s.sheet_angle || 'front';
      const angleButtonsHtml = angles.map(ang => {
        const hasImg = !!sheet[ang.key];
        const isSel = curAngle === ang.key;
        return `
          <button type="button" class="btn btn-ghost btn-sm"
            style="font-size:10.5px;padding:2px 7px;${isSel ? 'border-color:var(--accent);color:var(--accent);background:rgba(99,102,241,0.14);font-weight:700;' : ''}${!hasImg ? 'opacity:0.6;' : ''}"
            onclick="applyCharSheetAngle(${s.id}, '${matchedChar.id}', '${ang.key}')"
            title="${hasImg ? `Khóa góc ${ang.label} từ Character Sheet` : `Góc này chưa có ảnh trong Sheet - bấm để tạo hoặc dùng ảnh avatar`}">
            ${ang.label} ${hasImg ? '✓' : ''}
          </button>`;
      }).join('');

      container.outerHTML = `
        <div class="char-match-banner" id="scene-char-match-${s.id}">
          <div style="width:36px;height:36px;border-radius:50%;overflow:hidden;background:var(--bg2);flex-shrink:0;border:2px solid var(--accent);display:flex;align-items:center;justify-content:center;">
            ${matchedChar.avatar ? `<img loading="lazy" decoding="async" src="${esc(matchedChar.avatar)}" style="width:100%;height:100%;object-fit:cover;">` : `<span class="u-fs18">👤</span>`}
          </div>
          <div class="fx-f1">
            <div class="u-fs125 u-fw700 u-c-text fx fx-ac fx-g6 fx-wrap">
              <span>🎯 Đã nhận diện nhân vật: <b class="u-c-accent">${esc(matchedChar.name)}</b></span>
              <span class="pill running u-fs10 u-p-1-6">Tự động tham chiếu</span>
            </div>
            <div class="u-fs11 u-c-muted u-mt2">
              🎙️ Giọng: <b>${esc(matchedChar.voice || 'Mặc định')}</b> ${matchedChar.description ? `• Tạo hình: ${esc(matchedChar.description.slice(0, 50))}...` : ''}
            </div>
            <div class="fx fx-ac fx-g5 fx-wrap u-mt5">
              <span class="u-fs11 u-c-muted u-fw600">Góc quay Bible:</span>
              ${angleButtonsHtml}
            </div>
          </div>
          <div class="fx fx-g5 fx-ac fx-wrap">
            ${matchedChar.avatar ? `
              <button type="button" class="btn btn-ghost btn-sm u-fs11 u-p-2-7 u-c-accent" onclick="applyCharToScene(${s.id}, '${matchedChar.id}', 'image')" title="Khóa ảnh tham chiếu này cho cảnh">🔒 Khóa ảnh</button>
            ` : ''}
            ${matchedChar.description ? `
              <button type="button" class="btn btn-ghost btn-sm u-fs11 u-p-2-7 u-c-green" onclick="applyCharToScene(${s.id}, '${matchedChar.id}', 'desc')" title="Chèn mô tả tạo hình vào Prompt">➕ Thêm mô tả</button>
            ` : ''}
            <button type="button" class="btn btn-ghost btn-sm u-fs11 u-p-2-7" onclick="applyCharToScene(${s.id}, '${matchedChar.id}', 'all')" title="Đồng bộ ảnh, giọng nói và mô tả">⚡ Áp dụng tất cả</button>
          </div>
        </div>`;
    } else {
      container.outerHTML = `<div id="scene-char-match-${s.id}"></div>`;
    }
  }, 350);
}

// ── Production Bible & Chaining Handlers ────────────────────────────────
function applyCharSheetAngle(sceneId, charId, angle) {
  syncScenesFromDOM();
  const s = scenes.find(sc => sc.id === sceneId);
  const ch = charactersList.find(c => c.id === charId);
  if (!s || !ch) return;

  s.sheet_angle = angle;
  const sheet = ch.character_sheet || {};
  if (sheet[angle]) {
    s.reference_image = sheet[angle];
    s.reference_image_name = `${ch.name} (${angle})`;
  } else if (ch.avatar) {
    s.reference_image = ch.avatar;
    s.reference_image_name = `${ch.name} (Gốc)`;
  }
  renderScenes();
}

function toggleSceneChaining(sceneId, isChecked) {
  syncScenesFromDOM();
  const s = scenes.find(sc => sc.id === sceneId);
  if (s) {
    s.chain_from_previous = isChecked;
  }
  if (!updateSceneCard(sceneId)) renderScenes();
}

async function generateFullCharacterSheet(charId, angle = 'all') {
  const c = charactersList.find(ch => ch.id === charId);
  if (!c) return;
  const angleLabel = angle === 'all' ? 'trọn bộ 4 góc' : `góc ${angle}`;

  const btn = event?.currentTarget;
  const origText = btn ? btn.innerHTML : '';
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '⏳ Đang sinh...';
  }

  try {
    const res = await apiFetch(`/api/characters/${charId}/generate-sheet`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ angle })
    });
    const data = await res.json();
    if (data.error || !res.ok) throw new Error(data.detail || data.error || 'Lỗi server');
    await loadCharacters();
    renderScenes();
    alert(`🎉 Đã sinh thành công ${angleLabel} cho nhân vật "${c.name}"!`);
  } catch (e) {
    alert(`Lỗi sinh Character Sheet: ${e.message}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = origText;
    }
  }
}

async function promptSetSheetAngle(charId, angle) {
  const url = prompt(`Nhập URL ảnh cho góc "${angle}":`);
  if (!url || !url.trim()) return;
  try {
    const res = await apiFetch(`/api/characters/${charId}/set-sheet-angle`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ angle, image_url: url.trim() })
    });
    if (res.ok) {
      await loadCharacters();
      renderScenes();
    } else {
      alert("Không thể cập nhật ảnh!");
    }
  } catch (e) {
    alert("Lỗi: " + e.message);
  }
}

function openBeatBreakdownModal() {
  const card = document.getElementById('beatBreakdownCard');
  if (card) {
    card.style.display = 'block';
    card.scrollIntoView({ behavior: 'smooth' });
    document.getElementById('beatScriptInput')?.focus();
  }
}

function closeBeatBreakdownModal() {
  const card = document.getElementById('beatBreakdownCard');
  if (card) card.style.display = 'none';
}

async function executeBeatBreakdown() {
  const input = document.getElementById('beatScriptInput');
  const text = input ? input.value.trim() : '';
  if (!text) {
    alert("Vui lòng nhập kịch bản hoặc đoạn văn cần phân rã nhịp!");
    return;
  }

  const btn = document.getElementById('btnRunBeatBreakdown');
  const origText = btn ? btn.innerHTML : '';
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '⏳ Đang dùng Muse AI phân rã nhịp 3-5s...';
  }

  try {
    const res = await apiFetch('/api/ai/breakdown-beats', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ script_text: text, target_beat_duration: 4 })
    });
    const data = await res.json();
    if (data.beats && data.beats.length > 0) {
      clearAll();
      const globalRatio = document.getElementById('globalAspectRatio')?.value || cfg.aspectRatio || "16:9";
      data.beats.forEach((b, idx) => {
        addScene(
          b.prompt,
          b.voiceover || '',
          b.duration || 4,
          globalRatio,
          b.title || `Nhịp ${idx + 1}`,
          '',
          [],
          b.chain_from_previous !== undefined ? b.chain_from_previous : (idx > 0),
          b.camera_angle || 'front'
        );
      });
      closeBeatBreakdownModal();
      alert(`🎉 Đã phân rã thành ${data.beats.length} nhịp điện ảnh với First/Last Frame Chaining được tự động kích hoạt!`);
    } else {
      alert("Không thể phân rã nhịp. Vui lòng thử lại!");
    }
  } catch (e) {
    alert("Lỗi: " + e.message);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = origText;
    }
  }
}

// ── Init ────────────────────────────────────────────────────────────────
(async function init() {
  loadCfg();
  checkStudioAuthStatus();
  await loadVoices();
  await loadCharacterLibrary();
  await loadCharacters();
  loadSampleStoryboard();
  checkApiHealth();
  // Auto refresh library every 30s if panel is visible
  setInterval(() => {
    if (document.hidden) return; // tab an: dung refresh thu vien (da co SSE/poll rieng khi mo lai)
    if (document.getElementById('panel-library').classList.contains('active')) {
      loadLibrary();
    }
  }, 30000);
})();

// ── QUICK VIDEO ────────────────────────────────────────────────────────────
let _qvRefDataUrl = null;   // base64 data URL của ảnh reference đã chọn
let _qvPollTimer  = null;   // setInterval handle khi đang poll
let _qvTaskId     = null;   // task_id đang poll
let _qvHistory    = [];     // lịch sử trong phiên [{taskId, prompt, videoUrl}]

async function handleQvRefUpload(input) {
  const file = input.files[0];
  if (!file) return;
  document.getElementById('qvRefName').textContent = file.name;
  document.getElementById('qvRefClearBtn').style.display = '';

  // Upload tới server để dùng lại URL chuẩn
  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await apiFetch('/api/upload-image', { method: 'POST', body: fd });
    const d = await r.json();
    if (d.success) {
      _qvRefDataUrl = d.url;          // lưu /api/uploads/... path
      document.getElementById('qvRefImg').src = d.url;
    } else {
      // fallback: dùng data URL trực tiếp
      _qvRefDataUrl = d.data_url;
      document.getElementById('qvRefImg').src = d.data_url;
    }
  } catch {
    // fallback local preview
    const reader = new FileReader();
    reader.onload = e => {
      _qvRefDataUrl = e.target.result;
      document.getElementById('qvRefImg').src = e.target.result;
    };
    reader.readAsDataURL(file);
  }
  document.getElementById('qvRefPreview').style.display = '';
}

function clearQvRef() {
  _qvRefDataUrl = null;
  document.getElementById('qvRefFile').value = '';
  document.getElementById('qvRefName').textContent = 'Chưa chọn ảnh';
  document.getElementById('qvRefClearBtn').style.display = 'none';
  document.getElementById('qvRefPreview').style.display = 'none';
  document.getElementById('qvRefImg').src = '';
}

function _qvSetStatus(msg, color = 'var(--text-muted)') {
  const el = document.getElementById('qvStatus');
  el.style.color = color;
  el.textContent = msg;
}

function _qvSetProgress(pct, label) {
  document.getElementById('qvProgressBar').style.width = pct + '%';
  document.getElementById('qvProgressLabel').textContent = label;
}

async function enhanceQvPrompt() {
  const ta = document.getElementById('qvPrompt');
  const prompt = ta.value.trim();
  if (!prompt) { showToast('Vui lòng nhập nội dung cần nâng cấp!', 'error'); return; }
  const size = document.getElementById('qvSize').value;
  const btn = document.getElementById('qvEnhanceBtn');
  const origBtnText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '⏳ Đang dịch & nâng cấp...';
  try {
    const r = await apiFetch('/api/quick-video/enhance-prompt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt, size })
    });
    const d = await r.json();
    if (d && d.enhanced) {
      ta.value = d.enhanced;
      showToast('✨ Đã nâng cấp thành Prompt Cinematic Hollywood!');
    }
  } catch (err) {
    showToast('Lỗi nâng cấp: ' + err, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = origBtnText;
  }
}

async function generateQuickVideo() {

  const prompt = document.getElementById('qvPrompt').value.trim();
  if (!prompt) { showToast('Vui lòng nhập prompt!', 'error'); return; }

  const size     = document.getElementById('qvSize').value;
  const duration = parseInt(document.getElementById('qvDuration').value) || 5;

  // Reset UI
  document.getElementById('qvResultCard').style.display = '';
  document.getElementById('qvProgressWrap').style.display = '';
  document.getElementById('qvVideoWrap').style.display = 'none';
  document.getElementById('qvError').style.display = 'none';
  document.getElementById('qvError').textContent = '';
  document.getElementById('qvGenerateBtn').disabled = true;
  _qvSetStatus('⏳ Đang gửi yêu cầu…');
  _qvSetProgress(5, 'Đang gửi tới muse2api…');

  const body = { prompt, size, duration };
  if (_qvRefDataUrl) body.reference_image = _qvRefDataUrl;

  try {
    const r = await apiFetch('/api/quick-video', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const d = await r.json();
    if (!r.ok || !d.success) {
      throw new Error(d.error || `HTTP ${r.status}`);
    }

    _qvTaskId = d.task_id || d.id;
    _qvSetStatus(`🎬 Đang render… (task: ${_qvTaskId?.slice(0,8)})`);
    _qvSetProgress(15, 'Muse AI đang xử lý…');

    // Start polling
    if (_qvPollTimer) clearInterval(_qvPollTimer);
    let elapsed = 0;
    _qvPollTimer = setInterval(async () => {
      if (document.hidden) return; // tab an: tam dung poll Quick Video, mo lai se tiep tuc
      elapsed += 3;
      // fake progress: tăng dần lên tối đa 90% cho tới khi done
      const fakePct = Math.min(15 + Math.floor(elapsed / 120 * 75), 90);
      _qvSetProgress(fakePct, `Đang render… (~${elapsed}s)`);
      await _qvPollTask(_qvTaskId);
    }, 3000);

  } catch (err) {
    _qvShowError(err.message || String(err));
  }
}

async function _qvPollTask(taskId) {
  try {
    const r = await apiFetch(`/api/quick-video/${taskId}`);
    const d = await r.json();

    if (d.status === 'done' || d.status === 'completed') {
      clearInterval(_qvPollTimer);
      _qvPollTimer = null;
      _qvSetProgress(100, '✅ Hoàn thành!');

      const videoUrl = d.video_proxy_url || d.video_url || d.url || (d.result && d.result.url);
      const playUrl = (videoUrl && videoUrl.startsWith('/api/quick-video/'))
        ? videoUrl
        : (videoUrl && !videoUrl.startsWith('http'))
          ? `/api/quick-video/${taskId}/stream`
          : videoUrl;


      document.getElementById('qvVideoPlayer').src = playUrl || '';
      document.getElementById('qvDownloadLink').href = playUrl || '#';
      document.getElementById('qvVideoWrap').style.display = '';
      document.getElementById('qvProgressWrap').style.display = 'none';
      _qvSetStatus('✅ Xong!', 'var(--success)');
      document.getElementById('qvGenerateBtn').disabled = false;

      // Save to history
      const prompt = document.getElementById('qvPrompt').value.trim();
      _qvAddHistory({ taskId, prompt, videoUrl: playUrl });

    } else if (d.status === 'error' || d.error) {
      clearInterval(_qvPollTimer);
      _qvPollTimer = null;
      _qvShowError(d.error || d.message || 'Lỗi không xác định');
    }
    // 'pending' / 'running' → tiếp tục poll
  } catch (err) {
    // Lỗi mạng tạm thời → bỏ qua, tiếp tục poll
    console.warn('[QV poll error]', err);
  }
}

function _qvShowError(msg) {
  document.getElementById('qvError').textContent = '❌ ' + msg;
  document.getElementById('qvError').style.display = '';
  document.getElementById('qvProgressWrap').style.display = 'none';
  _qvSetStatus('❌ Lỗi', '#ef4444');
  document.getElementById('qvGenerateBtn').disabled = false;
}

function _qvAddHistory(item) {
  _qvHistory.unshift(item);
  _renderQvHistory();
}

function _renderQvHistory() {
  const grid = document.getElementById('qvHistoryGrid');
  if (_qvHistory.length === 0) {
    grid.innerHTML = '<div style="color:var(--text-muted);font-size:13px;grid-column:1/-1;text-align:center;padding:20px;">Chưa có video nào được tạo trong phiên này</div>';
    return;
  }
  grid.innerHTML = _qvHistory.map((h, i) => `
    <div style="background:var(--surface-2);border-radius:8px;padding:8px;border:1px solid var(--border);">
      <video src="${h.videoUrl}" style="width:100%;border-radius:5px;background:#000;" muted loop
        onmouseenter="this.play()" onmouseleave="this.pause()"></video>
      <div style="font-size:11px;color:var(--text-muted);margin-top:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"
        title="${h.prompt}">${h.prompt}</div>
      <div class="fx fx-g5 u-mt5">
        <a href="${h.videoUrl}" download="quick_video_${i+1}.mp4" class="btn btn-ghost btn-sm u-fs11">⬇️</a>
        <button class="btn btn-ghost btn-sm u-fs11"
          onclick="document.getElementById('qvPrompt').value=${JSON.stringify(h.prompt)}">📋</button>
      </div>
    </div>
  `).join('');
}

function clearQvHistory() {
  _qvHistory = [];
  _renderQvHistory();
}

function copyQvPromptToScene() {
  const prompt = document.getElementById('qvPrompt').value.trim();
  if (!prompt) return;
  showPanel('create');
  // Thêm 1 cảnh mới có prompt từ quick video
  addScene();
  const scenes = document.querySelectorAll('.scene-block');
  if (scenes.length > 0) {
    const last = scenes[scenes.length - 1];
    const ta = last.querySelector('textarea[data-field="prompt"]');
    if (ta) { ta.value = prompt; ta.dispatchEvent(new Event('input')); }
  }
  showToast('✅ Đã thêm prompt vào cảnh mới!');
}

// ════════════════════════════════════════════════════════════════════════════
// ── FILMMAKING PIPELINE & AUDIT MODULES ─────────────────────────────────────
// ════════════════════════════════════════════════════════════════════════════

// ── 1. Music Management ───────────────────────────────────────────────────
async function loadMusicList() {
  try {
    const res = await apiFetch('/api/music/list');
    const list = await res.json();
    const sel = document.getElementById('bgMusicSelect');
    if (!sel) return;
    const curVal = sel.value;
    sel.innerHTML = '<option value="">🚫 Không dùng nhạc nền</option>' +
      list.map(m => `<option value="${esc(m.path)}" data-url="${esc(m.url)}">🎵 ${esc(m.name)}</option>`).join('');
    if (curVal) sel.value = curVal;
  } catch (e) {
    console.warn('[loadMusicList error]', e);
  }
}

async function uploadMusicFile(input) {
  if (!input.files || !input.files[0]) return;
  const file = input.files[0];
  const fd = new FormData();
  fd.append('file', file);
  try {
    const res = await apiFetch('/api/upload/music', { method: 'POST', body: fd });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    await loadMusicList();
    const sel = document.getElementById('bgMusicSelect');
    if (sel) sel.value = data.path;
    alert(`✅ Đã tải lên nhạc nền "${data.name}"!`);
  } catch (e) {
    alert(`Lỗi tải nhạc: ${e.message}`);
  }
}

// ── 2. Style Locks Panel Toggle ───────────────────────────────────────────
function toggleStyleLocksPanel() {
  const box = document.getElementById('styleLocksContent');
  const icon = document.getElementById('styleLocksToggleIcon');
  if (!box) return;
  if (box.style.display === 'none') {
    box.style.display = 'block';
    if (icon) icon.textContent = '▲ Thu gọn';
  } else {
    box.style.display = 'none';
    if (icon) icon.textContent = '▼ Mở rộng';
  }
}

// ── 3. Drag and Drop Scene Reordering ─────────────────────────────────────
let draggedSceneId = null;

function handleSceneDragStart(e, id) {
  draggedSceneId = id;
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', String(id));
  e.target.closest('.scene-item')?.classList.add('dragging');
}

function handleSceneDragOver(e) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
}

function handleSceneDropReorder(e, targetId) {
  e.preventDefault();
  document.querySelectorAll('.scene-item').forEach(el => el.classList.remove('dragging'));
  if (draggedSceneId === null || draggedSceneId === targetId) return;
  syncScenesFromDOM();
  const fromIdx = scenes.findIndex(s => s.id === draggedSceneId);
  const toIdx = scenes.findIndex(s => s.id === targetId);
  if (fromIdx !== -1 && toIdx !== -1) {
    const [moved] = scenes.splice(fromIdx, 1);
    scenes.splice(toIdx, 0, moved);
    renderScenes();
    showToast(`🔄 Đã đổi Cảnh ${fromIdx + 1} sang vị trí Cảnh ${toIdx + 1}!`);
  }
  draggedSceneId = null;
}

// ── 4. Keyframe Preview Generation ─────────────────────────────────────────
async function previewSceneKeyframe(sceneId) {
  syncScenesFromDOM();
  const sc = scenes.find(s => s.id === sceneId);
  if (!sc || !sc.prompt) {
    alert('Vui lòng nhập Prompt trước khi xem Keyframe!');
    return;
  }
  const ratio = sc.size || document.getElementById('globalAspectRatio')?.value || '16:9';
  const styleLocks = {
    style: document.getElementById('lockStyle')?.value || '',
    lighting: document.getElementById('lockLighting')?.value || '',
    color: document.getElementById('lockColor')?.value || '',
    camera: document.getElementById('lockCamera')?.value || '',
    character: document.getElementById('lockCharacter')?.value || '',
  };

  const box = document.getElementById(`scene-keyframe-box-${sceneId}`);
  if (box) {
    box.style.display = 'block';
    box.innerHTML = `
      <div class="keyframe-preview-card" style="border:1px dashed var(--accent);">
        <div style="font-size:24px;animation:spin 2s linear infinite;">⏳</div>
        <div class="u-fs12 u-c-accent u-fw600">
          Đang sinh ảnh Keyframe mẫu qua Muse Image... Vui lòng đợi trong giây lát!
        </div>
      </div>`;
  }

  try {
    const res = await apiFetch('/api/scenes/preview-keyframe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prompt: sc.prompt,
        aspect_ratio: ratio,
        reference_image: sc.reference_image || null,
        style_locks: styleLocks,
      })
    });
    const data = await res.json();
    if (data.error || !data.success) throw new Error(data.error || 'Lỗi không xác định');

    sc.keyframe_url = data.url;
    if (box) {
      box.innerHTML = `
        <div class="keyframe-preview-card">
          <img loading="lazy" decoding="async" src="${esc(data.url)}" class="keyframe-preview-thumb" alt="Keyframe Preview" onclick="window.open('${esc(data.url)}')">
          <div class="fx-f1">
            <div class="u-fs12 u-fw700 u-c-green">✅ Đã sinh ảnh Keyframe thành công!</div>
            <div style="font-size:11px;color:var(--muted);margin-top:2px;max-height:36px;overflow:hidden;text-overflow:ellipsis;">${esc(data.prompt)}</div>
          </div>
          <div class="fx fx-g6 fx-wrap">
            <button type="button" class="btn btn-primary btn-sm u-fs11" onclick="applyKeyframeAsRef(${sceneId}, '${esc(data.url)}')">🔒 Khóa ảnh này cho Cảnh</button>
            <a href="${esc(data.url)}" download="keyframe_scene_${sceneId}.png" class="btn btn-ghost btn-sm u-fs11">⬇️ Tải</a>
            <button type="button" class="btn btn-ghost btn-sm u-fs11" onclick="closeSceneKeyframeBox(${sceneId})">✕</button>
          </div>
        </div>`;
    }
  } catch (e) {
    if (box) {
      box.innerHTML = `
        <div class="keyframe-preview-card" style="border-color:var(--red);color:var(--red);font-size:12px;">
          ❌ Lỗi tạo Keyframe: ${esc(e.message)}
          <button type="button" class="btn btn-ghost btn-sm" style="margin-left:auto;" onclick="closeSceneKeyframeBox(${sceneId})">✕</button>
        </div>`;
    }
  }
}

function applyKeyframeAsRef(sceneId, url) {
  const sc = scenes.find(s => s.id === sceneId);
  if (sc) {
    sc.reference_image = url;
    sc.reference_image_name = 'Keyframe đã tạo';
    renderScenes();
    showToast(`🔒 Đã khóa Keyframe làm ảnh tham chiếu cho Cảnh!`);
  }
}

function closeSceneKeyframeBox(sceneId) {
  const box = document.getElementById(`scene-keyframe-box-${sceneId}`);
  if (box) box.style.display = 'none';
}

// ── 5. Job Controls: Stop & Resume ─────────────────────────────────────────
async function cancelCurrentJob() {
  if (!activeJobId) return;
  if (!confirm('🛑 Bạn có chắc muốn dừng tiến trình sản xuất phim hiện tại?')) return;
  try {
    const res = await apiFetch(`/api/jobs/${activeJobId}/cancel`, { method: 'POST' });
    const data = await res.json();
    alert(data.message || 'Đã gửi lệnh dừng job');
  } catch (e) {
    alert(`Lỗi dừng job: ${e.message}`);
  }
}

async function resumeJobFromScene() {
  if (!activeJobId) {
    alert('Không có job nào để tiếp tục!');
    return;
  }
  const idx = parseInt(document.getElementById('resumeSceneIdx')?.value || '1');
  if (!confirm(`▶️ Tiếp tục chạy job từ Cảnh ${idx}? Hệ thống sẽ giữ nguyên các cảnh trước đã render xong.`)) return;
  try {
    const res = await apiFetch(`/api/jobs/${activeJobId}/resume`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ from_scene_idx: idx })
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    alert(`✅ ${data.message}`);
    setSseStatus('live', 'Đang tiếp tục...');
  } catch (e) {
    alert(`Lỗi chạy tiếp: ${e.message}`);
  }
}

// ── 6. Pre-Export Scene Audit Card & Modal ──────────────────────────────────
let lastAuditData = null;

function renderSceneAuditCard(audit, data, jobId) {
  lastAuditData = audit;
  const card = document.getElementById('sceneAuditCard');
  if (!card) return;
  card.style.display = 'block';

  const badge = document.getElementById('auditStatsBadge');
  const noticeBox = document.getElementById('auditNoticeBox');
  const tbody = document.getElementById('auditTableBody');

  if (audit) {
    if (audit.is_complete) {
      badge.className = 'pill done';
      badge.textContent = `✅ 100% Hoàn tất (${audit.done_count}/${audit.total})`;
      noticeBox.innerHTML = `
        <span class="u-c-green u-fw700">🎉 Tất cả ${audit.total} cảnh đã render thành công và sẵn sàng xuất phim hoàn chỉnh!</span>
      `;
    } else {
      badge.className = 'pill error';
      badge.textContent = `⚠️ Thiếu ${audit.failed_count + audit.pending_count}/${audit.total} cảnh`;
      noticeBox.innerHTML = `
        <div class="u-c-yellow u-fw700 u-mb4">
          ⚠️ CẢNH BÁO KIỂM ĐỊNH TÍNH TOÀN VẸN:
        </div>
        <div class="u-c-text u-fs12">
          Chỉ có <b>${audit.done_count}/${audit.total}</b> cảnh hoàn thành. Phát hiện <b>${audit.failed_count}</b> cảnh bị lỗi và <b>${audit.pending_count}</b> cảnh chưa sẵn sàng.<br>
          <span class="u-c-accent">Hệ thống đã dừng xuất phim tự động để tránh xuất phim thiếu cảnh ngoài ý muốn của bạn.</span>
        </div>
      `;
    }

    if (audit.details && audit.details.length) {
      tbody.innerHTML = audit.details.map(d => {
        let stBadge = '<span class="audit-badge pending">⌛ Chờ</span>';
        if (d.status === 'done') stBadge = '<span class="audit-badge done">✅ Xong</span>';
        else if (d.status === 'error') stBadge = '<span class="audit-badge error">❌ Lỗi</span>';

        return `
          <tr>
            <td class="u-fw700">Cảnh ${d.index}</td>
            <td>${stBadge}</td>
            <td>
              <div style="max-height:48px;overflow-y:auto;font-size:11.5px;color:var(--text);line-height:1.4;">
                ${esc(d.prompt || '')}
              </div>
            </td>
            <td>
              <div style="font-size:11.5px;color:${d.error ? 'var(--red)' : 'var(--muted)'};max-height:48px;overflow-y:auto;">
                ${esc(d.error || (d.status === 'done' ? 'Hợp lệ 100%' : 'Đang chờ'))}
              </div>
            </td>
            <td>
              ${d.status !== 'done' ? `
                <button class="btn btn-ghost btn-sm u-fs11 u-c-accent" onclick="retrySingleSceneDirect(${d.index}, event)">
                  🔄 Thử lại
                </button>
              ` : `
                <button class="btn btn-ghost btn-sm u-fs11 u-c-green" onclick="window.open('/api/jobs/${jobId}/scenes/${d.index}/video')">
                  ▶️ Xem clip
                </button>
              `}
            </td>
          </tr>
        `;
      }).join('');
    }
  }
}

async function composeDraftMovie() {
  if (!activeJobId) return;
  if (!confirm('⚠️ Bạn có chắc muốn xuất Bản Nháp (Draft Movie)?\nPhim sẽ chỉ ghép các cảnh đã thành công và bỏ qua các cảnh bị lỗi.')) {
    return;
  }
  try {
    const res = await apiFetch(`/api/jobs/${activeJobId}/compose`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        allow_partial: true,
        quality: document.getElementById('exportQuality')?.value || '720p',
        subtitle_mode: document.getElementById('subtitleMode')?.value || 'burn_in',
        transition: document.getElementById('sceneTransition')?.value || 'none',
        bg_music_path: document.getElementById('bgMusicSelect')?.value || null,
        music_volume: parseFloat(document.getElementById('musicVolume')?.value || '0.25'),
        voice_volume: parseFloat(document.getElementById('voiceVolume')?.value || '1.0'),
      })
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    alert('🎉 Đã bắt đầu xuất bản nháp! Vui lòng theo dõi khung Video thành phẩm.');
  } catch (e) {
    alert(`Lỗi xuất bản nháp: ${e.message}`);
  }
}

function showSceneAuditModalDirect() {
  if (!lastAuditData) {
    alert('Chưa có dữ liệu kiểm định cho job này!');
    return;
  }
  const modal = document.createElement('div');
  modal.className = 'modal-backdrop';
  modal.innerHTML = `
    <div class="modal-window">
      <div class="modal-header">
        <div class="u-fw700 u-fs16 u-c-accent">📋 BÁO CÁO KIỂM ĐỊNH TÍNH TOÀN VẸN PHIM</div>
        <button class="btn btn-ghost btn-sm" onclick="this.closest('.modal-backdrop').remove()">✕</button>
      </div>
      <div class="modal-body">
        <div class="u-fs13 u-mb12">
          Tổng số cảnh: <b>${lastAuditData.total}</b> • Thành công: <b class="u-c-green">${lastAuditData.done_count}</b> • Thất bại: <b class="u-c-red">${lastAuditData.failed_count}</b>
        </div>
        <table class="audit-table">
          <thead>
            <tr>
              <th>Cảnh</th>
              <th>Trạng thái</th>
              <th>Prompt</th>
              <th>Chi tiết lỗi</th>
            </tr>
          </thead>
          <tbody>
            ${(lastAuditData.details || []).map(d => `
              <tr>
                <td><b>Cảnh ${d.index}</b></td>
                <td><span class="audit-badge ${d.status}">${d.status === 'done' ? '✅ Xong' : (d.status === 'error' ? '❌ Lỗi' : '⌛ Chờ')}</span></td>
                <td style="font-size:11.5px;max-width:300px;">${esc(d.prompt || '')}</td>
                <td style="font-size:11.5px;color:${d.error ? 'var(--red)' : 'var(--muted)'};">${esc(d.error || 'Hợp lệ')}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
      <div class="modal-footer">
        <button class="btn btn-primary" onclick="this.closest('.modal-backdrop').remove()">Đóng</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
}

// ── 7. Download Project Metadata ───────────────────────────────────────────
async function downloadJobMetadata() {
  if (!activeJobId) return;
  try {
    const res = await apiFetch(`/api/jobs/${activeJobId}/metadata`);
    const data = await res.json();
    const str = JSON.stringify(data, null, 2);
    const blob = new Blob([str], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `project_metadata_${activeJobId}.json`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert(`Lỗi tải metadata: ${e.message}`);
  }
}

// ── 8. Project Persistence (Lưu / Mở / Nhập / Xuất Dự Án) ───────────────────
async function saveCurrentProject() {
  syncScenesFromDOM();
  if (scenes.length === 0) {
    alert('Vui lòng thêm cảnh vào kịch bản trước khi lưu project!');
    return;
  }
  const curTitle = document.getElementById('projectTitle')?.value || 'my_project';
  const name = prompt('Nhập tên lưu Project (dùng để mở lại sau này):', curTitle);
  if (!name || !name.trim()) return;

  const payload = {
    name: name.trim(),
    title: document.getElementById('projectTitle')?.value || '',
    script_text: document.getElementById('rawScriptInput')?.value || '',
    aspect_ratio: document.getElementById('globalAspectRatio')?.value || '16:9',
    voice: document.getElementById('voiceSelect')?.value || 'vi-VN-HoaiMyNeural',
    voice_mode: document.getElementById('voiceModeSelect')?.value || 'tts',
    export_mode: document.getElementById('exportMode')?.value || 'full',
    chain_mode: document.getElementById('chainMode')?.value || 'none',
    chain_fallback_mode: document.getElementById('chainFallbackMode')?.value || 'independent',
    subtitle_mode: document.getElementById('subtitleMode')?.value || 'burn_in',
    quality: document.getElementById('exportQuality')?.value || '720p',
    transition: document.getElementById('sceneTransition')?.value || 'none',
    bg_music_path: document.getElementById('bgMusicSelect')?.value || null,
    music_volume: parseFloat(document.getElementById('musicVolume')?.value || '0.25'),
    voice_volume: parseFloat(document.getElementById('voiceVolume')?.value || '1.0'),
    scenes: scenes,
    characters: (typeof CHARACTERS !== 'undefined') ? CHARACTERS : [],
    style_locks: {
      style: document.getElementById('lockStyle')?.value || '',
      lighting: document.getElementById('lockLighting')?.value || '',
      color: document.getElementById('lockColor')?.value || '',
      camera: document.getElementById('lockCamera')?.value || '',
      character: document.getElementById('lockCharacter')?.value || '',
    }
  };

  try {
    const res = await apiFetch('/api/projects/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    alert(`✅ Đã lưu Project "${name}" thành công!`);
  } catch (e) {
    alert(`Lỗi lưu project: ${e.message}`);
  }
}

async function openProjectsModal() {
  try {
    const res = await apiFetch('/api/projects');
    const list = await res.json();

    const modal = document.createElement('div');
    modal.className = 'modal-backdrop';
    modal.innerHTML = `
      <div class="modal-window">
        <div class="modal-header">
          <div class="u-fw700 u-fs16 u-c-accent">📂 DANH SÁCH DỰ ÁN ĐÃ LƯU</div>
          <button class="btn btn-ghost btn-sm" onclick="this.closest('.modal-backdrop').remove()">✕</button>
        </div>
        <div class="modal-body">
          ${list.length === 0 ? `
            <div style="color:var(--muted);text-align:center;padding:30px;">Chưa có dự án nào được lưu. Hãy bấm "Lưu Project" để tạo dự án mới!</div>
          ` : `
            <table class="audit-table">
              <thead>
                <tr>
                  <th>Tên Project</th>
                  <th>Tiêu đề phim</th>
                  <th>Số cảnh</th>
                  <th>Thời gian lưu</th>
                  <th style="width:140px;">Hành động</th>
                </tr>
              </thead>
              <tbody>
                ${list.map(p => `
                  <tr>
                    <td class="u-fw700 u-c-accent">${esc(p.name)}</td>
                    <td>${esc(p.title || p.name)}</td>
                    <td><span class="pill running">${p.scenes_count} cảnh</span></td>
                    <td class="u-fs11 u-c-muted">${new Date(p.saved_at * 1000).toLocaleString('vi-VN')}</td>
                    <td>
                      <button class="btn btn-primary btn-sm" onclick="loadProjectByName('${esc(p.name)}'); this.closest('.modal-backdrop').remove();">📂 Mở</button>
                      <button class="btn btn-ghost btn-sm u-c-red" onclick="deleteProjectByName('${esc(p.name)}', this)">🗑️</button>
                    </td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          `}
        </div>
        <div class="modal-footer">
          <button class="btn btn-ghost" onclick="this.closest('.modal-backdrop').remove()">Đóng</button>
        </div>
      </div>
    `;
    document.body.appendChild(modal);
  } catch (e) {
    alert(`Lỗi mở danh sách project: ${e.message}`);
  }
}

async function loadProjectByName(name) {
  try {
    const res = await apiFetch(`/api/projects/${encodeURIComponent(name)}`);
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    if (data.title) document.getElementById('projectTitle').value = data.title;
    if (data.aspect_ratio) applyGlobalAspectRatio(data.aspect_ratio, false);
    if (data.voice) document.getElementById('voiceSelect').value = data.voice;
    if (data.export_mode) document.getElementById('exportMode').value = data.export_mode;
    if (data.chain_mode) document.getElementById('chainMode').value = data.chain_mode;
    if (data.chain_fallback_mode && document.getElementById('chainFallbackMode')) document.getElementById('chainFallbackMode').value = data.chain_fallback_mode;
    if (data.subtitle_mode && document.getElementById('subtitleMode')) document.getElementById('subtitleMode').value = data.subtitle_mode;
    if (data.quality && document.getElementById('exportQuality')) document.getElementById('exportQuality').value = data.quality;
    if (data.transition && document.getElementById('sceneTransition')) document.getElementById('sceneTransition').value = data.transition;
    if (data.bg_music_path && document.getElementById('bgMusicSelect')) document.getElementById('bgMusicSelect').value = data.bg_music_path;
    if (data.music_volume !== undefined && document.getElementById('musicVolume')) {
      document.getElementById('musicVolume').value = data.music_volume;
      document.getElementById('lblMusicVol').textContent = Math.round(data.music_volume * 100) + '%';
    }
    if (data.voice_volume !== undefined && document.getElementById('voiceVolume')) {
      document.getElementById('voiceVolume').value = data.voice_volume;
      document.getElementById('lblVoiceVol').textContent = Math.round(data.voice_volume * 100) + '%';
    }

    if (data.style_locks) {
      if (data.style_locks.style && document.getElementById('lockStyle')) document.getElementById('lockStyle').value = data.style_locks.style;
      if (data.style_locks.lighting && document.getElementById('lockLighting')) document.getElementById('lockLighting').value = data.style_locks.lighting;
      if (data.style_locks.color && document.getElementById('lockColor')) document.getElementById('lockColor').value = data.style_locks.color;
      if (data.style_locks.camera && document.getElementById('lockCamera')) document.getElementById('lockCamera').value = data.style_locks.camera;
      if (data.style_locks.character && document.getElementById('lockCharacter')) document.getElementById('lockCharacter').value = data.style_locks.character;
    }

    if (Array.isArray(data.scenes)) {
      scenes = data.scenes.map((s, idx) => ({
        id: ++sceneIdCounter,
        title: s.title || `Cảnh ${idx + 1}`,
        prompt: s.prompt || '',
        voiceover: s.voiceover || '',
        voice: s.voice || '',
        duration: s.duration || 5,
        size: s.size || data.aspect_ratio || '16:9',
        reference_image: s.reference_image || '',
        reference_image_name: s.reference_image_name || '',
        chain_from_previous: !!s.chain_from_previous,
        sheet_angle: s.sheet_angle || 'front',
        voiceMode: (s.voice_segments && s.voice_segments.length > 0) ? 'multi' : 'single',
        voiceSegments: s.voice_segments || [],
      }));
      renderScenes();
    }

    showPanel('create');
    alert(`🎉 Đã mở thành công Project "${name}" với ${scenes.length} cảnh!`);
  } catch (e) {
    alert(`Lỗi mở project: ${e.message}`);
  }
}

async function deleteProjectByName(name, btnEl) {
  if (!confirm(`⚠️ Bạn có chắc muốn xóa dự án "${name}"? Hành động này không thể hoàn tác.`)) {
    return;
  }
  try {
    const res = await apiFetch(`/api/projects/${encodeURIComponent(name)}`, { method: 'DELETE' });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    btnEl?.closest('tr')?.remove();
    showToast(`🗑️ Đã xóa project "${name}"`);
  } catch (e) {
    alert(`Lỗi xóa project: ${e.message}`);
  }
}

function exportProjectJson() {
  syncScenesFromDOM();
  const payload = {
    title: document.getElementById('projectTitle')?.value || '',
    aspect_ratio: document.getElementById('globalAspectRatio')?.value || '16:9',
    voice: document.getElementById('voiceSelect')?.value || 'vi-VN-HoaiMyNeural',
    voice_mode: document.getElementById('voiceModeSelect')?.value || 'tts',
    export_mode: document.getElementById('exportMode')?.value || 'full',
    chain_mode: document.getElementById('chainMode')?.value || 'none',
    chain_fallback_mode: document.getElementById('chainFallbackMode')?.value || 'independent',
    subtitle_mode: document.getElementById('subtitleMode')?.value || 'burn_in',
    quality: document.getElementById('exportQuality')?.value || '720p',
    transition: document.getElementById('sceneTransition')?.value || 'none',
    bg_music_path: document.getElementById('bgMusicSelect')?.value || null,
    music_volume: parseFloat(document.getElementById('musicVolume')?.value || '0.25'),
    voice_volume: parseFloat(document.getElementById('voiceVolume')?.value || '1.0'),
    scenes: scenes,
    style_locks: {
      style: document.getElementById('lockStyle')?.value || '',
      lighting: document.getElementById('lockLighting')?.value || '',
      color: document.getElementById('lockColor')?.value || '',
      camera: document.getElementById('lockCamera')?.value || '',
      character: document.getElementById('lockCharacter')?.value || '',
    }
  };
  const str = JSON.stringify(payload, null, 2);
  const blob = new Blob([str], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${(payload.title || 'movie_project').replace(/\s+/g, '_')}.json`;
  a.click();
  URL.revokeObjectURL(url);
}

function importProjectJson() {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = '.json,application/json';
  input.onchange = (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (re) => {
      try {
        const data = JSON.parse(re.target.result);
        if (data.title) document.getElementById('projectTitle').value = data.title;
        if (data.aspect_ratio) applyGlobalAspectRatio(data.aspect_ratio, false);
        if (data.voice) document.getElementById('voiceSelect').value = data.voice;
        if (data.export_mode) document.getElementById('exportMode').value = data.export_mode;
        if (data.chain_mode) document.getElementById('chainMode').value = data.chain_mode;
        if (data.chain_fallback_mode && document.getElementById('chainFallbackMode')) document.getElementById('chainFallbackMode').value = data.chain_fallback_mode;
        if (data.subtitle_mode && document.getElementById('subtitleMode')) document.getElementById('subtitleMode').value = data.subtitle_mode;
        if (data.quality && document.getElementById('exportQuality')) document.getElementById('exportQuality').value = data.quality;
        if (data.transition && document.getElementById('sceneTransition')) document.getElementById('sceneTransition').value = data.transition;
        if (data.style_locks) {
          if (data.style_locks.style && document.getElementById('lockStyle')) document.getElementById('lockStyle').value = data.style_locks.style;
          if (data.style_locks.lighting && document.getElementById('lockLighting')) document.getElementById('lockLighting').value = data.style_locks.lighting;
          if (data.style_locks.color && document.getElementById('lockColor')) document.getElementById('lockColor').value = data.style_locks.color;
          if (data.style_locks.camera && document.getElementById('lockCamera')) document.getElementById('lockCamera').value = data.style_locks.camera;
          if (data.style_locks.character && document.getElementById('lockCharacter')) document.getElementById('lockCharacter').value = data.style_locks.character;
        }
        if (Array.isArray(data.scenes)) {
          scenes = data.scenes.map((s, idx) => ({
            id: ++sceneIdCounter,
            title: s.title || `Cảnh ${idx + 1}`,
            prompt: s.prompt || '',
            voiceover: s.voiceover || '',
            voice: s.voice || '',
            duration: s.duration || 5,
            size: s.size || data.aspect_ratio || '16:9',
            reference_image: s.reference_image || '',
            reference_image_name: s.reference_image_name || '',
            chain_from_previous: !!s.chain_from_previous,
            sheet_angle: s.sheet_angle || 'front',
            voiceMode: (s.voice_segments && s.voice_segments.length > 0) ? 'multi' : 'single',
            voiceSegments: s.voice_segments || [],
          }));
          renderScenes();
        }
        alert(`🎉 Đã nhập thành công dự án với ${scenes.length} cảnh!`);
      } catch (err) {
        alert(`Lỗi đọc file JSON: ${err.message}`);
      }
    };
    reader.readAsText(file);
  };
  input.click();
}

// ── 9. Workspace Cleanup ───────────────────────────────────────────────────
async function cleanWorkspace() {
  if (!confirm('⚠️ Bạn có chắc muốn dọn dẹp các thư mục tác vụ tạm thời trong Workspace?\n\n- Các file cache, video nháp tạm sẽ bị xóa để giải phóng dung lượng.\n- Hồ sơ nhân vật, nhạc nền và các dự án đã lưu vẫn được bảo toàn nguyên vẹn.')) {
    return;
  }
  try {
    const res = await apiFetch('/api/workspace/clean', { method: 'POST' });
    const data = await res.json();
    alert(`✅ ${data.message}`);
  } catch (e) {
    alert(`Lỗi dọn workspace: ${e.message}`);
  }
}

// ── 10. Prompt Sheet & Script Templates Hub ────────────────────────────────
let promptSheetDataCache = null;

async function openPromptSheetModal(initialTab = 'chatgpt') {
  let modal = document.getElementById('promptSheetModal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'promptSheetModal';
    modal.className = 'modal-backdrop';
    document.body.appendChild(modal);
  }
  modal.style.display = 'flex';

  if (!promptSheetDataCache) {
    modal.innerHTML = `
      <div class="modal-window" style="max-width:920px;">
        <div class="modal-header">
          <div class="u-fw700 u-fs16 u-c-accent">💡 THƯ VIỆN PROMPT MẪU & CÚ PHÁP KỊCH BẢN CHUẨN</div>
          <button class="btn btn-ghost btn-sm" onclick="closePromptSheetModal()">✕</button>
        </div>
        <div class="modal-body" style="text-align:center;padding:40px;">
          <div class="u-fs24 u-mb10">⏳</div>
          <div class="u-c-muted u-fs13">Đang tải Thư viện Prompt Mẫu & Cẩm nang Kịch bản Studio...</div>
        </div>
      </div>
    `;
    try {
      const res = await apiFetch('/api/prompt-sheet');
      promptSheetDataCache = await res.json();
    } catch (e) {
      modal.innerHTML = `
        <div class="modal-window">
          <div class="modal-header">
            <div class="u-fw700 u-c-red">Lỗi tải Thư viện Prompt</div>
            <button class="btn btn-ghost btn-sm" onclick="closePromptSheetModal()">✕</button>
          </div>
          <div class="modal-body u-c-red">${e.message}</div>
        </div>
      `;
      return;
    }
  }

  renderPromptSheetContent(initialTab);
}

function closePromptSheetModal() {
  const modal = document.getElementById('promptSheetModal');
  if (modal) modal.style.display = 'none';
}

function renderPromptSheetContent(activeTab = 'chatgpt') {
  const modal = document.getElementById('promptSheetModal');
  if (!modal || !promptSheetDataCache) return;

  const d = promptSheetDataCache;
  const samples = d.sample_scripts || {};
  const dict = d.cinematic_dictionary || {};
  const rules = d.format_rules || [];

  modal.innerHTML = `
    <div class="modal-window" style="max-width:960px;">
      <div class="modal-header">
        <div class="fx fx-ac fx-g8">
          <span class="u-fs18">💡</span>
          <div>
            <div class="u-fw700 u-fs16 u-c-accent">THƯ VIỆN PROMPT MẪU & CÚ PHÁP CHUẨN CHO AI</div>
            <div class="u-fs115 u-c-muted u-mt2">Dành cho người mới: Copy kịch bản từ ChatGPT hoặc tự viết chuẩn 100% không lỗi</div>
          </div>
        </div>
        <button class="btn btn-ghost btn-sm" onclick="closePromptSheetModal()">✕</button>
      </div>

      <div class="modal-body">
        <!-- Tabs Bar -->
        <div class="ps-tabs">
          <button class="ps-tab-btn ${activeTab === 'chatgpt' ? 'active' : ''}" onclick="switchPromptSheetTab('chatgpt')">🤖 1. Prompt Cho ChatGPT/Claude</button>
          <button class="ps-tab-btn ${activeTab === 'samples' ? 'active' : ''}" onclick="switchPromptSheetTab('samples')">📑 2. Kịch Bản Mẫu Sẵn (4 Thể Loại)</button>
          <button class="ps-tab-btn ${activeTab === 'rules' ? 'active' : ''}" onclick="switchPromptSheetTab('rules')">📐 3. Quy Tắc Cú Pháp Chuẩn</button>
          <button class="ps-tab-btn ${activeTab === 'dictionary' ? 'active' : ''}" onclick="switchPromptSheetTab('dictionary')">🎥 4. Từ Điển Prompt Điện Ảnh</button>
          <button class="ps-tab-btn ${activeTab === 'safety' ? 'active' : ''}" onclick="switchPromptSheetTab('safety')">🛡️ 5. Mẹo Hành Động An Toàn</button>
        </div>

        <!-- TAB 1: ChatGPT Master Prompt -->
        <div id="psTab-chatgpt" class="ps-tab-content ${activeTab === 'chatgpt' ? 'active' : ''}">
          <div style="background:linear-gradient(135deg,rgba(99,102,241,0.08),rgba(99,102,241,0.08));border:1px solid rgba(99,102,241,0.3);border-radius:8px;padding:12px;margin-bottom:14px;">
            <div class="u-fw700 u-c-accent u-fs13 fx fx-ac fx-g6">
              <span>⭐ BÍ QUYẾT CHO NGƯỜI MỚI: DÙNG CHATGPT VIẾT HỘ KỊCH BẢN CHUẨN 100%</span>
            </div>
            <div class="u-fs12 u-c-text u-mt6 u-lh15">
              Người mới thường copy văn xuôi hoặc bảng biểu từ ChatGPT vào Studio, khiến tool khó nhận diện đâu là góc máy và đâu là lời thoại.<br>
              👉 <b>Cách làm đúng:</b> Chỉ cần bấm nút <b>"📋 Sao chép Prompt cho ChatGPT"</b> dưới đây, dán vào ChatGPT / Claude / DeepSeek và thêm yêu cầu của bạn. ChatGPT sẽ tự động trả về kịch bản theo đúng định dạng Studio nhận diện ngay lập tức!
            </div>
          </div>

          <div class="fx fx-jc-sb fx-ac u-mb6">
            <span class="u-fs12 u-fw600 u-c-muted">CÂU LỆNH SYSTEM PROMPT CHO CHATGPT (BẤM ĐỂ SAO CHÉP):</span>
            <button class="btn btn-primary btn-sm" onclick="copyPromptSheetContent(promptSheetDataCache.chatgpt_master_prompt, this)">
              📋 Sao chép Prompt cho ChatGPT
            </button>
          </div>
          <div class="ps-code-box" id="psCodeMasterPrompt">${esc(d.chatgpt_master_prompt)}</div>

          <div style="margin-top:14px;background:var(--bg0);padding:10px 14px;border-radius:6px;border:1px solid var(--border);font-size:12px;color:var(--muted);">
            💡 <b>Gợi ý câu lệnh mẫu sau khi dán prompt:</b><br>
            <span style="color:var(--green);font-family:monospace;">"Hãy viết cho tôi kịch bản phim 3 cảnh về cuộc rượt đuổi nghẹt thở trong thành phố tương lai giữa nữ sát thủ Phụng Anh và kiếm sĩ Lương Phong."</span>
          </div>
        </div>

        <!-- TAB 2: Ready-to-use Sample Scripts -->
        <div id="psTab-samples" class="ps-tab-content ${activeTab === 'samples' ? 'active' : ''}">
          <div class="u-fs12 u-c-muted u-mb14">
            Dưới đây là 4 kịch bản mẫu hoàn chỉnh đã được tinh chỉnh chuẩn Hollywood. Bạn có thể bấm <b>[📥 Nạp vào Dự án]</b> để Studio tự động tạo toàn bộ cảnh, prompt và phân vai ngay lập tức!
          </div>

          <div class="fx fx-col fx-g14">
            ${Object.values(samples).map(s => `
              <div class="ps-card">
                <div class="fx fx-jc-sb fx-as u-mb8 fx-wrap fx-g8">
                  <div>
                    <div class="u-fw700 u-fs14 u-c-text fx fx-ac fx-g8">
                      <span>${esc(s.title)}</span>
                      <span class="ps-badge">${esc(s.badge)}</span>
                    </div>
                    <div class="u-fs12 u-c-muted u-mt3">${esc(s.description)}</div>
                  </div>
                  <div class="fx fx-g6">
                    <button class="btn btn-ghost btn-sm" onclick="copyPromptSheetContent(promptSheetDataCache.sample_scripts['${s.id}'].text, this)">📋 Sao chép</button>
                    <button class="btn btn-primary btn-sm" onclick="applySampleScriptToStudio('${s.id}')">📥 Nạp vào Dự án</button>
                  </div>
                </div>
                <div class="ps-code-box" style="max-height:160px;font-size:11.5px;">${esc(s.text.trim())}</div>
              </div>
            `).join('')}
          </div>
        </div>

        <!-- TAB 3: Format Rules -->
        <div id="psTab-rules" class="ps-tab-content ${activeTab === 'rules' ? 'active' : ''}">
          <div class="u-fs12 u-c-muted u-mb14">
            AI Movie Studio tích hợp bộ nhận diện ngôn ngữ thông minh, hỗ trợ 3 cấu trúc kịch bản chính sau:
          </div>

          <div class="fx fx-col fx-g14">
            ${rules.map(r => `
              <div class="ps-card">
                <div style="font-weight:700;font-size:13.5px;color:var(--accent);margin-bottom:6px;">${esc(r.name)}</div>
                <div class="u-fs115 u-c-green u-mb8">✨ Ưu điểm: ${esc(r.benefit)}</div>
                <div class="ps-code-box" style="max-height:150px;color:#a5f3fc;">${esc(r.syntax.trim())}</div>
              </div>
            `).join('')}
          </div>
        </div>

        <!-- TAB 4: Cinematic Prompt Dictionary -->
        <div id="psTab-dictionary" class="ps-tab-content ${activeTab === 'dictionary' ? 'active' : ''}">
          <div class="u-fs12 u-c-muted u-mb14">
            Tra cứu nhanh các từ khóa điện ảnh thông dụng. Bấm <b>[📋 Copy]</b> để chép và ghép vào prompt của bạn:
          </div>

          <div class="u-mb16">
            <div class="u-fw700 u-c-accent u-fs13 u-mb8">🎥 1. Góc Máy & Chuyển Động Camera (Camera Angles)</div>
            ${(dict.camera_angles || []).map(item => `
              <div class="ps-dict-item">
                <div class="fx-f1">
                  <div style="font-weight:600;font-size:12px;color:var(--text);font-family:monospace;">${esc(item.keyword)}</div>
                  <div class="u-fs11 u-c-muted u-mt2"><b>${esc(item.vn)}</b>: ${esc(item.desc)}</div>
                </div>
                <button class="btn btn-ghost btn-sm u-fs11 u-p-2-8" onclick="copyPromptSheetContent('${esc(item.keyword)}', this)">📋 Copy</button>
              </div>
            `).join('')}
          </div>

          <div class="u-mb16">
            <div class="u-fw700 u-c-accent u-fs13 u-mb8">💡 2. Ánh Sáng Điện Ảnh (Lighting & Atmosphere)</div>
            ${(dict.lighting || []).map(item => `
              <div class="ps-dict-item">
                <div class="fx-f1">
                  <div style="font-weight:600;font-size:12px;color:var(--text);font-family:monospace;">${esc(item.keyword)}</div>
                  <div class="u-fs11 u-c-muted u-mt2"><b>${esc(item.vn)}</b>: ${esc(item.desc)}</div>
                </div>
                <button class="btn btn-ghost btn-sm u-fs11 u-p-2-8" onclick="copyPromptSheetContent('${esc(item.keyword)}', this)">📋 Copy</button>
              </div>
            `).join('')}
          </div>

          <div class="u-mb16">
            <div class="u-fw700 u-c-accent u-fs13 u-mb8">🎬 3. Máy Quay & Ống Kính (Cameras & Lenses)</div>
            ${(dict.cameras_lenses || []).map(item => `
              <div class="ps-dict-item">
                <div class="fx-f1">
                  <div style="font-weight:600;font-size:12px;color:var(--text);font-family:monospace;">${esc(item.keyword)}</div>
                  <div class="u-fs11 u-c-muted u-mt2"><b>${esc(item.vn)}</b>: ${esc(item.desc)}</div>
                </div>
                <button class="btn btn-ghost btn-sm u-fs11 u-p-2-8" onclick="copyPromptSheetContent('${esc(item.keyword)}', this)">📋 Copy</button>
              </div>
            `).join('')}
          </div>
        </div>

        <!-- TAB 5: Safety & Anti-Rejection Tips -->
        <div id="psTab-safety" class="ps-tab-content ${activeTab === 'safety' ? 'active' : ''}">
          <div style="background:rgba(234,179,8,0.08);border:1px solid rgba(234,179,8,0.25);border-radius:8px;padding:12px;margin-bottom:14px;">
            <div class="u-fw700 u-c-yellow u-fs13">🛡️ TẠI SAO CẦN DÙNG TỪ KHÓA HÀNH ĐỘNG AN TOÀN?</div>
            <div style="font-size:12px;color:var(--text);margin-top:4px;line-height:1.45;">
              Mô hình tạo video (Muse AI / Sora) có bộ lọc an toàn rất nghiêm ngặt. Khi gặp các từ bạo lực trực diện như đâm, chém, máu me, súng ống, mô hình sẽ từ chối render và làm hỏng cảnh phim của bạn.<br>
              Hãy dùng các từ vựng điện ảnh Hollywood dưới đây để vừa giữ được cảnh hành động mãn nhãn, vừa an toàn 100%:
            </div>
          </div>

          <table class="audit-table">
            <thead>
              <tr>
                <th style="color:var(--red);width:35%;">❌ Từ khóa dễ bị từ chối</th>
                <th style="color:var(--green);width:40%;">✅ Từ điện ảnh Hollywood an toàn</th>
                <th style="width:25%;">💡 Ý nghĩa</th>
              </tr>
            </thead>
            <tbody>
              ${(dict.safe_action_replacements || []).map(item => `
                <tr>
                  <td><code class="u-c-red">${esc(item.bad)}</code></td>
                  <td><code class="u-c-green">${esc(item.good)}</code></td>
                  <td class="u-fs115 u-c-muted">${esc(item.reason)}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>

      </div>

      <div class="modal-footer">
        <button class="btn btn-ghost" onclick="closePromptSheetModal()">Đóng</button>
      </div>
    </div>
  `;
}

function switchPromptSheetTab(tabKey) {
  document.querySelectorAll('.ps-tab-btn').forEach(btn => btn.classList.remove('active'));
  document.querySelectorAll('.ps-tab-content').forEach(box => box.classList.remove('active'));

  const targetBtn = Array.from(document.querySelectorAll('.ps-tab-btn')).find(b => b.getAttribute('onclick')?.includes(tabKey));
  if (targetBtn) targetBtn.classList.add('active');

  const targetContent = document.getElementById(`psTab-${tabKey}`);
  if (targetContent) targetContent.classList.add('active');
}

function copyPromptSheetContent(text, btnElement) {
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    if (btnElement) {
      const orig = btnElement.innerHTML;
      btnElement.innerHTML = '✅ Đã sao chép!';
      btnElement.classList.add('btn-primary');
      setTimeout(() => {
        btnElement.innerHTML = orig;
      }, 2000);
    }
  }).catch(err => {
    prompt('Sao chép nội dung bên dưới (Ctrl+C):', text);
  });
}

function applySampleScriptToStudio(scriptId) {
  if (!promptSheetDataCache || !promptSheetDataCache.sample_scripts) return;
  const s = promptSheetDataCache.sample_scripts[scriptId];
  if (!s) return;

  closePromptSheetModal();
  showPanel('create');

  if (s.aspect_ratio) {
    const sel = document.getElementById('globalAspectRatio');
    if (sel) {
      sel.value = s.aspect_ratio;
      applyGlobalAspectRatio(s.aspect_ratio, true);
    }
  }

  const ta = document.getElementById('rawScriptInput');
  const importBox = document.getElementById('importTextArea');
  if (ta) {
    ta.value = s.text;
    if (importBox) importBox.style.display = 'block';
    parseProfessionalMarkdownScript(s.text);
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// 🎬 SHOT DESK STUDIO (QUY TRÌNH CHUẨN SHOT DESK & REVIEW DESK)
// ═══════════════════════════════════════════════════════════════════════════

let shotDeskProjectsList = [];
let currentShotDeskProject = null;
let currentShotDeskSubtab = 'matrix';
let currentReviewShotIndex = 1;
let shotDeskPollTimer = null;
let selectedShotIndices = new Set();
let activeQuickAudio = null;
let activeQuickAudioBtn = null;

async function initShotDeskStudio() {
  if (!VOICES || VOICES.length === 0) {
    await loadVoices();
  }
  await loadShotDeskProjectsList();
  if (typeof loadMusicList === 'function') loadMusicList();
  populateShotDeskMusicOptions();
}

async function loadShotDeskProjectsList() {
  try {
    const res = await apiFetch('/api/shot-desk/projects');
    if (!res.ok) throw new Error('Không thể tải danh sách dự án');
    shotDeskProjectsList = await res.json();

    const sel = document.getElementById('sdProjectSelect');
    if (!sel) return;

    const currentVal = currentShotDeskProject?.id || '';
    sel.innerHTML = '<option value="">-- Chọn dự án Shot Desk --</option>';

    shotDeskProjectsList.forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = `${p.title} (${p.total_shots} shots - ${p.aspect_ratio})`;
      if (p.id === currentVal) opt.selected = true;
      sel.appendChild(opt);
    });

    if (!currentShotDeskProject && shotDeskProjectsList.length > 0) {
      sel.value = shotDeskProjectsList[0].id;
      await onSelectShotDeskProject(shotDeskProjectsList[0].id);
    } else if (currentShotDeskProject) {
      await onSelectShotDeskProject(currentShotDeskProject.id, false);
    } else {
      renderShotDeskUI();
    }
  } catch (err) {
    console.error('[ShotDesk] Lỗi tải danh sách dự án:', err);
  }
}

async function onSelectShotDeskProject(projectId, resetReviewIndex = true) {
  if (!projectId) {
    currentShotDeskProject = null;
    selectedShotIndices.clear();
    updateBatchActionBar();
    renderShotDeskUI();
    return;
  }
  try {
    const res = await apiFetch(`/api/shot-desk/projects/${encodeURIComponent(projectId)}`);
    if (!res.ok) throw new Error('Không thể tải thông tin dự án');
    currentShotDeskProject = await res.json();
    if (resetReviewIndex) {
      currentReviewShotIndex = 1;
      selectedShotIndices.clear();
    }
    updateBatchActionBar();
    renderShotDeskUI();
    checkAutoStartShotDeskPolling();
  } catch (err) {
    console.error('[ShotDesk] Lỗi nạp dự án:', err);
    alert('Lỗi nạp dự án: ' + err.message);
  }
}

async function loadShotDeskProject(projectId, resetReviewIndex = false) {
  return await onSelectShotDeskProject(projectId, resetReviewIndex);
}

async function loadShotDeskSampleProject() {
  try {
    const res = await apiFetch('/api/shot-desk/load-sample', { method: 'POST' });
    const data = await res.json();

    if (data.success && data.project) {
      alert(`✅ Đã nạp thành công dự án mẫu "${data.project.title}" với ${data.project.shots?.length || 0} shots!`);
      await loadShotDeskProjectsList();
      await onSelectShotDeskProject(data.project.id);
    } else {
      alert('Không thể nạp mẫu: ' + (data.error || 'Lỗi không xác định'));
    }
  } catch (err) {
    alert('Lỗi nạp dự án mẫu: ' + err.message);
  }
}

async function handleShotDeskZipUpload(input) {
  const file = input.files?.[0];
  if (!file) return;

  const formData = new FormData();
  formData.append('file', file);

  try {
    const btn = document.querySelector('button[onclick*="sdZipFileInput"]');
    if (btn) {
      btn.disabled = true;
      btn.textContent = '⏳ Đang giải nén & nạp...';
    }

    const res = await apiFetch('/api/shot-desk/import-zip', {
      method: 'POST',
      body: formData,
    });
    const data = await res.json();

    if (btn) {
      btn.disabled = false;
      btn.textContent = '📥 Nhập File ZIP';
    }

    if (!res.ok || data.error) {
      throw new Error(data.error || 'Lỗi không xác định khi giải nén ZIP');
    }

    alert(`🎉 Nhập thành công dự án "${data.project.title}" (${data.project.shots?.length || 0} cảnh) theo chuẩn Shot Desk!`);
    input.value = '';
    await loadShotDeskProjectsList();
    await onSelectShotDeskProject(data.project.id);
  } catch (err) {
    alert('Lỗi nhập file ZIP: ' + err.message);
    input.value = '';
  }
}

function exportCurrentShotDeskZip() {
  if (!currentShotDeskProject) {
    alert('Vui lòng chọn hoặc nạp một dự án Shot Desk trước!');
    return;
  }
  const url = `/api/shot-desk/projects/${encodeURIComponent(currentShotDeskProject.id)}/export-zip`;
  window.open(url, '_blank');
}

async function deleteCurrentShotDeskProject() {
  if (!currentShotDeskProject) return;
  if (!confirm(`Bạn có chắc chắn muốn xóa dự án "${currentShotDeskProject.title}" (${currentShotDeskProject.id}) khỏi Studio?`)) return;

  try {
    const res = await apiFetch(`/api/shot-desk/projects/${encodeURIComponent(currentShotDeskProject.id)}`, {
      method: 'DELETE'
    });
    if (!res.ok) throw new Error('Lỗi khi xóa dự án');
    alert('Đã xóa dự án thành công!');
    currentShotDeskProject = null;
    await loadShotDeskProjectsList();
  } catch (err) {
    alert('Lỗi xóa dự án: ' + err.message);
  }
}

function switchShotDeskSubtab(tabName) {
  currentShotDeskSubtab = tabName;
  ['matrix', 'review', 'timeline'].forEach(t => {
    const btn = document.getElementById(`sdTabBtn-${t}`);
    const pane = document.getElementById(`sdSubtabContent-${t}`);
    if (btn) {
      if (t === tabName) btn.classList.add('active');
      else btn.classList.remove('active');
    }
    if (pane) {
      if (t === tabName) pane.style.display = 'block';
      else pane.style.display = 'none';
    }
  });

  if (tabName === 'review') renderShotDeskReviewScreen();
  else if (tabName === 'timeline') renderShotDeskTimelineScreen();
  else renderShotDeskMatrixScreen();
}

function renderShotDeskUI(fromPolling = false) {
  if (!fromPolling) renderShotDeskOverview();
  if (currentShotDeskSubtab === 'review') {
    renderShotDeskReviewScreen(fromPolling);
  } else if (currentShotDeskSubtab === 'timeline') {
    renderShotDeskTimelineScreen();
  } else {
    renderShotDeskMatrixScreen(fromPolling);
  }
}

function renderShotDeskOverview() {
  const card = document.getElementById('sdProjectOverviewCard');
  if (!card) return;

  if (!currentShotDeskProject) {
    card.style.display = 'none';
    return;
  }

  card.style.display = 'block';
  const p = currentShotDeskProject;
  const chars = p.characters || [];

  const charCardsHtml = chars.map(c => `
    <div style="background:var(--bg0);border:1px solid var(--border);border-radius:6px;padding:8px 10px;display:flex;gap:10px;align-items:center;min-width:220px;">
      ${c.reference_image ? `
        <img loading="lazy" decoding="async" src="/api/shot-desk/projects/${p.id}/assets/${c.reference_image}" style="width:40px;height:40px;border-radius:50%;object-fit:cover;border:1px solid var(--accent);cursor:pointer;" onclick="openShotDeskImageModal(this.src, '${esc(c.name)}')">
      ` : `
        <div style="width:40px;height:40px;border-radius:50%;background:rgba(99,102,241,0.15);color:var(--accent);display:flex;align-items:center;justify-content:center;font-weight:700;">${(c.name||'C')[0]}</div>
      `}
      <div class="fx-f1">
        <div class="u-fw700 u-fs125 u-c-text">${esc(c.name)}</div>
        <div class="u-fs105 u-c-muted">${esc(c.voice || 'Giọng mặc định')}</div>
        <div style="font-size:10px;color:var(--accent);cursor:pointer;margin-top:2px;" onclick="openCharacterBlockModal('${esc(c.name)}', '${esc(c.character_block || '')}')">👁️ Xem Character Block</div>
      </div>
    </div>
  `).join('');

  card.innerHTML = `
    <div class="fx fx-jc-sb fx-as fx-wrap fx-g12 u-mb12">
      <div>
        <div style="font-size:17px;font-weight:800;color:var(--text);display:flex;align-items:center;gap:8px;">
          <span>🎞️</span> ${esc(p.title)}
          <span class="badge" style="background:rgba(99,102,241,0.15);color:var(--accent);font-size:11px;">Tỉ lệ ${esc(p.aspect_ratio || '9:16')}</span>
          <span class="badge" style="background:rgba(99,102,241,0.15);color:#a5b4fc;font-size:11px;">${esc(p.genre || 'Phim ngắn')}</span>
        </div>
        ${p.logline ? `<div class="u-fs125 u-c-muted u-mt4 u-fs-it">"${esc(p.logline)}"</div>` : ''}
      </div>

      <div style="font-size:12px;color:var(--muted);text-align:right;display:flex;flex-direction:column;align-items:flex-end;gap:4px;">
        <div>Tổng cảnh: <b class="u-c-text">${p.shots?.length || 0}</b> | Dự kiến: <b class="u-c-text">${p.total_duration || (p.shots?.length || 0)*10}s</b></div>
        <div class="fx fx-ac fx-g6 fx-wrap fx-jc-fe">
          <span>Giọng đọc: <code class="u-c-accent">${(() => {
            const found = VOICES.find(v => v.id === p.default_voice);
            return found ? (found.label.length > 28 ? found.label.slice(0,28)+'...' : found.label) : (p.default_voice || 'Hoài My');
          })()}</code></span>
          <span class="badge" style="background:${p.default_voice_mode === 'ai_voice' ? 'rgba(245,158,11,0.15)' : 'rgba(99,102,241,0.12)'};color:${p.default_voice_mode === 'ai_voice' ? 'var(--yellow)' : 'var(--accent)'};">${
            p.default_voice_mode === 'ai_voice' ? '🤖 Giọng gốc AI (Không lồng)' :
            p.default_voice_mode === 'dialogue' ? '🗣️ Khớp môi' : '🎙️ Thuyết minh'
          }</span>
          <button class="btn btn-ghost btn-sm u-p-2-8 u-fs11" onclick="openShotDeskProjectVoiceModal()" title="Cấu hình giọng đọc & phong cách thoại cho toàn dự án">⚙️ Đổi Giọng Dự Án</button>
        </div>
      </div>
    </div>

    <!-- Character Bible Section -->
    <div style="border-top:1px solid var(--border);padding-top:10px;margin-top:10px;">
      <div style="font-weight:700;font-size:12px;color:var(--muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:0.5px;">
        👥 Thư viện Nhân vật (Character Bible & Character Blocks chuẩn hoá)
      </div>
      <div style="display:flex;gap:10px;overflow-x:auto;padding-bottom:4px;">
        ${charCardsHtml || '<div class="u-c-muted u-fs12">Không có nhân vật nào trong bible.</div>'}
      </div>
    </div>
  `;
}

function updateShotDeskStats() {
  if (!currentShotDeskProject || !currentShotDeskProject.shots) return;
  const countBadge = document.getElementById('sdShotsCountBadge');
  const statsEl = document.getElementById('sdMatrixStats');
  const shots = currentShotDeskProject.shots;
  if (countBadge) countBadge.textContent = shots.length;

  const readyCnt = shots.filter(s => s.status === 'ready' || s.status === 'imported').length;
  const renderingCnt = shots.filter(s => s.status === 'rendering').length;
  const reviewCnt = shots.filter(s => s.status === 'review' || (!!s.video_url && s.status !== 'approved' && s.status !== 'done')).length;
  const approvedCnt = shots.filter(s => s.status === 'approved' || s.status === 'done').length;
  const failedCnt = shots.filter(s => !!s.error && !s.video_url).length;
  const unapprovedCnt = shots.length - approvedCnt;

  // Cập nhật số đếm trên các Filter Pills
  const setCnt = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
  setCnt('sdFilterCnt-all', shots.length);
  setCnt('sdFilterCnt-unapproved', unapprovedCnt);
  setCnt('sdFilterCnt-review', reviewCnt);
  setCnt('sdFilterCnt-ready', readyCnt);
  setCnt('sdFilterCnt-rendering', renderingCnt);
  setCnt('sdFilterCnt-error', failedCnt);
  setCnt('sdFilterCnt-approved', approvedCnt);

  // Cập nhật Jump to Shot dropdown
  const jumpEl = document.getElementById('sdJumpSelect');
  if (jumpEl && (jumpEl.options.length <= 1 || jumpEl.options.length !== shots.length + 1)) {
    const curVal = jumpEl.value;
    jumpEl.innerHTML = '<option value="">Nhảy tới Shot...</option>' + shots.map(s => {
      const isApp = s.status === 'approved' || s.status === 'done';
      const isRev = s.status === 'review' || !!s.video_url;
      const icon = isApp ? '✅' : (isRev ? '👁️' : (s.error ? '❌' : (s.status === 'rendering' ? '⏳' : '⚪')));
      return `<option value="${s.index}">${icon} #${s.index} (${s.duration}s)</option>`;
    }).join('');
    if (curVal) jumpEl.value = curVal;
  }

  // Cập nhật nút Phê Duyệt Tất Cả
  const approveAllBtn = document.getElementById('sdApproveAllBtn');
  if (approveAllBtn) {
    if (reviewCnt > 0) {
      approveAllBtn.innerHTML = `✅ Phê Duyệt ${reviewCnt} Cảnh Đã Render`;
      approveAllBtn.classList.remove('btn-outline');
      approveAllBtn.classList.add('btn-success');
    } else {
      approveAllBtn.innerHTML = `✅ Phê Duyệt Tất Cả Cảnh Đã Render`;
      approveAllBtn.classList.remove('btn-success');
      approveAllBtn.classList.add('btn-outline');
    }
  }

  if (statsEl) {
    statsEl.innerHTML = `
      <span class="sd-stat-clickable" onclick="setShotDeskFilter('ready')" title="Bấm để lọc các cảnh chờ render">Chờ render: <b class="u-c-accent">${readyCnt}</b></span>
      <span class="sd-stat-clickable" onclick="setShotDeskFilter('rendering')" title="Bấm để lọc các cảnh đang render">Đang render: <b class="u-c-yellow">${renderingCnt}</b></span>
      <span class="sd-stat-clickable" onclick="setShotDeskFilter('review')" title="Bấm để lọc các cảnh đã có video chờ duyệt">Chờ Review: <b style="color:#a5b4fc;">${reviewCnt}</b></span>
      <span class="sd-stat-clickable" onclick="setShotDeskFilter('approved')" title="Bấm để lọc các cảnh đã duyệt">Đã Approved: <b class="u-c-green">${approvedCnt}/${shots.length}</b></span>
      <span class="sd-stat-clickable" onclick="setShotDeskFilter('unapproved')" title="Bấm để lọc toàn bộ các cảnh CHƯA duyệt" style="color:#f59e0b;">Chưa duyệt: <b>${unapprovedCnt}</b></span>
      ${failedCnt > 0 ? `
        <span class="sd-stat-clickable u-c-red" onclick="setShotDeskFilter('error')">Bị lỗi: <b>${failedCnt}</b></span>
        <button class="btn btn-warning btn-sm u-p-2-10 u-fs11 u-fw700" onclick="autoHealAllFailedShots()" title="Tự động sửa toàn bộ các shot đang bị lỗi và render lại">
          ⚡ AI Sửa & Render Lại ${failedCnt} Shot Lỗi
        </button>
      ` : ''}
    `;
  }

  // Cập nhật Persistent Floating Render HUD
  updateShotDeskRenderHUD(shots, renderingCnt, approvedCnt, reviewCnt);
}

window.currentShotDeskFilter = 'all';

function setShotDeskFilter(filterKey) {
  window.currentShotDeskFilter = filterKey;
  document.querySelectorAll('#sdFilterPills .sd-filter-pill').forEach(pill => {
    pill.classList.toggle('is-active', pill.getAttribute('data-filter') === filterKey);
  });
  applyShotDeskFilter();
  if (currentShotDeskProject && currentShotDeskProject.shots) renderCinemaMiniMap(currentShotDeskProject.shots);
  applyShotDeskScrollFrameState();
}

function applyShotDeskFilter() {
  if (!currentShotDeskProject || !currentShotDeskProject.shots) return;
  const filterKey = window.currentShotDeskFilter || 'all';
  const query = (document.getElementById('sdSearchInput')?.value || '').toLowerCase().trim();
  const clearBtn = document.getElementById('sdSearchClear');
  if (clearBtn) clearBtn.style.display = query ? 'inline' : 'none';

  let visibleCount = 0;
  currentShotDeskProject.shots.forEach(s => {
    const card = document.getElementById(`sdShotCard-${s.index}`);
    const sbCard = document.getElementById(`sdSbCard-${s.index}`);
    if (!card && !sbCard) return;

    // 1. Status Filter match
    let matchStatus = true;
    const isApproved = s.status === 'approved' || s.status === 'done';
    const isReview = s.status === 'review' || (!!s.video_url && !isApproved);
    const isReady = s.status === 'ready' || s.status === 'imported';
    const isRendering = s.status === 'rendering';
    const hasError = !!s.error;

    if (filterKey === 'unapproved') {
      matchStatus = !isApproved;
    } else if (filterKey === 'review') {
      matchStatus = isReview;
    } else if (filterKey === 'ready') {
      matchStatus = isReady;
    } else if (filterKey === 'rendering') {
      matchStatus = isRendering;
    } else if (filterKey === 'error') {
      matchStatus = hasError;
    } else if (filterKey === 'approved') {
      matchStatus = isApproved;
    }

    // 2. Text Search match
    let matchQuery = true;
    if (query) {
      const idxStr = String(s.index);
      const title = (s.title || '').toLowerCase();
      const prompt = (s.prompt || '').toLowerCase();
      const voiceover = (s.voiceover || '').toLowerCase();
      const trans = (s.transition_note || '').toLowerCase();
      const speaker = (s.speaker || '').toLowerCase();
      const voice = (s.voice || '').toLowerCase();
      const voiceMode = (s.voice_mode || '').toLowerCase();
      const isAiVoice = voiceMode === 'ai_voice';
      const lipSyncStr = isAiVoice ? 'không lồng tiếng ai voice native giọng gốc ai' :
                         (s.lip_sync || voiceMode === 'dialogue') ? 'khớp môi lipsync lip-sync' : 'thuyết minh voiceover';

      matchQuery = idxStr === query ||
                   `#${idxStr}` === query ||
                   `shot ${idxStr}` === query ||
                   idxStr.includes(query) ||
                   title.includes(query) ||
                   prompt.includes(query) ||
                   voiceover.includes(query) ||
                   trans.includes(query) ||
                   speaker.includes(query) ||
                   voice.includes(query) ||
                   voiceMode.includes(query) ||
                   lipSyncStr.includes(query);
    }

    const isVisible = matchStatus && matchQuery;
    if (card) card.classList.toggle('is-filtered-out', !isVisible);
    if (sbCard) sbCard.classList.toggle('is-filtered-out', !isVisible);
    if (isVisible) visibleCount++;
  });

  // Empty message
  let emptyMsg = document.getElementById('sdFilterEmptyMsg');
  if (visibleCount === 0) {
    if (!emptyMsg) {
      emptyMsg = document.createElement('div');
      emptyMsg.id = 'sdFilterEmptyMsg';
      emptyMsg.className = 'sd-empty-msg'; // style o css/ui.css (.sd-empty-msg)
      const container = document.getElementById('sdShotsListContainer');
      if (container) container.appendChild(emptyMsg);
    }
    emptyMsg.innerHTML = `
      <div class="empty-icon">🔍</div>
      <div class="empty-title">Không tìm thấy shot nào phù hợp với bộ lọc</div>
      <div class="empty-text">Bộ lọc: "<b>${filterKey}</b>"${query ? ` | Từ khóa: "<b>${esc(query)}</b>"` : ''}</div>
      <button class="btn btn-ghost btn-sm" onclick="setShotDeskFilter('all'); clearShotDeskSearch();">✕ Bỏ Lọc & Hiện Tất Cả</button>
    `;
    emptyMsg.style.display = 'block';
  } else if (emptyMsg) {
    emptyMsg.style.display = 'none';
  }
}

function clearShotDeskSearch() {
  const input = document.getElementById('sdSearchInput');
  if (input) input.value = '';
  applyShotDeskFilter();
  if (currentShotDeskProject && currentShotDeskProject.shots) renderCinemaMiniMap(currentShotDeskProject.shots);
  applyShotDeskScrollFrameState();
}

function jumpToShotDeskShot(shotIndex) {
  if (!shotIndex) return;
  const idx = parseInt(shotIndex);
  if (!idx) return;

  const targetCard = document.getElementById(`sdSbCard-${idx}`) || document.getElementById(`sdShotCard-${idx}`);
  if (targetCard && targetCard.classList.contains('is-filtered-out')) {
    setShotDeskFilter('all');
    clearShotDeskSearch();
  }

  const highlightCard = document.getElementById(`sdSbCard-${idx}`) || document.getElementById(`sdShotCard-${idx}`);
  if (highlightCard) {
    highlightCard.scrollIntoView({ behavior: 'smooth', block: 'center' });
    highlightCard.style.transition = 'box-shadow 0.3s, border-color 0.3s, transform 0.3s';
    highlightCard.style.borderColor = 'var(--accent)';
    highlightCard.style.boxShadow = '0 0 24px rgba(99, 102, 241, 0.6)';
    highlightCard.style.transform = 'scale(1.02)';
    setTimeout(() => {
      highlightCard.style.borderColor = '';
      highlightCard.style.boxShadow = '';
      highlightCard.style.transform = '';
    }, 1500);
  }
}

function renderShotErrorHtml(s) {
  if (!s.error) return '';
  return `
    <div style="background:rgba(248,113,113,0.1);border:1px solid rgba(248,113,113,0.3);border-radius:6px;padding:8px 12px;color:var(--red);font-size:12px;">
      <div class="u-mb6"><b>❌ Lỗi render:</b> ${esc(s.error)}</div>
      <div class="fx fx-g8 fx-wrap">
        <button class="btn btn-warning btn-sm" onclick="autoHealAndRetryShotDesk(${s.index})" title="Tự động loại bỏ từ khóa cấm/nhạy cảm và render lại ngay">
          ⚡ AI Sửa Prompt & Thử Lại
        </button>
        <button class="btn btn-ghost btn-sm" style="color:var(--text);border-color:var(--border);" onclick="openShotDeskEditModal(${s.index})">
          ✏️ Sửa Thủ Công
        </button>
      </div>
    </div>
  `;
}

function renderShotMediaHtml(s) {
  if (!s.video_url) return '';
  const isAiVoice = s.voice_mode === 'ai_voice';
  const isDialogue = !isAiVoice && (s.voice_mode === 'dialogue' || s.lip_sync);
  return `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;background:var(--bg0);border:1px solid var(--border);border-radius:8px;padding:10px;margin-top:8px;">
      <div style="background:#000;border-radius:6px;overflow:hidden;">
        <video src="${s.video_url}" data-src="${s.video_url}" controls preload="none" playsinline style="width:100%;max-height:220px;display:block;"></video>
      </div>
      <div class="fx fx-col fx-jc-c fx-g8 u-fs12">
        <div class="u-fw700 u-c-green">✅ Clip đã render thành công</div>
        <div><b>Audit:</b> <span class="badge" style="background:rgba(52,211,153,0.15);color:var(--green);">${esc(s.audit?.character_check || 'Đạt chuẩn')}</span></div>
        <div><b>Âm thanh / Thoại:</b> <span class="badge" style="background:${isAiVoice ? 'rgba(245,158,11,0.15)' : isDialogue ? 'rgba(52,211,153,0.15)' : 'rgba(99,102,241,0.15)'};color:${isAiVoice ? 'var(--yellow)' : isDialogue ? 'var(--green)' : 'var(--accent)'};">${
          isAiVoice ? '🤖 Không lồng tiếng (Giọng gốc AI)' :
          isDialogue ? '👄 Nhân vật tự nói (Khớp môi)' : '🎙️ Thuyết minh'
        }</span></div>
        ${s.audio_url ? `
          <div class="u-mt4">
            <div class="fx fx-jc-sb fx-ac u-mb2">
              <label class="u-fs11 u-mb0">${isAiVoice ? 'Âm thanh gốc AI:' : 'Giọng đọc thoại:'}</label>
              <span style="font-size:10px;color:${isAiVoice ? 'var(--yellow)' : 'var(--accent)'};">${isAiVoice ? '🤖 AI Native Sound' : (s.voice || '').includes('NamMinh') ? '👨 Nam Minh' : '👩 Hoài My'}</span>
            </div>
            <audio src="${s.audio_url}" controls preload="none" style="width:100%;height:32px;"></audio>
          </div>
        ` : ''}
      </div>
    </div>
  `;
}

function renderShotActionsHtml(s) {
  const isRendering = s.status === 'rendering';
  const isApproved = s.status === 'approved' || s.status === 'done';
  return `
    <div class="fx fx-g6 fx-wrap">
      <button class="btn btn-primary btn-sm" onclick="renderSingleShotDeskShot(${s.index}, false)" ${isRendering ? 'disabled' : ''}>
        ${isRendering ? '⏳ Đang Render...' : (s.video_url ? '🔄 Render Lại (Retry)' : '🎬 Render Shot')}
      </button>

      ${s.compliance_prompt ? `
        <button class="btn btn-ghost btn-sm" style="color:var(--yellow);border-color:rgba(234,179,8,0.3);" onclick="renderSingleShotDeskShot(${s.index}, true)" ${isRendering ? 'disabled' : ''} title="Dùng prompt an toàn đã chuẩn bị sẵn để tránh bị AI từ chối">
          🛡️ Render Compliance
        </button>
      ` : ''}
    </div>

    <div class="fx fx-g6 fx-wrap">
      <button class="btn btn-ghost btn-sm" style="color:#a5b4fc;border-color:rgba(99,102,241,0.4);" onclick="openReviewDeskForShot(${s.index})" title="Mở phòng duyệt Director Review Desk cho shot này">
        👁️ Mở Review Desk
      </button>

      ${isApproved ? `
        <button class="btn btn-ghost btn-sm" style="color:var(--green);border-color:var(--green);" disabled>
          ✅ Đã Approved
        </button>
      ` : `
        <button class="btn btn-success btn-sm" onclick="approveShotDeskShot(${s.index})" ${!s.video_url ? 'disabled title="Cần render clip trước khi duyệt"' : ''}>
          ✅ Phê Duyệt (Approve)
        </button>
      `}
    </div>
  `;
}

function renderSingleShotCardHtml(s) {
  const isRendering = s.status === 'rendering';
  const isReview = s.status === 'review';
  const isApproved = s.status === 'approved' || s.status === 'done';
  const hasError = !!s.error;

  let statusBadgeClass = 'sd-status-ready';
  let statusLabel = 'Ready (Sẵn sàng)';
  if (s.status === 'imported') { statusBadgeClass = 'sd-status-imported'; statusLabel = 'Imported'; }
  else if (isRendering) { statusBadgeClass = 'sd-status-rendering'; statusLabel = '⏳ Đang render...'; }
  else if (isReview) { statusBadgeClass = 'sd-status-review'; statusLabel = '👁️ Chờ Review Desk duyệt'; }
  else if (s.status === 'approved') { statusBadgeClass = 'sd-status-approved'; statusLabel = '✅ Approved (Đã duyệt)'; }
  else if (s.status === 'done') { statusBadgeClass = 'sd-status-done'; statusLabel = '🎬 Done (Đã dựng)'; }

  return `
    <div class="sd-shot-card ${isReview ? 'is-review' : ''} ${isApproved ? 'is-approved' : ''}" id="sdShotCard-${s.index}" data-status="${s.status}" data-video-url="${s.video_url || ''}" data-error="${s.error || ''}">
      <div class="fx fx-jc-sb fx-ac fx-wrap fx-g8">
        <div class="fx fx-ac fx-g10">
          <input type="checkbox" class="sd-card-check" id="sdDetCheck-${s.index}" ${selectedShotIndices.has(s.index) ? 'checked' : ''} onchange="onToggleShotSelect(${s.index}, this.checked)" title="Chọn cảnh này">
          <div style="background:var(--accent2);color:#fff;width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:12px;">
            ${s.index}
          </div>
          <div>
            <span class="u-fw700 u-fs14 u-c-text">${esc(s.title || `Shot ${s.index}`)}</span>
            <span class="u-fs12 u-c-muted u-ml6">⏱️ ${s.duration}s</span>
            ${s.transition_note ? `<span class="sd-transition-pill u-ml8">${esc(s.transition_note)}</span>` : ''}
          </div>
        </div>

        <div class="fx fx-ac fx-g8">
          <span id="sdShotBadge-${s.index}" class="sd-status-badge ${statusBadgeClass}">${statusLabel}</span>
          <button class="btn btn-ghost btn-sm" onclick="openShotDeskEditModal(${s.index})" title="Chỉnh sửa prompt, lời thoại, thời lượng">✏️ Sửa</button>
        </div>
      </div>

      ${s.has_middle_frame ? `
        <div style="background:rgba(234,179,8,0.08);border:1px solid rgba(234,179,8,0.25);border-radius:6px;padding:6px 10px;font-size:11.5px;color:var(--yellow);display:flex;align-items:center;gap:6px;margin-top:8px;">
          <span>⚡</span> <b>Middle Frame tự động:</b> Hệ thống sẽ chia Sub-clip A (0-5s) & Sub-clip B (5-10s) rồi ghép liền mạch để chống trôi nhân vật!
        </div>
      ` : ''}

      <div class="sd-frames-strip">
        <div class="sd-frame-slot">
          <span class="sd-frame-label">📸 First Frame (Bắt đầu)</span>
          ${s.first_frame_url ? `
            <img loading="lazy" decoding="async" src="${s.first_frame_url}" alt="First Frame" onclick="openShotDeskImageModal('${s.first_frame_url}', 'Shot ${s.index} - First Frame')" title="Bấm để xem ảnh lớn">
          ` : `
            <div style="width:100%;aspect-ratio:9/16;max-height:180px;background:var(--bg1);border:1px dashed var(--border);border-radius:6px;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:11px;">Chưa có ảnh</div>
          `}
        </div>

        <div class="sd-frame-slot">
          <span class="sd-frame-label">📸 Middle Frame (Giữa)</span>
          ${s.middle_frame_url ? `
            <img loading="lazy" decoding="async" src="${s.middle_frame_url}" alt="Middle Frame" onclick="openShotDeskImageModal('${s.middle_frame_url}', 'Shot ${s.index} - Middle Frame')" title="Bấm để xem ảnh lớn">
          ` : `
            <div style="width:100%;aspect-ratio:9/16;max-height:180px;background:var(--bg1);border:1px dashed var(--border);border-radius:6px;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:11px;">Không dùng (Shot đơn)</div>
          `}
        </div>

        <div class="sd-frame-slot">
          <span class="sd-frame-label">📸 Last Frame (Kết thúc)</span>
          ${s.last_frame_url ? `
            <img loading="lazy" decoding="async" src="${s.last_frame_url}" alt="Last Frame" onclick="openShotDeskImageModal('${s.last_frame_url}', 'Shot ${s.index} - Last Frame')" title="Bấm để xem ảnh lớn">
          ` : `
            <div style="width:100%;aspect-ratio:9/16;max-height:180px;background:var(--bg1);border:1px dashed var(--border);border-radius:6px;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:11px;">Chưa có ảnh</div>
          `}
        </div>
      </div>

      <div style="background:var(--bg0);border:1px solid var(--border);border-radius:6px;padding:10px 12px;font-size:12px;line-height:1.5;margin-top:8px;">
        <div class="u-fw600 u-c-accent u-mb2 fx fx-jc-sb">
          <span>PROMPT ĐIỆN ẢNH:</span>
          ${s.used_compliance ? '<span class="u-c-green u-fs11">🛡️ Đã tự kích hoạt Compliance Prompt</span>' : ''}
        </div>
        <div style="color:var(--text);font-family:monospace;font-size:11.5px;max-height:60px;overflow-y:auto;white-space:pre-wrap;">${esc(s.prompt)}</div>

        ${s.compliance_prompt ? `
          <div style="margin-top:6px;padding-top:6px;border-top:1px dashed var(--border);font-size:11px;">
            <span class="u-c-yellow u-fw600">🛡️ Compliance Prompt (Dự phòng chống vi phạm):</span>
            <div style="color:var(--muted);font-family:monospace;margin-top:2px;">${esc(s.compliance_prompt)}</div>
          </div>
        ` : ''}

        ${s.voiceover ? `
          <div style="margin-top:6px;padding-top:6px;border-top:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px;">
            <div style="color:#a5b4fc;font-size:12px;">
              <b>${s.voice_mode === 'ai_voice' ? '🤖 Thoại gốc AI (Subtitles):' : (s.voice_mode === 'dialogue' || s.lip_sync) ? '🗣️ Thoại nhân vật:' : '🎙️ Thuyết minh:'}</b> <i>"${esc(s.voiceover)}"</i>
            </div>
            <div class="fx fx-g6 fx-ac u-fs11">
              ${s.speaker ? `<span class="badge" style="background:rgba(99,102,241,0.15);color:#a5b4fc;padding:2px 6px;border-radius:4px;">👤 ${esc(s.speaker)}</span>` : ''}
              ${s.voice_mode === 'ai_voice' ? `
                <span class="badge" style="background:rgba(245,158,11,0.15);color:var(--yellow);padding:2px 6px;border-radius:4px;" title="Phụ thuộc hoàn toàn vào giọng & âm thanh do AI video sinh ra">🤖 Giọng gốc AI</span>
                <span class="badge" style="background:rgba(148,163,184,0.15);color:var(--muted);padding:2px 6px;border-radius:4px;">Không lồng tiếng</span>
              ` : `
                <span class="badge" style="background:rgba(99,102,241,0.15);color:var(--accent);padding:2px 6px;border-radius:4px;">
                  ${(s.voice || '').includes('NamMinh') ? '👨 Nam Minh' : '👩 Hoài My'}
                </span>
                ${(s.voice_mode === 'dialogue' || s.lip_sync) ?
                  `<span class="badge" style="background:rgba(52,211,153,0.15);color:var(--green);padding:2px 6px;border-radius:4px;" title="Khẩu hình miệng khớp với câu thoại">👄 Khớp môi</span>` :
                  `<span class="badge" style="background:rgba(148,163,184,0.15);color:var(--muted);padding:2px 6px;border-radius:4px;">🎙️ Dẫn chuyện</span>`}
              `}
            </div>
          </div>
        ` : (s.voice_mode === 'ai_voice' ? `
          <div style="margin-top:6px;padding-top:6px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;">
            <span class="badge" style="background:rgba(245,158,11,0.15);color:var(--yellow);padding:2px 6px;border-radius:4px;">🤖 Âm thanh & Giọng gốc của AI (Không lồng tiếng)</span>
          </div>
        ` : '')}
      </div>

      <div class="u-mt8" id="sdShotError-${s.index}">
        ${renderShotErrorHtml(s)}
      </div>

      <div id="sdShotMedia-${s.index}">
        ${renderShotMediaHtml(s)}
      </div>

      <div id="sdShotActions-${s.index}" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;padding-top:8px;">
        ${renderShotActionsHtml(s)}
      </div>
    </div>
  `;
}

let currentShotDeskViewMode = localStorage.getItem('sd_view_mode') || 'grid';

function setShotDeskViewMode(mode) {
  currentShotDeskViewMode = mode;
  try { localStorage.setItem('sd_view_mode', mode); } catch (e) {}
  updateViewModeButtons();
  renderShotDeskMatrixScreen();
}

function updateViewModeButtons() {
  const gridBtn = document.getElementById('sdViewBtn-grid');
  const listBtn = document.getElementById('sdViewBtn-detailed');
  if (gridBtn && listBtn) {
    if (currentShotDeskViewMode === 'grid') {
      gridBtn.style.background = 'var(--accent)';
      gridBtn.style.color = '#000';
      gridBtn.style.fontWeight = '700';
      gridBtn.style.borderColor = 'var(--accent)';
      listBtn.style.background = 'transparent';
      listBtn.style.color = 'var(--muted)';
      listBtn.style.fontWeight = 'normal';
      listBtn.style.borderColor = 'transparent';
    } else {
      listBtn.style.background = 'var(--accent)';
      listBtn.style.color = '#000';
      listBtn.style.fontWeight = '700';
      listBtn.style.borderColor = 'var(--accent)';
      gridBtn.style.background = 'transparent';
      gridBtn.style.color = 'var(--muted)';
      gridBtn.style.fontWeight = 'normal';
      gridBtn.style.borderColor = 'transparent';
    }
  }
}

function renderSingleShotStoryboardCardHtml(s) {
  const isRendering = s.status === 'rendering';
  const isReview = s.status === 'review';
  const isApproved = s.status === 'approved' || s.status === 'done';
  const hasError = !!s.error;

  let statusBadgeClass = 'sd-status-ready';
  let statusShortLabel = 'Sẵn sàng';
  if (s.status === 'imported') { statusBadgeClass = 'sd-status-imported'; statusShortLabel = 'Imported'; }
  else if (isRendering) { statusBadgeClass = 'sd-status-rendering'; statusShortLabel = 'Render...'; }
  else if (isReview) { statusBadgeClass = 'sd-status-review'; statusShortLabel = 'Chờ duyệt'; }
  else if (s.status === 'approved') { statusBadgeClass = 'sd-status-approved'; statusShortLabel = 'Đã duyệt'; }
  else if (s.status === 'done') { statusBadgeClass = 'sd-status-done'; statusShortLabel = 'Đã dựng'; }
  else if (hasError) { statusBadgeClass = 'sd-status-error'; statusShortLabel = 'Lỗi'; }

  const thumbUrl = s.first_frame_url || s.last_frame_url || s.middle_frame_url || '';
  const isSelected = selectedShotIndices.has(s.index);

  return `
    <div class="sd-sb-card ${isReview ? 'is-review' : ''} ${isApproved ? 'is-approved' : ''} ${isRendering ? 'is-rendering' : ''} ${hasError ? 'is-error' : ''} ${isSelected ? 'is-selected' : ''}" id="sdSbCard-${s.index}" data-shot-index="${s.index}" data-status="${s.status}" onclick="openReviewDeskForShot(${s.index})">
      ${isRendering ? '<div class="sd-card-shimmer-progress"></div>' : ''}

      <!-- Selection Checkbox -->
      <div class="sd-card-check-wrap" onclick="event.stopPropagation();">
        <input type="checkbox" class="sd-card-check" id="sdCheck-${s.index}" ${isSelected ? 'checked' : ''} onchange="onToggleShotSelect(${s.index}, this.checked)" title="Chọn cảnh này">
      </div>

      <div class="sd-sb-thumb-wrap">
        ${thumbUrl ? `
          <img src="${thumbUrl}" class="sd-sb-thumb" alt="Shot ${s.index}" loading="lazy">
        ` : `
          <div style="width:100%;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;color:var(--muted);background:rgba(0,0,0,0.45);">
            <span style="font-size:28px;opacity:0.35;">🎬</span>
            <span style="font-size:11px;margin-top:4px;font-family:'JetBrains Mono',monospace;">Shot ${s.index}</span>
          </div>
        `}

        <div class="sd-sb-overlay">
          <div class="sd-sb-badges-top" style="padding-left:24px;">
            <span class="sd-sb-shot-num">#${s.index}</span>
            <span id="sdSbBadge-${s.index}" class="sd-status-badge ${statusBadgeClass} u-fs10 u-p-2-7">${statusShortLabel}</span>
          </div>

          <div class="sd-sb-bottom-info">
            <div style="display:flex;justify-content:space-between;align-items:center;font-size:10.5px;color:#fff;">
              <span style="background:rgba(0,0,0,0.7);padding:2px 6px;border-radius:4px;font-family:'JetBrains Mono',monospace;">⏱️ ${s.duration}s</span>
              ${s.transition_note ? `<span class="sd-transition-pill" style="font-size:9.5px;padding:1px 5px;">${esc(s.transition_note)}</span>` : ''}
            </div>
          </div>
        </div>

        <div class="sd-sb-quick-actions" onclick="event.stopPropagation();">
          <button class="btn btn-primary btn-sm" style="width:100%;font-size:11.5px;padding:5px;" onclick="openReviewDeskForShot(${s.index})">
            👁️ Review Desk
          </button>
          ${s.video_url && !isApproved ? `
            <button class="btn btn-success btn-sm" style="width:100%;font-size:11.5px;padding:5px;" onclick="approveShotDeskShot(${s.index}, true)">
              ✅ Phê Duyệt
            </button>
          ` : ''}
          <div class="fx fx-g4 u-w100">
            <button class="btn btn-ghost btn-sm" style="flex:1;font-size:11px;padding:4px;background:rgba(255,255,255,0.1);color:#fff;" onclick="openShotDeskEditModal(${s.index})" title="Sửa Prompt">
              ✏️ Sửa
            </button>
            <button class="btn btn-ghost btn-sm" style="flex:1;font-size:11px;padding:4px;background:rgba(255,255,255,0.1);color:#fff;" onclick="renderSingleShotDeskShot(${s.index}, false)" ${isRendering ? 'disabled' : ''} title="Render lại">
              ${isRendering ? '⏳' : '🔄'}
            </button>
          </div>
        </div>
      </div>

      <div class="sd-sb-meta">
        <div class="sd-sb-title" title="${esc(s.title || `Shot ${s.index}`)}">
          ${esc(s.title || `Shot ${s.index}`)}
        </div>
        <div style="font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:'JetBrains Mono',monospace;" title="${esc(s.prompt)}">
          ${esc(s.prompt)}
        </div>
        <div class="sd-sb-voice-badge">
          <span>${s.speaker ? `👤 ${esc(s.speaker)}` : (s.voice_mode === 'ai_voice' ? '🤖 Native AI' : '🎙️ Thuyết minh')}</span>
          <div class="fx fx-ac fx-g6">
            ${s.audio_url ? `
              <div class="sd-wave-widget" id="sdWave-${s.index}" onclick="event.stopPropagation(); playQuickCardAudio('${s.audio_url}', this, ${s.index})" title="Nghe thử giọng đọc">
                <span class="sd-wave-icon" id="sdWaveIcon-${s.index}" style="font-size:9.5px;color:var(--accent);">▶</span>
                <div class="sd-wave-bars">
                  <span class="sd-wave-bar"></span>
                  <span class="sd-wave-bar"></span>
                  <span class="sd-wave-bar"></span>
                  <span class="sd-wave-bar"></span>
                  <span class="sd-wave-bar"></span>
                  <span class="sd-wave-bar"></span>
                  <span class="sd-wave-bar"></span>
                  <span class="sd-wave-bar"></span>
                </div>
              </div>
            ` : ''}
            ${s.video_url ? '<span class="u-c-green u-fw700">▶ Sẵn sàng</span>' : '<span class="u-c-muted">Chưa có clip</span>'}
          </div>
        </div>
      </div>
    </div>
  `;
}

function renderCinemaMiniMap(shots) {
  const wrap = document.getElementById('sdCinemaMiniMapWrap');
  const track = document.getElementById('sdCinemaMiniMapTrack');
  const statsEl = document.getElementById('sdMiniMapStats');
  if (!wrap || !track) return;

  if (!shots || shots.length === 0) {
    wrap.style.display = 'none';
    return;
  }

  wrap.style.display = 'flex';
  const approved = shots.filter(s => s.status === 'approved' || s.status === 'done').length;
  if (statsEl) statsEl.textContent = `${approved}/${shots.length} Approved`;

  track.innerHTML = shots.map(s => {
    let st = s.status || 'ready';
    if (s.error) st = 'error';
    const isApp = s.status === 'approved' || s.status === 'done';
    const isRev = s.status === 'review' || (!!s.video_url && !isApp);
    const cls = isApp ? 'status-approved' : (isRev ? 'status-review' : (s.status === 'rendering' ? 'status-rendering' : (s.error ? 'status-error' : 'status-ready')));
    return `<div class="sd-minimap-seg ${cls}" onclick="scrollToShotCard(${s.index})" title="Shot #${s.index} • ${esc(s.title || '')} [${(s.status || 'ready').toUpperCase()}]"></div>`;
  }).join('');
}

function scrollToShotCard(shotIndex) {
  const card = document.getElementById(`sdSbCard-${shotIndex}`) || document.getElementById(`sdShotCard-${shotIndex}`);
  if (card) {
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    card.classList.remove('sd-card-highlight');
    void card.offsetWidth;
    card.classList.add('sd-card-highlight');
  } else {
    openReviewDeskForShot(shotIndex);
  }
}

function renderShotDeskMatrixScreen(fromPolling = false) {
  const container = document.getElementById('sdShotsListContainer');
  if (!container) return;

  if (!currentShotDeskProject || !currentShotDeskProject.shots || currentShotDeskProject.shots.length === 0) {
    renderCinemaMiniMap([]);
    container.innerHTML = `
      <div style="text-align:center;padding:50px 20px;color:var(--muted);background:var(--bg0);border-radius:10px;border:1px dashed var(--border);">
        <div style="font-size:36px;margin-bottom:10px;">🎬</div>
        <div class="u-fw600 u-fs15 u-c-text">Chưa có kịch bản Shot Desk nào được nạp</div>
        <div style="font-size:13px;margin:8px 0 16px;">Hãy bấm "Nhập File ZIP" hoặc bấm "Nạp Dự Án Mẫu" để bắt đầu làm phim theo chuẩn Shot Desk!</div>
        <button class="btn btn-primary btn-sm" onclick="loadShotDeskSampleProject()">⚡ Nạp Mẫu: Bức Thư Chưa Gửi</button>
      </div>
    `;
    const countBadge = document.getElementById('sdShotsCountBadge');
    const statsEl = document.getElementById('sdMatrixStats');
    if (countBadge) countBadge.textContent = '0';
    if (statsEl) statsEl.innerHTML = '';
    return;
  }

  const p = currentShotDeskProject;
  const shots = p.shots;
  updateShotDeskStats();

  const currentProjectId = container.getAttribute('data-project-id');
  const currentViewMode = container.getAttribute('data-view-mode');
  const needsFullRender = !fromPolling && (currentProjectId !== p.id || currentViewMode !== currentShotDeskViewMode || (currentShotDeskViewMode === 'grid' ? !container.querySelector('.sd-storyboard-grid') : !container.querySelector('.sd-shot-card')));

  if (needsFullRender) {
    container.setAttribute('data-project-id', p.id);
    container.setAttribute('data-view-mode', currentShotDeskViewMode);
    if (currentShotDeskViewMode === 'grid') {
      container.innerHTML = `<div class="sd-storyboard-grid">${shots.map(s => renderSingleShotStoryboardCardHtml(s)).join('')}</div>`;
    } else {
      container.innerHTML = shots.map(s => renderSingleShotCardHtml(s)).join('');
    }
    renderCinemaMiniMap(shots);
    applyShotDeskFilter();
    updateViewModeButtons();
    applyShotDeskScrollFrameState();
    return;
  }

  // Cập nhật từng phần (Granular update): Tuyệt đối KHÔNG xóa DOM hay video đang phát
  shots.forEach(s => {
    // 1. Cập nhật Storyboard Card nếu đang ở View Grid
    const sbCard = document.getElementById(`sdSbCard-${s.index}`);
    if (sbCard) {
      const prevSbStatus = sbCard.getAttribute('data-status');
      const curStatus = s.status;
      if (prevSbStatus !== curStatus) {
        sbCard.setAttribute('data-status', curStatus);
        const isRev = curStatus === 'review';
        const isApp = curStatus === 'approved' || curStatus === 'done';
        const isRend = curStatus === 'rendering';
        const isErr = !!s.error;
        sbCard.className = `sd-sb-card ${isRev ? 'is-review' : ''} ${isApp ? 'is-approved' : ''} ${isRend ? 'is-rendering' : ''} ${isErr ? 'is-error' : ''}`;

        const sbBadge = document.getElementById(`sdSbBadge-${s.index}`);
        if (sbBadge) {
          let bClass = 'sd-status-ready';
          let bLabel = 'Sẵn sàng';
          if (curStatus === 'imported') { bClass = 'sd-status-imported'; bLabel = 'Imported'; }
          else if (isRend) { bClass = 'sd-status-rendering'; bLabel = 'Render...'; }
          else if (isRev) { bClass = 'sd-status-review'; bLabel = 'Chờ duyệt'; }
          else if (isApp) { bClass = 'sd-status-approved'; bLabel = 'Đã duyệt'; }
          else if (isErr) { bClass = 'sd-status-error'; bLabel = 'Lỗi'; }
          sbBadge.className = `sd-status-badge ${bClass}`;
          sbBadge.textContent = bLabel;
        }

        const thumbImg = sbCard.querySelector('.sd-sb-thumb');
        const newThumb = s.first_frame_url || s.last_frame_url || '';
        if (thumbImg && newThumb && thumbImg.src !== newThumb) {
          thumbImg.src = newThumb;
        }
      }
    }

    // 2. Cập nhật Detailed Card nếu đang ở View Detailed
    const card = document.getElementById(`sdShotCard-${s.index}`);
    if (!card) return;

    const prevStatus = card.getAttribute('data-status');
    const prevVideo = card.getAttribute('data-video-url') || '';
    const prevError = card.getAttribute('data-error') || '';
    const curVideo = s.video_url || '';
    const curError = s.error || '';

    // Nếu thẻ này không có gì thay đổi so với lần trước -> Bỏ qua, bảo tồn 100% video đang phát
    if (prevStatus === s.status && prevVideo === curVideo && prevError === curError) {
      return;
    }

    card.setAttribute('data-status', s.status);
    card.setAttribute('data-video-url', curVideo);
    card.setAttribute('data-error', curError);

    const isReview = s.status === 'review';
    const isApproved = s.status === 'approved' || s.status === 'done';
    card.className = `sd-shot-card ${isReview ? 'is-review' : ''} ${isApproved ? 'is-approved' : ''}`;

    // Cập nhật Badge
    const badge = document.getElementById(`sdShotBadge-${s.index}`);
    if (badge) {
      let statusBadgeClass = 'sd-status-ready';
      let statusLabel = 'Ready (Sẵn sàng)';
      if (s.status === 'imported') { statusBadgeClass = 'sd-status-imported'; statusLabel = 'Imported'; }
      else if (s.status === 'rendering') { statusBadgeClass = 'sd-status-rendering'; statusLabel = '⏳ Đang render...'; }
      else if (s.status === 'review') { statusBadgeClass = 'sd-status-review'; statusLabel = '👁️ Chờ Review Desk duyệt'; }
      else if (s.status === 'approved') { statusBadgeClass = 'sd-status-approved'; statusLabel = '✅ Approved (Đã duyệt)'; }
      else if (s.status === 'done') { statusBadgeClass = 'sd-status-done'; statusLabel = '🎬 Done (Đã dựng)'; }
      badge.className = `sd-status-badge ${statusBadgeClass}`;
      badge.textContent = statusLabel;
    }

    // Cập nhật Error
    const errEl = document.getElementById(`sdShotError-${s.index}`);
    if (errEl) {
      errEl.innerHTML = renderShotErrorHtml(s);
    }

    // Cập nhật Video (chỉ khi URL video thay đổi từ rỗng sang có hoặc ngược lại)
    const mediaEl = document.getElementById(`sdShotMedia-${s.index}`);
    if (mediaEl && prevVideo !== curVideo) {
      const existingVid = mediaEl.querySelector('video');
      // Nếu video đang phát mà URL giống nhau thì tuyệt đối không chạm vào
      if (!existingVid || existingVid.getAttribute('data-src') !== curVideo) {
        mediaEl.innerHTML = renderShotMediaHtml(s);
      }
    }

    // Cập nhật Actions
    const actEl = document.getElementById(`sdShotActions-${s.index}`);
    if (actEl) {
      actEl.innerHTML = renderShotActionsHtml(s);
    }
  });

  applyShotDeskFilter();
  renderCinemaMiniMap(shots);
  applyShotDeskScrollFrameState();
}

function renderReviewDecisionButtons(shot, isApproved, isRendering) {
  const hasError = !!shot.error;
  return `
    <div class="u-fw700 u-fs13 u-c-text">Quyết Định Của Đạo Diễn:</div>
    <div class="fx fx-g8 fx-wrap">
      ${isApproved ? `
        <button class="btn btn-success" style="flex:1;" disabled>
          ✅ ĐÃ PHÊ DUYỆT (APPROVED)
        </button>
      ` : `
        <button class="btn btn-success" style="flex:1;font-size:14px;padding:10px;" onclick="approveShotDeskShot(${shot.index}, true)" ${!shot.video_url ? 'disabled title="Phải render video trước khi duyệt"' : ''}>
          ✅ PHÊ DUYỆT SHOT NÀY (APPROVE)
        </button>
      `}

      ${hasError ? `
        <button class="btn btn-warning" onclick="autoHealAndRetryShotDesk(${shot.index})" title="Tự động làm sạch từ khóa nhạy cảm và render lại an toàn">
          ⚡ AI Sửa Prompt & Thử Lại
        </button>
      ` : ''}

      <button class="btn btn-primary" onclick="renderSingleShotDeskShot(${shot.index}, false)" ${isRendering ? 'disabled' : ''}>
        🔄 Render Lại
      </button>

      ${shot.compliance_prompt ? `
        <button class="btn btn-ghost" style="color:var(--yellow);border-color:rgba(234,179,8,0.4);" onclick="renderSingleShotDeskShot(${shot.index}, true)" ${isRendering ? 'disabled' : ''} title="Render với prompt an toàn">
          🛡️ Retry với Compliance
        </button>
      ` : ''}

      <button class="btn btn-ghost" onclick="openShotDeskEditModal(${shot.index})">
        ✏️ Sửa Prompt
      </button>
    </div>
  `;
}

function toggleReviewDeskLoop() {
  const vid = document.getElementById('sdReviewVideoPlayer');
  const btn = document.getElementById('sdLoopToggleBtn');
  if (!vid || !btn) return;
  vid.loop = !vid.loop;
  btn.innerText = vid.loop ? '🔁 Lặp: Bật' : '🔁 Lặp: Tắt';
  btn.style.borderColor = vid.loop ? 'var(--accent)' : 'rgba(255,255,255,0.25)';
  btn.style.color = vid.loop ? 'var(--accent)' : '#fff';
}

function renderShotDeskReviewScreen(fromPolling = false) {
  const card = document.getElementById('sdReviewDeskCard');
  if (!card) return;

  if (!currentShotDeskProject || !currentShotDeskProject.shots || currentShotDeskProject.shots.length === 0) {
    card.innerHTML = `<div style="text-align:center;padding:40px;color:var(--muted);">Chưa có shot nào trong dự án để review.</div>`;
    return;
  }

  const p = currentShotDeskProject;
  const shots = p.shots;
  const shot = shots.find(s => s.index === currentReviewShotIndex) || shots[0];
  currentReviewShotIndex = shot.index;

  const isApproved = shot.status === 'approved' || shot.status === 'done';
  const isRendering = shot.status === 'rendering';
  const refFrameUrl = shot.first_frame_url || shot.middle_frame_url || shot.last_frame_url || '';

  // Khi cập nhật ngầm từ polling: Không xóa DOM video để người dùng không bị reset pause/play hoặc nhảy clip
  const existingPlayer = document.getElementById('sdReviewVideoPlayer');
  const targetVideoUrl = shot.video_url || '';
  if (fromPolling && card.getAttribute('data-current-shot') == shot.index) {
    if ((existingPlayer && (existingPlayer.getAttribute('data-shot-url') || '') === targetVideoUrl) || (!existingPlayer && !targetVideoUrl)) {
      const decisionCard = document.getElementById('sdReviewDecisionArea');
      if (decisionCard) {
        decisionCard.innerHTML = renderReviewDecisionButtons(shot, isApproved, isRendering);
      }
      return;
    }
  }

  card.setAttribute('data-current-shot', shot.index);

  let charBlocksHtml = '';
  if (shot.characters && shot.characters.length > 0) {
    shot.characters.forEach(cName => {
      const charObj = (p.characters || []).find(c => c.name.toLowerCase() === cName.toLowerCase() || c.id === cName);
      if (charObj) {
        charBlocksHtml += `
          <div style="background:rgba(99,102,241,0.06);border:1px solid rgba(99,102,241,0.25);border-radius:6px;padding:8px 10px;margin-bottom:8px;">
            <div class="u-fw700 u-c-accent u-fs12">👤 ${esc(charObj.name)} - Character Block:</div>
            <div style="font-size:11px;color:var(--text);font-family:monospace;white-space:pre-wrap;margin-top:4px;">${esc(charObj.character_block || 'N/A')}</div>
          </div>
        `;
      }
    });
  }

  card.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:16px;border-bottom:1px solid var(--border);padding-bottom:12px;">
      <div class="fx fx-ac fx-g10">
        <span class="u-fs20">🎬</span>
        <div>
          <div class="u-fw800 u-fs16 u-c-text">MÀN HÌNH REVIEW DESK (ĐẠO DIỄN DUYỆT SHOT)</div>
          <div class="u-fs12 u-c-muted">Kiểm định chất lượng, khuôn mặt nhân vật, watermark và sự liền mạch trước khi ghép phim</div>
        </div>
      </div>

      <div class="fx fx-ac fx-g8 fx-wrap">
        <span class="sd-hotkeys-pill" onclick="openHotkeysCheatSheetModal()" style="cursor:pointer;" title="Bấm xem bảng phím tắt">
          ⌨️ <span class="sd-kbd">Space</span> Play &nbsp; <span class="sd-kbd">V</span> Duyệt &nbsp; <span class="sd-kbd">?</span> Phím tắt
        </span>
        <button class="btn btn-ghost btn-sm" onclick="stepReviewShot(-1)" ${shot.index <= 1 ? 'disabled' : ''}>⏮️ Trước (<span class="sd-kbd" style="font-size:9.5px;padding:0 3px;">A</span>)</button>
        <select style="width:auto;font-weight:700;background:var(--bg0);" onchange="currentReviewShotIndex = parseInt(this.value); renderShotDeskReviewScreen();">
          ${shots.map(s => `
            <option value="${s.index}" ${s.index === shot.index ? 'selected' : ''}>
              Shot ${s.index}: ${s.title || ''} [${s.status.toUpperCase()}]
            </option>
          `).join('')}
        </select>
        <button class="btn btn-ghost btn-sm" onclick="stepReviewShot(1)" ${shot.index >= shots.length ? 'disabled' : ''}>Kế (<span class="sd-kbd" style="font-size:9.5px;padding:0 3px;">D</span>) ⏭️</button>
      </div>
    </div>

    <div class="sd-review-grid">
      <div class="sd-review-sticky-col">
        <div class="sd-review-player">
          ${shot.video_url ? `
            <div style="position:relative;background:#000;border-radius:8px;overflow:hidden;">
              ${(window.isShotDeskAbMode && refFrameUrl) ? `
                <div id="sdAbCompareContainer" class="sd-ab-compare-container" onmousedown="startAbDrag(event)" ontouchstart="startAbDrag(event)" style="aspect-ratio:9/16;max-height:460px;cursor:ew-resize;">
                  <div class="sd-ab-layer">
                    <img loading="lazy" decoding="async" src="${refFrameUrl}" alt="A: Reference Frame" style="width:100%;height:100%;object-fit:cover;">
                    <span class="sd-ab-tag tag-left">A: FRAME GỐC</span>
                  </div>
                  <div id="sdAbOverlay" class="sd-ab-layer sd-ab-layer-overlay" style="clip-path: inset(0 0 0 50%);">
                    <video id="sdReviewVideoPlayer" data-shot-index="${shot.index}" data-shot-url="${shot.video_url}" src="${shot.video_url}" controls playsinline preload="metadata" style="width:100%;height:100%;object-fit:cover;background:#000;"></video>
                    <span class="sd-ab-tag tag-right">B: CLIP VIDEO</span>
                  </div>
                  <div id="sdAbDivider" class="sd-ab-divider" style="left:50%;">
                    <div class="sd-ab-handle">↔</div>
                  </div>
                </div>
              ` : `
                <video id="sdReviewVideoPlayer" data-shot-index="${shot.index}" data-shot-url="${shot.video_url}" src="${shot.video_url}" controls playsinline preload="metadata" style="width:100%;max-height:460px;display:block;border-radius:6px;background:#000;"></video>
              `}
              <div style="position:absolute;top:8px;right:8px;display:flex;gap:6px;z-index:15;">
                ${refFrameUrl ? `
                  <button id="sdAbToggleBtn" class="btn btn-ghost btn-sm" style="background:rgba(0,0,0,0.65);border:1px solid ${window.isShotDeskAbMode ? 'var(--accent)' : 'rgba(255,255,255,0.25)'};color:${window.isShotDeskAbMode ? 'var(--accent)' : '#fff'};font-size:11px;padding:4px 8px;backdrop-filter:blur(4px);" onclick="toggleShotDeskAbMode()" title="So sánh Frame gốc (A) và Clip Video (B)">
                    🔀 So Sánh A/B ${window.isShotDeskAbMode ? '(BẬT)' : ''}
                  </button>
                ` : ''}
                <button id="sdLoopToggleBtn" class="btn btn-ghost btn-sm" style="background:rgba(0,0,0,0.65);border:1px solid rgba(255,255,255,0.25);color:#fff;font-size:11px;padding:4px 8px;backdrop-filter:blur(4px);" onclick="toggleReviewDeskLoop()" title="Bật/Tắt chế độ tự động lặp lại video">
                  🔁 Lặp: Tắt
                </button>
              </div>
              <!-- Playback Speed, Split Slider & Frame Stepping Controls -->
              <div style="display:flex;align-items:center;justify-content:space-between;background:rgba(10,15,29,0.92);padding:6px 10px;border-top:1px solid rgba(255,255,255,0.1);font-size:11px;flex-wrap:wrap;gap:6px;">
                <div class="fx fx-g4 fx-ac">
                  <span class="u-c-muted u-fs105">Tốc độ:</span>
                  <button class="sd-speed-pill" onclick="setReviewVideoSpeed(0.5, this)">0.5x</button>
                  <button class="sd-speed-pill is-active" onclick="setReviewVideoSpeed(1.0, this)">1.0x</button>
                  <button class="sd-speed-pill" onclick="setReviewVideoSpeed(1.25, this)">1.25x</button>
                  <button class="sd-speed-pill" onclick="setReviewVideoSpeed(1.5, this)">1.5x</button>
                  <button class="sd-speed-pill" onclick="setReviewVideoSpeed(2.0, this)">2.0x</button>
                </div>
                ${(window.isShotDeskAbMode && refFrameUrl) ? `
                  <div style="display:flex;gap:6px;align-items:center;flex:1;max-width:200px;margin:0 4px;">
                    <span style="font-size:10px;color:var(--muted);white-space:nowrap;">A (Ảnh)</span>
                    <input id="sdAbRangeSlider" type="range" min="0" max="100" value="50" oninput="updateAbSplit(this.value)" style="flex:1;accent-color:var(--accent);height:4px;cursor:pointer;">
                    <span style="font-size:10px;color:var(--muted);white-space:nowrap;">B (Clip)</span>
                  </div>
                ` : ''}
                <div class="fx fx-g4 fx-ac">
                  <button class="btn btn-ghost btn-sm" onclick="stepReviewVideoFrame(-1)" style="font-size:10.5px;padding:2px 6px;height:22px;" title="Lùi 1 Frame (phím J)">⏮️ -1 Frame (<span class="sd-kbd" style="font-size:9px;padding:0 3px;">J</span>)</button>
                  <button class="btn btn-ghost btn-sm" onclick="stepReviewVideoFrame(1)" style="font-size:10.5px;padding:2px 6px;height:22px;" title="Tới 1 Frame (phím L)">+1 Frame (<span class="sd-kbd" style="font-size:9px;padding:0 3px;">L</span>) ⏭️</button>
                  <button class="btn btn-ghost btn-sm" onclick="toggleReviewTheaterMode()" style="font-size:11px;padding:2px 6px;height:22px;" title="Mở Rộng Rạp Chiếu (Theater Mode)">⛶ Rạp Chiếu</button>
                </div>
              </div>
            </div>
          ` : `
            <div style="aspect-ratio:9/16;max-height:460px;width:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;color:var(--muted);gap:12px;">
              <span style="font-size:48px;opacity:0.3;">⏳</span>
              <div>${isRendering ? 'Đang trong quá trình render video...' : 'Shot này chưa được render clip video.'}</div>
              <button class="btn btn-primary btn-sm" onclick="renderSingleShotDeskShot(${shot.index}, false)">🎬 Render Ngay</button>
            </div>
          `}
        </div>

        ${shot.audio_url ? `
          <div style="background:var(--bg0);border:1px solid var(--border);border-radius:8px;padding:10px 12px;">
            <div class="fx fx-jc-sb fx-ac u-mb8">
              <div style="font-weight:600;font-size:12px;color:${shot.voice_mode === 'ai_voice' ? 'var(--yellow)' : 'var(--accent)'};">
                ${shot.voice_mode === 'ai_voice' ? '🤖 Âm thanh & Giọng gốc của AI (Không lồng tiếng):' :
                  (shot.voice_mode === 'dialogue' || shot.lip_sync) ? '🗣️ Lời thoại nhân vật (Khớp môi):' :
                  '🎙️ Thuyết minh giọng đọc tiếng Việt:'}
              </div>
              <div class="u-fs11 fx fx-g6">
                ${shot.speaker ? `<span class="badge" style="background:rgba(99,102,241,0.15);color:#a5b4fc;">👤 ${esc(shot.speaker)}</span>` : ''}
                ${shot.voice_mode === 'ai_voice' ?
                  `<span class="badge" style="background:rgba(245,158,11,0.15);color:var(--yellow);">🤖 Native AI Sound</span>` :
                  `<span class="badge" style="background:rgba(99,102,241,0.15);color:var(--accent);">${(shot.voice || '').includes('NamMinh') ? '👨 Nam Minh' : '👩 Hoài My'}</span>
                   ${(shot.voice_mode === 'dialogue' || shot.lip_sync) ? '<span class="badge" style="background:rgba(52,211,153,0.15);color:var(--green);">👄 Khớp môi</span>' : ''}`}
              </div>
            </div>
            <div class="fx fx-ac fx-g10">
              <button class="sd-wave-widget" id="sdReviewWaveWidget" onclick="playReviewDeskAudio('${shot.audio_url}', this)" title="Nghe thoại kèm visualizer sóng âm sống động" style="padding:6px 12px;border-radius:6px;background:rgba(255,255,255,0.05);border:1px solid var(--border);cursor:pointer;color:var(--text);">
                <span class="sd-wave-icon" style="font-size:11px;margin-right:2px;">▶</span>
                <span class="sd-wave-bars">
                  <span class="sd-wave-bar"></span><span class="sd-wave-bar"></span>
                  <span class="sd-wave-bar"></span><span class="sd-wave-bar"></span>
                  <span class="sd-wave-bar"></span><span class="sd-wave-bar"></span>
                  <span class="sd-wave-bar"></span><span class="sd-wave-bar"></span>
                </span>
                <span class="u-fs11 u-fw700 u-ml4">Phát Voice</span>
              </button>
              <audio id="sdReviewDeskAudioEl" src="${shot.audio_url}" controls style="flex:1;height:34px;border-radius:4px;"></audio>
            </div>
          </div>
        ` : ''}

        ${shot.error ? `
          <div style="background:rgba(248,113,113,0.1);border:1px solid rgba(248,113,113,0.3);border-radius:8px;padding:12px;color:var(--red);font-size:12px;">
            <div class="u-fw700 u-mb4">❌ Lỗi Render:</div>
            <div class="u-lh14">${esc(shot.error)}</div>
            <div class="u-mt8">
              <button class="btn btn-warning btn-sm" onclick="autoHealAndRetryShotDesk(${shot.index})">
                ⚡ AI Tự Sửa Prompt & Render Lại Ngay
              </button>
            </div>
          </div>
        ` : ''}

        <div id="sdReviewDecisionArea" style="background:var(--bg0);border:1px solid var(--border);border-radius:8px;padding:14px;display:flex;flex-direction:column;gap:10px;">
          ${renderReviewDecisionButtons(shot, isApproved, isRendering)}
          <div style="margin-top:6px;padding-top:8px;border-top:1px dashed var(--border);">
            <div class="fx fx-jc-sb fx-ac u-mb4">
              <span class="u-fs115 u-fw700 u-c-text">📝 Ghi Chú Đạo Diễn (Director Memo):</span>
              <span id="sdNoteSavedHint" style="font-size:10.5px;color:var(--green);display:none;font-weight:700;">✓ Đã lưu</span>
            </div>
            <input type="text" id="sdDirectorNoteInput" value="${esc(shot.director_note || (currentShotDeskProject ? localStorage.getItem('sd_note_' + currentShotDeskProject.id + '_' + shot.index) : '') || '')}" placeholder="Ghi nhận xét cho shot này (vd: Mặt chuẩn, background đẹp...)" oninput="saveDirectorNote(${shot.index}, this.value)" style="width:100%;font-size:11.5px;padding:5px 8px;background:var(--bg1);border:1px solid var(--border);border-radius:6px;color:var(--text);">
          </div>
        </div>
      </div>

      <div class="sd-review-scroll-panel">
        <div style="background:var(--bg0);border:1px solid var(--border);border-radius:8px;padding:12px;">
          <div style="font-weight:700;font-size:12.5px;color:var(--muted);margin-bottom:8px;text-transform:uppercase;">
            📸 Ảnh Tham Chiếu Khung Hình (Reference Frames)
          </div>
          <div style="display:grid;grid-template-columns:repeat(3, 1fr);gap:8px;">
            <div>
              <div style="font-size:10.5px;color:var(--muted);text-align:center;margin-bottom:3px;">First Frame</div>
              ${shot.first_frame_url ? `<img loading="lazy" decoding="async" src="${shot.first_frame_url}" style="width:100%;aspect-ratio:9/16;object-fit:cover;border-radius:4px;border:1px solid var(--border);cursor:pointer;" onclick="openShotDeskImageModal(this.src, 'First Frame')" title="Bấm xem lớn">` : '<div class="u-fs10 u-c-muted u-ta-c">None</div>'}
            </div>
            <div>
              <div style="font-size:10.5px;color:var(--muted);text-align:center;margin-bottom:3px;">Middle Frame</div>
              ${shot.middle_frame_url ? `<img loading="lazy" decoding="async" src="${shot.middle_frame_url}" style="width:100%;aspect-ratio:9/16;object-fit:cover;border-radius:4px;border:1px solid var(--border);cursor:pointer;" onclick="openShotDeskImageModal(this.src, 'Middle Frame')" title="Bấm xem lớn">` : '<div class="u-fs10 u-c-muted u-ta-c">Không dùng</div>'}
            </div>
            <div>
              <div style="font-size:10.5px;color:var(--muted);text-align:center;margin-bottom:3px;">Last Frame</div>
              ${shot.last_frame_url ? `<img loading="lazy" decoding="async" src="${shot.last_frame_url}" style="width:100%;aspect-ratio:9/16;object-fit:cover;border-radius:4px;border:1px solid var(--border);cursor:pointer;" onclick="openShotDeskImageModal(this.src, 'Last Frame')" title="Bấm xem lớn">` : '<div class="u-fs10 u-c-muted u-ta-c">None</div>'}
            </div>
          </div>
        </div>

        ${charBlocksHtml ? `
          <div style="background:var(--bg0);border:1px solid var(--border);border-radius:8px;padding:12px;">
            <div style="font-weight:700;font-size:12.5px;color:var(--muted);margin-bottom:8px;text-transform:uppercase;">
              👤 Kiểm Tra Độ Nhất Quán Nhân Vật (Character Bible Block)
            </div>
            ${charBlocksHtml}
          </div>
        ` : ''}

        <div style="background:var(--bg0);border:1px solid var(--border);border-radius:8px;padding:12px;font-size:12px;">
          <div style="font-weight:700;font-size:12.5px;color:var(--muted);margin-bottom:6px;text-transform:uppercase;">
            📝 Prompt Chi Tiết
          </div>
          <div style="color:var(--text);font-family:monospace;line-height:1.5;background:rgba(255,255,255,0.02);padding:8px;border-radius:4px;">
            ${esc(shot.used_prompt || shot.prompt)}
          </div>
          ${shot.compliance_prompt ? `
            <div class="u-mt6 u-fs11 u-c-yellow">
              <b>Compliance:</b> ${esc(shot.compliance_prompt)}
            </div>
          ` : ''}
          ${shot.voiceover ? `
            <div style="margin-top:6px;font-size:12px;color:#a5b4fc;">
              <b>Lời thoại:</b> "${esc(shot.voiceover)}"
            </div>
          ` : ''}
        </div>

        <div style="background:var(--bg0);border:1px solid var(--border);border-radius:8px;padding:12px;">
          <div style="font-weight:700;font-size:12.5px;color:var(--muted);margin-bottom:8px;text-transform:uppercase;">
            🔍 Danh Mục Kiểm Định Tiêu Chuẩn (Audit Checklist)
          </div>
          <div class="fx fx-col fx-g6 u-fs12">
            <label class="fx fx-ac fx-g8 u-mb0">
              <input class="u-w-auto" type="checkbox" checked disabled>
              <span>Nhất quán nhân vật: Khớp khuôn mặt, trang phục trong Character Block</span>
            </label>
            <label class="fx fx-ac fx-g8 u-mb0">
              <input class="u-w-auto" type="checkbox" checked disabled>
              <span>Tỉ lệ khung hình: Chuẩn ${esc(p.aspect_ratio || '9:16')}</span>
            </label>
            <label class="fx fx-ac fx-g8 u-mb0">
              <input class="u-w-auto" type="checkbox" checked disabled>
              <span>Watermark: Sạch 100%, không dính text méo hay logo rác</span>
            </label>
            <label class="fx fx-ac fx-g8 u-mb0">
              <input class="u-w-auto" type="checkbox" checked disabled>
              <span>Chuyển cảnh (Continuity): ${esc(shot.transition_note || 'Match cut')}</span>
            </label>
          </div>
        </div>

      </div>
    </div>
  `;

  // Bind Review Audio Visualizer events
  const rAudio = document.getElementById('sdReviewDeskAudioEl');
  const rWidget = document.getElementById('sdReviewWaveWidget');
  if (rAudio && rWidget) {
    rAudio.onplay = () => {
      rWidget.classList.add('is-playing');
      const icon = rWidget.querySelector('.sd-wave-icon');
      if (icon) icon.textContent = '⏸';
    };
    rAudio.onpause = () => {
      rWidget.classList.remove('is-playing');
      const icon = rWidget.querySelector('.sd-wave-icon');
      if (icon) icon.textContent = '▶';
    };
    rAudio.onended = () => {
      rWidget.classList.remove('is-playing');
      const icon = rWidget.querySelector('.sd-wave-icon');
      if (icon) icon.textContent = '▶';
    };
  }
}

window.isShotDeskAbMode = false;
let isAbDragging = false;

function toggleShotDeskAbMode() {
  window.isShotDeskAbMode = !window.isShotDeskAbMode;
  renderShotDeskReviewScreen();
}

function startAbDrag(e) {
  isAbDragging = true;
  handleAbDrag(e);
}

function stopAbDrag() {
  isAbDragging = false;
}

function handleAbDrag(e) {
  if (!isAbDragging) return;
  const container = document.getElementById('sdAbCompareContainer');
  if (!container) return;
  const rect = container.getBoundingClientRect();
  const clientX = e.touches ? e.touches[0].clientX : e.clientX;
  let pct = ((clientX - rect.left) / rect.width) * 100;
  pct = Math.max(0, Math.min(100, pct));
  updateAbSplit(pct);
}

function updateAbSplit(pct) {
  const divider = document.getElementById('sdAbDivider');
  const overlay = document.getElementById('sdAbOverlay');
  const slider = document.getElementById('sdAbRangeSlider');
  if (divider) divider.style.left = `${pct}%`;
  if (overlay) overlay.style.clipPath = `inset(0 0 0 ${pct}%)`;
  if (slider && slider.value != pct) slider.value = pct;
}

window.addEventListener('mousemove', (e) => {
  if (isAbDragging) handleAbDrag(e);
});
window.addEventListener('mouseup', () => {
  if (isAbDragging) stopAbDrag();
});
window.addEventListener('touchmove', (e) => {
  if (isAbDragging) handleAbDrag(e);
}, { passive: false });
window.addEventListener('touchend', () => {
  if (isAbDragging) stopAbDrag();
});

function playReviewDeskAudio(audioUrl, widgetEl) {
  const audio = document.getElementById('sdReviewDeskAudioEl');
  if (!audio) return;
  if (!audio.paused) {
    audio.pause();
    if (widgetEl) {
      widgetEl.classList.remove('is-playing');
      const icon = widgetEl.querySelector('.sd-wave-icon');
      if (icon) icon.textContent = '▶';
    }
  } else {
    audio.play();
    if (widgetEl) {
      widgetEl.classList.add('is-playing');
      const icon = widgetEl.querySelector('.sd-wave-icon');
      if (icon) icon.textContent = '⏸';
    }
  }
}

function stepReviewShot(delta) {
  if (!currentShotDeskProject || !currentShotDeskProject.shots) return;
  const count = currentShotDeskProject.shots.length;
  let target = currentReviewShotIndex + delta;
  if (target < 1) target = 1;
  if (target > count) target = count;
  currentReviewShotIndex = target;
  renderShotDeskReviewScreen();
}

function openReviewDeskForShot(shotIndex) {
  currentReviewShotIndex = shotIndex;
  switchShotDeskSubtab('review');
}

function renderShotDeskTimelineScreen() {
  const container = document.getElementById('sdVisualTimelineTracks');
  if (!container) return;

  if (!currentShotDeskProject || !currentShotDeskProject.shots || currentShotDeskProject.shots.length === 0) {
    container.innerHTML = `<div style="text-align:center;padding:30px;color:var(--muted);">Chưa có shot nào để hiển thị timeline.</div>`;
    return;
  }

  const p = currentShotDeskProject;
  const shots = p.shots;
  const approvedShots = shots.filter(s => s.status === 'approved' || s.status === 'done');

  let videoTrackHtml = '';
  shots.forEach((s, idx) => {
    const isApproved = s.status === 'approved' || s.status === 'done';
    const thumbUrl = s.first_frame_url || s.last_frame_url || '';
    videoTrackHtml += `
      <div class="sd-clip-block" onclick="openReviewDeskForShot(${s.index})" style="${isApproved ? 'border-color:var(--green);background:rgba(52,211,153,0.08);' : 'opacity:0.65;'}cursor:pointer;" title="Bấm để mở Review Desk cho Shot ${s.index}">
        <div class="fx fx-ac fx-g8">
          ${thumbUrl ? `<img loading="lazy" decoding="async" src="${thumbUrl}" style="width:34px;height:48px;object-fit:cover;border-radius:4px;border:1px solid rgba(255,255,255,0.15);flex-shrink:0;">` : ''}
          <div style="overflow:hidden;flex:1;">
            <div class="u-fw700 fx fx-jc-sb fx-ac">
              <span>Shot ${s.index}</span>
              <span style="font-size:10px;color:${isApproved ? 'var(--green)' : 'var(--muted)'};">${isApproved ? '✓ Approved' : s.status}</span>
            </div>
            <div style="font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${s.duration}s • ${esc(s.title || '')}</div>
          </div>
        </div>
      </div>
    `;

    if (idx < shots.length - 1) {
      const trans = s.transition_note || (s.transition_out || 'MATCH CUT');
      videoTrackHtml += `<div class="sd-transition-pill">${esc(trans)}</div>`;
    }
  });

  let voiceTrackHtml = '';
  shots.forEach((s, idx) => {
    const hasVoice = !!s.voiceover;
    voiceTrackHtml += `
      <div style="background:rgba(99,102,241,0.1);border:1px solid rgba(99,102,241,0.3);border-radius:6px;padding:6px 10px;min-width:130px;font-size:11.5px;">
        <div style="font-weight:600;color:#a5b4fc;">🎙️ Thoại Shot ${s.index}</div>
        <div style="font-size:10.5px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:120px;">
          ${hasVoice ? `"${esc(s.voiceover)}"` : '(Không thoại)'}
        </div>
      </div>
    `;
    if (idx < shots.length - 1) {
      voiceTrackHtml += `<div class="sd-sound-bridge-bar">Sound Bridge: 0.75s</div>`;
    }
  });

  container.innerHTML = `
    <div class="sd-track">
      <div class="sd-track-label">🎬 VIDEO TRACK</div>
      <div class="sd-track-content">${videoTrackHtml}</div>
    </div>

    <div class="sd-track">
      <div class="sd-track-label">🎙️ THOẠI & BRIDGE</div>
      <div class="sd-track-content">${voiceTrackHtml}</div>
    </div>

    <div class="sd-track">
      <div class="sd-track-label">🎵 NHẠC NỀN (BGM)</div>
      <div class="sd-track-content">
        <div style="background:rgba(251,191,36,0.1);border:1px solid rgba(251,191,36,0.3);border-radius:6px;padding:6px 14px;color:var(--yellow);font-size:12px;flex:1;">
          Fade In 1.5s ➔ Tự động khớp toàn bộ thời lượng phim ➔ Fade Out 1.5s cuối phim
        </div>
      </div>
    </div>

    <div class="sd-track">
      <div class="sd-track-label">📝 PHỤ ĐỀ (SUB)</div>
      <div class="sd-track-content">
        <div style="background:rgba(52,211,153,0.1);border:1px solid rgba(52,211,153,0.3);border-radius:6px;padding:6px 14px;color:var(--green);font-size:12px;flex:1;">
          Đồng bộ chuẩn mili-giây từng câu thoại • Viền đen chống lóa • Font Roboto Semibold
        </div>
      </div>
    </div>
  `;

  const composeBtn = document.getElementById('sdBtnComposeTimeline');
  if (composeBtn) {
    if (approvedShots.length === 0) {
      composeBtn.disabled = true;
      composeBtn.textContent = '⚠️ Cần ít nhất 1 Shot Approved để dựng phim';
    } else {
      composeBtn.disabled = false;
      composeBtn.textContent = `🎬 Dựng Phim Hoàn Chỉnh (${approvedShots.length}/${shots.length} shots)`;
    }
  }

  // Update Hollywood Timecode Box
  const totalApprovedSec = approvedShots.reduce((acc, s) => acc + (parseFloat(s.duration) || 0), 0);
  const tcVal = formatSecondsToSMPTETimecode(totalApprovedSec);
  const tcEl = document.getElementById('sdTimelineTcValue');
  if (tcEl) tcEl.textContent = tcVal;

  if (p.final_video) {
    displayShotDeskFinalMovie(p.final_video, p.srt_file, p.total_duration, p.final_dir);
  }
}

function populateShotDeskMusicOptions() {
  const sel = document.getElementById('sdBgMusicSelect');
  if (!sel) return;

  const currentVal = sel.value;
  sel.innerHTML = '<option value="">(Không dùng nhạc nền)</option>';

  apiFetch('/api/music/list')
    .then(r => r.json())
    .then(list => {
      list.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m.path;
        opt.textContent = m.name;
        if (m.path === currentVal) opt.selected = true;
        sel.appendChild(opt);
      });
    })
    .catch(() => {});
}

let sdComposePollTimer = null;

async function executeShotDeskTimelineCompose() {
  if (!currentShotDeskProject) return;

  const btn = document.getElementById('sdBtnComposeTimeline');
  const allowPartial = document.getElementById('sdAllowPartialExport')?.checked || false;
  const quality = document.getElementById('sdExportQuality')?.value || '720p';
  const subtitleMode = document.getElementById('sdSubtitleMode')?.value || 'burn_in';
  const soundBridge = parseFloat(document.getElementById('sdSoundBridgeDur')?.value || '0.75');
  const bgm = document.getElementById('sdBgMusicSelect')?.value || null;
  const musicVol = parseFloat(document.getElementById('sdMusicVol')?.value || '0.25');
  const voiceVol = parseFloat(document.getElementById('sdVoiceVol')?.value || '1.0');

  const progContainer = document.getElementById('sdComposeProgressContainer');
  const progBar = document.getElementById('sdComposeProgressBar');
  const progPct = document.getElementById('sdComposePercentText');
  const progStep = document.getElementById('sdComposeStepText');

  if (progContainer) {
    progContainer.style.display = 'block';
    if (progBar) progBar.style.width = '0%';
    if (progPct) progPct.textContent = '0%';
    if (progStep) progStep.textContent = '🚀 Bắt đầu khởi tạo tiến trình dựng phim...';
  }

  if (btn) {
    btn.disabled = true;
    btn.textContent = '⏳ Đang dựng phim timeline & hòa âm...';
  }

  try {
    const res = await apiFetch(`/api/shot-desk/projects/${encodeURIComponent(currentShotDeskProject.id)}/compose`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        allow_partial: allowPartial,
        bg_music_path: bgm,
        music_volume: musicVol,
        voice_volume: voiceVol,
        subtitle_mode: subtitleMode,
        quality: quality,
        sound_bridge_duration: soundBridge,
      })
    });

    const initData = await res.json();
    if (!res.ok || initData.error) {
      throw new Error(initData.error || 'Lỗi khi kích hoạt dựng phim');
    }

    // Bắt đầu poll tiến độ % dựng phim thời gian thực
    await pollComposeProgress(currentShotDeskProject.id);

  } catch (err) {
    alert('Lỗi dựng phim: ' + err.message);
    if (progContainer) progContainer.style.display = 'none';
    if (btn) {
      btn.disabled = false;
      btn.textContent = '🎬 Bắt Đầu Dựng Phim Timeline';
    }
  }
}

async function pollComposeProgress(projectId) {
  if (document.hidden) { window.__sdComposePaused = true; return; } // tab an: dung poll compose, ui.js resume khi mo lai
  if (sdComposePollTimer) clearTimeout(sdComposePollTimer);

  const btn = document.getElementById('sdBtnComposeTimeline');
  const progContainer = document.getElementById('sdComposeProgressContainer');
  const progBar = document.getElementById('sdComposeProgressBar');
  const progPct = document.getElementById('sdComposePercentText');
  const progStep = document.getElementById('sdComposeStepText');

  try {
    const res = await apiFetch(`/api/shot-desk/projects/${encodeURIComponent(projectId)}/compose-status`);
    if (!res.ok) throw new Error('Không thể lấy tiến độ dựng phim');
    const data = await res.json();

    const pct = Math.max(0, Math.min(100, Math.round(data.progress || 0)));
    if (progBar) progBar.style.width = pct + '%';
    if (progPct) progPct.textContent = pct + '%';
    if (progStep && data.step) progStep.textContent = data.step;

    if (data.status === 'done') {
      if (progStep) progStep.textContent = '🎉 Dựng phim hoàn tất 100%!';
      const resData = data.result || {};
      displayShotDeskFinalMovie(resData.final_video, resData.srt_file, resData.duration, data.final_dir);
      await onSelectShotDeskProject(projectId, false);
      if (btn) {
        btn.disabled = false;
        btn.textContent = '🎬 Bắt Đầu Dựng Phim Timeline';
      }
      return;
    } else if (data.status === 'error') {
      throw new Error(data.error || 'Lỗi xử lý FFmpeg khi dựng timeline');
    }

    // Tiếp tục poll mỗi 800ms
    sdComposePollTimer = setTimeout(() => pollComposeProgress(projectId), 800);

  } catch (e) {
    alert('Lỗi tiến trình dựng phim: ' + e.message);
    if (progContainer) progContainer.style.display = 'none';
    if (btn) {
      btn.disabled = false;
      btn.textContent = '🎬 Bắt Đầu Dựng Phim Timeline';
    }
  }
}

function displayShotDeskFinalMovie(videoUrl, srtUrl, duration, folderPath) {
  const container = document.getElementById('sdFinalMovieContainer');
  const player = document.getElementById('sdFinalVideoPlayer');
  const meta = document.getElementById('sdFinalVideoMeta');
  const mp4Btn = document.getElementById('sdDownloadMp4Btn');
  const srtBtn = document.getElementById('sdDownloadSrtBtn');
  const pathInput = document.getElementById('sdFinalFolderPath');

  if (!container || !player) return;

  container.style.display = 'block';
  player.src = videoUrl;

  const fDir = folderPath || currentShotDeskProject?.final_dir || '';
  if (pathInput && fDir) {
    pathInput.value = fDir;
  }

  if (meta) {
    meta.innerHTML = `
      <div><b>Dự án:</b> ${esc(currentShotDeskProject?.title || 'Phim')}</div>
      <div><b>Thời lượng:</b> ${duration || 0}s</div>
      <div><b>Tỉ lệ:</b> ${esc(currentShotDeskProject?.aspect_ratio || '9:16')}</div>
      <div class="u-c-green u-mt4">✓ Đã ghép video, hòa âm chuẩn 44.1kHz, phụ đề 1 tầng sát đáy.</div>
    `;
  }

  if (mp4Btn) mp4Btn.href = videoUrl;
  if (srtBtn && srtUrl) srtBtn.href = srtUrl;
}

async function openFinalFolderOnDisk() {
  if (!currentShotDeskProject) return;
  try {
    const res = await apiFetch(`/api/shot-desk/projects/${encodeURIComponent(currentShotDeskProject.id)}/open-folder`, {
      method: 'POST'
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Không thể mở thư mục');
  } catch (err) {
    alert('Không thể mở thư mục: ' + err.message);
  }
}

function copyFinalFolderPath() {
  const input = document.getElementById('sdFinalFolderPath');
  if (!input || !input.value) return;
  navigator.clipboard.writeText(input.value).then(() => {
    alert('📋 Đã sao chép đường dẫn thư mục vào Clipboard:\n' + input.value);
  }).catch(() => {
    prompt('Đường dẫn thư mục:', input.value);
  });
}

async function renderSingleShotDeskShot(shotIndex, useCompliance = false) {
  if (!currentShotDeskProject) return;

  try {
    const shot = currentShotDeskProject.shots.find(s => s.index === shotIndex);
    if (shot) {
      shot.status = 'rendering';
      shot.error = null;
      renderShotDeskUI(true);
    }

    const res = await apiFetch(`/api/shot-desk/projects/${encodeURIComponent(currentShotDeskProject.id)}/shots/${shotIndex}/render`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        use_compliance: useCompliance,
        background: true
      })
    });

    const data = await res.json();
    if (!res.ok || data.error) {
      throw new Error(data.error || 'Lỗi khi kích hoạt render');
    }

    startShotDeskPolling();
  } catch (err) {
    alert(`Lỗi render Shot ${shotIndex}: ` + err.message);
    const shot = currentShotDeskProject?.shots?.find(s => s.index === shotIndex);
    if (shot) {
      shot.status = 'ready';
      shot.error = err.message;
      renderShotDeskUI(true);
    }
  }
}

async function renderAllShotDeskShots() {
  if (!currentShotDeskProject) return;
  const concurrentVal = parseInt(document.getElementById('sdConcurrentSelect')?.value) || window.currentAccountsCount || 6;
  if (!confirm(`Bạn có muốn bắt đầu render toàn bộ các shot chưa hoàn thành của "${currentShotDeskProject.title}"?\n⚡ Hệ thống sẽ chia tải chạy song song qua ${concurrentVal} luồng (toàn bộ tài khoản Muse).`)) return;

  try {
    const res = await apiFetch(`/api/shot-desk/projects/${encodeURIComponent(currentShotDeskProject.id)}/render-all`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ concurrent: concurrentVal })
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Lỗi bắt đầu hàng đợi');

    currentShotDeskProject.shots.forEach(s => {
      if (s.status !== 'approved' && s.status !== 'done' && s.status !== 'review') {
        s.status = 'rendering';
      }
    });
    renderShotDeskUI(true);
    startShotDeskPolling();
  } catch (err) {
    alert('Lỗi: ' + err.message);
  }
}

function retryFailedShotDeskShots() {
  if (!currentShotDeskProject) return;
  const failed = currentShotDeskProject.shots.filter(s => s.error || s.status === 'ready' || s.status === 'rendering');
  if (failed.length === 0) {
    alert('Không có cảnh nào chưa hoàn thành hoặc bị lỗi!');
    return;
  }
  renderAllShotDeskShots();
}

async function resetStuckShotDeskShots() {
  if (!currentShotDeskProject) return;
  try {
    const res = await apiFetch(`/api/shot-desk/projects/${encodeURIComponent(currentShotDeskProject.id)}/reset-stuck`, {
      method: 'POST'
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Lỗi giải phóng cảnh kẹt');
    alert(data.message || `Đã giải phóng ${data.reset_count || 0} cảnh bị kẹt!`);
    await loadShotDeskProject(currentShotDeskProject.id);
  } catch (err) {
    alert('Lỗi: ' + err.message);
  }
}

async function approveShotDeskShot(shotIndex, advanceNext = false) {
  if (!currentShotDeskProject) return;

  try {
    const res = await apiFetch(`/api/shot-desk/projects/${encodeURIComponent(currentShotDeskProject.id)}/shots/${shotIndex}/approve`, {
      method: 'POST'
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Lỗi phê duyệt shot');

    const shot = currentShotDeskProject.shots.find(s => s.index === shotIndex);
    if (shot) shot.status = 'approved';

    if (advanceNext && currentReviewShotIndex < currentShotDeskProject.shots.length) {
      currentReviewShotIndex++;
    }

    renderShotDeskUI(true);
  } catch (err) {
    alert('Lỗi phê duyệt: ' + err.message);
  }
}

async function approveAllShotDeskShots() {
  if (!currentShotDeskProject) return;
  const shots = currentShotDeskProject.shots || [];
  const readyToApprove = shots.filter(s => (s.status === 'review' || !!s.video_url) && s.status !== 'approved' && s.status !== 'done');

  if (readyToApprove.length === 0) {
    alert('Hiện chưa có cảnh nào có video clip chờ duyệt!\n(Gợi ý: Cần bấm "Render Toàn Bộ" để sinh video trước khi duyệt)');
    return;
  }

  const indicesStr = readyToApprove.map(s => '#' + s.index).join(', ');
  if (!confirm(`Bạn có chắc muốn PHÊ DUYỆT TẤT CẢ ${readyToApprove.length} cảnh đã render xong?\n\nDanh sách: ${indicesStr}`)) {
    return;
  }

  try {
    const res = await apiFetch(`/api/shot-desk/projects/${encodeURIComponent(currentShotDeskProject.id)}/approve-all`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ only_with_video: true })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Lỗi phê duyệt');

    alert(`🎉 Đã phê duyệt thành công ${data.approved_count} cảnh!`);
    await loadShotDeskProject(currentShotDeskProject.id);
  } catch (err) {
    alert('Lỗi phê duyệt: ' + err.message);
  }
}

// ── Batch Multi-Shot Operations & Director Desk Controls ──

function onToggleShotSelect(shotIndex, isChecked) {
  if (isChecked) {
    selectedShotIndices.add(shotIndex);
  } else {
    selectedShotIndices.delete(shotIndex);
  }
  const cb1 = document.getElementById(`sdCheck-${shotIndex}`);
  const cb2 = document.getElementById(`sdDetCheck-${shotIndex}`);
  if (cb1) cb1.checked = isChecked;
  if (cb2) cb2.checked = isChecked;
  updateBatchActionBar();
}

function toggleSelectAllShots() {
  if (!currentShotDeskProject || !currentShotDeskProject.shots) return;
  const total = currentShotDeskProject.shots.length;
  const allSelected = selectedShotIndices.size === total && total > 0;
  if (allSelected) {
    selectedShotIndices.clear();
  } else {
    currentShotDeskProject.shots.forEach(s => selectedShotIndices.add(s.index));
  }
  currentShotDeskProject.shots.forEach(s => {
    const cb1 = document.getElementById(`sdCheck-${s.index}`);
    const cb2 = document.getElementById(`sdDetCheck-${s.index}`);
    const shouldCheck = !allSelected;
    if (cb1) cb1.checked = shouldCheck;
    if (cb2) cb2.checked = shouldCheck;
  });
  updateBatchActionBar();
}

function clearSelectedShots() {
  selectedShotIndices.clear();
  if (currentShotDeskProject && currentShotDeskProject.shots) {
    currentShotDeskProject.shots.forEach(s => {
      const cb1 = document.getElementById(`sdCheck-${s.index}`);
      const cb2 = document.getElementById(`sdDetCheck-${s.index}`);
      if (cb1) cb1.checked = false;
      if (cb2) cb2.checked = false;
    });
  }
  updateBatchActionBar();
}

function updateBatchActionBar() {
  const bar = document.getElementById('sdBatchActionBar');
  const countEl = document.getElementById('sdBatchCount');
  const selectAllBtn = document.getElementById('sdSelectAllBtn');
  const count = selectedShotIndices.size;
  if (countEl) countEl.textContent = count;
  if (bar) {
    if (count > 0) {
      bar.classList.add('is-visible');
    } else {
      bar.classList.remove('is-visible');
    }
  }
  if (selectAllBtn && currentShotDeskProject && currentShotDeskProject.shots) {
    const total = currentShotDeskProject.shots.length;
    if (count === total && total > 0) {
      selectAllBtn.textContent = '☒ Bỏ chọn';
      selectAllBtn.style.color = 'var(--accent)';
      selectAllBtn.style.borderColor = 'var(--accent)';
    } else {
      selectAllBtn.textContent = '☑️ Chọn tất cả';
      selectAllBtn.style.color = 'var(--muted)';
      selectAllBtn.style.borderColor = 'var(--border)';
    }
  }
}

async function renderSelectedShots() {
  if (!currentShotDeskProject || selectedShotIndices.size === 0) return;
  const indices = Array.from(selectedShotIndices);
  if (!confirm(`Bạn có muốn render đồng loạt ${indices.length} cảnh đã chọn?`)) return;

  for (const idx of indices) {
    const shot = currentShotDeskProject.shots.find(s => s.index === idx);
    if (shot) {
      shot.status = 'rendering';
      shot.error = null;
    }
  }
  renderShotDeskUI(true);

  for (const idx of indices) {
    try {
      await apiFetch(`/api/shot-desk/projects/${encodeURIComponent(currentShotDeskProject.id)}/shots/${idx}/render`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ background: true })
      });
    } catch (e) {
      console.warn(`Render error for shot ${idx}:`, e);
    }
  }
  startShotDeskPolling();
  clearSelectedShots();
}

async function approveSelectedShots() {
  if (!currentShotDeskProject || selectedShotIndices.size === 0) return;
  const indices = Array.from(selectedShotIndices);
  let approvedCount = 0;
  for (const idx of indices) {
    try {
      const res = await apiFetch(`/api/shot-desk/projects/${encodeURIComponent(currentShotDeskProject.id)}/shots/${idx}/approve`, {
        method: 'POST'
      });
      if (res.ok) {
        const shot = currentShotDeskProject.shots.find(s => s.index === idx);
        if (shot) shot.status = 'approved';
        approvedCount++;
      }
    } catch (e) {
      console.warn(`Approve error for shot ${idx}:`, e);
    }
  }
  renderShotDeskUI(true);
  clearSelectedShots();
  alert(`✅ Đã phê duyệt thành công ${approvedCount} cảnh!`);
}

async function autoHealSelectedShots() {
  if (!currentShotDeskProject || selectedShotIndices.size === 0) return;
  const indices = Array.from(selectedShotIndices);
  if (!confirm(`Bạn có muốn AI phân tích & sửa prompt cho ${indices.length} cảnh đã chọn?`)) return;
  for (const idx of indices) {
    try {
      await apiFetch(`/api/shot-desk/projects/${encodeURIComponent(currentShotDeskProject.id)}/shots/${idx}/auto-heal`, {
        method: 'POST'
      });
    } catch (e) {
      console.warn(`Auto-heal error for shot ${idx}:`, e);
    }
  }
  clearSelectedShots();
  await onSelectShotDeskProject(currentShotDeskProject.id, false);
}

let activeQuickAudioShotIdx = null;

function playQuickCardAudio(audioUrl, triggerEl, shotIndex) {
  if (!audioUrl) {
    alert('Shot này chưa có audio giọng đọc');
    return;
  }

  const stopActiveAudio = () => {
    if (activeQuickAudio) {
      activeQuickAudio.pause();
      if (activeQuickAudioShotIdx) {
        const prevWidget = document.getElementById(`sdWave-${activeQuickAudioShotIdx}`);
        const prevIcon = document.getElementById(`sdWaveIcon-${activeQuickAudioShotIdx}`);
        if (prevWidget) prevWidget.classList.remove('is-playing');
        if (prevIcon) prevIcon.textContent = '▶';
      }
      if (activeQuickAudioBtn && activeQuickAudioBtn.tagName === 'BUTTON') {
        activeQuickAudioBtn.innerHTML = '🔊 Nghe';
        activeQuickAudioBtn.style.color = '';
      }
      activeQuickAudio = null;
      activeQuickAudioBtn = null;
      activeQuickAudioShotIdx = null;
    }
  };

  if (activeQuickAudio && activeQuickAudio.src === audioUrl && !activeQuickAudio.ended) {
    stopActiveAudio();
    return;
  }

  stopActiveAudio();

  activeQuickAudio = new Audio(audioUrl);
  activeQuickAudioBtn = triggerEl;
  activeQuickAudioShotIdx = shotIndex;

  const currentWidget = shotIndex ? document.getElementById(`sdWave-${shotIndex}`) : null;
  const currentIcon = shotIndex ? document.getElementById(`sdWaveIcon-${shotIndex}`) : null;

  if (currentWidget) currentWidget.classList.add('is-playing');
  if (currentIcon) currentIcon.textContent = '⏹';
  if (triggerEl && triggerEl.tagName === 'BUTTON') {
    triggerEl.innerHTML = '⏹️ Dừng';
    triggerEl.style.color = 'var(--accent)';
  }

  activeQuickAudio.play().catch(e => {
    console.warn('Audio play error:', e);
    stopActiveAudio();
  });

  activeQuickAudio.onended = () => {
    stopActiveAudio();
  };
}

function setReviewVideoSpeed(speed, btnEl) {
  const vid = document.getElementById('sdReviewVideoPlayer');
  if (vid) vid.playbackRate = speed;
  const container = btnEl ? btnEl.parentElement : null;
  if (container) {
    container.querySelectorAll('.sd-speed-pill').forEach(p => p.classList.remove('is-active'));
    if (btnEl) btnEl.classList.add('is-active');
  }
}

function stepReviewVideoFrame(direction) {
  const vid = document.getElementById('sdReviewVideoPlayer');
  if (!vid) return;
  if (!vid.paused) vid.pause();
  const fps = 24;
  const delta = (1 / fps) * direction;
  vid.currentTime = Math.max(0, Math.min(vid.duration || 9999, vid.currentTime + delta));
}

function toggleReviewTheaterMode() {
  const playerWrap = document.getElementById('sdReviewDeskCard');
  if (!playerWrap) return;
  const isTheater = playerWrap.classList.toggle('sd-theater-mode');
  const btn = playerWrap.querySelector('button[title*="Theater"]');
  if (btn) btn.textContent = isTheater ? '🗗 Thu Gọn' : '⛶ Rạp Chiếu Phim';
}

function saveDirectorNote(shotIndex, noteText) {
  if (!currentShotDeskProject || !currentShotDeskProject.shots) return;
  const shot = currentShotDeskProject.shots.find(s => s.index === shotIndex);
  if (shot) {
    shot.director_note = noteText;
    try {
      localStorage.setItem(`sd_note_${currentShotDeskProject.id}_${shotIndex}`, noteText);
    } catch (e) {}
    const hint = document.getElementById('sdNoteSavedHint');
    if (hint) {
      hint.style.display = 'inline';
      clearTimeout(window._sdNoteTimer);
      window._sdNoteTimer = setTimeout(() => { hint.style.display = 'none'; }, 2000);
    }
  }
}

function formatSecondsToSMPTETimecode(totalSeconds, fps = 24) {
  const sec = Math.max(0, parseFloat(totalSeconds) || 0);
  const hrs = Math.floor(sec / 3600);
  const mins = Math.floor((sec % 3600) / 60);
  const secs = Math.floor(sec % 60);
  const frames = Math.floor((sec % 1) * fps);
  return `${String(hrs).padStart(2, '0')}:${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}:${String(frames).padStart(2, '0')}`;
}

let seqShotsList = [];
let seqCurrentIndex = 0;

function openTimelineSequentialPlayer() {
  if (!currentShotDeskProject || !currentShotDeskProject.shots || currentShotDeskProject.shots.length === 0) {
    alert('Dự án chưa có shot nào!');
    return;
  }
  seqShotsList = currentShotDeskProject.shots.filter(s => !!s.video_url);
  if (seqShotsList.length === 0) {
    alert('Chưa có shot nào được render hoàn thành video! Vui lòng render trước khi xem thử phim.');
    return;
  }
  seqCurrentIndex = 0;
  showSequentialPlayerModal();
}

function showSequentialPlayerModal() {
  const existing = document.getElementById('sdSequentialModal');
  if (existing) existing.remove();

  const modal = document.createElement('div');
  modal.id = 'sdSequentialModal';
  modal.className = 'modal-backdrop';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.92);backdrop-filter:blur(20px);z-index:100000;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px;';

  modal.innerHTML = `
    <div style="width:100%;max-width:960px;background:#0b0f19;border:1px solid rgba(99,102,241,0.4);box-shadow:0 24px 64px rgba(0,0,0,0.9);border-radius:14px;overflow:hidden;display:flex;flex-direction:column;">
      <div style="padding:14px 20px;background:rgba(255,255,255,0.03);border-bottom:1px solid rgba(255,255,255,0.1);display:flex;justify-content:space-between;align-items:center;">
        <div class="fx fx-ac fx-g12">
          <span class="u-fs16">🎬</span>
          <div>
            <div style="font-weight:700;font-size:14px;color:#fff;">Xem Thử Toàn Bộ Phim (Rough-Cut Sequential Preview)</div>
            <div class="u-fs11 u-c-muted">${esc(currentShotDeskProject.title)} • <span id="sdSeqCounter">Shot 1/${seqShotsList.length}</span></div>
          </div>
        </div>
        <div class="fx fx-ac fx-g10">
          <button class="btn btn-ghost btn-sm u-fs14 u-c-muted u-p-4-10" onclick="closeSequentialPlayerModal()">✕ Đóng</button>
        </div>
      </div>
      <div style="position:relative;background:#000;display:flex;align-items:center;justify-content:center;min-height:420px;max-height:68vh;">
        <video id="sdSeqVideo" controls autoplay style="width:100%;max-height:68vh;object-fit:contain;"></video>
        <div id="sdSeqOverlayInfo" style="position:absolute;top:16px;left:16px;background:rgba(0,0,0,0.75);backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,0.15);border-radius:6px;padding:6px 12px;font-size:12px;color:#fff;pointer-events:none;">
          <span class="u-fw700 u-c-accent" id="sdSeqShotBadge">Shot 1</span>: <span id="sdSeqShotTitle">Tiêu đề</span>
        </div>
      </div>
      <div style="padding:12px 20px;background:rgba(255,255,255,0.02);border-top:1px solid rgba(255,255,255,0.08);display:flex;justify-content:space-between;align-items:center;">
        <div class="fx fx-g8">
          <button class="btn btn-outline btn-sm" onclick="stepSeqShot(-1)">⏮️ Shot Trước</button>
          <button class="btn btn-outline btn-sm" onclick="stepSeqShot(1)">Shot Tiếp Theo ⏭️</button>
        </div>
        <div class="u-fs115 u-c-muted">Tự động chuyển tiếp cảnh kế tiếp khi kết thúc clip</div>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
  loadSeqShot(0);
}

function loadSeqShot(idx) {
  if (idx < 0 || idx >= seqShotsList.length) return;
  seqCurrentIndex = idx;
  const s = seqShotsList[idx];
  const video = document.getElementById('sdSeqVideo');
  const counter = document.getElementById('sdSeqCounter');
  const badge = document.getElementById('sdSeqShotBadge');
  const title = document.getElementById('sdSeqShotTitle');

  if (counter) counter.textContent = `Shot ${idx + 1}/${seqShotsList.length}`;
  if (badge) badge.textContent = `Shot ${s.index}`;
  if (title) title.textContent = s.title || '';

  if (video) {
    video.src = s.video_url;
    video.play().catch(e => console.warn(e));
    video.onended = () => {
      if (seqCurrentIndex < seqShotsList.length - 1) {
        loadSeqShot(seqCurrentIndex + 1);
      }
    };
  }
}

function stepSeqShot(direction) {
  const target = seqCurrentIndex + direction;
  if (target >= 0 && target < seqShotsList.length) {
    loadSeqShot(target);
  }
}

function closeSequentialPlayerModal() {
  const modal = document.getElementById('sdSequentialModal');
  if (modal) {
    const vid = modal.querySelector('video');
    if (vid) vid.pause();
    modal.remove();
  }
}

let isShotDeskPollFetching = false;

function checkAutoStartShotDeskPolling() {
  if (!currentShotDeskProject) return;
  const isRendering = (currentShotDeskProject.shots || []).some(s => s.status === 'rendering');
  if (isRendering) startShotDeskPolling();
  else stopShotDeskPolling();
}

function startShotDeskPolling() {
  if (shotDeskPollTimer) return;
  scheduleShotDeskPoll(1500);
}

function stopShotDeskPolling() {
  if (shotDeskPollTimer) {
    clearTimeout(shotDeskPollTimer);
    shotDeskPollTimer = null;
  }
  isShotDeskPollFetching = false;
}

function scheduleShotDeskPoll(delayMs = 4000) {
  if (document.hidden) { window.__sdPollPaused = true; return; } // tab an: dung poll, ui.js resume khi mo lai
  if (shotDeskPollTimer) clearTimeout(shotDeskPollTimer);
  shotDeskPollTimer = setTimeout(async () => {
    shotDeskPollTimer = null;
    await executeShotDeskPoll();
  }, delayMs);
}

async function executeShotDeskPoll() {
  if (!currentShotDeskProject) {
    stopShotDeskPolling();
    return;
  }
  if (isShotDeskPollFetching) return;
  isShotDeskPollFetching = true;

  try {
    // Dùng endpoint /status siêu nhẹ để không gây áp lực mạng và không làm lag video
    const res = await apiFetch(`/api/shot-desk/projects/${encodeURIComponent(currentShotDeskProject.id)}/status`);
    if (res.ok) {
      const data = await res.json();
      let hasChange = false;

      if (data.shots && currentShotDeskProject.shots) {
        data.shots.forEach(st => {
          const s = currentShotDeskProject.shots.find(x => x.index === st.index);
          if (s) {
            if (s.status !== st.status || s.video_url !== st.video_url || s.error !== st.error || s.audio_url !== st.audio_url) {
              s.status = st.status;
              s.video_url = st.video_url;
              s.audio_url = st.audio_url;
              s.error = st.error;
              if (st.audit) s.audit = st.audit;
              hasChange = true;
            }
          }
        });
      }

      // Chỉ cập nhật DOM nếu thực sự có shot thay đổi trạng thái
      if (hasChange) {
        renderShotDeskUI(true);
      }

      const stillRendering = data.is_rendering || (currentShotDeskProject.shots || []).some(s => s.status === 'rendering');
      if (stillRendering) {
        scheduleShotDeskPoll(4000);
      } else {
        stopShotDeskPolling();
        // Khi tất cả hoàn tất, đồng bộ toàn bộ project 1 lần để có đầy đủ dữ liệu
        try {
          const fullRes = await apiFetch(`/api/shot-desk/projects/${encodeURIComponent(currentShotDeskProject.id)}`);
          if (fullRes.ok) {
            currentShotDeskProject = await fullRes.json();
            renderShotDeskUI(true);
          }
        } catch (e) {}
      }
    } else {
      // Fallback nếu có lỗi mạng
      scheduleShotDeskPoll(5000);
    }
  } catch (e) {
    console.warn('[ShotDesk Poll] Lỗi polling:', e);
    scheduleShotDeskPoll(6000);
  } finally {
    isShotDeskPollFetching = false;
  }
}

function openShotDeskImageModal(src, title) {
  const modal = document.createElement('div');
  modal.className = 'modal-backdrop';
  modal.innerHTML = `
    <div class="modal-window" style="max-width:520px;background:#000;">
      <div class="modal-header" style="border-color:#222;">
        <div class="u-fw700 u-c-text">${esc(title || 'Xem Ảnh Tham Chiếu')}</div>
        <button class="btn btn-ghost btn-sm" onclick="this.closest('.modal-backdrop').remove()">✕</button>
      </div>
      <div class="modal-body" style="padding:10px;display:flex;justify-content:center;">
        <img loading="lazy" decoding="async" src="${src}" style="max-width:100%;max-height:80vh;object-fit:contain;border-radius:6px;">
      </div>
    </div>
  `;
  document.body.appendChild(modal);
}

function openCharacterBlockModal(name, block) {
  const modal = document.createElement('div');
  modal.className = 'modal-backdrop';
  modal.innerHTML = `
    <div class="modal-window" style="max-width:640px;">
      <div class="modal-header">
        <div class="u-fw700 u-fs15 u-c-accent">👤 Character Block: ${esc(name)}</div>
        <button class="btn btn-ghost btn-sm" onclick="this.closest('.modal-backdrop').remove()">✕</button>
      </div>
      <div class="modal-body">
        <div class="u-fs12 u-c-muted u-mb8">
          Character Block này được nạp trực tiếp vào prompt của mọi cảnh có nhân vật này xuất hiện để đảm bảo diện mạo và trang phục nhất quán 100%:
        </div>
        <div style="background:#090d16;border:1px solid var(--border);border-radius:6px;padding:12px;font-family:monospace;font-size:12px;color:#818cf8;white-space:pre-wrap;line-height:1.5;">
          ${esc(block)}
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-primary" onclick="copyPromptSheetContent('${esc(block)}', this)">📋 Sao Chép Block</button>
        <button class="btn btn-ghost" onclick="this.closest('.modal-backdrop').remove()">Đóng</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
}

function openShotDeskEditModal(shotIndex) {
  if (!currentShotDeskProject) return;
  const shot = currentShotDeskProject.shots.find(s => s.index === shotIndex);
  if (!shot) return;

  const p = currentShotDeskProject;
  const voiceMode = shot.voice_mode || 'narration';
  const speaker = shot.speaker || (shot.characters && shot.characters[0] ? shot.characters[0] : '');
  const voice = shot.voice || p.default_voice || 'vi-VN-HoaiMyNeural';
  const lipSync = shot.lip_sync !== undefined ? shot.lip_sync : (voiceMode === 'dialogue');

  const chars = p.characters || [];
  let charOptions = `<option value="">-- Chọn nhân vật phát ngôn --</option>`;
  charOptions += `<option value="Narrator" ${speaker === 'Narrator' || !speaker ? 'selected' : ''}>🎙️ Thuyết minh viên (Narrator)</option>`;
  chars.forEach(c => {
    charOptions += `<option value="${esc(c.name)}" ${speaker === c.name ? 'selected' : ''}>👤 ${esc(c.name)}</option>`;
  });

  const modal = document.createElement('div');
  modal.className = 'modal-backdrop';
  modal.id = 'sdEditModalEl';
  modal.innerHTML = `
    <div class="modal-window" style="max-width:680px;">
      <div class="modal-header">
        <div class="u-fw700 u-fs15">✏️ Chỉnh Sửa Shot ${shot.index}: ${esc(shot.title || '')}</div>
        <button class="btn btn-ghost btn-sm" onclick="this.closest('.modal-backdrop').remove()">✕</button>
      </div>
      <div class="modal-body">
        <div class="form-row">
          <div class="fx fx-jc-sb fx-ac u-mb4">
            <label class="u-mb0">Prompt Mô Tả Cảnh (English Cinematic Prompt)</label>
            <button type="button" class="btn btn-warning btn-sm u-p-2-8 u-fs11 u-fw700" onclick="quickCleanModalPrompt()" title="Tự động loại bỏ từ khóa bạo lực/bắt giữ nhạy cảm và chuyển thành ngôn ngữ điện ảnh an toàn">
              ⚡ AI Làm Sạch Prompt Này
            </button>
          </div>
          <textarea id="sdEditPromptInput" rows="3" style="font-family:monospace;font-size:12px;">${esc(shot.prompt || '')}</textarea>
        </div>
        <div class="form-row">
          <label>Compliance Prompt Dự Phòng (Safe Alternative Prompt)</label>
          <textarea id="sdEditComplianceInput" rows="2" style="font-family:monospace;font-size:12px;">${esc(shot.compliance_prompt || '')}</textarea>
        </div>
        <div class="form-row cols-2">
          <div>
            <label>Lời Thoại Tiếng Việt (Voiceover / Lời thoại)</label>
            <input type="text" id="sdEditVoiceoverInput" value="${esc(shot.voiceover || '')}" placeholder="Nhập câu thoại hoặc lời kể...">
          </div>
          <div>
            <label>Thời lượng (giây)</label>
            <input type="number" id="sdEditDurationInput" min="3" max="20" value="${shot.duration || 10}">
          </div>
        </div>

        <!-- Cấu hình Giọng đọc & Khớp môi (Voice & Lip-Sync) -->
        <div class="form-row" style="background:var(--bg0);border:1px solid var(--border);border-radius:8px;padding:12px;margin:12px 0;">
          <div class="u-fw700 u-fs12 u-c-accent u-mb10 fx fx-jc-sb fx-ac">
            <span>🎙️ CẤU HÌNH GIỌNG ĐỌC & KHỚP MÔI (VOICE & LIP-SYNC)</span>
            <span style="font-size:11px;color:var(--muted);font-weight:normal;">Edge-TTS / Native AI Audio</span>
          </div>
          <div class="gd gd-2 fx-g10 u-mb10">
            <div>
              <label style="font-size:11px;margin-bottom:4px;display:block;">Chế độ phát giọng đọc</label>
              <select class="u-w100" id="sdEditVoiceModeInput" onchange="onShotVoiceModeChanged()">
                <option value="narration" ${voiceMode === 'narration' ? 'selected' : ''}>🎙️ Thuyết minh dẫn chuyện (Lồng tiếng TTS)</option>
                <option value="dialogue" ${voiceMode === 'dialogue' ? 'selected' : ''}>🗣️ Nhân vật tự nói (Lồng tiếng TTS + Khớp môi)</option>
                <option value="ai_voice" ${voiceMode === 'ai_voice' ? 'selected' : ''}>🤖 Không lồng tiếng (Dùng giọng & âm thanh gốc của AI)</option>
              </select>
            </div>
            <div>
              <label style="font-size:11px;margin-bottom:4px;display:block;">Nhân vật phát ngôn (Speaker)</label>
              <select class="u-w100" id="sdEditSpeakerInput" onchange="onShotSpeakerChanged()">
                ${charOptions}
              </select>
            </div>
          </div>

          <div class="gd gd-2 fx-g10 fx-ac">
            <div id="sdEditVoiceGroup">
              <div class="fx fx-jc-sb fx-ac u-mb4">
                <label class="u-fs11 u-mb0">Giọng đọc (Voice)</label>
                <button type="button" class="btn btn-ghost btn-sm" style="font-size:10.5px;padding:1px 5px;color:var(--accent);" onclick="testPlayVoice(document.getElementById('sdEditVoiceInput').value, document.getElementById('sdEditVoiceoverInput').value)">🔊 Nghe thử</button>
              </div>
              <select class="u-w100" id="sdEditVoiceInput">
                ${voiceSelectHtml(voice)}
              </select>
            </div>
            <div style="padding-top:16px;">
              <label style="display:flex;align-items:center;gap:8px;cursor:pointer;margin-bottom:0;font-size:12px;">
                <input type="checkbox" id="sdEditLipSyncInput" ${lipSync ? 'checked' : ''} style="width:16px;height:16px;accent-color:var(--accent);">
                <span class="u-fw600 u-c-green">👄 Tự động khớp môi (AI Lip-Sync)</span>
              </label>
              <div class="u-fs10 u-c-muted u-mt2">Điều khiển khẩu hình chuyển động nhịp nhàng theo câu thoại</div>
            </div>
          </div>

          <div id="sdEditVoiceNote" style="font-size:11px;color:var(--muted);margin-top:10px;padding:6px 10px;background:rgba(255,255,255,0.03);border-radius:4px;border-left:3px solid var(--accent);display:none;"></div>
        </div>

        <div class="form-row">
          <label>Ghi Chú Chuyển Cảnh (Transition Note)</label>
          <input type="text" id="sdEditTransitionInput" value="${esc(shot.transition_note || '')}" placeholder="MATCH CUT to next scene">
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-ghost" onclick="this.closest('.modal-backdrop').remove()">Hủy</button>
        <button class="btn btn-primary" onclick="submitShotDeskEditSave(${shot.index}, false)">💾 Lưu Thay Đổi</button>
        <button class="btn btn-success" onclick="submitShotDeskEditSave(${shot.index}, true)">🚀 Lưu & Render Lại</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
  onShotVoiceModeChanged();
}

function onShotVoiceModeChanged() {
  const mode = document.getElementById('sdEditVoiceModeInput')?.value;
  const lipInput = document.getElementById('sdEditLipSyncInput');
  const voiceInput = document.getElementById('sdEditVoiceInput');
  const voiceNote = document.getElementById('sdEditVoiceNote');

  if (mode === 'ai_voice') {
    if (lipInput) {
      lipInput.checked = false;
      lipInput.disabled = true;
    }
    if (voiceInput) {
      voiceInput.disabled = true;
      voiceInput.style.opacity = '0.45';
    }
    if (voiceNote) {
      voiceNote.innerHTML = '<span class="u-c-yellow u-fw600">🤖 Giọng gốc của AI:</span> Hệ thống không lồng tiếng TTS đè lên; giữ nguyên 100% âm thanh, tiếng động và lời thoại tự sinh từ mô hình AI video.';
      voiceNote.style.display = 'block';
    }
  } else {
    if (voiceInput) {
      voiceInput.disabled = false;
      voiceInput.style.opacity = '1';
    }
    if (lipInput) {
      lipInput.disabled = false;
      lipInput.checked = (mode === 'dialogue');
    }
    if (voiceNote) {
      voiceNote.innerHTML = (mode === 'dialogue') ?
        '🗣️ <b class="u-c-green">Nhân vật tự nói:</b> Sinh giọng đọc tiếng Việt và điều khiển cơ miệng chuyển động khớp câu thoại.' :
        '🎙️ <b class="u-c-accent">Thuyết minh:</b> Giọng đọc người dẫn chuyện ngoài khung hình, nhân vật giữ phong thái tự nhiên.';
      voiceNote.style.display = 'block';
    }
  }
}

function onShotSpeakerChanged() {
  const speakerVal = document.getElementById('sdEditSpeakerInput')?.value;
  if (!speakerVal || !currentShotDeskProject) return;
  const charObj = (currentShotDeskProject.characters || []).find(c => c.name.toLowerCase() === speakerVal.toLowerCase());
  if (charObj && charObj.voice) {
    const vInput = document.getElementById('sdEditVoiceInput');
    if (vInput) vInput.value = charObj.voice;
  }
}

async function quickCleanModalPrompt() {
  const pInput = document.getElementById('sdEditPromptInput');
  if (!pInput || !pInput.value.trim()) return;
  try {
    showToast('⚡ Đang làm sạch prompt theo chuẩn điện ảnh an toàn...');
    const res = await apiFetch('/api/ai/fix-prompt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prompt: pInput.value,
        error: 'Safety filter violation: restraint, violence or physical interaction'
      })
    });
    const data = await res.json();
    if (data.fixed_prompt) {
      pInput.value = data.fixed_prompt;
      showToast('⚡ Đã làm sạch prompt thành công!');
    }
  } catch (err) {
    alert('Lỗi làm sạch prompt: ' + err.message);
  }
}

async function autoHealAndRetryShotDesk(shotIndex) {
  if (!currentShotDeskProject) return;
  try {
    showToast(`⚡ Đang phân tích & làm sạch prompt Shot ${shotIndex}...`);
    const res = await apiFetch(`/api/shot-desk/projects/${encodeURIComponent(currentShotDeskProject.id)}/shots/${shotIndex}/auto-heal`, {
      method: 'POST'
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Lỗi auto-heal');
    showToast(data.message || 'Đã sửa prompt và đang render lại!');
    await onSelectShotDeskProject(currentShotDeskProject.id, true);
  } catch (err) {
    alert('Lỗi auto-heal: ' + err.message);
  }
}

async function autoHealAllFailedShots() {
  if (!currentShotDeskProject) return;
  try {
    showToast('⚡ Đang tự động sửa prompt và render lại toàn bộ các shot lỗi...');
    const res = await apiFetch(`/api/shot-desk/projects/${encodeURIComponent(currentShotDeskProject.id)}/auto-heal-all`, {
      method: 'POST'
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Lỗi');
    showToast(data.message || 'Đang xử lý!');
    await onSelectShotDeskProject(currentShotDeskProject.id, true);
  } catch (err) {
    alert('Lỗi: ' + err.message);
  }
}

async function submitShotDeskEditSave(shotIndex, reRender = false) {
  const pInput = document.getElementById('sdEditPromptInput')?.value || '';
  const cInput = document.getElementById('sdEditComplianceInput')?.value || '';
  const vInput = document.getElementById('sdEditVoiceoverInput')?.value || '';
  const dInput = parseInt(document.getElementById('sdEditDurationInput')?.value) || 10;
  const tInput = document.getElementById('sdEditTransitionInput')?.value || '';
  const vmInput = document.getElementById('sdEditVoiceModeInput')?.value || 'narration';
  const spkInput = document.getElementById('sdEditSpeakerInput')?.value || '';
  const vcInput = document.getElementById('sdEditVoiceInput')?.value || 'vi-VN-HoaiMyNeural';
  const lsInput = !!document.getElementById('sdEditLipSyncInput')?.checked;

  try {
    const res = await apiFetch(`/api/shot-desk/projects/${encodeURIComponent(currentShotDeskProject.id)}/shots/${shotIndex}/edit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prompt: pInput,
        compliance_prompt: cInput,
        voiceover: vInput,
        duration: dInput,
        transition_note: tInput,
        reset_status: reRender,
        voice_mode: vmInput,
        speaker: spkInput,
        voice: vcInput,
        lip_sync: lsInput
      })
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Lỗi lưu thông tin');

    document.getElementById('sdEditModalEl')?.remove();
    await onSelectShotDeskProject(currentShotDeskProject.id, false);

    if (reRender) {
      renderSingleShotDeskShot(shotIndex, false);
    }
  } catch (err) {
    alert('Lỗi lưu chỉnh sửa: ' + err.message);
  }
}

function openShotDeskProjectVoiceModal() {
  if (!currentShotDeskProject) return;
  const p = currentShotDeskProject;
  const defaultVoice = p.default_voice || 'vi-VN-HoaiMyNeural';
  const defaultMode = p.default_voice_mode || 'narration';

  const modal = document.createElement('div');
  modal.className = 'modal-backdrop';
  modal.id = 'sdProjVoiceModalEl';
  modal.innerHTML = `
    <div class="modal-window" style="max-width:520px;">
      <div class="modal-header">
        <div class="u-fw700 u-fs15">🎙️ Cấu Hình Giọng Đọc & Khớp Môi Toàn Dự Án</div>
        <button class="btn btn-ghost btn-sm" onclick="this.closest('.modal-backdrop').remove()">✕</button>
      </div>
      <div class="modal-body">
        <div class="u-fs125 u-c-muted u-mb12">
          Thiết lập giọng đọc mặc định và phong cách thoại cho dự án <b>${esc(p.title || p.id)}</b>.
        </div>

        <div class="form-row" id="sdProjVoiceRow">
          <div class="fx fx-jc-sb fx-ac u-mb4">
            <label class="u-mb0">Giọng đọc mặc định (VieNeu / OmniVoice / Edge-TTS)</label>
            <button type="button" class="btn btn-ghost btn-sm u-fs11 u-p-1-6 u-c-accent" onclick="testPlayVoice(document.getElementById('sdProjDefaultVoice').value)">🔊 Nghe thử</button>
          </div>
          <select id="sdProjDefaultVoice">
            ${voiceSelectHtml(defaultVoice)}
          </select>
        </div>

        <div class="form-row">
          <label>Chế độ thoại mặc định</label>
          <select id="sdProjDefaultMode" onchange="onProjVoiceModeChanged()">
            <option value="narration" ${defaultMode === 'narration' ? 'selected' : ''}>🎙️ Thuyết minh dẫn chuyện (Lồng tiếng TTS)</option>
            <option value="dialogue" ${defaultMode === 'dialogue' ? 'selected' : ''}>🗣️ Nhân vật tự nói (Lồng tiếng TTS + Khớp môi)</option>
            <option value="ai_voice" ${defaultMode === 'ai_voice' ? 'selected' : ''}>🤖 Không lồng tiếng (Dùng giọng & âm thanh gốc của AI)</option>
          </select>
        </div>

        <div class="form-row" style="background:rgba(99,102,241,0.06);border:1px solid rgba(99,102,241,0.25);border-radius:6px;padding:10px 12px;margin-top:10px;">
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer;margin-bottom:0;font-size:12.5px;">
            <input type="checkbox" id="sdProjApplyAllShots" checked style="width:16px;height:16px;accent-color:var(--accent);">
            <span class="u-fw700 u-c-accent">Áp dụng ngay cho tất cả các Shot hiện có (${(p.shots || []).length} shots)</span>
          </label>
          <div class="u-fs11 u-c-muted u-mt4">
            Đồng bộ giọng đọc và chế độ khớp môi cho toàn bộ các shot trong dự án.
          </div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-ghost" onclick="this.closest('.modal-backdrop').remove()">Hủy</button>
        <button class="btn btn-primary" onclick="submitShotDeskProjectVoiceSave()">💾 Lưu Thiết Lập</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
  onProjVoiceModeChanged();
}

function onProjVoiceModeChanged() {
  const mode = document.getElementById('sdProjDefaultMode')?.value;
  const vSelect = document.getElementById('sdProjDefaultVoice');
  if (vSelect) {
    vSelect.disabled = (mode === 'ai_voice');
    vSelect.style.opacity = (mode === 'ai_voice') ? '0.45' : '1';
  }
}

async function submitShotDeskProjectVoiceSave() {
  if (!currentShotDeskProject) return;
  const defaultVoice = document.getElementById('sdProjDefaultVoice')?.value || 'vi-VN-HoaiMyNeural';
  const defaultMode = document.getElementById('sdProjDefaultMode')?.value || 'narration';
  const applyAll = !!document.getElementById('sdProjApplyAllShots')?.checked;

  try {
    showToast('💾 Đang cập nhật thiết lập giọng đọc dự án...');
    const res = await apiFetch(`/api/shot-desk/projects/${encodeURIComponent(currentShotDeskProject.id)}/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        default_voice: defaultVoice,
        default_voice_mode: defaultMode,
        apply_to_all_shots: applyAll
      })
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Lỗi lưu thiết lập');

    document.getElementById('sdProjVoiceModalEl')?.remove();
    showToast('✅ Đã cập nhật thành công thiết lập giọng đọc!');
    await onSelectShotDeskProject(currentShotDeskProject.id, false);
  } catch (err) {
    alert('Lỗi cập nhật: ' + err.message);
  }
}

// ── Persistence Floating Render HUD Logic ──
let lastRenderedShotIndex = null;
let hudHideTimeout = null;

function updateShotDeskRenderHUD(shots, renderingCnt, approvedCnt, reviewCnt) {
  const dock = document.getElementById('sdPersistentRenderDock');
  if (!dock) return;

  if (renderingCnt > 0) {
    if (hudHideTimeout) {
      clearTimeout(hudHideTimeout);
      hudHideTimeout = null;
    }
    dock.classList.add('is-visible');

    const total = shots.length;
    const completed = shots.filter(s => s.status === 'review' || s.status === 'approved' || s.status === 'done').length;
    const percent = total > 0 ? Math.round((completed / total) * 100) : 0;

    // Theo dõi shot vừa render xong gần nhất
    const newlyDone = shots.find(s => (s.status === 'review' || s.status === 'approved') && s.video_url);
    if (newlyDone) lastRenderedShotIndex = newlyDone.index;

    const titleEl = document.getElementById('sdHudTitle');
    const ratioEl = document.getElementById('sdHudRatio');
    const percentEl = document.getElementById('sdHudPercent');
    const barEl = document.getElementById('sdHudProgressBar');
    const detailsEl = document.getElementById('sdHudDetails');

    const remainingShots = total - completed;
    const activeThreads = Math.max(1, renderingCnt);
    const estimatedSec = Math.ceil((remainingShots * 35) / activeThreads);
    const etaMins = Math.floor(estimatedSec / 60);
    const etaSecs = estimatedSec % 60;
    const etaStr = etaMins > 0 ? `${etaMins}m ${String(etaSecs).padStart(2, '0')}s` : `${etaSecs}s`;

    if (titleEl) titleEl.textContent = `Đang Render Cảnh...`;
    if (ratioEl) ratioEl.textContent = `${completed}/${total}`;
    if (percentEl) percentEl.textContent = `${percent}%`;
    if (barEl) barEl.style.width = `${percent}%`;
    if (detailsEl) detailsEl.textContent = `⚡ ${renderingCnt} luồng • ETA ~${etaStr}`;
  } else {
    // Khi vừa render xong
    if (dock.classList.contains('is-visible')) {
      const titleEl = document.getElementById('sdHudTitle');
      const percentEl = document.getElementById('sdHudPercent');
      const barEl = document.getElementById('sdHudProgressBar');
      const detailsEl = document.getElementById('sdHudDetails');
      if (titleEl) titleEl.textContent = `✅ Render Đã Xong!`;
      if (percentEl) percentEl.textContent = `100%`;
      if (barEl) barEl.style.width = `100%`;
      if (detailsEl) detailsEl.textContent = `Sẵn sàng duyệt`;

      if (!hudHideTimeout) {
        hudHideTimeout = setTimeout(() => {
          dock.classList.remove('is-visible');
          hudHideTimeout = null;
        }, 3500);
      }
    }
  }
}

function jumpToLatestRenderedShot() {
  if (!currentShotDeskProject || !currentShotDeskProject.shots) return;
  const shots = currentShotDeskProject.shots;
  let target = lastRenderedShotIndex;
  if (!target) {
    const rev = shots.find(s => (s.status === 'review' || s.status === 'approved') && s.video_url);
    if (rev) target = rev.index;
    else target = 1;
  }
  openReviewDeskForShot(target);
}

function toggleRenderHudManual(forceShow = null) {
  const dock = document.getElementById('sdPersistentRenderDock');
  if (!dock) return;
  if (forceShow === false) {
    dock.classList.remove('is-visible');
  } else if (forceShow === true) {
    dock.classList.add('is-visible');
  } else {
    dock.classList.toggle('is-visible');
  }
}

// ── Director Hotkeys Cheatsheet & Keyboard Listener ──
function openHotkeysCheatSheetModal() {
  document.getElementById('sdHotkeysModalEl')?.remove();
  const modal = document.createElement('div');
  modal.id = 'sdHotkeysModalEl';
  modal.className = 'modal-backdrop';
  modal.innerHTML = `
    <div class="modal" style="max-width:560px;background:rgba(13,20,36,0.96);backdrop-filter:blur(24px);border:1px solid rgba(99,102,241,0.3);box-shadow:0 24px 64px rgba(0,0,0,0.8);">
      <div class="modal-header" style="border-bottom:1px solid var(--border);">
        <div class="modal-title fx fx-ac fx-g8">
          <span>🎬</span> Bảng Phím Tắt Đạo Diễn (Director Hotkeys)
        </div>
        <button class="modal-close" onclick="this.closest('.modal-backdrop').remove()">✕</button>
      </div>
      <div class="modal-body" style="padding:16px 20px;">
        <div class="u-fs12 u-c-muted u-mb14">
          Điều khiển duyệt cảnh và phê duyệt phim nhanh như DaVinci Resolve & Hollywood Suite:
        </div>
        <div class="gd gd-2 fx-g10 u-fs125">
          <div style="display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.03);padding:8px 12px;border-radius:6px;border:1px solid var(--border);">
            <span>Phát / Dừng Video</span>
            <span class="sd-kbd">Space</span>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.03);padding:8px 12px;border-radius:6px;border:1px solid var(--border);">
            <span>Phê Duyệt Shot</span>
            <span class="sd-kbd">V / Enter</span>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.03);padding:8px 12px;border-radius:6px;border:1px solid var(--border);">
            <span>Shot Sau</span>
            <span class="sd-kbd">→ / D</span>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.03);padding:8px 12px;border-radius:6px;border:1px solid var(--border);">
            <span>Shot Trước</span>
            <span class="sd-kbd">← / A</span>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.03);padding:8px 12px;border-radius:6px;border:1px solid var(--border);">
            <span>Sửa Prompt Shot</span>
            <span class="sd-kbd">E</span>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.03);padding:8px 12px;border-radius:6px;border:1px solid var(--border);">
            <span>Render Lại Shot</span>
            <span class="sd-kbd">R</span>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.03);padding:8px 12px;border-radius:6px;border:1px solid var(--border);">
            <span>Lùi 1 Khung Hình</span>
            <span class="sd-kbd">J</span>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.03);padding:8px 12px;border-radius:6px;border:1px solid var(--border);">
            <span>Phát / Dừng (J-K-L)</span>
            <span class="sd-kbd">K / Space</span>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.03);padding:8px 12px;border-radius:6px;border:1px solid var(--border);">
            <span>Tiến 1 Khung Hình</span>
            <span class="sd-kbd">L</span>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.03);padding:8px 12px;border-radius:6px;border:1px solid var(--border);">
            <span>Rạp Chiếu Phim</span>
            <span class="sd-kbd">T</span>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.03);padding:8px 12px;border-radius:6px;border:1px solid var(--border);">
            <span>Bật/Tắt Lặp Clip</span>
            <span class="sd-kbd">Shift + L</span>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.03);padding:8px 12px;border-radius:6px;border:1px solid var(--border);">
            <span>Xem Bảng Phím Tắt</span>
            <span class="sd-kbd">?</span>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.03);padding:8px 12px;border-radius:6px;border:1px solid var(--border);">
            <span>Chuyển Sang Matrix</span>
            <span class="sd-kbd">1</span>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.03);padding:8px 12px;border-radius:6px;border:1px solid var(--border);">
            <span>Chuyển Sang Review</span>
            <span class="sd-kbd">2</span>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.03);padding:8px 12px;border-radius:6px;border:1px solid var(--border);">
            <span>Chuyển Sang Timeline</span>
            <span class="sd-kbd">3</span>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.03);padding:8px 12px;border-radius:6px;border:1px solid var(--border);">
            <span>Đổi Lưới / Chi tiết</span>
            <span class="sd-kbd">G</span>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.03);padding:8px 12px;border-radius:6px;border:1px solid var(--border);">
            <span>Bật/Tắt Khung Cuộn</span>
            <span class="sd-kbd">S</span>
          </div>
        </div>
      </div>
      <div class="modal-footer" style="border-top:1px solid var(--border);justify-content:flex-end;">
        <button class="btn btn-primary btn-sm" onclick="this.closest('.modal-backdrop').remove()">Đã Hiểu</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
}

function initDirectorHotkeys() {
  window.addEventListener('keydown', (e) => {
    // Không bắt phím khi đang nhập text
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName) || e.target.isContentEditable) {
      if (e.key === 'Escape') {
        e.target.blur();
      }
      return;
    }

    // Modal close on Escape
    if (e.key === 'Escape') {
      const openModal = document.querySelector('.modal-backdrop');
      if (openModal) {
        openModal.remove();
        e.preventDefault();
        return;
      }
    }

    // Hotkey: ? -> Mở cheatsheet
    if (e.key === '?' || (e.key === '/' && e.shiftKey)) {
      openHotkeysCheatSheetModal();
      e.preventDefault();
      return;
    }

    // Switch tabs: 1 -> Matrix, 2 -> Review, 3 -> Timeline
    if (e.key === '1') {
      switchShotDeskSubtab('matrix');
      e.preventDefault();
      return;
    }
    if (e.key === '2') {
      switchShotDeskSubtab('review');
      e.preventDefault();
      return;
    }
    if (e.key === '3') {
      switchShotDeskSubtab('timeline');
      e.preventDefault();
      return;
    }

    // Toggle View Mode: G -> Grid / Detailed (chỉ khi đang ở subtab Matrix)
    if ((e.key === 'g' || e.key === 'G') && (typeof currentShotDeskSubtab === 'undefined' || currentShotDeskSubtab === 'matrix')) {
      setShotDeskViewMode(currentShotDeskViewMode === 'grid' ? 'detailed' : 'grid');
      e.preventDefault();
      return;
    }

    // Toggle Scroll Frame: S -> Khung Cuộn / Toàn Trang (chỉ khi đang ở subtab Matrix)
    if ((e.key === 's' || e.key === 'S') && (typeof currentShotDeskSubtab === 'undefined' || currentShotDeskSubtab === 'matrix')) {
      toggleShotDeskScrollFrame();
      e.preventDefault();
      return;
    }

    // Review Desk Controls
    if (typeof currentShotDeskSubtab !== 'undefined' && currentShotDeskSubtab === 'review') {
      // Space: Play / Pause Video
      if (e.code === 'Space') {
        const vid = document.getElementById('sdReviewVideoPlayer');
        if (vid) {
          if (vid.paused) vid.play();
          else vid.pause();
          e.preventDefault();
        }
        return;
      }

      // ArrowRight or KeyD: Next Shot
      if (e.code === 'ArrowRight' || e.key === 'd' || e.key === 'D') {
        stepReviewShot(1);
        e.preventDefault();
        return;
      }

      // ArrowLeft or KeyA: Previous Shot
      if (e.code === 'ArrowLeft' || e.key === 'a' || e.key === 'A') {
        stepReviewShot(-1);
        e.preventDefault();
        return;
      }

      // Enter or KeyV: Approve Shot
      if (e.key === 'Enter' || e.key === 'v' || e.key === 'V') {
        if (currentShotDeskProject && currentReviewShotIndex) {
          const s = currentShotDeskProject.shots.find(sh => sh.index === currentReviewShotIndex);
          if (s && s.video_url && s.status !== 'approved' && s.status !== 'done') {
            approveShotDeskShot(currentReviewShotIndex, true);
            e.preventDefault();
          }
        }
        return;
      }

      // KeyE: Edit Prompt
      if (e.key === 'e' || e.key === 'E') {
        if (typeof currentReviewShotIndex !== 'undefined') {
          openShotDeskEditModal(currentReviewShotIndex);
          e.preventDefault();
        }
        return;
      }

      // KeyR: Retry / Render Shot
      if (e.key === 'r' || e.key === 'R') {
        if (typeof currentReviewShotIndex !== 'undefined') {
          renderSingleShotDeskShot(currentReviewShotIndex, false);
          e.preventDefault();
        }
        return;
      }

      // J: Step -1 Frame
      if (e.key === 'j' || e.key === 'J') {
        stepReviewVideoFrame(-1);
        e.preventDefault();
        return;
      }

      // K: Play / Pause
      if (e.key === 'k' || e.key === 'K') {
        const vid = document.getElementById('sdReviewVideoPlayer');
        if (vid) {
          if (vid.paused) vid.play();
          else vid.pause();
          e.preventDefault();
        }
        return;
      }

      // L: Step +1 Frame (Hoặc Shift+L để Toggle Loop)
      if (e.key === 'l' || e.key === 'L') {
        if (e.shiftKey) {
          toggleReviewDeskLoop();
        } else {
          stepReviewVideoFrame(1);
        }
        e.preventDefault();
        return;
      }

      // T: Toggle Theater Mode
      if (e.key === 't' || e.key === 'T') {
        toggleReviewTheaterMode();
        e.preventDefault();
        return;
      }
    }
  });
}

// ── Scroll Frame & Common Settings Controls ──
let isShotDeskScrollFrameEnabled = localStorage.getItem('sd_scroll_frame') !== 'false';

function toggleShotDeskScrollFrame() {
  isShotDeskScrollFrameEnabled = !isShotDeskScrollFrameEnabled;
  try { localStorage.setItem('sd_scroll_frame', isShotDeskScrollFrameEnabled ? 'true' : 'false'); } catch (e) {}
  applyShotDeskScrollFrameState();
}

function applyShotDeskScrollFrameState() {
  const container = document.getElementById('sdShotsListContainer');
  const btn = document.getElementById('sdToggleScrollFrameBtn');
  const label = document.getElementById('sdScrollFrameLabel');
  const icon = document.getElementById('sdScrollFrameIcon');

  if (container) {
    if (isShotDeskScrollFrameEnabled) {
      container.classList.add('sd-shots-scroll-frame');
    } else {
      container.classList.remove('sd-shots-scroll-frame');
    }
  }

  if (btn && label && icon) {
    if (isShotDeskScrollFrameEnabled) {
      btn.style.color = 'var(--accent)';
      btn.style.borderColor = 'rgba(99, 102, 241, 0.4)';
      label.textContent = 'Khung Cuộn';
      icon.textContent = '🔒';
      btn.title = 'Đang bật khung cuộn cố định (Common settings ghim cố định ở trên). Bấm để chuyển sang Toàn Trang';
    } else {
      btn.style.color = 'var(--muted)';
      btn.style.borderColor = 'var(--border)';
      label.textContent = 'Toàn Trang';
      icon.textContent = '↕️';
      btn.title = 'Đang ở chế độ Toàn Trang (Cuộn cả trang). Bấm để bật khung cuộn cố định';
    }
  }
}

function toggleCreateCommonSettings() {
  const body = document.getElementById('createCommonSettingsBody');
  const btn = document.getElementById('btnToggleCreateCommonSettings');
  if (!body) return;
  const isHidden = body.style.display === 'none';
  body.style.display = isHidden ? 'block' : 'none';
  if (btn) btn.textContent = isHidden ? '🔽 Cài Đặt Chung' : '▶️ Cài Đặt Chung (Đã thu gọn)';
}

function toggleSceneScrollFrame() {
  const el = document.getElementById('sceneList');
  const btn = document.getElementById('btnToggleSceneScrollFrame');
  if (!el) return;
  const isScroll = el.classList.toggle('has-scroll-frame');
  if (btn) btn.textContent = isScroll ? '🔒 Khung Cuộn' : '↕️ Toàn Trang';
}


// ══════════════════════════════════════════════════════════════════════════════
// 🛍️ AFFILIATE MARKETING STUDIO (TIẾP THỊ LIÊN KẾT)
// ══════════════════════════════════════════════════════════════════════════════

window.affiliateTemplates = null;
window.affiliateCurrentScript = null;
window.affiliateProductImage = null;
window.affiliateAudioPreviewObj = null;


function showAffiliateFinishedVideo(finalVideoUrl, title, srtUrl) {
  const card = document.getElementById('affiliateFinishedVideoCard');
  const player = document.getElementById('affiliateFinishedPlayer');
  const titleEl = document.getElementById('affiliateFinishedTitle');
  const dlBtn = document.getElementById('btnAffiliateDlVideo');
  const dlBtnBig = document.getElementById('btnAffiliateDlVideoBig');
  const dlSrt = document.getElementById('btnAffiliateDlSrt');

  if (card && player) {
    card.style.display = 'block';
    // Affiliate continuity 2026-11-07: video cuối nổi bật ở đầu panel affiliate.
    card.classList.add('aff-finished-prominent');
    if (document.getElementById('panel-affiliate')?.classList.contains('active')) {
      card.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    player.src = finalVideoUrl;
    if (titleEl) titleEl.textContent = title || "Video Tiếp Thị Đã Xuất Bản";
    const dlUrl = finalVideoUrl.replace('/video/', '/download/');
    if (dlBtn) dlBtn.href = dlUrl;
    if (dlBtnBig) dlBtnBig.href = dlUrl;
    if (srtUrl && dlSrt) {
      dlSrt.style.display = 'inline-flex';
      dlSrt.href = srtUrl;
    }
  }
}

async function initAffiliateStudio() {
  try {
    // 1. Tải templates danh mục & framework từ backend
    if (!window.affiliateTemplates) {
      const resp = await apiFetch('/api/affiliate/templates');
      if (resp.ok) {
        window.affiliateTemplates = await resp.json();
      }
    }
  } catch (err) {
    console.warn("[Affiliate] Không thể tải templates:", err);
  }

  // 2. Điền dropdown danh mục nếu có
  const catSel = document.getElementById('affiliateCategory');
  if (catSel && window.affiliateTemplates?.categories) {
    const curCat = catSel.value || 'gadgets';
    catSel.innerHTML = Object.entries(window.affiliateTemplates.categories).map(([k, v]) => {
      return `<option value="${k}" ${k === curCat ? 'selected' : ''}>${v.icon} ${v.name}</option>`;
    }).join('');
  }

  // 3. Cập nhật sample pills
  renderAffiliateSamplePills();

  // 4. Đồng bộ dropdown giọng tiếp thị (ưu tiên Adam bựa)
  const voiceSel = document.getElementById('affiliateVoiceSelect');
  if (voiceSel && typeof voiceSelectHtml === 'function') {
    const curV = voiceSel.value || 'vieneu:AdamBua';
    voiceSel.innerHTML = voiceSelectHtml(curV);
    if ([...voiceSel.options].some(o => o.value === curV)) {
      voiceSel.value = curV;
    }
  }

  // 5. Đồng bộ dropdown nhạc nền BGM
  const bgmSel = document.getElementById('affiliateBgmSelect');
  if (bgmSel && window.serverMusicList && window.serverMusicList.length > 0) {
    const curB = bgmSel.value;
    bgmSel.innerHTML = '<option value="">(Không dùng BGM)</option>' +
      window.serverMusicList.map(m => `<option value="${m.path}">${m.title || m.name}</option>`).join('');
    if (curB) bgmSel.value = curB;
  }

  // 6. Cập nhật ghi chú framework
  onAffiliateFrameworkChange();

  // 7. Nếu chưa có kịch bản và chưa nhập tên sản phẩm, hiển thị gợi ý
  if (!window.affiliateCurrentScript) {
    const prodInput = document.getElementById('affiliateProdName');
    if (!prodInput.value) {
      prodInput.value = "Giá đỡ điện thoại xoay 360 hợp kim nhôm";
      onAffiliateCategoryChange();
    }
  }

  // Tự động phát hiện và hiển thị video tiếp thị đã sản xuất gần nhất
  try {
    const jobsResp = await apiFetch('/api/jobs');
    if (jobsResp.ok) {
      const jobs = await jobsResp.json();
      const lastDone = jobs.find(j => j.status === 'done' && j.final_video);
      if (lastDone) {
        showAffiliateFinishedVideo(lastDone.final_video, lastDone.title, `/api/jobs/${lastDone.id}/subtitles`);
      }
    }
  } catch (e) {}
}

function renderAffiliateSamplePills() {
  const container = document.getElementById('affiliateSamplePills');
  if (!container) return;
  const cat = document.getElementById('affiliateCategory')?.value || 'gadgets';
  const catData = window.affiliateTemplates?.categories?.[cat];
  if (!catData || !catData.sample_products) {
    container.innerHTML = '';
    return;
  }
  container.innerHTML = catData.sample_products.map(p => {
    return `<button type="button" class="btn btn-ghost btn-xs" onclick="selectAffiliateSampleProduct('${p.replace(/'/g, "\'")}')" style="background:rgba(255,255,255,0.05);border:1px solid var(--border);border-radius:12px;font-size:11px;padding:2px 8px;color:var(--text);">${p}</button>`;
  }).join('');
}

function onAffiliateCategoryChange() {
  renderAffiliateSamplePills();
  const cat = document.getElementById('affiliateCategory')?.value || 'gadgets';
  const catData = window.affiliateTemplates?.categories?.[cat];
  if (catData) {
    const painEl = document.getElementById('affiliatePain');
    const featEl = document.getElementById('affiliateFeatures');
    const voiceEl = document.getElementById('affiliateVoiceSelect');
    if (painEl && !painEl.value) painEl.value = catData.default_pain;
    if (featEl && !featEl.value) featEl.value = catData.default_features;
    if (voiceEl && catData.default_voice) {
      if ([...voiceEl.options].some(o => o.value === catData.default_voice)) {
        voiceEl.value = catData.default_voice;
      }
    }
  }
}

function selectAffiliateSampleProduct(pName) {
  const input = document.getElementById('affiliateProdName');
  if (input) {
    input.value = pName;
    input.focus();
  }
  const cat = document.getElementById('affiliateCategory')?.value || 'gadgets';
  const catData = window.affiliateTemplates?.categories?.[cat];
  if (catData) {
    document.getElementById('affiliatePain').value = catData.default_pain;
    document.getElementById('affiliateFeatures').value = catData.default_features;
  }
  document.getElementById('affiliatePriceSale').value = "Chỉ 89k (Giảm 50%)";
  document.getElementById('affiliatePromotion').value = "Freeship đơn 0đ + Tặng kẹp phụ kiện";
}

function onAffiliateShootingStyleChange() {
  const style = document.getElementById('affiliateShootingStyle')?.value || 'faceless';
  const descEl = document.getElementById('affiliateShootingStyleDesc');

  const styleInfo = {
    'faceless': {
      desc: '💡 Người mẫu mặc/dùng sản phẩm, chỉ quay từ cổ trở xuống hoặc từ phía sau. Tinh tế, sang trọng, loại bỏ hoàn toàn lỗi biến dạng mặt!'
    },
    'hands': {
      desc: '💡 Cận cảnh đôi tay cầm, xoay 360°, thao tác đập hộp trên mặt bàn gỗ/đá cao cấp. Cực kỳ chân thực và trực quan!'
    },
    'product': {
      desc: '💡 Quảng cáo sản phẩm điện ảnh: Xoay mâm 360°, ánh sáng studio siêu thực, HOÀN TOÀN KHÔNG CÓ NGƯỜI VÀ TAY.'
    },
    'koc_face': {
      desc: '💡 KOC trực diện nói chuyện và review trước ống kính, tự động khóa chặt khuôn mặt và trang phục đồng nhất 100%!'
    }
  };

  const info = styleInfo[style] || styleInfo['faceless'];
  if (descEl) descEl.textContent = info.desc;
}

function onAffiliateKocChange() {
  const koc = document.getElementById('affiliateKocPersona')?.value || 'nam_bua_hai';
  const voiceSel = document.getElementById('affiliateVoiceSelect');
  const fwSel = document.getElementById('affiliateFramework');
  const descEl = document.getElementById('affiliateKocDesc');

  const kocInfo = {
    'nam_bua_hai': {
      voice: 'vieneu:AdamBua',
      fw: 'hai_bua',
      desc: '🧑 Tuấn Bựa: Phong cách hài hước dí dỏm, biểu cảm bựa chém gió cực dính, đồng bộ cùng giọng đọc ⭐ Adam bựa.'
    },
    'nu_tre_trung': {
      voice: 'vieneu:TrucLy',
      fw: 'review_koc',
      desc: '👩 Trúc Linh: Nữ KOC 23 tuổi xinh xắn, nụ cười rạng rỡ, giọng điệu ngọt ngào tự nhiên, tạo thiện cảm lớn khi review.'
    },
    'chuyen_gia_nu': {
      voice: 'vieneu:MaiAnh',
      fw: 'pas',
      desc: '👩‍💼 Mai Phương: Chuyên gia 29 tuổi thanh lịch, trang nhã, tạo dựng niềm tin tuyệt đối cho sản phẩm cao cấp.'
    },
    'nam_lich_lam': {
      voice: 'vi-VN-NamMinhNeural',
      fw: 'pas',
      desc: '🧔 Hoàng Nam: Nam reviewer 30 tuổi chững chạc, lịch lãm nam tính, phù hợp đồ công nghệ cao cấp và thời trang.'
    }
  };

  const info = kocInfo[koc];
  if (info) {
    if (descEl) descEl.textContent = info.desc;
    if (voiceSel && info.voice) {
      if ([...voiceSel.options].some(o => o.value === info.voice)) {
        voiceSel.value = info.voice;
      }
    }
    if (fwSel && info.fw) {
      fwSel.value = info.fw;
      onAffiliateFrameworkChange();
    }
  }
}

function onAffiliateFrameworkChange() {
  const fw = document.getElementById('affiliateFramework')?.value || 'pas';
  const descEl = document.getElementById('affiliateFrameworkDesc');
  const voiceSel = document.getElementById('affiliateVoiceSelect');

  const fwDescriptions = {
    'pas': '💡 Đánh trúng nỗi đau bực bội thường gặp ➔ Thổi bùng sự khó chịu ➔ Sản phẩm giải quyết ngay tức thì ➔ Chốt đơn giỏ hàng.',
    'hai_bua': '⭐ Tình huống bựa dí dỏm, chém gió cười nghiêng ngả, giọng đọc Adam bựa giữ chân người xem 100%!',
    'review_koc': '📦 Đập hộp trên tay, thử tính năng trực tiếp trước camera, khen chê khách quan tạo dựng lòng tin vững chắc.',
    'before_after': '🔄 So sánh đối lập gay gắt: Cảnh trước đây khốn khổ chật vật vs Cảnh sau khi dùng xịn sò sung sướng.',
    'aida': '⚡ Thu hút sự chú ý 3s đầu ➔ Kích thích tò mò ➔ Khao khát sở hữu tính năng vượt trội ➔ Hành động mua ngay.'
  };

  if (descEl) descEl.textContent = fwDescriptions[fw] || '';

  // Tự động chuyển giọng phù hợp với framework
  if (voiceSel) {
    if (fw === 'hai_bua') {
      voiceSel.value = 'vieneu:AdamBua';
    } else if (fw === 'review_koc' && voiceSel.value === 'vieneu:AdamBua') {
      voiceSel.value = 'vieneu:TrucLy';
    }
  }
}

function testAffiliateVoice() {
  const v = document.getElementById('affiliateVoiceSelect')?.value || 'vieneu:AdamBua';
  const btn = document.getElementById('btnAffiliateVoiceTest');
  if (typeof testPlayVoice === 'function') {
    testPlayVoice(v, btn);
  }
}

async function onAffiliateImageUpload(event) {
  const file = event.target.files[0];
  if (!file) return;

  const formData = new FormData();
  formData.append('file', file);

  try {
    const res = await apiFetch('/api/upload-image', {
      method: 'POST',
      body: formData
    });
    const data = await res.json();
    if (data.url || data.data_url) {
      window.affiliateProductImage = data.url || data.data_url;
      const preview = document.getElementById('affiliateImgPreview');
      const wrap = document.getElementById('affiliateImgPreviewWrap');
      if (preview) preview.src = data.url || data.data_url;
      if (wrap) wrap.style.display = 'flex';
      
      // Đồng bộ ảnh vào các cảnh đã render nếu có
      if (window.affiliateCurrentScript?.scenes) {
        window.affiliateCurrentScript.scenes.forEach(s => {
          s.reference_image = window.affiliateProductImage;
        });
        renderAffiliateScenes(window.affiliateCurrentScript.scenes);
      }
    }
  } catch (err) {
    alert("Lỗi khi tải ảnh sản phẩm: " + err.message);
  }
}

function clearAffiliateImage() {
  window.affiliateProductImage = null;
  const input = document.getElementById('affiliateImgUpload');
  if (input) input.value = '';
  const wrap = document.getElementById('affiliateImgPreviewWrap');
  if (wrap) wrap.style.display = 'none';
  if (window.affiliateCurrentScript?.scenes) {
    window.affiliateCurrentScript.scenes.forEach(s => {
      s.reference_image = null;
    });
    renderAffiliateScenes(window.affiliateCurrentScript.scenes);
  }
}

async function generateAffiliateScriptAction() {
  const pName = document.getElementById('affiliateProdName')?.value.trim();
  if (!pName) {
    alert("Vui lòng nhập tên sản phẩm tiếp thị!");
    document.getElementById('affiliateProdName')?.focus();
    return;
  }

  const btn = document.getElementById('btnGenAffiliateScript');
  const oldText = btn ? btn.innerHTML : '';
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '⏳ AI Đang Sáng Tạo Kịch Bản Bán Hàng Triệu View...';
  }

  const payload = {
    product_name: pName,
    category: document.getElementById('affiliateCategory')?.value || 'gadgets',
    price_sale: document.getElementById('affiliatePriceSale')?.value.trim() || 'Chỉ 99k',
    promotion_deal: document.getElementById('affiliatePromotion')?.value.trim() || 'Freeship đơn 0đ + Quà tặng kèm',
    key_features: document.getElementById('affiliateFeatures')?.value.trim() || '',
    pain_points: document.getElementById('affiliatePain')?.value.trim() || '',
    framework: document.getElementById('affiliateFramework')?.value || 'pas',
    shooting_style: document.getElementById('affiliateShootingStyle')?.value || 'faceless',
    koc_persona: document.getElementById('affiliateKocPersona')?.value || 'nam_bua_hai',
    tone: document.getElementById('affiliateFramework')?.value === 'hai_bua' ? 'hai_bua' : 'energetic',
    voice: document.getElementById('affiliateVoiceSelect')?.value || 'vieneu:AdamBua',
    aspect_ratio: document.getElementById('affiliateAspectRatio')?.value || '9:16',
    reference_image: window.affiliateProductImage || null
  };

  try {
    const r = await apiFetch('/api/affiliate/generate-script', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!r.ok) {
      const err = await r.json();
      throw new Error(err.error || "Không thể tạo kịch bản");
    }

    const data = await r.json();
    window.affiliateCurrentScript = data;

    // Render scenes
    renderAffiliateScenes(data.scenes);

    // Hiển thị campaign bar & render box
    const campBar = document.getElementById('affiliateCampaignBar');
    const campTitle = document.getElementById('affiliateCampaignTitle');
    const actionBox = document.getElementById('affiliateRenderActionBox');
    if (campBar) campBar.style.display = 'block';
    if (campTitle) campTitle.textContent = data.title;
    if (actionBox) actionBox.style.display = 'block';

  } catch (err) {
    alert("Lỗi tạo kịch bản: " + err.message);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = oldText;
    }
  }
}

function renderAffiliateScenes(scenes) {
  const container = document.getElementById('affiliateScenesList');
  const emptyState = document.getElementById('affiliateEmptyState');
  if (!container) return;

  if (!scenes || scenes.length === 0) {
    if (emptyState) emptyState.style.display = 'block';
    return;
  }
  if (emptyState) emptyState.style.display = 'none';

  const roleStyles = {
    'hook': { bg: 'linear-gradient(135deg, rgba(236,72,153,0.2) 0%, rgba(244,63,94,0.1) 100%)', border: 'rgba(236,72,153,0.4)', text: '#f43f5e', label: '🔥 CẢNH 1: HOOK 3S CHẶN LƯỚT' },
    'pain': { bg: 'linear-gradient(135deg, rgba(245,158,11,0.2) 0%, rgba(217,119,6,0.1) 100%)', border: 'rgba(245,158,11,0.4)', text: '#fbbf24', label: '😫 CẢNH 2: NỖI ĐAU KHÁCH HÀNG' },
    'demo': { bg: 'linear-gradient(135deg, rgba(99,102,241,0.2) 0%, rgba(139,92,246,0.1) 100%)', border: 'rgba(99,102,241,0.4)', text: '#a5b4fc', label: '✨ CẢNH 3: TRÌNH DIỄN & GIẢI PHÁP' },
    'proof': { bg: 'linear-gradient(135deg, rgba(16,185,129,0.2) 0%, rgba(5,150,105,0.1) 100%)', border: 'rgba(16,185,129,0.4)', text: '#34d399', label: '⭐ CẢNH 4: DEAL HỜI & ĐÁNH GIÁ' },
    'cta': { bg: 'linear-gradient(135deg, rgba(59,130,246,0.2) 0%, rgba(37,99,235,0.1) 100%)', border: 'rgba(59,130,246,0.4)', text: '#60a5fa', label: '👇 CẢNH 5: KÊU GỌI BẤM GIỎ HÀNG' },
  };

  container.innerHTML = scenes.map((s, idx) => {
    const roleCfg = roleStyles[s.role] || roleStyles['demo'];
    const voiceOpts = (typeof voiceSelectHtml === 'function') ? voiceSelectHtml(s.voice || 'vieneu:AdamBua') : '';
    const imgBadge = s.reference_image ? `<span class="badge" style="background:rgba(34,197,94,0.2);color:#4ade80;font-size:10px;">🔒 Product Lock</span>` : '';

    return `
      <div class="card" id="affSceneCard_${idx}" style="background:var(--bg2);border:1px solid ${roleCfg.border};padding:12px;border-radius:var(--radius);margin-bottom:8px;">
        <!-- Card Header -->
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid rgba(255,255,255,0.06);">
          <div class="fx fx-ac fx-g8">
            <span style="font-weight:700;font-size:12px;color:${roleCfg.text};background:${roleCfg.bg};padding:3px 8px;border-radius:6px;border:1px solid ${roleCfg.border};">
              ${roleCfg.label}
            </span>
            ${imgBadge}
            ${s.badge ? `<span class="badge" style="background:rgba(255,255,255,0.08);color:var(--text);font-size:10px;">🏷️ ${s.badge}</span>` : ''}
          </div>
          <div class="fx fx-ac fx-g8">
            <label class="u-fs11 u-c-muted u-m0">Thời lượng:</label>
            <input type="number" id="affSceneDur_${idx}" value="10" min="10" max="10" disabled title="Chuẩn 10 giây — đúng clip API 10s trả về, dùng trọn vẹn không cắt đuôi" style="width:48px;padding:2px 6px;text-align:center;font-size:12px;background:var(--bg3);border:1px solid var(--border);border-radius:4px;color:var(--text);">
            <span class="u-fs11 u-c-muted">giây (chuẩn 10s)</span>
            <button type="button" class="btn btn-ghost btn-xs" onclick="deleteAffiliateScene(${idx})" style="color:#ef4444;" title="Xóa cảnh này">✕</button>
          </div>
        </div>

        <!-- Voiceover Text -->
        <div class="u-mb8">
          <div class="fx fx-jc-sb fx-ac u-mb4">
            <label class="u-fs115 u-fw600 u-c-accent u-m0">
              🎙️ Thoại Lồng Tiếng (Tiếng Việt):
            </label>
            <button type="button" id="btnTestSceneVoice_${idx}" class="btn btn-ghost btn-xs" onclick="testAffiliateSceneVoice(${idx})" style="color:#818cf8;padding:1px 6px;font-size:11px;">
              🔊 Nghe thử thoại
            </button>
          </div>
          <textarea id="affSceneVo_${idx}" rows="2" class="form-control" style="width:100%;font-size:13px;line-height:1.45;background:var(--bg3);border-color:rgba(255,255,255,0.1);">${s.voiceover || ''}</textarea>
        </div>

        <!-- Prompt Video (English) -->
        <div class="u-mb6">
          <label style="font-size:11px;color:var(--muted);display:block;margin-bottom:2px;">
            🎥 Prompt Quay Video (Tiếng Anh thương mại 9:16):
          </label>
          <textarea id="affScenePrompt_${idx}" rows="2" class="form-control" style="width:100%;font-size:11px;font-family:'JetBrains Mono',monospace;color:#94a3b8;background:rgba(0,0,0,0.2);">${s.prompt || ''}</textarea>
        </div>

        <!-- Scene Voice Override -->
        <div class="fx fx-ac fx-g10 u-mt6">
          <label class="u-fs11 u-c-muted u-m0">Giọng cảnh này:</label>
          <select id="affSceneVoice_${idx}" class="form-control" style="font-size:11px;padding:3px 6px;width:240px;">
            ${voiceOpts}
          </select>
        </div>
      </div>
    `;
  }).join('');
}

async function testAffiliateSceneVoice(idx) {
  const voText = document.getElementById(`affSceneVo_${idx}`)?.value.trim();
  const voice = document.getElementById(`affSceneVoice_${idx}`)?.value || document.getElementById('affiliateVoiceSelect')?.value || 'vieneu:AdamBua';
  const btn = document.getElementById(`btnTestSceneVoice_${idx}`);
  if (!voText) {
    alert("Nội dung thoại đang trống!");
    return;
  }

  const oldHtml = btn ? btn.innerHTML : '';
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '⏳ Đang đọc...';
  }

  try {
    if (window.affiliateAudioPreviewObj) {
      window.affiliateAudioPreviewObj.pause();
      window.affiliateAudioPreviewObj = null;
    }
    const audioUrl = `/api/voices/preview?voice=${encodeURIComponent(voice)}&text=${encodeURIComponent(voText)}`;
    const audio = new Audio(audioUrl);
    window.affiliateAudioPreviewObj = audio;
    
    audio.onended = () => {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = oldHtml;
      }
    };
    audio.onerror = () => {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = oldHtml;
      }
    };
    await audio.play();
  } catch (err) {
    if (err.name !== 'AbortError') {
      console.warn("Audio play error:", err);
    }
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = oldHtml;
    }
  }
}

function deleteAffiliateScene(idx) {
  if (!window.affiliateCurrentScript?.scenes) return;
  window.affiliateCurrentScript.scenes.splice(idx, 1);
  renderAffiliateScenes(window.affiliateCurrentScript.scenes);
}

function addAffiliateSceneCustom() {
  if (!window.affiliateCurrentScript) {
    window.affiliateCurrentScript = {
      title: "Video Tiếp Thị Sản Phẩm",
      scenes: []
    };
  }
  const newIdx = window.affiliateCurrentScript.scenes.length + 1;
  window.affiliateCurrentScript.scenes.push({
    index: newIdx,
    title: `Cảnh ${newIdx}`,
    role: 'demo',
    duration: 10, // Affiliate chuẩn 10s/cảnh = clip API trả về
    prompt: `Commercial showcase shot, vertical 9:16 framing, studio lighting, 8k, photorealistic, masterpiece, no text, no watermark`,
    voiceover: `Trải nghiệm thực tế sản phẩm vô cùng mượt mà và tiện lợi.`,
    voice: document.getElementById('affiliateVoiceSelect')?.value || 'vieneu:AdamBua',
    badge: 'DEMO TIỆN ÍCH ✨',
    reference_image: window.affiliateProductImage || null
  });
  renderAffiliateScenes(window.affiliateCurrentScript.scenes);
}

function copyAffiliateScriptText() {
  if (!window.affiliateCurrentScript?.scenes || window.affiliateCurrentScript.scenes.length === 0) {
    alert("Chưa có kịch bản để copy!");
    return;
  }
  let md = `# 🛍️ KỊCH BẢN TIẾP THỊ LIÊN KẾT: ${window.affiliateCurrentScript.title}\n\n`;
  window.affiliateCurrentScript.scenes.forEach((s, i) => {
    const vo = document.getElementById(`affSceneVo_${i}`)?.value || s.voiceover;
    const pr = document.getElementById(`affScenePrompt_${i}`)?.value || s.prompt;
    const dur = document.getElementById(`affSceneDur_${i}`)?.value || s.duration;
    md += `🎥 CẢNH ${i + 1} (${dur}s) - [${s.badge || s.role.toUpperCase()}]\n`;
    md += `PROMPT:\n${pr}\n\n`;
    md += `🎙️ THOẠI TIẾNG VIỆT:\n"${vo}"\n\n---\n\n`;
  });
  navigator.clipboard.writeText(md).then(() => {
    alert("✅ Đã copy toàn bộ kịch bản phân cảnh vào Clipboard!");
  }).catch(() => {
    alert("Không thể tự động copy, vui lòng copy thủ công.");
  });
}

function loadSampleAffiliateQuick() {
  selectAffiliateSampleProduct("Giá đỡ điện thoại xoay 360 hợp kim nhôm");
  generateAffiliateScriptAction();
}

async function startAffiliateRenderJob() {
  if (!window.affiliateCurrentScript?.scenes || window.affiliateCurrentScript.scenes.length === 0) {
    alert("Vui lòng tạo kịch bản trước khi render!");
    return;
  }

  // Thu thập dữ liệu từ các input trên màn hình
  // Affiliate chuẩn 10s/cảnh = clip API trả về (backend cũng ép 10 ở
  // /api/affiliate/render kể cả khi script cũ gửi duration khác).
  const scenesPayload = window.affiliateCurrentScript.scenes.map((s, idx) => {
    const vo = document.getElementById(`affSceneVo_${idx}`)?.value || s.voiceover;
    const pr = document.getElementById(`affScenePrompt_${idx}`)?.value || s.prompt;
    const dur = 10;
    const vc = document.getElementById(`affSceneVoice_${idx}`)?.value || s.voice;
    return {
      prompt: pr,
      voiceover: vo,
      duration: dur,
      voice: vc,
      reference_image: s.reference_image || window.affiliateProductImage || null
    };
  });

  const payload = {
    title: window.affiliateCurrentScript.title || "Video Tiếp Thị Sản Phẩm",
    scenes: scenesPayload,
    voice: document.getElementById('affiliateVoiceSelect')?.value || 'vieneu:AdamBua',
    aspect_ratio: document.getElementById('affiliateAspectRatio')?.value || '9:16',
    bg_music_path: document.getElementById('affiliateBgmSelect')?.value || null,
    music_volume: 0.20,
    voice_volume: 1.0,
    quality: "720p",
    transition: "dissolve",
    font_size: 24
  };

  const btn = document.getElementById('btnAffiliateStartRender');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '🚀 Đang Khởi Động Tiến Trình Sản Xuất...';
  }

  try {
    const resp = await apiFetch('/api/affiliate/render', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.error || "Không thể khởi động render");
    }
    const data = await resp.json();
    const jobId = data.job_id;

    // Chuyển sang panel Monitor để theo dõi realtime
    showPanel('monitor');
    if (typeof connectSSE === 'function') {
      connectSSE(jobId);
    }
    alert(`🎉 Đã bắt đầu sản xuất Video Tiếp Thị Liên Kết 9:16!\nJob ID: ${jobId}\nĐang tiến hành tạo video, lồng tiếng và ghép phụ đề tự động...`);

  } catch (err) {
    alert("Lỗi khi render: " + err.message);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '🚀 Bắt Đầu Tạo & Xuất Video Tiếp Thị 9:16';
    }
  }
}

function sendAffiliateToMainStudio() {
  if (!window.affiliateCurrentScript?.scenes || window.affiliateCurrentScript.scenes.length === 0) {
    alert("Chưa có kịch bản để nạp vào Studio!");
    return;
  }

  if (confirm("Thao tác này sẽ nạp 5 cảnh tiếp thị vào tab 'Tạo Phim Mới' với tỷ lệ 9:16. Bạn có muốn tiếp tục?")) {
    // Đặt tên phim và tỷ lệ khung hình (id hien tai: projectTitle/globalAspectRatio;
    // ban cu dung movieTitle/aspectRatio da khong con trong HTML -> doan nay chet lam)
    const titleInput = document.getElementById('projectTitle');
    const ratioSel = document.getElementById('globalAspectRatio');
    const voiceSel = document.getElementById('voiceSelect');
    if (titleInput) titleInput.value = window.affiliateCurrentScript.title || "Video_Tiep_Thi";
    if (ratioSel) ratioSel.value = document.getElementById('affiliateAspectRatio')?.value || '9:16';
    if (voiceSel) voiceSel.value = document.getElementById('affiliateVoiceSelect')?.value || 'vieneu:AdamBua';

    // Xóa các cảnh cũ và thêm từng cảnh tiếp thị
    if (typeof scenes !== 'undefined' && Array.isArray(scenes)) {
      scenes.length = 0;
      window.affiliateCurrentScript.scenes.forEach((s, idx) => {
        const vo = document.getElementById(`affSceneVo_${idx}`)?.value || s.voiceover;
        const pr = document.getElementById(`affScenePrompt_${idx}`)?.value || s.prompt;
        const dur = 10; // Affiliate chuẩn 10s/cảnh = clip API trả về
        const vc = document.getElementById(`affSceneVoice_${idx}`)?.value || s.voice;
        if (typeof addScene === 'function') {
          addScene(
            pr,
            vo,
            dur,
            document.getElementById('affiliateAspectRatio')?.value || '9:16',
            s.title || `Cảnh ${idx + 1}`,
            vc,
            [],
            s.reference_image || window.affiliateProductImage || ''
          );
        }
      });
    }

    showPanel('create');
  }
}

// Tự động tải danh sách nhạc nền và kích hoạt Hotkeys khi tải trang
window.addEventListener('DOMContentLoaded', () => {
  loadMusicList();
  initDirectorHotkeys();
  updateViewModeButtons();
  applyShotDeskScrollFrameState();
});
