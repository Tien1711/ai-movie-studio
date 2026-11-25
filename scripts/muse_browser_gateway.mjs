import http from 'node:http';
import { timingSafeEqual, randomUUID } from 'node:crypto';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const MAX_IMAGE_BYTES = 20 * 1024 * 1024;
const MAX_BODY_BYTES = Math.ceil(MAX_IMAGE_BYTES * 4 / 3) + 128 * 1024;
const MAX_PENDING = 3;
const TASK_TTL_MS = 60 * 60 * 1000;
const MAX_TASKS = 200;
const MEDIA_EXTENSIONS = {
  image: /\.(png|jpe?g|gif|webp)$/i,
  video: /\.(mp4|webm)$/i,
};

class RequestError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function jsonBody(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request.iterator({ destroyOnReturn: false })) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) {
      request.resume();
      throw new RequestError(413, 'Yêu cầu vượt giới hạn kích thước.');
    }
    chunks.push(chunk);
  }
  try {
    const data = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (data && typeof data === 'object' && !Array.isArray(data)) return data;
  } catch {}
  throw new RequestError(400, 'Yêu cầu phải chứa một đối tượng JSON.');
}

function send(response, status, body) {
  response.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
  });
  response.end(JSON.stringify(body));
}

function authenticated(request, key) {
  const actual = Buffer.from(request.headers.authorization || '');
  const expected = Buffer.from('Bearer ' + key);
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}

function imageAttachment(value) {
  if (!value) return [];
  const match = typeof value === 'string'
    && /^data:image\/(png|jpeg|webp|gif);base64,([A-Za-z0-9+/]+={0,2})$/.exec(value);
  if (!match) throw new RequestError(422, 'Chế độ Browser cần ảnh tham chiếu tải lên Studio.');
  const [, format, encoded] = match;
  if (encoded.length > Math.ceil(MAX_IMAGE_BYTES / 3) * 4) {
    throw new RequestError(413, 'Ảnh tham chiếu vượt quá 20 MB.');
  }
  const buffer = Buffer.from(encoded, 'base64');
  if (buffer.length > MAX_IMAGE_BYTES) throw new RequestError(413, 'Ảnh tham chiếu vượt quá 20 MB.');
  const headers = {
    png: () => buffer.subarray(0, 8).toString('hex') === '89504e470d0a1a0a',
    jpeg: () => buffer.subarray(0, 3).toString('hex') === 'ffd8ff',
    gif: () => /^GIF8[79]a$/.test(buffer.toString('ascii', 0, 6)),
    webp: () => buffer.toString('ascii', 0, 4) === 'RIFF' && buffer.toString('ascii', 8, 12) === 'WEBP',
  };
  if (!headers[format]()) throw new RequestError(422, 'Ảnh tham chiếu không đúng định dạng.');
  return [{ buffer, mimeType: 'image/' + format, name: 'reference.' + format }];
}

function mediaPrompt(data, kind) {
  const prompt = typeof data.prompt === 'string' && data.prompt.trim();
  if (!prompt || prompt.length > 50000) throw new RequestError(422, 'Prompt tạo media không hợp lệ.');
  const ratio = data.aspect_ratio || data.size || '16:9';
  if (typeof ratio !== 'string' || !/^\d{1,4}[:x]\d{1,4}$/.test(ratio)) {
    throw new RequestError(422, 'Tỷ lệ khung hình không hợp lệ.');
  }
  const duration = Number(data.duration ?? 5);
  if (kind === 'video' && (!Number.isInteger(duration) || duration < 1 || duration > 60)) {
    throw new RequestError(422, 'Thời lượng video không hợp lệ.');
  }
  const format = kind === 'video' ? 'MP4 video, ' + duration + ' seconds' : 'PNG image';
  const attachment = data.reference_image ? ' Use the attached reference image.' : '';
  return 'Generate and attach an actual ' + format + ', aspect ratio ' + ratio
    + '.' + attachment + ' Return the generated media file in this chat.\n\n' + prompt;
}

function chatPrompt(messages) {
  if (!Array.isArray(messages)) throw new RequestError(422, 'Thiếu danh sách messages.');
  const prompt = messages
    .filter(message => message && (message.role === 'system' || message.role === 'user'))
    .map(message => typeof message.content === 'string' ? message.content.trim() : '')
    .filter(Boolean).join('\n\n');
  if (!prompt || prompt.length > 120000) throw new RequestError(422, 'Nội dung chat không hợp lệ.');
  return prompt;
}

