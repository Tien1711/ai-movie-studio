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
  refreshStudioApiKeyStatus();

})();
