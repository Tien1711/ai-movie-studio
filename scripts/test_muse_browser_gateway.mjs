import { once } from 'node:events';
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createGateway } from './muse_browser_gateway.mjs';
import { ChromeProfiles } from './chrome_profiles.mjs';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

class FakeBrowser {
  ready = true;
  messages = [];
  sent = [];
  running = 0;
  peak = 0;
  skipMedia = false;
  threadUrl = 'https://muse.ai/chat/test';

  async status() {
    return { loggedIn: this.ready, composerReady: this.ready };
  }

  async newChat() {
    this.messages = [];
  }

  async readChat(limit) {
    return { count: this.messages.length, messages: this.messages.slice(-limit), threadUrl: this.threadUrl };
  }

  async chat(prompt, { files }) {
    this.running++;
    this.peak = Math.max(this.peak, this.running);
    this.sent.push({ prompt, files });
    await new Promise(resolve => setTimeout(resolve, 20));
    this.running--;
    const kind = prompt.startsWith('Generate and attach an actual MP4') ? 'video'
      : prompt.startsWith('Generate and attach an actual image') ? 'image' : null;
    const url = kind === 'video' ? 'https://muse.ai/files/task/movie.mp4' : 'https://muse.ai/files/task/photo.png';
    const reply = kind ? 'Media generated.' : 'Result.';
    this.messages.push({ role: 'user', text: prompt });
    this.messages.push({ role: 'assistant', text: reply, media: kind && !this.skipMedia ? [url] : [] });
    return { reply, threadUrl: this.threadUrl };
  }
}