function textLinks(text) {
  return text.match(/https:\/\/[^\s<>\x22'\x60()\[\]]+/g) || [];
}

function mediaLink(values, kind) {
  for (const value of values) {
    try {
      const url = new URL(value);
      if (url.origin === 'https://muse.ai' && !url.username && !url.password
          && url.pathname.startsWith('/files/') && MEDIA_EXTENSIONS[kind].test(url.pathname)) {
        return url.href;
      }
    } catch {}
  }
  return null;
}

async function requireBrowser(driver) {
  const state = await driver.status();
  if (!state.loggedIn || !state.composerReady) {
    throw new RequestError(503, 'Mở Chrome của gateway và đăng nhập Muse để tiếp tục.');
  }
}

async function generate(driver, prompt, files, kind) {
  await requireBrowser(driver);
  await driver.newChat();
  const before = kind ? await driver.readChat(20) : null;
  const result = await driver.chat(prompt, { files, timeoutMs: kind === 'video' ? 360000 : kind ? 120000 : 35000 });
  if (result.needsApproval) throw new RequestError(409, 'Muse đang chờ xác nhận trong Chrome.');
  if (result.timedOut) throw new RequestError(504, 'Muse phản hồi quá thời gian chờ.');
  if (result.error) throw new RequestError(502, 'Muse báo lỗi. Kiểm tra cuộc trò chuyện trong Chrome.');

  const reply = result.reply || '';
  if (!kind) {
    if (!reply.trim()) throw new RequestError(502, 'Muse trả về phản hồi rỗng.');
    return reply;
  }
  const links = textLinks(reply);
  const after = await driver.readChat(20);
  if (after.threadUrl === result.threadUrl) {
    const appended = after.threadUrl === before.threadUrl ? after.count - before.count : after.count;
    if (appended > 0) {
      for (const message of after.messages.slice(-appended)) {
        if (message.role === 'assistant' && Array.isArray(message.media)) links.push(...message.media);
      }
    }
  }
  const earlierLinks = new Set([
    ...textLinks(prompt),
    ...before.messages.flatMap(message => [...textLinks(message.text || ''), ...(message.media || [])]),
  ]);
  const url = mediaLink(links.filter(link => !earlierLinks.has(link)), kind);
  if (!url) throw new RequestError(502, 'Muse chưa trả về tệp media của yêu cầu này.');
  return url;
}

export function createGateway(driver, key) {
  if (!key) throw new Error('Đặt MUSE_BROWSER_KEY trước khi khởi động gateway Browser.');
  const tasks = new Map();
  const queue = [];
  let busy = false;

  // Track all browser operations in a single lane, including the reply read.
  function drain() {
    if (busy || !queue.length) return;
    busy = true;
    const { run, fulfill, fail } = queue.shift();
    Promise.resolve().then(run).then(fulfill, fail).finally(() => {
      busy = false;
      drain();
    });
  }

  function schedule(run) {
    if (queue.length >= MAX_PENDING) throw new RequestError(429, 'Hàng đợi Muse Browser đã đầy.');
    return new Promise((fulfill, fail) => {
      queue.push({ run, fulfill, fail });
      drain();
    });
  }

  function submit(data, kind) {
    const prompt = mediaPrompt(data, kind);
    const files = imageAttachment(data.reference_image);
    const id = randomUUID();
    const task = { id, status: 'queued', kind };
    schedule(async () => {
      task.status = 'in_progress';
      try {
        task.url = await generate(driver, prompt, files, kind);
        task.status = 'completed';
      } catch (error) {
        task.status = 'failed';
        task.error = error instanceof RequestError ? error.message : 'Lỗi kết nối trình duyệt Muse.';
      } finally {
        task.finished = Date.now();
      }
    });
    tasks.set(id, task);
    for (const [taskId, old] of tasks) {
      if (old.finished && (Date.now() - old.finished > TASK_TTL_MS || tasks.size > MAX_TASKS)) {
        tasks.delete(taskId);
      }
    }
    return { id, status: 'queued' };
  }

  return http.createServer(async (request, response) => {
    if (!authenticated(request, key)) return send(response, 401, { error: 'Khóa Browser không hợp lệ.' });
    const path = (request.url || '').split('?', 1)[0];
    try {
      if (request.method === 'GET') {
        if (path === '/admin/accounts') {
          const state = await driver.status();
          const ready = Boolean(state.loggedIn && state.composerReady);
          const accounts = ready ? [{ id: 'browser', enabled: true }] : [];
          return send(response, 200, {
            accounts, stats: { total: accounts.length }, ready,
            error: ready ? '' : 'Mở Chrome của gateway và đăng nhập Muse.',
          });
        }
        const match = /^\/v1\/(videos|images\/tasks)\/([a-f0-9-]+)$/.exec(path);
        if (match) {
          const task = tasks.get(match[2]);
          if (task?.finished && Date.now() - task.finished > TASK_TTL_MS) tasks.delete(match[2]);
          if (!tasks.has(match[2]) || task.kind !== (match[1] === 'videos' ? 'video' : 'image')) {
            throw new RequestError(404, 'Không tìm thấy task Browser.');
          }
          const { id, status, error, url } = task;
          return send(response, 200, { id, status, error, url, ...(task.kind === 'video' && url ? { video_url: url } : {}) });
        }
      }
      if (request.method === 'POST') {
        if (path === '/v1/videos' || path === '/v1/images/tasks') {
          const data = await jsonBody(request);
          return send(response, 202, submit(data, path === '/v1/videos' ? 'video' : 'image'));
        }
        if (path === '/v1/images/generations') {
          const data = await jsonBody(request);
          if (data.async === true) return send(response, 202, submit(data, 'image'));
          if (busy) throw new RequestError(429, 'Muse Browser đang xử lý yêu cầu khác.');
          const prompt = mediaPrompt(data, 'image');
          const files = imageAttachment(data.reference_image);
          const url = await schedule(() => generate(driver, prompt, files, 'image'));
          return send(response, 200, { data: [{ url }] });
        }
        if (path === '/v1/chat/completions') {
          const data = await jsonBody(request);
          if (busy) throw new RequestError(429, 'Muse Browser đang xử lý yêu cầu khác.');
          const content = await schedule(() => generate(driver, chatPrompt(data.messages), [], null));
          return send(response, 200, { choices: [{ message: { role: 'assistant', content }, finish_reason: 'stop' }] });
        }
      }
      return send(response, 404, { error: 'Endpoint Browser không tồn tại.' });
    } catch (error) {
      const expected = error instanceof RequestError;
      return send(response, expected ? error.status : 502, {
        error: expected ? error.message : 'Lỗi kết nối trình duyệt Muse.',
      });
    }
  });
}

async function main() {
  const { MUSE_CHAT_MCP_DIR, MUSE_BROWSER_KEY, MUSE_BROWSER_PORT = '18787' } = process.env;
  if (!MUSE_CHAT_MCP_DIR) throw new Error('Đặt MUSE_CHAT_MCP_DIR trỏ tới thư mục Muse-Chat-MCP.');
  if (!MUSE_BROWSER_KEY) throw new Error('Đặt MUSE_BROWSER_KEY trước khi chạy gateway Browser.');
  const port = Number(MUSE_BROWSER_PORT);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('MUSE_BROWSER_PORT không hợp lệ.');
  const { driver } = await import(pathToFileURL(join(resolve(MUSE_CHAT_MCP_DIR), 'muse-driver.mjs')).href);
  const server = createGateway(driver, MUSE_BROWSER_KEY);
  server.on('error', error => {
    console.error('Không mở được cổng Browser:', error.message);
    process.exitCode = 1;
  });
  server.listen(port, '127.0.0.1', () => {
    console.log('Muse Browser gateway: http://127.0.0.1:' + port);
    driver.login(300000).then(state => {
      console.log(state.loggedIn ? 'Đã đăng nhập Muse.' : 'Hoàn tất đăng nhập trong Chrome.');
    }).catch(error => console.error('Không mở được Chrome:', error.message));
  });
  for (const signal of ['SIGINT', 'SIGTERM']) {
    process.once(signal, async () => {
      server.close();
      await driver.close();
    });
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
