(() => {
  const ROOT = '[data-hatch-composer-root]';
  const MESSAGE = '[data-message-item]';
  const ASSISTANT = MESSAGE + '[data-message-role="assistant"]';
  const USER = MESSAGE + '[data-message-role="user"]';
  const STOP = '[data-testid="hatch-composer-stop-button"]';
  const ERR = '[data-testid="assistant-response-error-notice"]';
  const APPROVAL = '[data-hatch-composer-approval-stack]';
  const tab = crypto.randomUUID();
  const delay = milliseconds => {
    if (milliseconds >= 200 && document.visibilityState !== 'visible') {
      return chrome.runtime.sendMessage({ type: 'sleep', ms: milliseconds })
        .then(res => { if (!res?.ok) return new Promise(r => setTimeout(r, milliseconds)); })
        .catch(() => new Promise(r => setTimeout(r, milliseconds)));
    }
    return new Promise(resolve => setTimeout(resolve, milliseconds));
  };
  const visible = node => !!node && node.getClientRects().length > 0;
  const all = selector => Array.from(document.querySelectorAll(selector));

  function editor() {
    const current = all(ROOT + ' textarea, ' + ROOT + ' [contenteditable="true"], [data-hatch-composer-prehydration-input]')
      .find(visible);
    return current && !current.disabled && !current.readOnly ? current : null;
  }

  async function waitFor(condition, deadline) {
    while (Date.now() < deadline && location.origin === 'https://muse.ai') {
      const value = condition();
      if (value) return value;
      await delay(200);
    }
    return null;
  }

  async function newConversation(deadline) {
    if (visible(document.querySelector(STOP))) throw new Error('Tab Muse đang có yêu cầu chạy.');
    const previous = all(MESSAGE).find(visible);
    const newChat = all('button, [role="button"]').find(button => {
      if (!visible(button)) return false;
      const labels = [
        button.getAttribute('aria-label'), button.title, button.innerText,
        document.getElementById(button.getAttribute('aria-describedby'))?.textContent,
      ];
      return labels.some(label => /^(new side chat|đoạn chat phụ mới|cuộc trò chuyện phụ mới)$/i.test((label || '').normalize('NFC').trim()));
    });
    if (newChat) {
      newChat.click();
      if (!await waitFor(() => editor() && (!previous || !visible(previous)), deadline)) {
        throw new Error('Muse chưa mở được cuộc trò chuyện mới.');
      }
    } else if (previous) {
      throw new Error('Không tìm thấy nút New side chat trong tab Muse.');
    }
  }

  async function openMainChat(deadline) {
    if (visible(document.querySelector(STOP))) throw new Error('Tab Muse đang có yêu cầu chạy.');
    if (location.pathname !== '/') {
      const row = await waitFor(() => all('[data-testid="hatch-thread-row"]').find(node =>
        visible(node) && (/^(main chat|chat chính|đoạn chat chính|trò chuyện với .+|chat with .+)$/i
          .test((node.innerText || '').normalize('NFC').trim())
          || node.parentElement?.querySelector(':scope > [data-testid="active-side-chat-load-more-sentinel"]')
            && node.parentElement.firstElementChild === node)), deadline);
      if (!row) throw new Error('Không tìm thấy chat chính trong thanh bên Muse.');
      const previous = all(MESSAGE).find(visible);
      row.click();
      if (!await waitFor(() => location.pathname === '/' && editor() && !visible(previous), deadline)) {
        throw new Error('Muse chưa mở được chat chính.');
      }
    }
    if (!await waitFor(editor, deadline)) throw new Error('Chat chính của Muse chưa sẵn sàng.');
    if (visible(document.querySelector(STOP))) throw new Error('Chat chính đang có yêu cầu chạy.');
  }

  function attach(files) {
    if (!files.length) return;
    const input = document.querySelector(ROOT + ' input[type="file"]');
    if (!input) throw new Error('Muse chưa sẵn sàng nhận ảnh tham chiếu.');
    const transfer = new DataTransfer();
    for (const { data, name, mimeType } of files) {
      const decoded = atob(data);
      const bytes = Uint8Array.from(decoded, char => char.charCodeAt(0));
      transfer.items.add(new File([bytes], name, { type: mimeType }));
    }
    input.files = transfer.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function fill(target, prompt) {
    target.focus();
    if (target instanceof HTMLTextAreaElement || target instanceof HTMLInputElement) {
      const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(target), 'value').set;
      setter.call(target, prompt);
      target.dispatchEvent(new InputEvent('input', { bubbles: true }));
      return;
    }
    const range = document.createRange();
    range.selectNodeContents(target);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    if (!document.execCommand('insertText', false, prompt)) {
      throw new Error('Không nhập được yêu cầu vào ô chat Muse.');
    }
  }

  function sendButton() {
    const buttons = all(ROOT + ' button');
    const enabled = button => visible(button) && !button.disabled && button.getAttribute('aria-disabled') !== 'true';
    const explicit = buttons.find(button => enabled(button)
      && (/^(hatch-composer-send-button|composer-send-button|send-button)$/i.test(button.dataset.testid || '')
      || /^(Send(?: message| prompt)?|Gửi(?: tin nhắn)?)$/i.test((button.getAttribute('aria-label') || button.title || '').trim())));
    if (explicit) return explicit;
    const action = document.querySelector('[data-hatch-composer-action-slot]');
    return Array.from(action?.querySelectorAll('button') || []).filter(enabled).at(-1) || null;
  }

  function latestMedia(elements) {
    return [...new Set(elements.flatMap(element => Array.from(
      element.querySelectorAll('a[href], img[src], video[src], source[src]'),
      node => node.href || node.currentSrc || node.src,
    )).filter(url => typeof url === 'string' && url.startsWith('https://muse.ai/files/')))].slice(0, 32);
  }

  const ATTACHMENT = '[data-testid^="hatch-chat-attachment-presentation-"]';
  const SHARE_EXTENSION = { video: /\.(mp4|webm)$/i, image: /\.(png|jpe?g|gif|webp)$/i };
  const MEDIA_CHUNK_SIZE = 256 * 1024;
  const MEDIA_LIMIT = { video: 500 * 1024 * 1024, image: 50 * 1024 * 1024 };

  function mediaSources(element) {
    return [element.currentSrc, element.src, ...Array.from(element.querySelectorAll('source[src]'), source => source.src)]
      .filter(Boolean);
  }

  function allowedMediaSource(source) {
    if (source.startsWith('blob:https://muse.ai/')) return true;
    try {
      const url = new URL(source);
      return url.protocol === 'https:' && !url.username && !url.password
        && (url.origin === location.origin || url.hostname.endsWith('.fbcdn.net'));
    } catch {
      return false;
    }
  }

  function isSmallUiIcon(element) {
    if (element.closest(ATTACHMENT)) return false;
    const attrW = Number(element.getAttribute('width')) || 0;
    const attrH = Number(element.getAttribute('height')) || 0;
    if ((attrW > 0 && attrW < 120) || (attrH > 0 && attrH < 120)) return true;
    const rect = element.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0 && Math.max(rect.width, rect.height) < 96) return true;
    if (element.naturalWidth > 0 && element.naturalHeight > 0
        && Math.min(element.naturalWidth, element.naturalHeight) < 240) {
      return true;
    }
    return false;
  }

  function freshMediaSource(kind, previousSources, inCurrentTurn) {
    const candidates = all(kind === 'video' ? 'video' : 'img').filter(node => node.isConnected
      && !node.closest(USER) && node.closest(ASSISTANT + ', ' + ATTACHMENT) && inCurrentTurn(node));
    for (const element of candidates) {
      if (kind === 'image') {
        if (element.loading === 'lazy') element.loading = 'eager';
        if (isSmallUiIcon(element)) continue;
      }
      const source = mediaSources(element).find(url => !previousSources.has(url) && allowedMediaSource(url));
      if (source) return source;
    }
    return null;
  }

  function asBase64(bytes) {
    let binary = '';
    for (let i = 0; i < bytes.length; i += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    }
    return btoa(binary);
  }

  async function uploadMedia(id, source, kind) {
    const response = await fetch(source, {
      credentials: new URL(source).origin === location.origin ? 'same-origin' : 'omit',
      redirect: 'error',
      signal: AbortSignal.timeout(55000),
    });
    if (!response.ok || !response.body) throw new Error('Không đọc được tệp từ tab Muse.');
    const type = (response.headers.get('content-type') || '').split(';')[0].toLowerCase();
    if (type && !type.startsWith(kind + '/') && type !== 'application/octet-stream') {
      throw new Error('Tệp trong tab Muse không đúng loại được yêu cầu.');
    }
    const length = Number(response.headers.get('content-length'));
    if (length > MEDIA_LIMIT[kind]) throw new Error('Tệp Muse vượt giới hạn kích thước.');
    const reader = response.body.getReader();
    let offset = 0;
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        if (offset + value.length > MEDIA_LIMIT[kind]) throw new Error('Tệp Muse vượt giới hạn kích thước.');
        for (let i = 0; i < value.length; i += MEDIA_CHUNK_SIZE) {
          const chunk = value.subarray(i, i + MEDIA_CHUNK_SIZE);
          const result = await chrome.runtime.sendMessage({
            type: kind === 'video' ? 'video-chunk' : 'media-chunk',
            chunk: { id, tab, offset, data: asBase64(chunk) },
          });
          if (!result?.ok || result.data?.size !== offset + chunk.length) {
            throw new Error(result?.error || 'Gateway không nhận được dữ liệu media.');
          }
          offset += chunk.length;
        }
      }
    } finally {
      await reader.cancel().catch(() => {});
    }
    if (offset < (kind === 'image' ? 4096 : 100)) throw new Error('Tab Muse trả về tệp media rỗng hoặc icon nhỏ.');
  }

  async function wakeSelf() {
    await chrome.runtime.sendMessage({ type: 'wake' }).catch(() => null);
  }

  async function run({ id, prompt, files, timeoutMs, kind, chatMode }) {
    const deadline = Date.now() + timeoutMs;
    if (document.visibilityState !== 'visible' || !editor()) {
      await wakeSelf();
      await delay(350);
    }
    const prepareBy = Math.min(deadline, Date.now() + 15000);
    const main = chatMode === 'main';
    if (main) await openMainChat(prepareBy);
    else await newConversation(prepareBy);
    const startingEditor = await waitFor(editor, prepareBy);
    if (!startingEditor) throw new Error('Mở tab Muse và kiểm tra phiên đăng nhập.');
    if (main && (startingEditor.value ?? startingEditor.textContent).trim()) {
      throw new Error('Chat chính đang có tin nhắn nháp. Lưu hoặc xóa bản nháp để Studio tiếp tục.');
    }
    attach(files);
    if (files.length) await delay(800);
    const target = editor();
    if (!target) throw new Error('Ô chat Muse đã thay đổi. Hãy thử lại.');
    const lastBeforeSend = all(MESSAGE).at(-1) || null;
    const previousUsers = new Set(all(USER));
    const previousAnswers = all(ASSISTANT);
    const previousAnswerSet = new Set(previousAnswers);
    const previousSources = new Set(kind ? all(kind === 'video' ? 'video' : 'img').flatMap(mediaSources) : []);
    const previousLinks = new Set(main ? latestMedia(previousAnswers) : []);
    const previousNotices = new Set(main ? all(ERR).filter(visible) : []);
    const normalizeText = text => text.normalize('NFC').replace(/\s+/g, ' ').trim();
    const fullPromptNorm = normalizeText(prompt);
    const promptStart = fullPromptNorm.slice(0, 50);
    const promptBody = fullPromptNorm.slice(
      Math.min(95, Math.max(0, fullPromptNorm.length - 60)),
      Math.min(210, fullPromptNorm.length),
    );
    const matchesPrompt = node => {
      const text = normalizeText(node.innerText || '');
      return text.includes(promptStart) && (!promptBody || text.includes(promptBody));
    };
    const findUserTurn = () => all(USER).filter(node => !previousUsers.has(node)
      && matchesPrompt(node)
      && (!lastBeforeSend?.isConnected
        || Boolean(lastBeforeSend.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING))).at(-1) || null;
    fill(target, prompt);
    const send = await waitFor(sendButton, prepareBy);
    if (!send) throw new Error('Không tìm thấy nút gửi trong tab Muse.');
    send.click();
    const sent = await waitFor(
      () => main ? findUserTurn() : all(USER).length > previousUsers.size
        || all(ASSISTANT).length > previousAnswers.length || visible(document.querySelector(STOP)),
      Math.min(deadline, Date.now() + 8000),
    );
    if (!sent) throw new Error('Muse chưa xác nhận đã gửi yêu cầu.');

    let activeSent = main ? sent : null;
    let trackedAnswers = [];
    let previous = '';
    let changed = Date.now();
    let playerSource = null;
    let playerChanged = Date.now();
    let lastWake = Date.now();
    while (Date.now() < deadline) {
      if (kind && document.visibilityState !== 'visible' && !playerSource && Date.now() - lastWake >= 25000) {
        lastWake = Date.now();
        await wakeSelf();
      }
      if (main && location.pathname !== '/') throw new Error('Tab Muse đã rời chat chính trong lúc xử lý.');
      if (main && !activeSent?.isConnected) {
        const remounted = findUserTurn();
        if (remounted) {
          activeSent = remounted;
        } else {
          if (all(USER).some(node => !previousUsers.has(node) && !matchesPrompt(node))) {
            throw new Error('Có lượt gửi khác trong chat chính. Kiểm tra kết quả trong tab Muse.');
          }
          const stillActive = trackedAnswers.some(node => node.isConnected)
            || all(ASSISTANT).some(node => !previousAnswerSet.has(node))
            || visible(document.querySelector(STOP));
          if (!stillActive) {
            throw new Error('Muse đã tải lại lượt chat. Kiểm tra kết quả trong tab Muse.');
          }
        }
      }
      if (main && activeSent?.isConnected) {
        const followingUsers = all(USER).filter(
          node => activeSent.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING,
        );
        if (followingUsers.some(node => !matchesPrompt(node))) {
          throw new Error('Có lượt gửi khác trong chat chính. Kiểm tra kết quả trong tab Muse.');
        }
        if (followingUsers.length) activeSent = followingUsers.at(-1);
      }
      const inCurrentTurn = node => {
        if (!main) return true;
        if (activeSent?.isConnected) {
          return Boolean(activeSent.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING);
        }
        if (lastBeforeSend?.isConnected) {
          return Boolean(lastBeforeSend.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING);
        }
        const firstTracked = trackedAnswers.find(item => item.isConnected);
        if (firstTracked) {
          return node === firstTracked || firstTracked.contains(node)
            || Boolean(firstTracked.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING);
        }
        const owner = node.closest(ASSISTANT);
        return owner ? !previousAnswerSet.has(owner) : true;
      };
      const answers = main ? all(ASSISTANT).filter(inCurrentTurn) : all(ASSISTANT).slice(previousAnswers.length);
      if (answers.length) {
        trackedAnswers = answers;
        try { answers.at(-1)?.scrollIntoView({ block: 'end' }); } catch {}
      }
      const reply = answers.at(-1)?.innerText?.trim() || '';
      const media = latestMedia(answers).filter(url => !previousLinks.has(url));
      const hasAttachmentSlot = kind && all(ATTACHMENT).some(node => node.isConnected && !node.closest(USER) && inCurrentTurn(node));
      const signature = reply + '\n' + media.join('\n') + '\n' + (hasAttachmentSlot ? 'att' : '');
      if (signature !== previous) {
        previous = signature;
        changed = Date.now();
      }
      const notice = all(ERR).find(node => visible(node) && !previousNotices.has(node)
        && (!node.closest(MESSAGE) || inCurrentTurn(node)));
      if (notice) return { error: notice.innerText.trim() || 'Muse báo lỗi trong tab.' };
      const isGenerating = visible(document.querySelector(STOP));
      const hasFile = kind && [...media, ...(reply.match(/https:\/\/muse\.ai\/files\/[^\s<>"'`()\[\]]+/g) || [])]
        .some(url => !previousLinks.has(url) && SHARE_EXTENSION[kind].test(new URL(url).pathname));
      if (kind) {
        const source = freshMediaSource(kind, previousSources, inCurrentTurn);
        if (source !== playerSource) {
          playerSource = source;
          playerChanged = Date.now();
        }
        if (playerSource && Date.now() - playerChanged >= 1200) {
          try {
            await uploadMedia(id, playerSource, kind);
            return kind === 'video' ? { reply, media, videoUploaded: true } : { reply, media, mediaUploaded: true };
          } catch (error) {
            if (hasFile) return { reply, media };
            return { error: 'Muse đã hiển thị kết quả, nhưng chưa lấy được tệp: ' + error.message };
          }
        }
      }
      if (!isGenerating && (kind ? hasFile : reply) && Date.now() - changed >= 2000) {
        return { reply, media };
      }
      // If Muse declined to generate media and finished a text-only reply with no attachment slot,
      // return immediately after 5s of DOM silence instead of hanging until the 120s/525s timeout.
      if (kind && !isGenerating && reply && !hasFile && !playerSource && !hasAttachmentSlot && Date.now() - changed >= 5000) {
        return { reply, media };
      }
      await delay(400);
    }
    return { timedOut: true, needsApproval: visible(document.querySelector(APPROVAL)) };
  }

  async function poll() {
    return chrome.runtime.sendMessage({
      type: 'poll', tab, ready: !!editor(), protocol: 2, mainChat: true,
      needsApproval: visible(document.querySelector(APPROVAL)),
    })
      .catch(() => null);
  }

  async function loop() {
    while (true) {
      const response = await poll();
      const command = response?.data?.command;
      if (!command) {
        await delay(response?.data?.enabled ? 1800 : 10000);
        continue;
      }
      const heartbeat = setInterval(() => { void poll(); }, 4000);
      let result;
      try {
        result = await run(command);
      } catch (error) {
        result = { error: error.message };
      } finally {
        clearInterval(heartbeat);
      }
      for (let attempt = 0; attempt < 3; attempt++) {
        const sent = await chrome.runtime.sendMessage({
          type: 'result', result: { ...result, id: command.id, tab },
        }).catch(() => null);
        if (sent?.ok) break;
        await delay(500);
      }
    }
  }

  void loop();
})();
