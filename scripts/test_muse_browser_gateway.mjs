import { once } from 'node:events';
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createGateway } from './muse_browser_gateway.mjs';

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
      : prompt.startsWith('Generate and attach an actual PNG') ? 'image' : null;
    const url = kind === 'video' ? 'https://muse.ai/files/task/movie.mp4' : 'https://muse.ai/files/task/photo.png';
    const reply = kind ? 'Media generated.' : 'Result.';
    this.messages.push({ role: 'user', text: prompt });
    this.messages.push({ role: 'assistant', text: reply, media: kind && !this.skipMedia ? [url] : [] });
    return { reply, threadUrl: this.threadUrl };
  }
}

async function withGateway(driver, callback) {
  const server = createGateway(driver, 'fixture-key');
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
    await callback(request);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
}

async function waitTask(request, path) {
  for (let attempt = 0; attempt < 100; attempt++) {
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
      reference_image: 'data:image/png;base64,iVBORw0KGgo=',
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
    assert.equal(driver.sent[1].files[0].mimeType, 'image/png');
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
    assert.equal((await waitTask(request, '/v1/videos/' + job.id)).status, 'failed');
  });
});