async function withGateway(driver, callback, options) {
  const server = createGateway(driver, 'fixture-key', options);
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const request = (path, data, authorized = true) => fetch(
    'http://127.0.0.1:' + server.address().port + path,
    {
      method: data ? 'POST' : 'GET',
      headers: {
        ...(authorized ? { Authorization: 'Bearer fixture-key' } : {}),
        ...(data ? { 'Content-Type': 'application/json' } : {}),
      },
      body: data ? JSON.stringify(data) : undefined,
    },
  );
  try {
    await callback(request, 'http://127.0.0.1:' + server.address().port);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
}

async function waitTask(request, path) {
  for (let attempt = 0; attempt < 200; attempt++) {
    const response = await request(path);
    assert.equal(response.status, 200);
    const task = await response.json();
    if (['completed', 'failed'].includes(task.status)) return task;
    await new Promise(resolve => setTimeout(resolve, 15));
  }
  throw new Error('Test task did not finish.');
}

test('health reports browser readiness and requires authorization', async () => {
  const driver = new FakeBrowser();
  await withGateway(driver, async request => {
    assert.equal((await request('/admin/accounts', undefined, false)).status, 401);
    driver.ready = false;
    assert.equal((await (await request('/admin/accounts')).json()).ready, false);
    driver.ready = true;
    const health = await (await request('/admin/accounts')).json();
    assert.equal(health.ready, true);
    assert.equal(health.accounts.length, 1);
  });
});

test('Studio can submit and poll media with one active browser operation', async () => {
  const driver = new FakeBrowser();
  await withGateway(driver, async request => {
    const picture = await request('/v1/images/tasks', { prompt: 'A garden.', size: '9:16' });
    const video = await request('/v1/videos', {
      prompt: 'A garden in motion.', duration: 8, size: '16:9',
      reference_image: 'data:image/png;base64,UklGRgAAAABXRUJQ',
    });
    assert.equal(picture.status, 202);
    assert.equal(video.status, 202);
    const imageJob = await picture.json();
    const videoJob = await video.json();
    assert.equal((await request('/v1/videos/' + imageJob.id)).status, 404);
    assert.equal((await waitTask(request, '/v1/images/tasks/' + imageJob.id)).url,
      'https://muse.ai/files/task/photo.png');
    assert.equal((await waitTask(request, '/v1/videos/' + videoJob.id)).video_url,
      'https://muse.ai/files/task/movie.mp4');
    assert.match(driver.sent[1].prompt, /8 seconds, aspect ratio 16:9/);
    assert.equal(driver.sent[1].files[0].mimeType, 'image/webp');
    assert.equal(driver.sent[1].files[0].name, 'reference.webp');
    assert.equal(driver.peak, 1);
  });
});

test('chat and synchronous image results use the Studio contract', async () => {
  const driver = new FakeBrowser();
  await withGateway(driver, async request => {
    const chat = await request('/v1/chat/completions', {
      model: 'muse', messages: [{ role: 'system', content: 'JSON only.' }, { role: 'user', content: 'A script.' }],
    });
    assert.equal(chat.status, 200);
    assert.equal((await chat.json()).choices[0].message.content, 'Result.');
    assert.equal(driver.sent[0].prompt, 'JSON only.\n\nA script.');
    const image = await request('/v1/images/generations', { prompt: 'A garden.', size: '1:1' });
    assert.equal(image.status, 200);
    assert.equal((await image.json()).data[0].url, 'https://muse.ai/files/task/photo.png');
  });
});

test('a reply without new media fails without reusing an old attachment', async () => {
  const driver = new FakeBrowser();
  driver.newChat = async () => {};
  driver.messages = [{ role: 'assistant', media: ['https://muse.ai/files/old/previous.mp4'] }];
  driver.skipMedia = true;
  await withGateway(driver, async request => {
    const response = await request('/v1/videos', { prompt: 'A new film.' });
    const job = await response.json();
    const failed = await waitTask(request, '/v1/videos/' + job.id);
    assert.equal(failed.status, 'failed');
    assert.match(failed.error, /Muse chưa tạo được tệp media \(Media generated\.\)/);
  });
});

test('the first paired profile is selected, requires a current extension and reports an approval pause', async () => {
  const driver = new FakeBrowser();
  const tab = 'a1e5c195-4465-45b4-a739-085d55d73576';
  const media = 'https://muse.ai/files/test/profile-video.mp4';
  await withGateway(driver, async (request, base) => {
    function call(path, method, data, token) {
      return fetch(base + path, {
        method,
        headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: 'Bearer ' + token } : {}) },
        body: data === undefined ? undefined : JSON.stringify(data),
      });
    }
    const create = await request('/admin/chrome-profiles/pair', {});
    const { code } = await create.json();
    const pair = await call('/extension/pair', 'POST', { code, name: 'Profile 1230' });
    assert.equal(pair.status, 201);
    const { id, token } = await pair.json();
    assert.equal((await call('/extension/pair', 'POST', { code, name: 'Duplicate' })).status, 401);

    const list = await (await request('/admin/chrome-profiles')).json();
    assert.equal(list.selected, id);
    assert.deepEqual(list.profiles, [{ id, name: 'Profile 1230', online: false, ready: false }]);
    assert.equal((await call('/admin/chrome-profiles/selection', 'PUT', { id }, token)).status, 401);
    const selected = await call('/admin/chrome-profiles/selection', 'PUT', { id }, 'fixture-key');
    assert.equal(selected.status, 200);
    assert.equal((await (await request('/admin/accounts')).json()).ready, false);

    const heartbeat = (state = {}) => call('/extension/poll', 'POST', { tab, ready: true, protocol: 2, ...state }, token);
    await heartbeat({ protocol: 1 });
    const outdated = await (await request('/admin/chrome-profiles')).json();
    assert.equal(outdated.profiles[0].reload, true);
    assert.equal(outdated.profiles[0].ready, false);
    assert.equal((await (await heartbeat()).json()).command, null);
    assert.equal((await (await request('/admin/accounts')).json()).ready, true);
    const created = await request('/v1/videos', { prompt: 'Make a short film.' });
    const task = await created.json();
    const { command } = await (await heartbeat()).json();
    assert.equal(command.kind, 'video');
    assert.match(command.prompt, /Make a short film/);
    await heartbeat({ ready: false, needsApproval: true });
    const paused = await (await request('/v1/videos/' + task.id)).json();
    assert.match(paused.message, /chờ bạn xác nhận/);
    assert.equal((await (await request('/admin/accounts')).json()).ready, true);
    assert.equal((await call('/admin/chrome-profiles/selection', 'PUT', { id: null }, 'fixture-key')).status, 409);

    const wrongTab = await call('/extension/result', 'POST', { id: command.id, tab: 'different-tab', media: [media] }, token);
    assert.equal(wrongTab.status, 404);
    const completed = await call('/extension/result', 'POST', { id: command.id, tab, media: [media] }, token);
    assert.equal(completed.status, 200);
    assert.equal((await waitTask(request, '/v1/videos/' + task.id)).video_url, media);
    assert.equal(driver.sent.length, 0);

    const removed = await call('/admin/chrome-profiles/' + id, 'DELETE', undefined, 'fixture-key');
    assert.equal(removed.status, 200);
    assert.deepEqual(await removed.json(), { selected: null, profiles: [], chat_mode: 'side' });
    assert.equal((await heartbeat()).status, 401);
  });
});

