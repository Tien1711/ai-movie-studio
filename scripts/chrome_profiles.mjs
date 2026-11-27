import { createHash, randomBytes, randomUUID } from 'node:crypto';
import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { BrowserMediaFiles } from './browser_media_files.mjs';

const PAIR_TTL_MS = 10 * 60 * 1000;
const ONLINE_TTL_MS = 90 * 1000;
const MAX_PROFILES = 32;
const PROFILE_PROTOCOL = 2;
const digest = token => createHash('sha256').update(token).digest('hex');

export class RequestError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export class ChromeProfiles {
  constructor(file, mediaDir = file ? join(dirname(file), 'browser-media') : undefined) {
    this.file = file;
    this.mediaFiles = new BrowserMediaFiles(mediaDir);
    this.saved = { selected: null, profiles: [] };
    if (file) {
      try {
        this.saved = JSON.parse(readFileSync(file, 'utf8'));
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
      }
    }
    this.online = new Map();
    this.pending = new Map();
    this.pairing = null;
    this.affinity = new Map();
    this.lastUsed = new Map();
  }

  // Pool mode: spread requests over every ready paired profile. It turns on by itself
  // once two or more profiles are paired, unless the user pinned the gateway's own window
  // (selected === null) or switched the pool off explicitly.
  get pool() {
    if (this.saved.profiles.length < 2 || this.saved.pool === false) return false;
    return this.saved.pool === true || this.saved.selected !== null;
  }

  selectPool(enabled) {
    if (typeof enabled !== 'boolean') throw new RequestError(422, 'Chế độ chạy song song không hợp lệ.');
    this.save({ ...this.saved, pool: enabled });
    return this.list();
  }

  readyProfiles() {
    return this.saved.profiles.filter(({ id }) => this.readiness(id).ready);
  }

  // Lease one idle ready profile. `affinity` (e.g. a film id) is a soft preference: the same
  // film keeps its browser while that browser is idle, otherwise the least recently used
  // idle profile takes the work so a single film can still fan out across browsers.
  acquire(affinity, taken) {
    const free = this.readyProfiles().filter(({ id }) => !taken.has(id) && !this.pending.has(id));
    if (!free.length) return null;
    const preferred = affinity ? this.affinity.get(affinity) : undefined;
    const chosen = free.find(({ id }) => id === preferred)
      || free.reduce((best, item) => ((this.lastUsed.get(item.id) ?? 0) < (this.lastUsed.get(best.id) ?? 0) ? item : best));
    this.lastUsed.set(chosen.id, Date.now());
    if (affinity) {
      this.affinity.delete(affinity);
      this.affinity.set(affinity, chosen.id);
      if (this.affinity.size > 500) this.affinity.delete(this.affinity.keys().next().value);
    }
    return { id: chosen.id, name: chosen.name, driver: this.profileDriver(chosen.id) };
  }

  save(next) {
    if (this.file) {
      mkdirSync(dirname(this.file), { recursive: true });
      const temp = this.file + '.' + randomUUID() + '.tmp';
      try {
        writeFileSync(temp, JSON.stringify(next, null, 2), { encoding: 'utf8', mode: 0o600 });
        renameSync(temp, this.file);
      } finally {
        rmSync(temp, { force: true });
      }
    }
    this.saved = next;
  }

  get chatMode() {
    return this.saved.chatMode === 'main' ? 'main' : 'side';
  }

  selectChatMode(mode) {
    if (mode !== 'main' && mode !== 'side') throw new RequestError(422, 'Chế độ chat không hợp lệ.');
    this.save({ ...this.saved, chatMode: mode });
    return this.list();
  }

  readiness(id) {
    const status = this.online.get(id);
    const online = !!status && Date.now() - status.seen < ONLINE_TTL_MS;
    const reload = online && (status.protocol !== PROFILE_PROTOCOL || (this.chatMode === 'main' && !status.mainChat));
    return { online, ready: online && !reload && status.ready, ...(reload ? { reload: true } : {}) };
  }

  list() {
    return {
      selected: this.saved.selected,
      chat_mode: this.chatMode,
      pool: this.pool,
      profiles: this.saved.profiles.map(({ id, name }) => ({
        id, name, ...this.readiness(id), ...(this.pending.has(id) ? { busy: true } : {}),
      })),
    };
  }

  issueCode() {
    const code = randomBytes(16).toString('hex');
    this.pairing = { hash: digest(code), expiresAt: Date.now() + PAIR_TTL_MS };
    return { code, expires_in: PAIR_TTL_MS / 1000 };
  }

  pair({ code, name }, selectFirst = false) {
    if (typeof code !== 'string' || !/^[a-f0-9]{32}$/.test(code)
        || !this.pairing || this.pairing.expiresAt <= Date.now()
        || this.pairing.hash !== digest(code)) {
      throw new RequestError(401, 'Mã ghép nối không đúng hoặc đã hết hạn.');
    }
    if (typeof name !== 'string' || !name.trim() || name.trim().length > 60) {
      throw new RequestError(422, 'Tên profile phải có từ 1 đến 60 ký tự.');
    }
    if (this.saved.profiles.length >= MAX_PROFILES) {
      throw new RequestError(409, 'Danh sách profile đã đầy. Hãy xóa profile không còn dùng.');
    }
    const id = randomUUID();
    const token = randomBytes(32).toString('hex');
    const autoSelect = selectFirst && this.saved.profiles.length === 0 && !this.saved.selectionExplicit;
    this.save({
      ...this.saved, selected: autoSelect ? id : this.saved.selected,
      profiles: [...this.saved.profiles, { id, name: name.trim(), hash: digest(token) }],
    });
    this.pairing = null;
    return { id, token };
  }

