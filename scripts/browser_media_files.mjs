import { copyFileSync, createReadStream, appendFileSync, closeSync, mkdirSync, mkdtempSync, openSync, readSync, readdirSync, renameSync, rmSync, rmdirSync, statSync } from 'node:fs';
import { basename, dirname, join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';

export const MEDIA_LIMIT = { video: 500 * 1024 * 1024, image: 50 * 1024 * 1024 };
export const MEDIA_PATH = '/v1/browser-media/';
const MEDIA_TTL = 60 * 60 * 1000;
const CHUNK_LIMIT = 256 * 1024;
const MEDIA_FILES = /^([a-f0-9-]{36})\.(mp4|webm|png|jpg|webp|gif|partial)$/;
const TYPES = {
  mp4: 'video/mp4', webm: 'video/webm', png: 'image/png',
  jpg: 'image/jpeg', webp: 'image/webp', gif: 'image/gif',
};

export function mediaExtension(head, kind) {
  if (kind === 'video') {
    if (head.toString('ascii', 4, 8) === 'ftyp') return 'mp4';
    if (head.subarray(0, 4).toString('hex') === '1a45dfa3') return 'webm';
    return null;
  }
  if (head.subarray(0, 8).toString('hex') === '89504e470d0a1a0a') return 'png';
  if (head.subarray(0, 3).toString('hex') === 'ffd8ff') return 'jpg';
  if (head.toString('ascii', 0, 4) === 'RIFF' && head.toString('ascii', 8, 12) === 'WEBP') return 'webp';
  if (/^GIF8[79]a$/.test(head.toString('ascii', 0, 6))) return 'gif';
  return null;
}

export class BrowserMediaFiles {
  constructor(dir) {
    this.dir = dir;
  }

  discard(id) {
    if (this.dir) rmSync(join(this.dir, id + '.partial'), { force: true });
  }

  append(id, offset, data, kind) {
    if (!this.dir) throw new Error('Gateway chưa có thư mục lưu media Browser.');
    if (!Number.isSafeInteger(offset) || offset < 0 || typeof data !== 'string'
        || data.length > Math.ceil(CHUNK_LIMIT / 3) * 4) {
      throw new Error('Gói dữ liệu media không hợp lệ.');
    }
    const chunk = Buffer.from(data, 'base64');
    if (!chunk.length || chunk.length > CHUNK_LIMIT || offset + chunk.length > MEDIA_LIMIT[kind]) {
      throw new Error('Tệp media vượt giới hạn hoặc gói dữ liệu không hợp lệ.');
    }
    mkdirSync(this.dir, { recursive: true });
    appendFileSync(join(this.dir, id + '.partial'), chunk, { mode: 0o600 });
    return offset + chunk.length;
  }

  finish(id, size, kind) {
    if (!this.dir || size < 100) throw new Error('Tệp media Muse rỗng hoặc không đầy đủ.');
    const path = join(this.dir, id + '.partial');
    const descriptor = openSync(path, 'r');
    const head = Buffer.alloc(16);
    try {
      readSync(descriptor, head, 0, head.length, 0);
    } finally {
      closeSync(descriptor);
    }
    const ext = mediaExtension(head, kind);
    if (!ext) throw new Error('Muse không trả về tệp ' + (kind === 'video' ? 'video' : 'ảnh') + ' hợp lệ.');
    const file = randomUUID() + '.' + ext;
    renameSync(path, join(this.dir, file));
    this.prune(file);
    return MEDIA_PATH + file;
  }

  async fromMuseDriver(driver, sources, kind) {
    if (!this.dir || typeof driver.downloadMedia !== 'function') return null;
    const blobs = [...new Set(sources)].filter(url => /^blob:https:\/\/muse\.ai\/[a-f0-9-]{36}$/i.test(url)).slice(0, 4);
    if (!blobs.length) return null;
    mkdirSync(this.dir, { recursive: true });
    const staging = mkdtempSync(join(resolve(this.dir), 'driver-'));
    try {
      for (const blob of blobs) {
        // Muse-Chat-MCP fetches the blob within its currently logged-in tab.
        const { saved } = await driver.downloadMedia([blob], staging);
        const file = saved.find(item => item.file && dirname(resolve(item.file)) === staging);
        if (!file) continue;
        const input = join(staging, basename(file.file));
        const size = statSync(input).size;
        if (size < 100 || size > MEDIA_LIMIT[kind]) continue;
        const id = randomUUID();
        try {
          copyFileSync(input, join(this.dir, id + '.partial'));
          return this.finish(id, size, kind);
        } catch {
          // A new assistant reply can contain both an image blob and a video blob.
        } finally {
          this.discard(id);
        }
      }
      return null;
    } finally {
      for (const entry of readdirSync(staging, { withFileTypes: true })) {
        if (entry.isFile()) rmSync(join(staging, entry.name), { force: true });
      }
      rmdirSync(staging);
    }
  }

  prune(currentFile) {
    const now = Date.now();
    let total = 0;
    const files = [];
    for (const entry of readdirSync(this.dir, { withFileTypes: true })) {
      if (!entry.isFile() || !MEDIA_FILES.test(entry.name)) continue;
      const path = join(this.dir, entry.name);
      const stat = statSync(path);
      if (now - stat.mtimeMs >= MEDIA_TTL) {
        rmSync(path, { force: true });
      } else if (!entry.name.endsWith('.partial')) {
        total += stat.size;
        if (entry.name !== currentFile) files.push({ path, stat });
      }
    }
    for (const { path, stat } of files.sort((a, b) => a.stat.mtimeMs - b.stat.mtimeMs)) {
      if (total <= 1024 * 1024 * 1024) break;
      rmSync(path, { force: true });
      total -= stat.size;
    }
  }

  serve(name, request, response) {
    const ext = MEDIA_FILES.exec(name)?.[2];
    if (!this.dir || !TYPES[ext]) return false;
    const path = join(this.dir, name);
    let info;
    try {
      info = statSync(path);
    } catch (error) {
      if (error.code === 'ENOENT') return false;
      throw error;
    }
    if (!info.isFile() || Date.now() - info.mtimeMs >= MEDIA_TTL) return false;
    const size = info.size;
    let start = 0;
    let end = size - 1;
    const headers = {
      'Content-Type': TYPES[ext],
      'Accept-Ranges': 'bytes',
      'Cache-Control': 'private, no-store',
    };
    if (request.headers.range) {
      const range = /^bytes=(\d*)-(\d*)$/.exec(request.headers.range);
      if (range && (range[1] || range[2])) {
        start = range[1] ? Number(range[1]) : Math.max(0, size - Number(range[2]));
        end = range[1] && range[2] ? Math.min(end, Number(range[2])) : end;
      } else {
        start = size;
      }
      if (!Number.isSafeInteger(start) || start > end || start >= size) {
        response.writeHead(416, { 'Content-Range': 'bytes */' + size });
        response.end();
        return true;
      }
      headers['Content-Range'] = `bytes ${start}-${end}/${size}`;
    }
    headers['Content-Length'] = end - start + 1;
    response.writeHead(request.headers.range ? 206 : 200, headers);
    const stream = createReadStream(path, { start, end });
    response.on('close', () => stream.destroy());
    stream.on('error', () => response.destroy());
    stream.pipe(response);
    return true;
  }
}
