/* ── AI Movie Studio · UI enhancements (refactor 2026-11-07) ─────────────────
   Nap SAU js/app.js (defer). Chi them tinh nang UI, khong doi id/ham/endpoint:
   theme, sidebar collapse/drawer, wizard Tao phim, debounce tim kiem Shot Desk,
   skeleton Thu vien, dung polling khi tab an (resume khi mo lai), thumbnail fade-in.
   Tat ca deu co guard typeof/null de khong vo app.js goc neu thieu element. */
(function () {
  'use strict';
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* bo qua */ } }
  };
  const debounce = (fn, ms) => { let t; return function () { const a = arguments, c = this; clearTimeout(t); t = setTimeout(() => fn.apply(c, a), ms); }; };

  /* ── Theme dark/light (mac dinh dark; luu localStorage 'ams-theme') ── */
  function applyTheme(t) {
    const theme = t === 'light' ? 'light' : 'dark';
    document.documentElement.dataset.theme = theme;
    store.set('ams-theme', theme);
    const b = $('#btnThemeToggle');
    if (b) { b.textContent = theme === 'light' ? '☀️' : '🌙'; b.title = theme === 'light' ? 'Chuyển sang Dark mode' : 'Chuyển sang Light mode'; }
  }
  applyTheme(store.get('ams-theme', 'dark'));
  $('#btnThemeToggle')?.addEventListener('click', () => {
    applyTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light');
  });

  /* ── Sidebar: collapse (desktop) / drawer (<=1024px) ── */
  const sidebar = $('.sidebar');
  if (store.get('ams-sidebar', '') === 'collapsed') document.body.classList.add('sidebar-collapsed');
  $('#btnSidebarToggle')?.addEventListener('click', () => {
    if (window.innerWidth <= 1024) {
      document.body.classList.toggle('sidebar-open');
    } else {
      const collapsed = document.body.classList.toggle('sidebar-collapsed');
      store.set('ams-sidebar', collapsed ? 'collapsed' : '');
    }
  });
  $('#sidebarOverlay')?.addEventListener('click', () => document.body.classList.remove('sidebar-open'));

  /* ── Sidebar nav: boc label (de tooltip khi collapse) + delegation ──
     (thay cho onclick="showPanel(...)" tren tung .nav-item; onclick cu da go o HTML) */
  $$('.sidebar .nav-item').forEach((item) => {
    const textNode = Array.from(item.childNodes).find((n) => n.nodeType === 3 && n.textContent.trim());
    if (textNode) {
      const label = document.createElement('span');
      label.className = 'nav-label';
      label.textContent = textNode.textContent.trim();
      item.setAttribute('data-label', label.textContent);
      item.replaceChild(label, textNode);
    }
  });
  sidebar?.addEventListener('click', (e) => {
    const item = e.target.closest('.nav-item');
    if (!item) return;
    if (item.dataset.panel && typeof window.showPanel === 'function') window.showPanel(item.dataset.panel);
    if (item.dataset.action === 'prompt-sheet' && typeof window.openPromptSheetModal === 'function') window.openPromptSheetModal();
    document.body.classList.remove('sidebar-open'); // dong drawer mobile sau khi chon
  });

  /* ── Wizard Tao phim (3 buoc) — gom card co san vao step, khong doi id ── */
  const panelCreate = $('#panel-create');
  if (panelCreate) {
    const cards = $$(':scope > .card', panelCreate);
    const launch = $('.wizard-sticky-cta', panelCreate);
    if (cards.length >= 3 && launch) {
      const nav = document.createElement('div');
      nav.className = 'create-wizard-nav';
      nav.id = 'createWizardNav';
      nav.innerHTML =
        '<button type="button" class="wizard-step-btn is-active" data-wizard-step="1"><span class="wizard-num">1</span>Thông tin phim</button>' +
        '<button type="button" class="wizard-step-btn" data-wizard-step="2"><span class="wizard-num">2</span>Danh sách cảnh</button>' +
        '<button type="button" class="wizard-step-btn" data-wizard-step="3"><span class="wizard-num">3</span>Render</button>' +
        '<span style="flex:1"></span>' +
        '<button type="button" class="wizard-step-btn" data-wizard-nav="prev">← Trước</button>' +
        '<button type="button" class="wizard-step-btn" data-wizard-nav="next">Sau →</button>';
      panelCreate.insertBefore(nav, panelCreate.firstChild);
      const steps = [document.createElement('div'), document.createElement('div'), document.createElement('div')];
      steps.forEach((el, i) => { el.className = 'wizard-step' + (i === 0 ? ' is-active' : ''); el.dataset.step = String(i + 1); });
      steps[0].appendChild(cards[0]);            // 1: Thong tin du an & Cai dat
      cards.slice(1).forEach((c) => steps[1].appendChild(c)); // 2: Beat + Sample + Danh sach canh
      steps[2].appendChild(launch);              // 3: Nut San xuat (sticky)
      steps.forEach((el) => panelCreate.appendChild(el));
      let wizardStep = 1;
      const showWizardStep = (n) => {
        wizardStep = Math.min(3, Math.max(1, n));
        $$('.wizard-step', panelCreate).forEach((el) => el.classList.toggle('is-active', +el.dataset.step === wizardStep));
        $$('[data-wizard-step]', nav).forEach((b) => b.classList.toggle('is-active', +b.dataset.wizardStep === wizardStep));
      };
      nav.addEventListener('click', (e) => {
        const stepBtn = e.target.closest('[data-wizard-step]');
        if (stepBtn) { showWizardStep(+stepBtn.dataset.wizardStep); return; }
        const dir = e.target.closest('[data-wizard-nav]');
        if (dir) showWizardStep(wizardStep + (dir.dataset.wizardNav === 'next' ? 1 : -1));
      });
      window.__showWizardStep = showWizardStep;
    }
  }

  /* ── showPanel: danh dau panel da load lan dau (lazy) + mo wizard o buoc 1 ── */
  if (typeof window.showPanel === 'function') {
    const origShowPanel = window.showPanel;
    window.showPanel = function (name) {
      const r = origShowPanel.apply(this, arguments);
      const p = document.getElementById('panel-' + name);
      if (p) p.classList.add('panel-loaded');
      if (name === 'create' && typeof window.__showWizardStep === 'function') window.__showWizardStep(1);
      return r;
    };
  }

  /* ── Tim kiem Shot Desk: debounce 180ms (chi o input, khong doi ham goc) ── */
  const sdSearch = $('#sdSearchInput');
  if (sdSearch) {
    sdSearch.oninput = debounce(() => {
      if (typeof window.applyShotDeskFilter === 'function') window.applyShotDeskFilter();
    }, 180);
    sdSearch.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && typeof window.applyShotDeskFilter === 'function') window.applyShotDeskFilter();
    });
  }

  /* ── Thu vien: skeleton rows trong luc loadLibrary() chay ── */
  if (typeof window.loadLibrary === 'function') {
    const origLoadLibrary = window.loadLibrary;
    window.loadLibrary = async function () {
      const tb = document.getElementById('jobsBody');
      if (tb && document.getElementById('panel-library')?.classList.contains('active')) {
        tb.innerHTML = Array.from({ length: 5 }, () =>
          '<tr><td colspan="6"><div class="sk" style="height:18px">&nbsp;</div></td></tr>').join('');
      }
      return origLoadLibrary.apply(this, arguments);
    };
  }

  /* ── Tab an/hien: dung polling Shot Desk, resume khi mo lai ── */
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      if (typeof window.stopShotDeskPolling === 'function') window.stopShotDeskPolling();
      return;
    }
    if (window.__sdPollPaused) {
      window.__sdPollPaused = false;
      if (typeof window.checkAutoStartShotDeskPolling === 'function') window.checkAutoStartShotDeskPolling();
    }
    if (window.__sdComposePaused) {
      window.__sdComposePaused = false;
      if (window.currentShotDeskProject && typeof window.pollComposeProgress === 'function') {
        window.pollComposeProgress(window.currentShotDeskProject.id);
      }
    }
  });

  /* ── Thumbnail fade-in khi cuon toi (IntersectionObserver, co throttle) ── */
  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((en) => {
        if (en.isIntersecting) { en.target.classList.add('img-in'); io.unobserve(en.target); }
      });
    }, { rootMargin: '200px' });
    let ioTimer = null;
    const observeImgs = () => {
      $$('img.img-lazy-fade, img.sd-sb-thumb, img.keyframe-preview-thumb').forEach((im) => {
        if (im.dataset.ioObserved) return;
        im.dataset.ioObserved = '1';
        im.classList.add('img-lazy-fade');
        io.observe(im);
      });
    };
    const scheduleObserve = () => { clearTimeout(ioTimer); ioTimer = setTimeout(observeImgs, 300); };
    observeImgs();
    new MutationObserver(scheduleObserve).observe(document.body, { childList: true, subtree: true });
  }

  /* ── Khóa API Studio (X-API-Key) — modal + panel Cài đặt ──────────────
     Key lưu ở localStorage key 'ams-api-key', chỉ gửi tới chính server
     Studio này qua header X-API-Key (xem apiFetch trong js/app.js).
     Chỉ dùng cho tool cá nhân / trình duyệt tin cậy, KHÔNG dùng chung máy
     công cộng. SSE vẫn miễn auth nên EventSource giữ nguyên. */
  function showApiKeyModal() {
    const m = $('#apiKeyModal');
    if (!m) return;
    m.style.display = 'flex';
    const input = $('#apiKeyModalInput');
    if (input) { input.value = ''; setTimeout(() => input.focus(), 50); }
    const err = $('#apiKeyModalError'); if (err) err.textContent = '';
  }
  function hideApiKeyModal() { const m = $('#apiKeyModal'); if (m) m.style.display = 'none'; }
  window.showApiKeyModal = showApiKeyModal;
  window.hideApiKeyModal = hideApiKeyModal;

  function saveStudioApiKey(value) {
    const v = (value || '').trim();
    if (!v) return false;
    try { localStorage.setItem('ams-api-key', v); } catch (e) { /* bỏ qua */ }
    if (typeof window.resetApiKeyModalFlag === 'function') window.resetApiKeyModalFlag();
    refreshStudioApiKeyStatus();
    if (typeof window.reloadStudioDataAfterKeySave === 'function') window.reloadStudioDataAfterKeySave();
    return true;
  }

  async function refreshStudioApiKeyStatus() {
    const el = $('#studioApiKeyStatus');
    let authRequired = null;
    try {
      const f = (typeof window.apiFetch === 'function') ? window.apiFetch : window.fetch.bind(window);
      const r = await f('/api/auth/status');
      const d = await r.json();
      authRequired = !!(d && d.auth_required);
    } catch { authRequired = null; }
    let saved = '';
    try { saved = localStorage.getItem('ams-api-key') || ''; } catch (e) { /* bỏ qua */ }
    if (el) {
      if (authRequired === null) {
        el.textContent = 'Không kiểm tra được server.';
      } else if (!authRequired) {
        el.textContent = 'Server đang TẮT xác thực — không cần key.';
      } else if (saved) {
        el.textContent = 'Server đang BẬT xác thực — đã lưu key (•••).';
      } else {
        el.textContent = 'Server đang BẬT xác thực — chưa nhập key.';
      }
    }
    const input = $('#studioApiKeyInput');
    if (input) input.placeholder = saved ? 'Đã lưu key (•••) — nhập key mới để đổi' : 'Nhập khóa X-API-Key do server cấp';
  }
  window.refreshStudioApiKeyStatus = refreshStudioApiKeyStatus;

  // Nút trong modal nhập key
  $('#btnSaveApiKeyModal')?.addEventListener('click', () => {
    const input = $('#apiKeyModalInput');
    const err = $('#apiKeyModalError');
    if (!saveStudioApiKey(input ? input.value : '')) {
      if (err) err.textContent = 'Chưa nhập key.';
      return;
    }
    hideApiKeyModal();
  });
  $('#apiKeyModalInput')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') $('#btnSaveApiKeyModal')?.click();
    if (e.key === 'Escape') hideApiKeyModal();
  });
  $('#btnCloseApiKeyModal')?.addEventListener('click', hideApiKeyModal);
  $('#btnSkipApiKeyModal')?.addEventListener('click', hideApiKeyModal);
  $('#apiKeyModal')?.addEventListener('click', (e) => {
    if (e.target && e.target.id === 'apiKeyModal') hideApiKeyModal(); // bấm ra ngoài = bỏ qua
  });

  // Mục "Khóa API Studio" trong panel Cài đặt
  $('#btnSaveStudioApiKey')?.addEventListener('click', () => {
    const input = $('#studioApiKeyInput');
    const v = (input ? input.value : '').trim();
    if (!v) {
      let saved = '';
      try { saved = localStorage.getItem('ams-api-key') || ''; } catch (e) { /* bỏ qua */ }
      if (saved) { if (input) input.value = ''; refreshStudioApiKeyStatus(); return; } // giữ nguyên key đã lưu
      const el = $('#studioApiKeyStatus');
      if (el) el.textContent = 'Chưa nhập key để lưu.';
      return;
    }
    if (saveStudioApiKey(v) && input) input.value = '';
  });
  $('#btnClearStudioApiKey')?.addEventListener('click', () => {
    try { localStorage.removeItem('ams-api-key'); } catch (e) { /* bỏ qua */ }
    const input = $('#studioApiKeyInput');
    if (input) input.value = '';
    if (typeof window.resetApiKeyModalFlag === 'function') window.resetApiKeyModalFlag();
    refreshStudioApiKeyStatus();
  });
  /* ── Batch Matrix: thu gọn thành dải tiến độ compact ───────────────────
     Khi job có video cuối, app.js (updateMonitor) tự gọi setTaskMatrixCollapsed(true).
     Mặc định mở đầy đủ lúc đang render; user mở lại bằng nút toggle ở header
     của card matrix. Dải compact đọc data-status live của từng .task-cell. */
  let taskMatrixCollapsed = false;
  function buildTaskMatrixCompact() {
    const strip = $('#taskMatrixCompact');
    const matrix = $('#taskMatrix');
    if (!strip || !matrix) return;
    const cells = $$('.task-cell', matrix);
    if (!cells.length) { strip.innerHTML = ''; return; }
    const iconFor = (st) => ({ done: '✅', error: '❌', running: '▶️', queued: '⌛' }[st] || '⌛');
    const done = cells.filter((c) => (c.dataset.status || '') === 'done').length;
    strip.innerHTML =
      `<span class="tm-compact-summary">Đã xong ${done}/${cells.length} cảnh</span>` +
      cells.map((c, i) => {
        const st = c.dataset.status || 'queued';
        const title = (c.querySelector('.cell-title')?.textContent || `Cảnh ${i + 1}`).trim();
        return `<span class="tm-compact-pill tm-${st}" title="${title} — ${st}">${iconFor(st)} ${title}</span>`;
      }).join('') +
      `<button type="button" class="btn btn-ghost btn-xs" onclick="toggleTaskMatrix()">🔼 Mở rộng xem từng cảnh</button>`;
  }
  function setTaskMatrixCollapsed(collapsed) {
    const matrix = $('#taskMatrix');
    const strip = $('#taskMatrixCompact');
    const btn = $('#btnToggleTaskMatrix');
    if (!matrix || !strip) return;
    taskMatrixCollapsed = !!collapsed;
    matrix.style.display = taskMatrixCollapsed ? 'none' : '';
    strip.style.display = taskMatrixCollapsed ? 'flex' : 'none';
    if (taskMatrixCollapsed) buildTaskMatrixCompact();
    if (btn) btn.textContent = taskMatrixCollapsed ? '🔼 Mở rộng' : '🔽 Thu gọn';
  }
  window.setTaskMatrixCollapsed = setTaskMatrixCollapsed;
  window.toggleTaskMatrix = function () { setTaskMatrixCollapsed(!taskMatrixCollapsed); };
  // Cell đổi trạng thái trong lúc đang thu gọn -> vẽ lại dải compact (debounce).
  const _matrixEl = $('#taskMatrix');
  if (_matrixEl && 'MutationObserver' in window) {
    let _tmTimer = null;
    new MutationObserver(() => {
      if (!taskMatrixCollapsed) return;
      clearTimeout(_tmTimer);
      _tmTimer = setTimeout(buildTaskMatrixCompact, 300);
    }).observe(_matrixEl, { attributes: true, subtree: true, attributeFilter: ['data-status'] });
  }

  refreshStudioApiKeyStatus();

  /* ── Timeline Editor: kéo-thả + xem trước + render lại từng shot ────────
     - Kéo-thả: HTML5 DnD có sẵn ở app.js (chỉ đổi local). Ở đây bọc lại để
       (a) chặn khi job đang chạy, (b) hiện vạch chỉ vị trí thả, (c) lưu thứ
       tự mới xuống server qua POST /scenes/reorder, lỗi thì hoàn lại.
     - Timeline strip + xem trước: vẽ từ mảng scenes (app.js), không sửa
       renderScenes gốc — chỉ bọc thêm sau mỗi lần render.
     - Render lại từng shot: dialog sửa prompt, POST /scenes/{idx}/rerender,
       poll nhẹ trạng thái job, so sánh A/B bản mới/bản cũ (previous-video).
     - Map vị trí local -> chỉ số server: server đánh index = vị trí + 1 lúc
       tạo job và đánh lại sau mỗi lần reorder đã lưu, nên vị trí hiện tại
       luôn tương ứng chỉ số server (khi job đã đồng bộ). */
  const TL = window.TimelineEditor = (function () {
    const escHtml = (v) => String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    const toast = (m, t) => {
      if (typeof window.showToast === 'function') window.showToast(m, t || 'info');
      else if (t === 'error') alert(m);
    };
    const api = (u, o) => (typeof window.apiFetch === 'function'
      ? window.apiFetch(u, o) : window.fetch(u, o));
    const listEl = () => document.getElementById('sceneList');
    const serverIdx = (localId) => {
      const i = scenes.findIndex((s) => s.id === localId);
      return i < 0 ? null : i + 1;
    };
    const totalDuration = () => scenes.reduce((a, s) => a + (parseInt(s.duration, 10) || 5), 0);

    const T = {
      selectedId: null,
      jobRunning: false,
      finalStale: false,
      jobScenes: {},          // server scenes {idx: {...}} từ lần poll gần nhất
      dragId: null,
      dropTargetId: null,
      dropBefore: true,
      rerenderTargetId: null,
      pollTimer: null,
      lastJobId: null,

      snapshotOrder() {
        return scenes.map((s, i) => ({ id: s.id, srv: i + 1 }));
      },

      /* ── Timeline strip + xem trước ── */
      ensureStrip() {
        const list = listEl();
        if (!list || document.getElementById('tlStripWrap')) return;
        const wrap = document.createElement('div');
        wrap.id = 'tlStripWrap';
        wrap.innerHTML =
          '<div class="tl-strip-head"><span id="tlStripTotal"></span>' +
          '<span id="tlStripStale" class="tl-stale-badge" style="display:none;">⚠️ Phim đã ghép cũ — cần ghép lại</span>' +
          '<span id="tlStripLock" class="tl-lock-badge" style="display:none;">🔒 Job đang render — tạm khóa đổi thứ tự</span></div>' +
          '<div class="tl-strip" id="tlStrip" title="Bấm vào ô để xem trước cảnh đó"></div>' +
          '<div id="tlPreviewBox" class="tl-preview-box" style="display:none;"></div>';
        list.parentNode.insertBefore(wrap, list);
      },
      renderStrip() {
        this.ensureStrip();
        const strip = document.getElementById('tlStrip');
        const totalEl = document.getElementById('tlStripTotal');
        const staleEl = document.getElementById('tlStripStale');
        const lockEl = document.getElementById('tlStripLock');
        const list = listEl();
        if (!strip) return;
        const total = totalDuration();
        if (totalEl) totalEl.textContent = `⏱️ Timeline: ${scenes.length} cảnh · tổng ${total}s — kéo ⠿ để đổi thứ tự, bấm ô để xem trước`;
        if (staleEl) staleEl.style.display = this.finalStale ? '' : 'none';
        if (lockEl) lockEl.style.display = this.jobRunning ? '' : 'none';
        if (list) list.classList.toggle('tl-locked', !!this.jobRunning);
        if (!scenes.length) { strip.innerHTML = '<div class="tl-strip-empty">Chưa có cảnh nào — bấm “＋ Thêm cảnh”.</div>'; return; }
        strip.innerHTML = scenes.map((s, i) => {
          const dur = parseInt(s.duration, 10) || 5;
          const st = (this.jobScenes[String(i + 1)] || {}).status || '';
          const cls = 'tl-cell' + (s.id === this.selectedId ? ' is-selected' : '')
            + (st === 'done' ? ' is-done' : st === 'error' ? ' is-error' : st === 'running' ? ' is-running' : '');
          return `<div class="${cls}" data-scene-id="${s.id}" style="flex-grow:${dur}" ` +
            `title="Cảnh ${i + 1} — ${escHtml(s.title || '')} (${dur}s)${st ? ' · ' + st : ''}">` +
            `<span class="tl-cell-num">${i + 1}</span><span class="tl-cell-dur">${dur}s</span></div>`;
        }).join('');
      },
      selectScene(localId, scroll) {
        this.selectedId = localId;
        this.renderStrip();
        this.renderPreview();
        if (scroll) {
          const el = document.getElementById(`scene-${localId}`);
          if (el) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
      },
      renderPreview() {
        const box = document.getElementById('tlPreviewBox');
        if (!box) return;
        const s = scenes.find((x) => x.id === this.selectedId);
        if (!s) { box.style.display = 'none'; box.innerHTML = ''; return; }
        const idx = serverIdx(s.id);
        const srv = (idx != null && this.jobScenes[String(idx)]) || {};
        const status = srv.status ? ` · ${srv.status}${srv.msg ? ' — ' + escHtml(srv.msg) : ''}` : '';
        const head = `<div class="tl-preview-head"><b>👁️ Xem trước — Cảnh ${idx} · ${escHtml(s.title || '')}</b>` +
          `<span class="tl-preview-meta">${parseInt(s.duration, 10) || 5}s${status}</span>` +
          `<button type="button" class="btn btn-ghost btn-sm" onclick="window.TimelineEditor.closePreview()">✕</button></div>`;
        box.style.display = 'block';
        if (activeJobId && idx != null && (srv.status === 'done' || srv.video_url)) {
          const vurl = `/api/jobs/${activeJobId}/scenes/${idx}/video`;
          box.innerHTML = head + `<video controls preload="metadata" src="${vurl}" class="tl-preview-video"></video>`;
          const v = box.querySelector('video');
          if (v) v.addEventListener('error', () => this.renderPreviewFallback(s, idx));
        } else if (s.keyframe_url) {
          box.innerHTML = head + `<img loading="lazy" decoding="async" src="${escHtml(s.keyframe_url)}" class="tl-preview-img" alt="Keyframe cảnh ${idx}">` +
            `<div class="tl-preview-note">🖼️ Keyframe xem trước — clip chưa render xong.</div>`;
        } else {
          this.renderPreviewFallback(s, idx, head);
        }
      },
      renderPreviewFallback(s, idx, headHtml) {
        const box = document.getElementById('tlPreviewBox');
        if (!box) return;
        const head = headHtml || box.querySelector('.tl-preview-head')?.outerHTML || '';
        box.innerHTML = head +
          `<div class="tl-preview-empty">Chưa có clip để xem.${s.keyframe_url ? '' : ' Nhập prompt rồi bấm “Bắt đầu Sản xuất” để render.'}</div>` +
          `<div class="tl-preview-prompt"><b>Prompt:</b> ${escHtml(s.prompt || '(trống)')}</div>`;
      },
      closePreview() {
        this.selectedId = null;
        const box = document.getElementById('tlPreviewBox');
        if (box) { box.style.display = 'none'; box.innerHTML = ''; }
        this.renderStrip();
      },
      afterRender() {
        if (this.selectedId != null && !scenes.some((s) => s.id === this.selectedId)) this.selectedId = null;
        this.renderStrip();
      },

      /* ── Lưu thứ tự mới xuống server (hoàn lại khi lỗi) ── */
      async persistReorder(pre) {
        if (!activeJobId) return; // chưa có job: chỉ đổi local
        const order = scenes.map((s) => {
          const p = pre.find((x) => x.id === s.id);
          return p ? p.srv : null;
        });
        if (order.includes(null) || order.length !== pre.length) return;
        try {
          const res = await api(`/api/jobs/${activeJobId}/scenes/reorder`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ order }),
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error((data && data.error) || ('HTTP ' + res.status));
          this.finalStale = !!data.final_stale;
          this.renderStrip();
          toast('✅ Đã lưu thứ tự mới lên server.');
        } catch (err) {
          const oldIds = pre.map((p) => p.id);
          scenes.sort((a, b) => oldIds.indexOf(a.id) - oldIds.indexOf(b.id));
          renderScenes();
          toast('❌ Không lưu được thứ tự mới: ' + err.message + ' — đã hoàn lại thứ tự cũ.', 'error');
        }
      },

      /* ── Poll trạng thái job (4s): khóa kéo-thả khi running, đồng bộ
         previous_clip / cờ rerendering / final_stale về card local ── */
      startJobPoll() {
        this.stopJobPoll();
        this.refreshJob();
        this.pollTimer = setInterval(() => this.refreshJob(), 4000);
      },
      stopJobPoll() {
        if (this.pollTimer) { clearInterval(this.pollTimer); this.pollTimer = null; }
        this.jobRunning = false;
        this.jobScenes = {};
      },
      async refreshJob() {
        if (!activeJobId || document.hidden) return;
        let data = null;
        try {
          const res = await api(`/api/jobs/${activeJobId}`);
          if (!res.ok) return;
          data = await res.json();
        } catch { return; }
        const wasRunning = this.jobRunning;
        this.jobRunning = data.status === 'running' || !!data.is_retrying;
        this.finalStale = !!data.final_stale;
        this.jobScenes = data.scenes || {};
        let changed = false;
        Object.entries(this.jobScenes).forEach(([idxStr, entry]) => {
          const idx = parseInt(idxStr, 10);
          const s = scenes[idx - 1];
          if (!s || !entry) return;
          const prevJson = JSON.stringify(entry.previous_clip || null);
          if (prevJson !== JSON.stringify(s.previous_clip || null)) {
            s.previous_clip = entry.previous_clip || undefined;
            if (!entry.previous_clip) delete s.previous_clip;
            changed = true;
          }
          const rer = !!entry.rerendering;
          if (rer !== !!s._rerendering) {
            s._rerendering = rer;
            if (!rer && this.rerenderTargetId === s.id) {
              this.rerenderTargetId = null;
              toast(entry.status === 'done' ? `✅ Render lại Cảnh ${idx} xong — xem so sánh A/B trên card.` : `❌ Render lại Cảnh ${idx} lỗi: ${entry.msg || entry.error || ''}`, entry.status === 'done' ? 'info' : 'error');
            }
            changed = true;
          }
          if (changed && typeof updateSceneCard === 'function') updateSceneCard(s.id);
        });
        if (wasRunning !== this.jobRunning || changed) this.renderStrip();
      },

      /* ── Render lại 1 shot + so sánh A/B ── */
      openRerender(localId) {
        if (!activeJobId) {
          alert('Chưa có job — hãy bấm “Bắt đầu Sản xuất” trước, render lại chỉ dùng cho shot của job đã tạo.');
          return;
        }
        const s = scenes.find((x) => x.id === localId);
        if (!s) return;
        if (typeof syncScenesFromDOM === 'function') syncScenesFromDOM();
        this.rerenderTargetId = localId;
        const idx = serverIdx(localId);
        const info = document.getElementById('tlRerenderInfo');
        if (info) info.textContent = `Cảnh ${idx} — “${s.title || ''}” · Giọng: ${s.voice || 'mặc định toàn phim'} · Ảnh tham chiếu: ${s.reference_image ? 'đã khóa 🔒' : 'không có'}.`;
        const p = document.getElementById('tlRerenderPrompt');
        if (p) p.value = s.prompt || '';
        const v = document.getElementById('tlRerenderVoiceover');
        if (v) v.value = s.voiceover || '';
        const d = document.getElementById('tlRerenderDuration');
        if (d) d.value = '';
        const err = document.getElementById('tlRerenderError');
        if (err) err.textContent = '';
        const m = document.getElementById('tlRerenderModal');
        if (m) { m.style.display = 'flex'; setTimeout(() => p && p.focus(), 50); }
      },
      hideRerenderModal() {
        const m = document.getElementById('tlRerenderModal');
        if (m) m.style.display = 'none';
      },
      async confirmRerender() {
        const localId = this.rerenderTargetId;
        const s = scenes.find((x) => x.id === localId);
        const errEl = document.getElementById('tlRerenderError');
        if (!s || !activeJobId) return;
        const idx = serverIdx(localId);
        const prompt = (document.getElementById('tlRerenderPrompt')?.value || '').trim();
        const voiceover = (document.getElementById('tlRerenderVoiceover')?.value || '').trim();
        const durVal = document.getElementById('tlRerenderDuration')?.value || '';
        const body = {};
        if (prompt && prompt !== s.prompt) body.prompt = prompt;
        if (voiceover !== (s.voiceover || '')) body.voiceover = voiceover;
        if (durVal) body.duration = parseInt(durVal, 10);
        try {
          const res = await api(`/api/jobs/${activeJobId}/scenes/${idx}/rerender`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error((data && data.error) || ('HTTP ' + res.status));
          if (body.prompt) s.prompt = body.prompt;
          if ('voiceover' in body) s.voiceover = body.voiceover;
          if (body.duration) s.duration = body.duration;
          if (data.previous_clip) s.previous_clip = data.previous_clip;
          s._rerendering = true;
          this.hideRerenderModal();
          if (typeof updateSceneCard === 'function') updateSceneCard(s.id);
          this.renderStrip();
          toast(`🔄 Đang render lại Cảnh ${idx}… xong sẽ hiện so sánh A/B trên card.`);
        } catch (err) {
          if (errEl) errEl.textContent = err.message;
        }
      },
      async keepNew(localId) {
        const idx = serverIdx(localId);
        const s = scenes.find((x) => x.id === localId);
        if (!s || !activeJobId || idx == null) return;
        try {
          const res = await api(`/api/jobs/${activeJobId}/scenes/${idx}/discard-previous`, { method: 'POST' });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error((data && data.error) || ('HTTP ' + res.status));
          delete s.previous_clip;
          if (typeof updateSceneCard === 'function') updateSceneCard(s.id);
          toast('✅ Đã giữ bản mới, xóa bản cũ.');
        } catch (err) { toast('❌ ' + err.message, 'error'); }
      },
      async restoreOld(localId) {
        const idx = serverIdx(localId);
        const s = scenes.find((x) => x.id === localId);
        if (!s || !activeJobId || idx == null) return;
        try {
          const res = await api(`/api/jobs/${activeJobId}/scenes/${idx}/restore-previous`, { method: 'POST' });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error((data && data.error) || ('HTTP ' + res.status));
          if (data.prompt != null) s.prompt = data.prompt;
          if (data.previous_clip) s.previous_clip = data.previous_clip;
          else delete s.previous_clip;
          if (typeof updateSceneCard === 'function') updateSceneCard(s.id);
          toast('↩️ Đã quay lại bản cũ (bản mới vừa render thành bản cũ để đổi lại được).');
        } catch (err) { toast('❌ ' + err.message, 'error'); }
      },

      /* ── HTML chèn vào card (gọi từ renderSceneItemHtml trong app.js) ── */
      headerButtonsHtml(s, i) {
        return `<button type="button" class="btn btn-ghost btn-sm tl-rerender-btn" title="Render lại shot này (giữ nguyên nhân vật/giọng, clip cũ được lưu để so sánh A/B)" onclick="window.TimelineEditor.openRerender(${s.id})">🔄</button>`;
      },
      cardExtraHtml(s, i) {
        if (s._rerendering) {
          return `<div class="tl-rerender-status">🔄 Đang render lại shot này… xong sẽ hiện so sánh A/B bên dưới.</div>`;
        }
        if (s.previous_clip && activeJobId) {
          const idx = i + 1;
          const oldPrompt = escHtml((s.previous_clip.prompt || '').slice(0, 80));
          return `<div class="tl-ab-panel">
            <div class="tl-ab-title">🆚 So sánh A/B — bản mới vs bản cũ <span class="tl-ab-note">Prompt cũ: ${oldPrompt}</span></div>
            <div class="tl-ab-grid">
              <div class="tl-ab-cell"><div class="tl-ab-label">✨ Bản mới</div>
                <video controls preload="metadata" src="/api/jobs/${activeJobId}/scenes/${idx}/video" class="tl-ab-video" onerror="this.style.display='none';this.nextElementSibling.style.display='block'"></video>
                <div class="tl-ab-novid" style="display:none;">Không xem được bản mới.</div></div>
              <div class="tl-ab-cell"><div class="tl-ab-label">📼 Bản cũ</div>
                <video controls preload="metadata" src="/api/jobs/${activeJobId}/scenes/${idx}/previous-video" class="tl-ab-video" onerror="this.style.display='none';this.nextElementSibling.style.display='block'"></video>
                <div class="tl-ab-novid" style="display:none;">Không xem được bản cũ.</div></div>
            </div>
            <div class="tl-ab-actions">
              <button type="button" class="btn btn-primary btn-sm" onclick="window.TimelineEditor.keepNew(${s.id})">✅ Giữ bản mới</button>
              <button type="button" class="btn btn-ghost btn-sm" onclick="window.TimelineEditor.restoreOld(${s.id})">↩️ Quay lại bản cũ</button>
            </div>
          </div>`;
        }
        return '';
      },
    };

    /* ── Bọc renderScenes: vẽ lại strip sau mỗi lần render danh sách ── */
    if (typeof window.renderScenes === 'function') {
      const _origRenderScenes = window.renderScenes;
      window.renderScenes = function () {
        const r = _origRenderScenes.apply(this, arguments);
        T.afterRender();
        return r;
      };
    }
    T.ensureStrip();
    T.renderStrip();

    /* ── Bọc moveScene + handleSceneDropReorder: lưu thứ tự lên server ── */
    if (typeof window.moveScene === 'function') {
      const _origMove = window.moveScene;
      window.moveScene = function (id, dir) {
        if (T.jobRunning) { toast('Job đang render — không đổi được thứ tự lúc này.'); return; }
        const pre = T.snapshotOrder();
        _origMove.apply(this, arguments);
        T.persistReorder(pre);
      };
    }
    if (typeof window.handleSceneDropReorder === 'function') {
      window.handleSceneDropReorder = function (e, targetId) {
        if (e && e.preventDefault) e.preventDefault();
        document.querySelectorAll('.scene-item').forEach((el) =>
          el.classList.remove('dragging', 'tl-drop-before', 'tl-drop-after'));
        const fromId = T.dragId;
        T.dragId = null; T.dropTargetId = null;
        try { draggedSceneId = null; } catch (_) { /* global let ở app.js */ }
        if (fromId == null || fromId === targetId) return;
        if (T.jobRunning) { toast('Job đang render — không đổi được thứ tự lúc này.'); return; }
        const pre = T.snapshotOrder();
        if (typeof syncScenesFromDOM === 'function') syncScenesFromDOM();
        const fromIdx = scenes.findIndex((s) => s.id === fromId);
        if (fromIdx < 0) return;
        const [moved] = scenes.splice(fromIdx, 1);
        let t = scenes.findIndex((s) => s.id === targetId);
        if (t < 0) { scenes.splice(fromIdx, 0, moved); return; }
        const insertAt = T.dropBefore ? t : t + 1;
        scenes.splice(insertAt, 0, moved);
        T.dropBefore = true;
        renderScenes();
        toast(`🔄 Đã đổi Cảnh ${fromIdx + 1} sang vị trí Cảnh ${insertAt + 1}!`);
        T.persistReorder(pre);
      };
    }

    /* ── Delegation trên #sceneList: chặn kéo khi running, vạch vị trí thả,
         bấm vào card để xem trước ── */
    const list = listEl();
    if (list) {
      list.addEventListener('dragstart', (e) => {
        const card = e.target.closest('.scene-item');
        if (!card) return;
        if (T.jobRunning) {
          e.preventDefault();
          toast('Job đang render — không đổi được thứ tự lúc này.');
          return;
        }
        T.dragId = parseInt(card.id.replace('scene-', ''), 10);
      }, true);
      list.addEventListener('dragover', (e) => {
        const card = e.target.closest('.scene-item');
        document.querySelectorAll('.scene-item.tl-drop-before, .scene-item.tl-drop-after')
          .forEach((el) => el.classList.remove('tl-drop-before', 'tl-drop-after'));
        if (!card || T.dragId == null) return;
        e.preventDefault();
        const r = card.getBoundingClientRect();
        const before = (e.clientY - r.top) < r.height / 2;
        card.classList.add(before ? 'tl-drop-before' : 'tl-drop-after');
        T.dropTargetId = parseInt(card.id.replace('scene-', ''), 10);
        T.dropBefore = before;
      });
      list.addEventListener('dragleave', (e) => {
        if (e.target === list) {
          document.querySelectorAll('.scene-item.tl-drop-before, .scene-item.tl-drop-after')
            .forEach((el) => el.classList.remove('tl-drop-before', 'tl-drop-after'));
        }
      });
      list.addEventListener('dragend', () => {
        document.querySelectorAll('.scene-item.tl-drop-before, .scene-item.tl-drop-after')
          .forEach((el) => el.classList.remove('tl-drop-before', 'tl-drop-after'));
        T.dragId = null; T.dropTargetId = null;
      });
      // Bấm vào card (không phải input/nút) -> xem trước cảnh đó.
      list.addEventListener('click', (e) => {
        if (e.target.closest('input, textarea, select, button, a, video')) return;
        const card = e.target.closest('.scene-item');
        if (!card) return;
        T.selectScene(parseInt(card.id.replace('scene-', ''), 10), false);
      });
      // Bấm ô trên strip -> xem trước + cuộn tới card.
      document.getElementById('tlStrip')?.addEventListener('click', (e) => {
        const cell = e.target.closest('.tl-cell');
        if (!cell) return;
        T.selectScene(parseInt(cell.dataset.sceneId, 10), true);
      });
    }

    /* ── Theo dõi activeJobId (đổi khi tạo job / mở project): bật/tắt poll ── */
    setInterval(() => {
      if (typeof activeJobId === 'undefined') return;
      if (activeJobId && activeJobId !== T.lastJobId) {
        T.lastJobId = activeJobId;
        T.startJobPoll();
      } else if (!activeJobId && T.lastJobId) {
        T.lastJobId = null;
        T.stopJobPoll();
        T.finalStale = false;
        T.renderStrip();
      }
    }, 2000);

    /* ── Modal render lại ── */
    document.getElementById('btnConfirmTlRerender')?.addEventListener('click', () => T.confirmRerender());
    document.getElementById('btnCancelTlRerender')?.addEventListener('click', () => T.hideRerenderModal());
    document.getElementById('btnCloseTlRerenderModal')?.addEventListener('click', () => T.hideRerenderModal());
    document.getElementById('tlRerenderModal')?.addEventListener('click', (e) => {
      if (e.target && e.target.id === 'tlRerenderModal') T.hideRerenderModal();
    });
    document.getElementById('tlRerenderPrompt')?.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') T.confirmRerender();
    });

    return T;
  })();

})();