test('main chat stays in the home conversation and excludes historical media', async () => {
  const driver = new FakeBrowser();
  const old = 'https://muse.ai/files/old/movie.mp4';
  driver.messages.push({ role: 'assistant', media: [old] });
  driver.openChat = async url => {
    assert.equal(url, 'https://muse.ai/');
    driver.threadUrl = url;
    return { url };
  };
  driver.newChat = async () => assert.fail('main chat must not create a side chat');
  await withGateway(driver, async (request, base) => {
    const response = await fetch(base + '/admin/chrome-profiles/chat-mode', {
      method: 'PUT', headers: { Authorization: 'Bearer fixture-key' }, body: '{"mode":"main"}',
    });
    assert.equal(response.status, 200);
    assert.equal((await response.json()).chat_mode, 'main');
    const task = await (await request('/v1/videos', { prompt: 'Use the main conversation.' })).json();
    const result = await waitTask(request, '/v1/videos/' + task.id);
    assert.equal(result.video_url, 'https://muse.ai/files/task/movie.mp4');
    assert.deepEqual(driver.messages[0].media, [old]);
  });
});

test('main chat commands require a compatible paired extension and persist the mode', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'studio-browser-chat-'));
  try {
    const file = join(dir, 'profiles.json');
    const profiles = new ChromeProfiles(file);
    const { id } = profiles.pair({ code: profiles.issueCode().code, name: 'Chrome' }, true);
    profiles.selectChatMode('main');

    const restored = new ChromeProfiles(file);
    assert.equal(restored.chatMode, 'main');
    const tab = 'a1e5c195-4465-45b4-a739-085d55d73576';
    const heartbeat = { tab, ready: true, protocol: 2 };
    restored.poll(id, heartbeat);
    const browser = restored.activeDriver(new FakeBrowser());
    assert.equal((await browser.status()).reload, true);

    restored.poll(id, { ...heartbeat, mainChat: true });
    assert.equal((await browser.status()).composerReady, true);
    const done = browser.run('Write a storyboard.', [], 100, null);
    assert.equal(restored.poll(id, heartbeat).command, null);
    const { command } = restored.poll(id, { ...heartbeat, mainChat: true });
    assert.equal(command.chatMode, 'main');
    restored.finish(id, { id: command.id, tab, reply: 'Storyboard' });
    assert.equal((await done).reply, 'Storyboard');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('Browser task failures preserve the specific message from Muse', async () => {
  const driver = new FakeBrowser();
  driver.chat = async () => ({ error: '  Chưa tìm thấy   nút gửi trong tab Muse.  ' });
  await withGateway(driver, async request => {
    const created = await request('/v1/videos', { prompt: 'A short video.' });
    const job = await created.json();
    const result = await waitTask(request, '/v1/videos/' + job.id);
    assert.equal(result.status, 'failed');
    assert.equal(result.error, 'Muse Browser: Chưa tìm thấy nút gửi trong tab Muse.');
  });
});

test('inline video and WebP are received in chunks and served privately', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'studio-browser-media-'));
  const tab = 'b1e5c195-4465-45b4-a739-085d55d73576';
  const contents = Buffer.alloc(1200, 77);
  contents.write('ftyp', 4);
  try {
    await withGateway(new FakeBrowser(), async (request, base) => {
      function send(path, method, data, token, headers = {}) {
        return fetch(base + path, {
          method,
          body: data === undefined ? undefined : JSON.stringify(data),
          headers: { ...headers, ...(token ? { Authorization: 'Bearer ' + token } : {}) },
        });
      }
      const { code } = await (await request('/admin/chrome-profiles/pair', {})).json();
      const { id, token } = await (await send('/extension/pair', 'POST', { code, name: 'Local profile' })).json();
      await send('/admin/chrome-profiles/selection', 'PUT', { id }, 'fixture-key');
      const poll = () => send('/extension/poll', 'POST', { tab, ready: true, protocol: 2 }, token);
      await poll();
      const video = await (await request('/v1/videos', { prompt: 'A short video with no share URL.' })).json();
      const { command } = await (await poll()).json();
      assert.equal(command.kind, 'video');
      const chunk = { id: command.id, tab, data: contents.subarray(0, 600).toString('base64') };
      assert.equal((await send('/extension/video-chunk', 'POST', { ...chunk, offset: 1 }, token)).status, 409);
      assert.deepEqual(await (await send('/extension/video-chunk', 'POST', { ...chunk, offset: 0 }, token)).json(), { size: 600 });
      await send('/extension/video-chunk', 'POST', { ...chunk, offset: 600, data: contents.subarray(600).toString('base64') }, token);
      assert.equal((await send('/extension/result', 'POST', { id: command.id, tab, videoUploaded: true }, token)).status, 200);
      const done = await waitTask(request, '/v1/videos/' + video.id);
      assert.equal(done.status, 'completed');
      assert.match(done.video_url, /^\/v1\/browser-media\/[a-f0-9-]+\.mp4$/);
      assert.equal((await send(done.video_url, 'GET')).status, 401);
      const partial = await send(done.video_url, 'GET', undefined, 'fixture-key', { Range: 'bytes=4-7' });
      assert.equal(partial.status, 206);
      assert.equal(partial.headers.get('content-range'), 'bytes 4-7/1200');
      assert.equal(await partial.text(), 'ftyp');

      const picture = await (await request('/v1/images/tasks', { prompt: 'An inline image.' })).json();
      const imageCommand = (await (await poll()).json()).command;
      assert.equal(imageCommand.kind, 'image');
      const webp = Buffer.alloc(180, 32);
      webp.write('RIFF', 0);
      webp.write('WEBP', 8);
      const imageChunk = { id: imageCommand.id, tab, offset: 0, data: webp.toString('base64') };
      assert.equal((await send('/extension/media-chunk', 'POST', imageChunk, token)).status, 200);
      assert.equal((await send('/extension/result', 'POST', { id: imageCommand.id, tab, mediaUploaded: true }, token)).status, 200);
      const image = await waitTask(request, '/v1/images/tasks/' + picture.id);
      assert.match(image.url, /\.webp$/);
      assert.equal((await send(image.url, 'GET')).status, 401);
      const body = await request(image.url);
      assert.equal(body.headers.get('content-type'), 'image/webp');
      assert.deepEqual(Buffer.from(await body.arrayBuffer()), webp);
    }, { profiles: new ChromeProfiles(undefined, dir) });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('dedicated Chrome uses Muse-Chat-MCP downloadMedia for a new blob even after chat times out', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'studio-muse-driver-'));
  const blob = 'blob:https://muse.ai/7a2afdac-fe49-4cd8-99da-df7abf8e09f5';
  const previousBlob = 'blob:https://muse.ai/163548a5-073f-47f4-ab1b-0765a8b89292';
  const driver = new FakeBrowser();
  let sent = false;
  let reads = 0;
  driver.readChat = async () => ({
    count: sent ? 2 : 1,
    messages: [
      { role: 'assistant', media: [previousBlob] },
      ...(sent ? [{ role: 'assistant', media: ++reads > 1 ? [blob] : [] }] : []),
    ],
    threadUrl: driver.threadUrl,
  });
  driver.chat = async () => {
    sent = true;
    return { reply: '', timedOut: true, threadUrl: driver.threadUrl };
  };
  driver.downloadMedia = async (urls, staging) => {
    assert.deepEqual(urls, [blob]);
    const file = join(staging, 'clip.mp4');
    const video = Buffer.alloc(1200, 88);
    video.write('ftyp', 4);
    writeFileSync(file, video);
    return { saved: [{ file }] };
  };
  try {
    await withGateway(driver, async request => {
      const task = await (await request('/v1/videos', { prompt: 'A fresh video.' })).json();
      const result = await waitTask(request, '/v1/videos/' + task.id);
      assert.equal(result.status, 'completed');
      const media = await request(result.video_url);
      assert.equal(media.status, 200);
      assert.equal(media.headers.get('content-length'), '1200');
      assert.equal(Buffer.from(await media.arrayBuffer()).toString('ascii', 4, 8), 'ftyp');
    }, { profiles: new ChromeProfiles(undefined, dir) });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
