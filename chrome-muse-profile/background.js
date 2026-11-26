const MUSE_ORIGIN = 'https://muse.ai';
const storageReady = chrome.storage.local.setAccessLevel({ accessLevel: 'TRUSTED_CONTEXTS' });

async function gateway(port, path, body, token) {
  const response = await fetch('http://127.0.0.1:' + port + path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: 'Bearer ' + token } : {}),
    },
    redirect: 'error',
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(10000),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Gateway Browser lỗi.');
  return data;
}

async function openMuse() {
  const { museTabId } = await chrome.storage.session.get('museTabId');
  if (Number.isInteger(museTabId)) {
    const tab = await chrome.tabs.get(museTabId).catch(() => null);
    if (tab?.url?.startsWith(MUSE_ORIGIN + '/')) {
      await chrome.tabs.update(tab.id, { active: true });
      await chrome.windows.update(tab.windowId, { focused: true });
      return;
    }
  }
  const tab = await chrome.tabs.create({ url: MUSE_ORIGIN + '/', active: false });
  await chrome.storage.session.set({ museTabId: tab.id });
  await chrome.tabs.update(tab.id, { active: true });
}

async function handle(message, sender) {
  await storageReady;
  const { link } = await chrome.storage.local.get('link');
  if (sender.url === chrome.runtime.getURL('popup.html')) {
    if (message.type === 'status') return { paired: !!link, name: link?.name };
    if (message.type === 'open' && link) return openMuse();
    if (message.type === 'pair') {
      const { port, code, name } = message;
      if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Cổng gateway không hợp lệ.');
      const { token } = await gateway(port, '/extension/pair', { code, name });
      await chrome.storage.local.set({ link: { port, token, name } });
      return;
    }
    throw new Error('Thao tác extension không hợp lệ.');
  }
  let { museTabId } = await chrome.storage.session.get('museTabId');
  if (link && sender.tab && sender.frameId === 0 && sender.url?.startsWith(MUSE_ORIGIN + '/')) {
    const tracked = Number.isInteger(museTabId)
      ? await chrome.tabs.get(museTabId).catch(() => null)
      : null;
    if (!tracked?.url?.startsWith(MUSE_ORIGIN + '/')) {
      museTabId = sender.tab.id;
      await chrome.storage.session.set({ museTabId });
    }
  }
  if (!link || !sender.tab || sender.frameId !== 0 || sender.tab.id !== museTabId
      || !sender.url?.startsWith(MUSE_ORIGIN + '/')) {
    return { enabled: false };
  }
  if (message.type === 'poll') {
    const result = await gateway(link.port, '/extension/poll', {
      tab: message.tab, ready: message.ready === true,
      protocol: message.protocol, needsApproval: message.needsApproval === true,
      mainChat: message.mainChat === true,
    }, link.token);
    return { enabled: true, ...result };
  }
  if (message.type === 'media-chunk' || message.type === 'video-chunk') {
    return gateway(link.port, '/extension/' + message.type, message.chunk, link.token);
  }
  if (message.type === 'result') {
    return gateway(link.port, '/extension/result', message.result, link.token);
  }
  throw new Error('Thao tác tab Muse không hợp lệ.');
}

chrome.runtime.onInstalled.addListener(async () => {
  await storageReady;
  const { link } = await chrome.storage.local.get('link');
  if (!link) return;
  const tabs = await chrome.tabs.query({ url: MUSE_ORIGIN + '/*' }).catch(() => []);
  const target = tabs.find(tab => tab.active) || tabs[0];
  if (target?.id) {
    await chrome.storage.session.set({ museTabId: target.id });
    await chrome.tabs.reload(target.id).catch(() => {});
  }
});

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  handle(message, sender).then(data => respond({ ok: true, data }))
    .catch(error => respond({ ok: false, error: error.message }));
  return true;
});