  select(id) {
    if (id !== null && !this.saved.profiles.some(profile => profile.id === id)) {
      throw new RequestError(404, 'Không tìm thấy profile Chrome.');
    }
    this.save({ ...this.saved, selected: id, selectionExplicit: true });
    return this.list();
  }

  remove(id) {
    if (!this.saved.profiles.some(profile => profile.id === id)) {
      throw new RequestError(404, 'Không tìm thấy profile Chrome.');
    }
    this.save({
      ...this.saved,
      selected: this.saved.selected === id ? null : this.saved.selected,
      profiles: this.saved.profiles.filter(profile => profile.id !== id),
    });
    this.online.delete(id);
    const pending = this.pending.get(id);
    if (pending) {
      clearTimeout(pending.timer);
      this.pending.delete(id);
      this.mediaFiles.discard(pending.command.id);
      pending.complete({ error: 'Profile Chrome đã bị xóa.' });
    }
    return this.list();
  }

  authenticate(header) {
    const match = /^Bearer ([a-f0-9]{64})$/.exec(header || '');
    const profile = match && this.saved.profiles.find(item => item.hash === digest(match[1]));
    if (!profile) throw new RequestError(401, 'Profile này chưa được ghép nối.');
    return profile.id;
  }

  poll(id, { tab, ready, protocol, needsApproval, mainChat }) {
    if (typeof tab !== 'string' || !/^[a-f0-9-]{36}$/.test(tab)) {
      throw new RequestError(422, 'Tab Muse không hợp lệ.');
    }
    const existing = this.pending.get(id);
    if (existing?.leasedTo && existing.leasedTo !== tab) {
      return { command: null };
    }
    this.online.set(id, { tab, ready: ready === true || !!existing?.leasedTo, protocol, mainChat: mainChat === true, seen: Date.now() });
    if (existing?.leasedTo === tab) existing.needsApproval = needsApproval === true;
    if (!existing || existing.leasedTo || !this.readiness(id).ready) return { command: null };
    existing.leasedTo = tab;
    return { command: existing.command };
  }

  leasedJob(id, data) {
    const job = this.pending.get(id);
    if (!job || !job.leasedTo || job.leasedTo !== data.tab || job.command.id !== data.id) {
      throw new RequestError(404, 'Yêu cầu Muse đã kết thúc hoặc không thuộc tab này.');
    }
    return job;
  }

  async appendMedia(id, data) {
    const job = this.leasedJob(id, data);
    if (!job.command.kind || data.offset !== job.mediaSize || job.writing) {
      throw new RequestError(409, 'Dữ liệu media gửi sai thứ tự.');
    }
    job.writing = true;
    try {
      job.mediaSize = await this.mediaFiles.append(job.command.id, job.mediaSize, data.data, job.command.kind);
    } catch (error) {
      throw new RequestError(422, error.message);
    } finally {
      job.writing = false;
    }
    return { size: job.mediaSize };
  }

  async finish(id, data) {
    const pending = this.leasedJob(id, data);
    clearTimeout(pending.timer);
    this.pending.delete(id);
    let mediaUrl;
    let mediaError;
    try {
      if (pending.command.kind && (data.mediaUploaded === true || (pending.command.kind === 'video' && data.videoUploaded === true)) && !data.error) {
        mediaUrl = await this.mediaFiles.finish(pending.command.id, pending.mediaSize, pending.command.kind);
      }
    } catch (error) {
      mediaError = error.message;
    } finally {
      await this.mediaFiles.discardAsync(pending.command.id);
    }
    const reply = typeof data.reply === 'string' ? data.reply.slice(0, 512000) : '';
    const media = Array.isArray(data.media)
      ? data.media.slice(0, 32).filter(link => typeof link === 'string' && link.length <= 4096) : [];
    pending.complete({
      reply, media, mediaUrl,
      error: mediaError || (typeof data.error === 'string' ? data.error.slice(0, 160) : undefined),
      timedOut: data.timedOut === true,
      needsApproval: data.needsApproval === true,
    });
    return { accepted: true };
  }

  close() {
    for (const job of this.pending.values()) {
      clearTimeout(job.timer);
      this.mediaFiles.discard(job.command.id);
      job.complete({ error: 'Gateway Muse Browser đã dừng.' });
    }
    this.pending.clear();
  }

  activeDriver(fallback) {
    const id = this.saved.selected;
    if (!id) return fallback;
    return this.profileDriver(id);
  }

  profileDriver(id) {
    const chatMode = this.chatMode;
    return {
      status: async () => {
        const { online, ready, reload } = this.readiness(id);
        return { loggedIn: ready, composerReady: ready, browserRunning: online, reload };
      },
      progress: () => this.pending.get(id)?.needsApproval ? 'Muse đang chờ bạn xác nhận trong tab Chrome.' : null,
      run: async (prompt, files, timeoutMs, kind) => {
        if (!this.readiness(id).ready) throw new RequestError(503, 'Mở tab Muse của Studio trong profile Chrome đã chọn.');
        if (this.pending.has(id)) throw new RequestError(409, 'Profile này đang xử lý yêu cầu Muse.');
        const command = {
          id: randomUUID(), prompt, timeoutMs, kind, chatMode,
          files: files.map(({ buffer, name, mimeType }) => ({ data: buffer.toString('base64'), name, mimeType })),
        };
        return new Promise(complete => {
          const timer = setTimeout(() => {
            this.pending.delete(id);
            this.mediaFiles.discard(command.id);
            complete({ timedOut: true });
          }, timeoutMs + (kind ? 65000 : 5000));
          this.pending.set(id, { command, complete, timer, leasedTo: null, mediaSize: 0 });
        });
      },
    };
  }
}
