const studioElement = id => document.getElementById(id);
const studioStatuses = { draft: 'Bản nháp', queued: 'Đang chờ', running: 'Đang dựng', failed: 'Có lỗi', completed: 'Hoàn tất', done: 'Có clip' };
const captionLabels = { cinema: 'Tối giản', headline: 'Chữ to nổi bật', boxed: 'Hộp chữ cổ điển', karaoke: 'Karaoke', word_box: 'Ô màu theo chữ', neon: 'Neon phát sáng' };
const studioState = { videos: [], selected: '', stamp: '', request: 0, filter: 'all' };

function setAffiliateStep(step) {
  studioElement('affProductStep').hidden = step !== 'product';
  studioElement('affReviewStep').hidden = step !== 'review';
  document.querySelectorAll('[data-aff-step]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.affStep === step)));
}
document.querySelectorAll('[data-aff-step]').forEach(b => b.onclick = () => setAffiliateStep(b.dataset.affStep));

function captionSettings(scope) {
  const box = document.querySelector('[data-caption-picker="' + scope + '"]');
  return { subtitle_style: box.querySelector('[name=style]').value, subtitle_position: box.querySelector('[name=position]').value };
}
for (const box of document.querySelectorAll('[data-caption-picker]')) {
  box.innerHTML = '<details class="studio-details"><summary>Phong cách phụ đề</summary><div class="caption-layout"><div>' +
    '<label class="studio-field">Kiểu chữ<select name="style"></select></label><label class="studio-field">Vị trí<select name="position"><option value="bottom">Dưới</option><option value="middle">Giữa</option><option value="top">Trên</option></select></label></div>' +
    '<div class="caption-preview" aria-label="Xem trước phụ đề"><span>Một <b>khoảnh khắc</b> đáng nhớ</span></div></div></details>';
  const select = box.querySelector('[name=style]');
  Object.entries(captionLabels).forEach(([key, label]) => select.add(new Option(label, key)));
  const update = () => {
    const prefs = captionSettings(box.dataset.captionPicker);
    const preview = box.querySelector('.caption-preview');
    preview.dataset.style = prefs.subtitle_style;
    preview.dataset.position = prefs.subtitle_position;
  };
  box.addEventListener('change', update);
  update();
}

function selectedStudioCast(scope) {
  return [...document.querySelectorAll('[data-cast-picker="' + scope + '"] input:checked')].map(el => el.value);
}
async function loadSavedStories() {
  try {
    const response = await apiFetch('/api/story/projects');
    if (!response.ok) return;
    const { projects } = await response.json();
    const select = studioElement('storySavedProjects');
    select.replaceChildren(new Option('Chọn truyện đã nhập', ''), ...projects.map(p => new Option(p.title, p.id)));
    select.value = window.storyState.projectId || '';
    studioElement('storySavedLabel').hidden = !projects.length;
  } catch { /* Source import and the open project remain available offline. */ }
}
async function loadStudioCast(scope) {
  const box = document.querySelector('[data-cast-picker="' + scope + '"]');
  const request = String(Number(box.dataset.request || 0) + 1);
  box.dataset.request = request;
  try {
    const response = await apiFetch('/api/characters');
    if (!response.ok) throw new Error();
    const chars = await response.json();
    if (request !== box.dataset.request) return;
    const selected = new Set(selectedStudioCast(scope));
    box.innerHTML = '<details class="studio-details"><summary>Nhân vật tham chiếu</summary><p>Chọn hồ sơ rồi dùng tên hoặc bí danh trong cảnh. Muse nhận ảnh của nhân vật được nhắc đến.</p><div class="studio-cast"></div><button type="button" class="btn btn-ghost btn-sm">Quản lý hồ sơ nhân vật</button></details>';
    const list = box.querySelector('.studio-cast');
    if (!chars.length) list.textContent = 'Chưa có hồ sơ nhân vật.';
    chars.forEach(c => {
      const label = document.createElement('label');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.value = c.id;
      input.checked = selected.has(c.id);
      label.append(input);
      if (c.avatar) {
        const portrait = document.createElement('img');
        portrait.src = c.avatar;
        portrait.alt = '';
        portrait.loading = 'lazy';
        label.append(portrait);
      }
      label.append(document.createTextNode(c.name));
      list.append(label);
    });
    box.querySelector('button').onclick = () => showPanel('characters');
  } catch {
    if (!box.querySelector('details')) box.textContent = 'Chưa tải được danh sách nhân vật.';
  }
}

function addStudioBeat(scene) {
  const beats = studioElement('msBeats');
  if (beats.children.length >= 40) return showToast('Tối đa 40 cảnh mỗi phim.', 'error');
  const row = document.createElement('fieldset');
  row.innerHTML = '<legend></legend><label class="studio-field">Hình ảnh và hành động<textarea required maxlength="3000" name="prompt" rows="2"></textarea></label>' +
    '<label class="studio-field">Lời kể<textarea required maxlength="3000" name="voiceover" rows="2"></textarea></label>' +
    '<div class="studio-actions"><label>Thời lượng <select name="duration"><option>5</option><option selected>6</option><option>8</option><option>10</option><option>12</option></select> giây</label><button type="button" class="btn btn-ghost btn-sm">Xóa cảnh</button></div>';
  beats.append(row);
  if (scene) {
    row.querySelector('[name=prompt]').value = scene.direction || scene.prompt;
    row.querySelector('[name=voiceover]').value = scene.voiceover;
    row.querySelector('[name=duration]').value = scene.duration;
  }
  function renumber() {
    [...beats.children].forEach((el, i) => { el.querySelector('legend').textContent = 'Cảnh ' + (i + 1); });
  }
  row.querySelector('button').onclick = () => { if (beats.children.length > 1) { row.remove(); renumber(); } };
  renumber();
}

async function openMuseStudio() {
  const styles = studioElement('msStyle');
  if (!styles.options.length) {
    for (const option of studioElement('storyStyleSelect').options) styles.add(new Option(option.text, option.value));
  }
  if (VOICES.length && !studioElement('msDraft').matches(':focus-within')) {
    const choice = studioElement('msVoice');
    choice.innerHTML = voiceSelectHtml(choice.value, false);
  }
  loadStudioCast('studio');
  loadStudioCatalog();
  await refreshMuseBoard();
}
async function refreshMuseBoard() {
  const request = ++studioState.request;
  try {
    const response = await apiFetch('/api/batch-videos/queue?sort=newest');
    if (!response.ok) throw new Error('Chưa tải được danh sách phim.');
    const data = await response.json();
    if (request !== studioState.request) return;
    studioState.videos = data.videos.filter(v => v.story_source === 'story' || v.studio_source === 'muse_studio');
    const selector = studioElement('msProject');
    const previous = studioState.selected;
    selector.replaceChildren(...studioState.videos.map(v => new Option(v.title, v.id)));
    if (studioState.videos.some(v => v.id === previous)) selector.value = previous;
    studioState.selected = selector.value;
    drawMuseBoard();
  } catch (error) { showToast(error.message, 'error'); }
}
async function editStudioDraft(v) {
    const form = studioElement('msDraft');
    form.dataset.editId = v.id;
    form.querySelector('h3').textContent = 'Sửa bản nháp';
    studioElement('msTitle').value = v.title;
    studioElement('msRatio').value = v.aspect_ratio;
    studioElement('msStyle').value = v.visual_style;
    studioElement('msVoice').value = v.voice;
    const captions = form.querySelector('[data-caption-picker]');
    captions.querySelector('[name=style]').value = v.subtitle_style || 'cinema';
    captions.querySelector('[name=position]').value = v.subtitle_position || 'bottom';
    captions.dispatchEvent(new Event('change'));
    studioElement('msBeats').replaceChildren();
    v.scenes.forEach(addStudioBeat);
    await loadStudioCast('studio');
    const ids = new Set((v.cast || []).map(c => c.id));
    form.querySelectorAll('.studio-cast input').forEach(input => { input.checked = ids.has(input.value); });
    form.hidden = false;
    studioElement('msTitle').focus();
}

function drawMuseBoard() {
  const board = studioElement('msBoard');
  const v = studioState.videos.find(item => item.id === studioState.selected);
  if (!v) {
    board.innerHTML = '<div class="studio-empty"><h3>Chưa có phim</h3><p>Chọn Phim mới để viết cảnh đầu tiên hoặc dựng một chương từ Truyện thành video.</p></div>';
    studioState.stamp = '';
    return;
  }
  const stamp = JSON.stringify([v.id, v.status, v.current_step, v.updated_at, studioState.filter]);
  if (stamp === studioState.stamp) return;
  studioState.stamp = stamp;
  const scenes = v.scenes || [];
  if (v.guided) { drawGuidedBoard(board, v); return; }
  const ready = s => s.video_status === 'done' || v.status === 'completed';
  const completed = scenes.filter(ready).length;
  const url = '/api/batch-videos/' + encodeURIComponent(v.id);
  const controls = '<div class="studio-actions">' +
    (['draft', 'failed'].includes(v.status) ? '<button type="button" class="btn btn-primary" id="msRun">' + (v.status === 'failed' ? 'Dựng tiếp từ cảnh lỗi' : 'Dựng phim bằng Muse') + '</button>' : '') +
    (v.status === 'draft' && v.studio_source ? '<button type="button" class="btn btn-ghost" id="msEdit">Sửa bản nháp</button>' : '') +
    (v.status === 'completed' ? '<a class="btn btn-primary" href="' + url + '/video" download>Tải phim</a><a class="btn btn-ghost" href="' + url + '/subtitles" download>Tải SRT</a>' : '') +
    '<button type="button" class="btn btn-ghost" data-rebuild>Dựng lại có duyệt ảnh</button><button type="button" class="btn btn-ghost" id="msShotDesk">Biên tập trong Shot Desk</button></div>';
  // Retain loaded players during progress updates.
  const players = new Map([...board.querySelectorAll('video')].map(el => [el.getAttribute('src'), el]));
  const scope = { all: 'Tất cả', done: 'Có clip', approved: 'Đã duyệt', failed: 'Có lỗi' };
  const filtered = scenes.filter(s => studioState.filter === 'all' || (studioState.filter === 'done' ? ready(s) : studioState.filter === 'approved' ? s.approved : s.video_status === 'failed'));
  const clipGrid = filtered.map(s => {
    const src = '/api/muse-studio/' + encodeURIComponent(v.id) + '/scenes/' + Number(s.index) + '/clip';
    const status = s.approved ? 'Đã duyệt' : ready(s) ? 'Có clip' : studioStatuses[s.video_status] || 'Chưa dựng';
    return '<article class="studio-shot"><header><strong>Cảnh ' + Number(s.index) + '</strong><small>' + status + '</small></header>' +
      (ready(s) ? '<video controls preload="metadata" playsinline src="' + src + '"></video>' : '') +
      '<p>' + esc(s.voiceover || '') + '</p><details><summary>' + (s.used_prompt ? 'Prompt đã gửi' : 'Mô tả cảnh') + '</summary><p>' + esc(s.used_prompt || s.prompt || '') + '</p></details>' +
      (v.status === 'completed' ? '<button type="button" class="btn btn-ghost btn-sm" data-review="' + Number(s.index) + '" aria-pressed="' + !!s.approved + '">' + (s.approved ? 'Bỏ duyệt' : 'Duyệt cảnh') + '</button>' : '') +
      (s.error ? '<p class="studio-error">' + esc(s.error) + '</p>' : '') + '</article>';
  }).join('');
  board.innerHTML = '<div class="card studio-overview"><h3>' + esc(v.title) + '</h3><p>' +
    esc(studioStatuses[v.status] || v.status) + ' · ' + completed + '/' + scenes.length + ' cảnh có clip · ' + esc(v.aspect_ratio) + ' · ' +
    (v.render_mode === 'ai_video' ? 'Video AI' : 'Ảnh minh họa') + '</p>' +
    (v.status === 'running' ? '<progress max="100" value="' + Number(v.progress || 0) + '"></progress><p>' + esc(v.current_step) + '</p>' : '') +
    (v.error ? '<p class="studio-error">' + esc(v.error) + '</p>' : '') + controls +
    (v.status === 'completed' ? '<video class="studio-final" controls preload="metadata" src="' + url + '/video"></video>' : '') +
    '</div><div class="studio-tabs">' + Object.entries(scope).map(([key, label]) => '<button data-ms-filter="' + key + '" aria-pressed="' + (key === studioState.filter) + '">' + label + '</button>').join('') +
    '</div><div class="studio-shots">' + (clipGrid || '<p>Chưa có cảnh trong bộ lọc này.</p>') + '</div>';
  board.querySelectorAll('video').forEach(el => { const old = players.get(el.getAttribute('src')); if (old) el.replaceWith(old); });
  board.querySelectorAll('[data-ms-filter]').forEach(btn => btn.onclick = () => { studioState.filter = btn.dataset.msFilter; drawMuseBoard(); });
  board.querySelectorAll('[data-review]').forEach(btn => btn.onclick = async () => {
    btn.disabled = true;
    try {
      const response = await apiFetch('/api/muse-studio/' + encodeURIComponent(v.id) + '/scenes/' + btn.dataset.review + '/review', {
        method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({approved: btn.getAttribute('aria-pressed') !== 'true'})});
      if (!response.ok) throw new Error('Không lưu được trạng thái duyệt cảnh.');
      await refreshMuseBoard();
    } catch (error) { showToast(error.message, 'error'); }
    finally { btn.disabled = false; }
  });
  board.querySelector('[data-rebuild]').onclick=()=>copyToReview(v).catch(reportFailure);
  studioElement('msShotDesk').onclick = () => openCurrentBatchInShotDesk(v.id);
  const edit = studioElement('msEdit');
  if (edit) edit.onclick = () => editStudioDraft(v);
  const run = studioElement('msRun');
  if (run) run.onclick = async () => {
    run.disabled = true;
    try {
      const response = await apiFetch(url + '/run', { method: 'POST' });
      if (!response.ok) throw new Error('Không khởi động được bản dựng.');
      await refreshMuseBoard();
    } catch (e) { showToast(e.message, 'error'); }
    finally { run.disabled = false; }
  };
}
function newStudioDraft() {
  const form = studioElement('msDraft');
  if (form.dataset.editId) {
    delete form.dataset.editId;
    form.reset();
    form.querySelector('[data-caption-picker]').dispatchEvent(new Event('change'));
    studioElement('msBeats').replaceChildren();
  }
  form.querySelector('h3').textContent = 'Phim mới';
  form.hidden = false;
  if (!studioElement('msBeats').children.length) addStudioBeat();
  studioElement('msTitle').focus();
}
studioElement('msNew').onclick = () => switchEditorView('create');
studioElement('msClose').onclick = () => switchEditorView('films');
studioElement('msAdd').onclick = () => addStudioBeat();
studioElement('msRefresh').onclick = refreshMuseBoard;
studioElement('msProject').onchange = e => {
  studioState.selected = e.target.value;
  studioState.filter = 'all';
  drawMuseBoard();
};
studioElement('msDraft').onsubmit = async e => {
  e.preventDefault();
  const submit = e.currentTarget.querySelector('[type=submit]');
  submit.disabled = true;
  try {
    const scenes = [...studioElement('msBeats').children].map(row => ({
      prompt: row.querySelector('[name=prompt]').value.trim(), voiceover: row.querySelector('[name=voiceover]').value.trim(),
      duration: Number(row.querySelector('[name=duration]').value),
    }));
    const editId = studioElement('msDraft').dataset.editId;
    const endpoint = '/api/muse-studio/drafts' + (editId ? '/' + encodeURIComponent(editId) : '');
    const response = await apiFetch(endpoint, { method: editId ? 'PUT' : 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({guided: true, title: studioElement('msTitle').value.trim(), scenes, cast_ids: selectedStudioCast('studio'),
        visual_style: studioElement('msStyle').value, voice: studioElement('msVoice').value,
        aspect_ratio: studioElement('msRatio').value, ...captionSettings('studio')}) });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Kiểm tra tên phim và nội dung từng cảnh.');
    studioState.selected = data.id;
    studioState.filter = 'all';
    studioElement('msDraft').hidden = true;
    delete studioElement('msDraft').dataset.editId;
    switchEditorView('films');
    studioElement('msTitle').value = '';
    studioElement('msBeats').replaceChildren();
    await refreshMuseBoard();
  } catch (error) { showToast(error.message, 'error'); }
  finally { submit.disabled = false; }
};
studioElement('storyVideosList').addEventListener('click', e => {
  const btn = e.target.closest('[data-open-studio]');
  if (!btn) return;
  switchEditorView('films');
  studioState.selected = btn.dataset.openStudio;
  studioState.filter = 'all';
  showPanel('muse-studio');
});
let _musePollInFlight = false;
setInterval(async () => {
  if (_musePollInFlight || document.hidden || !studioElement('panel-muse-studio').classList.contains('active')) return;
  if (!studioState.videos.some(v => ['running', 'queued'].includes(v.status))) return;
  _musePollInFlight = true;
  try { await refreshMuseBoard(); } finally { _musePollInFlight = false; }
}, 5000);
