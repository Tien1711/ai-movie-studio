const editor = {
  catalog: null,
  view: "films",
  chapterLayout: true,
  refs: [],
  portraits: [],
  filters: {
    all: "Tất cả",
    missing: "Chưa có ảnh",
    pending: "Chờ duyệt ảnh",
    image: "Ảnh đã duyệt",
    video: "Có video",
    approved: "Video đã duyệt",
    failed: "Lỗi",
  },
};
const el = (id) => document.getElementById(id);
const btn = (text, action, disabled = false, cls = "btn-ghost") =>
  '<button type="button" class="btn btn-sm ' +
  cls +
  '" data-do="' +
  action +
  '" ' +
  (disabled ? "disabled" : "") +
  ">" +
  esc(text) +
  "</button>";
async function editorApi(url, method = "GET", data) {
  const opts = { method };
  if (data instanceof FormData) opts.body = data;
  else if (data !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(data);
  }
  const res = await apiFetch(url, opts);
  const body = await res.json();
  if (!res.ok)
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : body.error || body.detail?.[0]?.msg || "Yêu cầu thất bại.",
    );
  return body;
}

function setupBook(project) {
  editor.bookFilter = "all";
  const save = el("msSaveStorySettings");
  save.hidden = false;
  save.onclick = async () => {
    const { voice, visual_style, aspect_ratio, scenes_count } = storyConfig();
    save.disabled = true;
    try {
      project.render_settings = await editorApi(
        "/api/story/projects/" +
          encodeURIComponent(project.id) +
          "/render-settings",
        "PUT",
        {
          voice,
          visual_style,
          aspect_ratio,
          scenes_count,
          cast_ids: selectedStudioCast("story"),
          render_mode: el("storyRenderMode").value,
          ...captionSettings("story"),
        },
      );
      showToast("Đã lưu cài đặt cho truyện này.", "success");
    } catch (error) {
      reportFailure(error);
    } finally {
      save.disabled = false;
    }
  };
  el("panel-story").classList.add("ms-book-open");
  el("msStoryTabs")
    .querySelectorAll("[data-book]")
    .forEach((b) => (b.onclick = () => bookTab(b.dataset.book)));
  bookTab("chapters");
  updateBookChapters(project, []);
}
function bookTab(tab) {
  const panel = el("panel-story");
  panel.dataset.bookTab = tab;
  el("msStoryTabs")
    .querySelectorAll("[data-book]")
    .forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.book === tab)),
    );
  for (const [id, key] of [
    ["msBookRefs", "refs"],
    ["msChapterTools", "chapters"],
    ["storyChapterList", "chapters"],
    ["msBookCost", "cost"],
    ["msStorySettings", "settings"],
  ])
    el(id).hidden = key !== tab;
}
function updateBookChapters(book, videos) {
  if (!book || !el("panel-story").classList.contains("ms-book-open")) return;
  const entries = (book.chapters || []).map((ch, i) => {
    const v = videos.find(
      (v) =>
        v.chapter_index === i ||
        (!Number.isInteger(v.chapter_index) &&
          v.title === book.title + " — " + ch.title),
    );
    const state = v?.error
      ? "error"
      : v?.final_video_url
        ? "done"
        : ["running", "queued"].includes(v?.status)
          ? "running"
          : "pending";
    return { ch, i, v, state };
  });
  const states = {
    all: "Tất cả",
    pending: "Chưa xong",
    running: "Đang chạy",
    done: "Hoàn tất",
    error: "Lỗi",
  };
  const list = el("storyChapterList");
  const signature = JSON.stringify([
    book.id,
    editor.bookFilter,
    entries.map(({ v }) => v?.updated_at),
  ]);
  if (
    list.dataset.bookSignature !== signature ||
    !list.querySelector("[data-chapter-filter]")
  ) {
    list.dataset.bookSignature = signature;
    list.innerHTML =
      '<div class="studio-tabs">' +
      Object.entries(states)
        .map(
          ([key, t]) =>
            '<button data-chapter-filter="' +
            key +
            '" aria-pressed="' +
            (editor.bookFilter === key) +
            '">' +
            t +
            " · " +
            entries.filter((e) => key === "all" || e.state === key).length +
            "</button>",
        )
        .join("") +
      "</div>" +
      entries
        .filter(
          (e) => editor.bookFilter === "all" || e.state === editor.bookFilter,
        )
        .map(
          ({ ch, i, v, state }) =>
            '<article class="ms-book-row"><div><strong>' +
            esc(ch.title) +
            "</strong><small>" +
            Number(ch.word_count || 0) +
            " từ · " +
            (v ? v.scenes.length + " cảnh" : "Chưa dựng") +
            " · " +
            states[state] +
            "</small></div>" +
            '<div class="ms-tools">' +
            btn("Kịch bản", "adapt") +
            (v ? btn("Mở chương", "open") : btn("Tạo bản dựng", "new")) +
            '</div><input type="hidden" value="' +
            i +
            '"></article>',
        )
        .join("");
    list.querySelectorAll("[data-chapter-filter]").forEach(
      (b) =>
        (b.onclick = () => {
          editor.bookFilter = b.dataset.chapterFilter;
          updateBookChapters(book, videos);
        }),
    );
    list.querySelectorAll("[data-do]").forEach(
      (b) =>
        (b.onclick = () => {
          const { i, v } =
            entries[Number(b.closest("article").querySelector("input").value)];
          if (b.dataset.do === "new") return createStoryVideos(i);
          if (b.dataset.do === "adapt") return openStoryAdaptModal(i);
          editor.chapterLayout = true;
          switchEditorView("films");
          studioState.selected = v.id;
          studioState.filter = "all";
          showPanel("muse-studio");
        }),
    );
  }
  const all = videos.flatMap((v) => v.scenes || []);
  el("msBookCost").innerHTML =
    "<h4>Lượt tạo đã ghi nhận</h4><dl><dt>Ảnh tạo thành công</dt><dd>" +
    all.reduce((n, s) => n + (s.image_generations || 0), 0) +
    "</dd><dt>Clip tạo thành công</dt><dd>" +
    all.reduce((n, s) => n + (s.clip_generations || 0), 0) +
    "</dd></dl><p>Chi phí tiền và hạn mức còn lại chưa được Muse cung cấp trong bản dựng này.</p>";
}

async function copyToReview(v) {
  const data = await editorApi(
    "/api/muse-studio/" + encodeURIComponent(v.id) + "/rebuild",
    "POST",
  );
  studioState.selected = data.id;
  studioState.filter = "all";
  await refreshMuseBoard();
}
const colorGradeLabels = {
  none: "Nguyên bản (không phủ màu)",
  cinema_teal_orange: "Điện ảnh Teal & Orange",
  warm_epic: "Sử thi ấm (Warm Epic)",
  vibrant_3d: "Hoạt hình 3D rực rỡ (Pixar/Disney)",
  moody_noir: "Kịch tính tương phản sâu (Moody Noir)",
  soft_film: "Phim nhựa cổ điển dịu mắt (Soft Film)",
};
const clipParallelModeLabels = {
  sequential: "Tuần tự toàn bộ (1 mạch nối khung cuối liên tục — an toàn nhất)",
  segment_parallel:
    "⚡ Song song theo phân đoạn (nhanh gấp 2–5x trên nhiều Chrome / tài khoản)",
};
const clipParallelNoteHtml =
  '<div class="ms-note-box">' +
  "<strong>📌 Lưu ý về chế độ dựng video song song theo phân đoạn:</strong>" +
  "<p>• <b>Tuần tự toàn bộ (mặc định):</b> Dựng từng cảnh 1 → 2 → 3… nối khung cuối liên tiếp toàn phim, mượt tuyệt đối ngay cả khi chỉ có 1 trình duyệt.</p>" +
  "<p>• <b>Song song theo phân đoạn:</b> Tự chia phim thành các phân đoạn độc lập tại điểm <i>Bắt đầu phân đoạn mới</i>, khi <i>đổi bối cảnh</i>, hoặc mốc neo lại mỗi 8 cảnh. Các phân đoạn chạy <b>song song trên Chrome Pool / nhiều tài khoản</b> (trong từng phân đoạn vẫn nối khung cuối tuần tự) — rút ngắn 2–5 lần thời gian mà vẫn giữ đồng nhất.</p>" +
  "</div>";

async function chapterSettings(v) {
  const music = await editorApi("/api/music/list");
  const options = (items, value) =>
    items
      .map(
        ([k, t]) =>
          '<option value="' +
          esc(k) +
          '" ' +
          (k === value ? "selected" : "") +
          ">" +
          esc(t) +
          "</option>",
      )
      .join("");
  const check = (name, label, checked) =>
    '<label class="ms-check-row"><input type="checkbox" name="' +
    name +
    '" ' +
    (checked ? "checked" : "") +
    "><span>" +
    esc(label) +
    "</span></label>";
  dialog(
    "Thiết lập bản dựng",
    '<label>Giọng kể mặc định<select name="voice">' +
      voiceSelectHtml(v.voice, false) +
      "</select></label>" +
      '<label>Phụ đề<select name="subtitle_style">' +
      options(Object.entries(captionLabels), v.subtitle_style) +
      "</select></label>" +
      '<label>Vị trí<select name="subtitle_position">' +
      options(
        [
          ["bottom", "Dưới"],
          ["middle", "Giữa"],
          ["top", "Trên"],
        ],
        v.subtitle_position,
      ) +
      "</select></label>" +
      '<label>Nhạc nền<select name="music">' +
      options(
        [["", "Không dùng nhạc"], ...music.map((m) => [m.filename, m.name])],
        v.music || "",
      ) +
      "</select></label>" +
      '<label>Âm lượng nhạc<input name="volume" type="range" min="0" max="1" step="0.05" value="' +
      (v.volume ?? 0.15) +
      '"></label>' +
      '<label>Ảnh đầu cảnh<select name="reference_mode">' +
      options(
        [
          ["chain", "Nối khung cuối · neo lại mỗi 8 cảnh"],
          ["approved", "Luôn dùng ảnh đã duyệt"],
        ],
        v.reference_mode || "chain",
      ) +
      "</select></label>" +
      '<label>Khi mất khung nối<select name="chain_fallback_mode">' +
      options(
        [
          ["fallback_image", "Dùng ảnh neo của cảnh"],
          ["stop", "Dừng chuỗi để kiểm tra"],
          ["independent", "Tạo độc lập từ mô tả"],
        ],
        v.chain_fallback_mode || "fallback_image",
      ) +
      "</select></label>" +
      '<div class="ms-cinema-section"><h5>🎬 Hậu kỳ điện ảnh & Tối ưu tốc độ</h5>' +
      '<label>Bảng màu điện ảnh (Color LUT)<select name="color_grade">' +
      options(Object.entries(colorGradeLabels), v.color_grade || "none") +
      "</select></label>" +
      '<label>Chế độ dựng video AI (Tính năng #6)<select name="clip_parallel_mode">' +
      options(
        Object.entries(clipParallelModeLabels),
        v.clip_parallel_mode || "sequential",
      ) +
      "</select></label>" +
      clipParallelNoteHtml +
      check(
        "smart_pacing",
        "Tự làm chậm mượt khi lời kể dài hơn clip (Smart Pacing — bỏ giật vòng lặp)",
        v.smart_pacing !== false,
      ) +
      check(
        "smart_trim",
        "Tự cắt 0.1–0.45s đen/đứng hình đầu clip AI (Smart Seam Trim)",
        !!v.smart_trim,
      ) +
      check(
        "auto_ducking",
        "Nhạc nền tự hạ êm khi có lời kể (Auto-Ducking)",
        v.auto_ducking !== false,
      ) +
      check(
        "transition_sfx",
        "Thêm tiếng chuyển cảnh điện ảnh nhẹ (Whoosh SFX khi hòa tan)",
        !!v.transition_sfx,
      ) +
      check(
        "vision_qc",
        "AI Vision tự chấm đồng nhất & tự vẽ lại ảnh dưới 75 điểm",
        !!v.vision_qc,
      ) +
      '<label>Watermark kênh (tùy chọn, tối đa 40 ký tự)<input name="watermark_text" maxlength="40" placeholder="Ví dụ: @TenKenhCuaBan" value="' +
      esc(v.watermark_text || "") +
      '"></label></div>',
    async (data) => {
      await editorApi("/api/muse-studio/" + v.id + "/chapter-settings", "PUT", {
        voice: data.get("voice"),
        subtitle_style: data.get("subtitle_style"),
        subtitle_position: data.get("subtitle_position"),
        music: data.get("music") || "",
        reference_mode: data.get("reference_mode") || "chain",
        chain_fallback_mode: data.get("chain_fallback_mode") || "fallback_image",
        color_grade: data.get("color_grade") || "none",
        clip_parallel_mode: data.get("clip_parallel_mode") || "sequential",
        smart_pacing: data.has("smart_pacing"),
        smart_trim: data.has("smart_trim"),
        auto_ducking: data.has("auto_ducking"),
        transition_sfx: data.has("transition_sfx"),
        vision_qc: data.has("vision_qc"),
        watermark_text: String(data.get("watermark_text") || "").trim(),
        version: v.updated_at,
        volume: Number(data.get("volume")),
      });
      await refreshMuseBoard();
    },
  );
}
const kindLabels = {
  character: "Nhân vật",
  setting: "Bối cảnh",
  prop: "Đạo cụ",
};
function refFields(ref = {}, locked = false) {
  return (
    (locked
      ? ""
      : '<label>Loại<select name="kind" ' +
        (ref.id ? "disabled" : "") +
        ">" +
        Object.entries(kindLabels)
          .map(
            ([key, t]) =>
              '<option value="' +
              key +
              '" ' +
              (ref.kind === key ? "selected" : "") +
              ">" +
              t +
              "</option>",
          )
          .join("") +
        "</select></label>") +
    '<label>Tên<input name="name" required maxlength="100" value="' +
    esc(ref.name || "") +
    '" ' +
    (ref.id ? "readonly" : "") +
    "></label>" +
    '<label>Mô tả nhận dạng<textarea name="description" maxlength="1500">' +
    esc(ref.description || "") +
    "</textarea></label>" +
    '<label>Ảnh tham chiếu<input name="photo" type="file" ' +
    (locked ? "required" : "") +
    ' accept="image/png,image/jpeg,image/webp"></label>' +
    (locked
      ? ""
      : '<label>Giọng nhân vật<select name="voice"><option value="">Theo giọng kể</option>' +
        voiceSelectHtml(ref.voice || "", false) +
        "</select></label>")
  );
}
async function readReference(data, ref = {}) {
  let avatar = ref.avatar || "";
  if (data.get("photo")?.size)
    avatar = await uploadStudioFile(data.get("photo"));
  return {
    kind: data.get("kind") || ref.kind || "character",
    name: data.get("name"),
    description: data.get("description") || "",
    avatar,
    voice: data.get("voice") || "",
  };
}
async function loadStoryReferences(pid) {
  if (!pid) return;
  const box = el("storyReferences");
  try {
    const rows = await editorApi(
      "/api/story/projects/" + encodeURIComponent(pid) + "/references",
    );
    if (window.storyState.projectId !== pid) return;
    editor.refs = rows;
    const base =
      "/api/story/projects/" + encodeURIComponent(pid) + "/references";
    const edit = (ref) =>
      dialog(
        ref.id ? "Sửa tham chiếu" : "Thêm tham chiếu",
        refFields(ref),
        async (data) => {
          await editorApi(base, "PUT", await readReference(data, ref));
          await loadStoryReferences(pid);
        },
      );
    box.innerHTML =
      '<div class="studio-actions">' +
      btn("＋ Thêm", "add") +
      btn("Trích xuất từ truyện", "extract") +
      (rows.length
        ? btn(
            "Vẽ ảnh còn thiếu",
            "draw-refs",
            rows.every((r) => r.avatar),
          )
        : "") +
      "</div>" +
      Object.entries(kindLabels)
        .map(
          ([key, label]) =>
            '<section class="ms-ref-group"><h4>' +
            label +
            " · " +
            rows.filter((r) => r.kind === key).length +
            "</h4>" +
            rows
              .filter((r) => r.kind === key)
              .map(
                (r) =>
                  '<article data-id="' +
                  esc(r.id) +
                  '">' +
                  (r.avatar ? '<img alt="" src="' + esc(r.avatar) + '">' : "") +
                  "<div><strong>" +
                  esc(r.name) +
                  "</strong><p>" +
                  esc(r.description) +
                  "</p>" +
                  btn("Sửa", "edit") +
                  btn(r.avatar ? "Vẽ lại" : "Vẽ ảnh", "draw") +
                  "</div></article>",
              )
              .join("") +
            "</section>",
        )
        .join("");
    box.querySelectorAll("[data-do]").forEach(
      (b) =>
        (b.onclick = async () => {
          const op = b.dataset.do,
            r = rows.find((i) => i.id === b.closest("[data-id]")?.dataset.id);
          if (op === "add" || op === "edit") return edit(r || {});
          b.disabled = true;
          const label = b.textContent;
          b.textContent = "Đang xử lý…";
          try {
            if (op === "extract") await editorApi(base + "/extract", "POST");
            else
              for (const item of r ? [r] : rows.filter((i) => !i.avatar))
                await editorApi(base + "/" + item.id + "/draw", "POST");
          } catch (error) {
            reportFailure(error);
          } finally {
            b.disabled = false;
            b.textContent = label;
            await loadStoryReferences(pid);
          }
        }),
    );
  } catch (error) {
    if (window.storyState.projectId === pid) box.textContent = error.message;
  }
}
async function initWorkshop() {
  const box = el("msWorkshop");
  if (box.dataset.ready) return;
  box.dataset.ready = "1";
  const steps = [
    ["Viết theo skill", "Lời kể và prompt theo mẫu đã chọn."],
    ["Vẽ ảnh từng cảnh", "Dùng nhân vật khoá cứng khi dựng hình."],
    ["Duyệt ảnh", "Sửa prompt hoặc thay ảnh trước khi làm clip."],
    ["Video từ ảnh duyệt", "Muse bắt đầu từ đúng ảnh đã chốt."],
    ["Dựng phim có lời kể", "Ghép clip, giọng kể, phụ đề và nhạc nền."],
  ];
  box.innerHTML =
    '<section class="ms-work-head"><h3>Video dài theo skill</h3><div class="ms-steps">' +
    steps
      .map(
        ([title, desc], i) =>
          "<div><strong>" +
          Number(i + 1) +
          ". " +
          title +
          "</strong><small>" +
          desc +
          "</small></div>",
      )
      .join("") +
    "</div></section>" +
    '<div class="ms-work-layout"><aside class="ms-film"><h3>Skill của bạn</h3><div id="msSkills"></div><div class="studio-actions">' +
    btn("＋ Nạp skill", "import") +
    btn("Dán nội dung", "paste") +
    '</div><input hidden id="msSkillFile" type="file" accept=".txt,.md,.docx,.zip"></aside>' +
    '<form id="msBrief" class="ms-film"><label class="studio-field">Chủ đề / nội dung video<textarea maxlength="1500" name="topic" required rows="4" placeholder="Nhập chủ đề và diễn biến bạn muốn kể"></textarea></label>' +
    '<div class="studio-fields"><label class="studio-field">Độ dài <output id="msMinutes">8 phút</output><input type="range" name="minutes" min="1" max="20" value="8"></label>' +
    '<label class="studio-field">Mỗi cảnh dài<select name="seconds"><option>6</option><option selected>8</option><option>10</option><option>12</option></select></label></div><p id="msEstimate"></p>' +
    '<div class="studio-fields"><label class="studio-field">Khung hình<select name="ratio"><option>9:16</option><option>16:9</option><option>1:1</option></select></label>' +
    '<label class="studio-field">Giọng kể<select name="voice">' +
    (voiceSelectHtml("vi-VN-HoaiMyNeural", false) ||
      '<option value="vi-VN-HoaiMyNeural">Hoài My</option>') +
    "</select></label>" +
    '<label class="studio-field">Phong cách<select name="style" id="msWorkshopStyle"><option value="cinematic">Điện ảnh 4K siêu thực</option></select></label></div><div data-style-cards="msWorkshopStyle"></div>' +
    '<h4>Nhân vật khoá cứng <small id="msCastCount">0/6</small></h4><div class="studio-actions">' +
    btn("Từ thư viện", "cast") +
    btn("Từ ảnh", "portrait") +
    '</div><div id="msPortraits" class="studio-cast"></div>' +
    '<button type="submit" class="btn btn-primary">Viết kịch bản theo chủ đề</button><p role="status"></p></form></div>';
  let chosen = "",
    cast = [];
  const refreshSkills = async () => {
    const skills = await editorApi("/api/muse-studio/skills");
    const list = el("msSkills");
    list.innerHTML =
      '<label class="studio-field">Skill cho phim<select><option value="">Không dùng skill</option>' +
      skills
        .map((s) => '<option value="' + s.id + '">' + esc(s.name) + "</option>")
        .join("") +
      "</select></label>";
    const select = list.querySelector("select");
    select.value = chosen;
    select.onchange = () => {
      chosen = select.value;
    };
  };
  const file = el("msSkillFile");
  box.querySelector("[data-do=import]").onclick = () => file.click();
  file.onchange = async () => {
    if (!file.files[0]) return;
    const data = new FormData();
    data.append("file", file.files[0]);
    try {
      const skill = await editorApi(
        "/api/muse-studio/skills/import",
        "POST",
        data,
      );
      chosen = skill.id;
      await refreshSkills();
    } catch (e) {
      reportFailure(e);
    } finally {
      file.value = "";
    }
  };
  box.querySelector("[data-do=paste]").onclick = () =>
    dialog(
      "Nạp skill",
      '<label>Tên skill<input name="name" maxlength="120" required></label><label>Nội dung hướng dẫn<textarea name="text" maxlength="40000" required rows="8"></textarea></label>',
      async (data) => {
        const skill = await editorApi(
          "/api/muse-studio/skills",
          "POST",
          Object.fromEntries(data),
        );
        chosen = skill.id;
        await refreshSkills();
      },
    );
  const form = el("msBrief");
  const showCast = () => {
    el("msCastCount").textContent = cast.length + "/6";
    const container = el("msPortraits");
    container.replaceChildren();
    cast.forEach((r, i) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn btn-ghost btn-sm";
      b.textContent = r.name + " ×";
      b.onclick = () => {
        cast.splice(i, 1);
        showCast();
      };
      container.append(b);
    });
  };
  form.querySelector("[data-do=portrait]").onclick = () => {
    if (cast.length >= 6) return showToast("Đã chọn sáu nhân vật.", "error");
    dialog("Khoá nhân vật từ ảnh", refFields({}, true), async (data) => {
      cast.push(await readReference(data));
      showCast();
    });
  };
  form.querySelector("[data-do=cast]").onclick = async () => {
    try {
      const chars = await editorApi("/api/characters");
      dialog(
        "Chọn nhân vật từ thư viện",
        chars
          .map(
            (c) =>
              '<label><span><input type="checkbox" name="id" value="' +
              esc(c.id) +
              '" ' +
              (cast.some((r) => r.id === c.id) ? "checked" : "") +
              "> " +
              esc(c.name) +
              "</span></label>",
          )
          .join("") || "<p>Thêm hồ sơ trong tab Nhân vật.</p>",
        (data) => {
          const picks = chars.filter((c) => data.getAll("id").includes(c.id));
          const next = [...cast.filter((c) => !c.id), ...picks];
          if (next.length > 6) throw Error("Chọn tối đa sáu nhân vật.");
          cast = next;
          showCast();
        },
      );
    } catch (e) {
      reportFailure(e);
    }
  };
  const estimate = () => {
    const n = Math.ceil((form.minutes.value * 60) / form.seconds.value);
    el("msMinutes").value = form.minutes.value + " phút";
    el("msEstimate").textContent =
      n + " cảnh · " + n + " ảnh khung hình · tối đa " + n + " lượt tạo clip";
  };
  form.minutes.oninput = estimate;
  form.seconds.onchange = estimate;
  estimate();
  form.onsubmit = async (e) => {
    e.preventDefault();
    const b = form.querySelector("[type=submit]");
    b.disabled = true;
    try {
      const v = await editorApi("/api/muse-studio/workshop", "POST", {
        ...Object.fromEntries(new FormData(form)),
        minutes: Number(form.minutes.value),
        seconds: Number(form.seconds.value),
        skill_id: chosen,
        cast_ids: cast.filter((r) => r.id).map((r) => r.id),
        portraits: cast.filter((r) => !r.id),
      });
      studioState.selected = v.id;
      studioState.filter = "all";
      switchEditorView("films");
      await refreshMuseBoard();
    } catch (error) {
      form.querySelector("[role=status]").textContent = error.message;
    } finally {
      b.disabled = false;
    }
  };
  await loadStudioCatalog();
  await refreshSkills();
}
function reportFailure(error) {
  showToast(error.message || String(error), "error");
}
async function uploadStudioFile(file) {
  const data = new FormData();
  data.append("file", file);
  return (await editorApi("/api/upload-image", "POST", data)).url;
}
function mediaUrl(v, s, kind) {
  return (
    "/api/muse-studio/" +
    encodeURIComponent(v.id) +
    "/scenes/" +
    Number(s.index) +
    "/media/" +
    kind +
    "?v=" +
    encodeURIComponent(s[kind] || "")
  );
}
function switchEditorView(view) {
  editor.view = view;
  el("msWorkshop").hidden = view !== "skill";
  for (const id of ["msProject", "msBoard"]) {
    const node =
      id === "msProject" ? el(id).closest(".studio-toolbar") : el(id);
    node.hidden = view !== "films";
  }
  el("msAssistant").hidden = view !== "assistant";
  el("msDraft").hidden = view !== "create";
  if (view === "create") newStudioDraft();
  if (view === "assistant") {
    loadStudioCast("assistant");
    loadStudioCatalog();
  }
  if (view === "skill") initWorkshop().catch(reportFailure);
  document
    .querySelectorAll("[data-ms-view]")
    .forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.msView === view)),
    );
}
document
  .querySelectorAll("[data-ms-view]")
  .forEach((b) => (b.onclick = () => switchEditorView(b.dataset.msView)));
el("msAssistant").onsubmit = async (e) => {
  e.preventDefault();
  const form = e.currentTarget,
    button = form.querySelector("[type=submit]"),
    status = form.querySelector("[role=status]");
  const fields = Object.fromEntries(new FormData(form));
  button.disabled = true;
  try {
    status.textContent = "Đang tạo bản nháp…";
    const data = await editorApi("/api/muse-studio/assistant", "POST", {
      ...fields,
      count: Number(fields.count),
      seconds: Number(fields.seconds),
      cast_ids: selectedStudioCast("assistant"),
    });
    studioState.selected = data.id;
    studioState.filter = "all";
    switchEditorView("films");
    await refreshMuseBoard();
    status.textContent = "";
  } catch (err) {
    status.textContent = err.message;
  } finally {
    button.disabled = false;
  }
};
async function loadStudioCatalog() {
  try {
    editor.catalog ||= await editorApi("/api/muse-studio/catalog");
    document.querySelectorAll("[data-style-cards]").forEach((box) => {
      if (box.dataset.ready) return;
      const select = el(box.dataset.styleCards);
      if (!select) return;
      const active = select.value || "cinematic";
      editor.catalog.groups.forEach((g) =>
        g.styles.forEach((s) => {
          if (![...select.options].some((o) => o.value === s.id))
            select.add(new Option(s.name, s.id));
        }),
      );
      select.value = active;
      const icons = {
        cinematic: "🎬",
        review_product: "🛍",
        serial: "📖",
        ugc: "📱",
        luxury: "💎",
        stick: "✍",
        miniature: "🔎",
        forest_spirit: "🌿",
        "3d_pixar": "✨",
        anime: "☁",
        clay: "🧱",
        donghua: "⚔",
        cyberpunk: "⚡",
        pet: "🐾",
        asmr: "≈",
        history: "🏛",
        zen: "🪷",
        folk: "🌾",
        horror_dark: "☾",
        food: "🍜",
      };
      box.innerHTML =
        '<details class="studio-details ms-catalog"><summary>Chọn theo bảng phong cách</summary>' +
        editor.catalog.groups
          .map(
            (g) =>
              "<h4>" +
              esc(g.name) +
              '</h4><div class="ms-styles">' +
              g.styles
                .map(
                  (s) =>
                    '<button type="button" data-style="' +
                    esc(s.id) +
                    '"><span aria-hidden="true" class="ms-style-icon">' +
                    icons[s.id] +
                    "</span><strong>" +
                    esc(s.name) +
                    "</strong><small>" +
                    esc(s.description) +
                    "</small></button>",
                )
                .join("") +
              "</div>",
          )
          .join("") +
        "</details>";
      const sync = () =>
        box
          .querySelectorAll("[data-style]")
          .forEach((b) =>
            b.setAttribute(
              "aria-pressed",
              String(b.dataset.style === select.value),
            ),
          );
      box.querySelectorAll("[data-style]").forEach(
        (b) =>
          (b.onclick = () => {
            select.value = b.dataset.style;
            select.dispatchEvent(new Event("change"));
          }),
      );
      select.addEventListener("change", sync);
      box.dataset.ready = "1";
      sync();
    });
  } catch (error) {
    reportFailure(error);
  }
}
function dialog(title, html, save, label = "Lưu") {
  const dlg = document.createElement("dialog");
  dlg.className = "ms-dialog";
  dlg.innerHTML =
    "<form><h3>" +
    esc(title) +
    "</h3>" +
    html +
    '<p class="studio-error" role="alert"></p><footer>' +
    '<button type="button" class="btn btn-ghost" data-close>Đóng</button><button class="btn btn-primary" type="submit">' +
    esc(label) +
    "</button></footer></form>";
  document.body.append(dlg);
  dlg.addEventListener("close", () => dlg.remove());
  dlg.querySelector("[data-close]").onclick = () => dlg.close();
  dlg.querySelector("form").onsubmit = async (e) => {
    e.preventDefault();
    const form = e.currentTarget,
      b = form.querySelector("[type=submit]");
    b.disabled = true;
    try {
      await save(new FormData(form), form);
      dlg.close();
    } catch (error) {
      form.querySelector("[role=alert]").textContent = error.message;
    } finally {
      b.disabled = false;
    }
  };
  dlg.showModal();
  return dlg;
}
function listFilter(s, key) {
  return {
    all: true,
    missing: !s.frame,
    pending: !!s.frame && !s.frame_approved,
    image: !!s.frame_approved,
    video: !!s.clip,
    approved: !!s.approved,
    failed: !!s.error,
  }[key];
}
function timeLabel(seconds) {
  if (!Number.isFinite(seconds)) return "—";
  const n = Math.round(seconds);
  return Math.floor(n / 60) + ":" + String(n % 60).padStart(2, "0");
}
function drawGuidedBoard(board, v) {
  const scenes = v.scenes,
    busy = ["running", "queued"].includes(v.status),
    total = scenes.length;
  const filter = Object.hasOwn(editor.filters, studioState.filter)
    ? studioState.filter
    : "all";
  const rows = scenes.filter((s) => listFilter(s, filter));
  const counts = [
    scenes.filter((s) => s.prompt).length,
    scenes.filter((s) => s.frame).length,
    scenes.filter((s) => s.frame_approved).length,
    scenes.filter((s) => s.clip).length,
    scenes.filter((s) => s.approved).length,
  ];
  const [written, frames, approvedFrames, clips, approvedClips] = counts;
  const done = !!v.final_video_url;
  const chapter = !!v.story_project_id && editor.chapterLayout;
  const progress = [
    ["Trích xuất", written === total],
    [
      v.cast?.length ? "Ảnh tham chiếu" : "Không chọn tham chiếu",
      (v.cast || []).every((c) => !!c.avatar),
    ],
    ["Phân cảnh", written === total],
    ["Lồng tiếng", scenes.every((s) => s.speech)],
    ["Dựng hình", clips === total],
    ["Ghép video", done],
    ["Xuất bản", done],
  ];
  const anchored = scenes.filter((s) => s.frame && s.anchors?.length).length;
  const report = v.inspection || {},
    words = scenes.reduce(
      (n, s) =>
        n + (s.voiceover || "").trim().split(/\s+/).filter(Boolean).length,
      0,
    );
  const duration = scenes.reduce((n, s) => n + (s.duration || 0), 0);
  const prefix = "/api/batch-videos/" + encodeURIComponent(v.id);
  const preserved = new Map(
    [...board.querySelectorAll("audio,video")].map((n) => [
      n.getAttribute("src"),
      n,
    ]),
  );
  const stillMode = scenes.every(
    (s) => s.approved || (s.frame_approved && !s.clip),
  );
  const action = (text, verb, disabled = false, cls) =>
    btn(text, verb, busy || disabled, cls);
  let html =
    '<section class="ms-film"><header><div><h3>' +
    esc(v.title) +
    "</h3><p>" +
    words +
    " từ · " +
    total +
    " cảnh · " +
    esc(v.aspect_ratio) +
    " · " +
    timeLabel(done ? report.duration_s : duration) +
    (done ? "" : " dự kiến") +
    "</p></div>" +
    (v.story_project_id
      ? btn(chapter ? "Dạng bảng" : "Dạng chương", "layout")
      : "") +
    "</header>";
  html +=
    '<div class="ms-counts">' +
    ["Kịch bản", "Ảnh đã vẽ", "Ảnh đã duyệt", "Video đã làm", "Video đã duyệt"]
      .map(
        (t, i) =>
          "<div><small>" +
          t +
          "</small><strong>" +
          counts[i] +
          "/" +
          total +
          "</strong></div>",
      )
      .join("") +
    "</div>";
  const autoCast = (v.cast || []).filter((c) => c.auto_cast);
  const needsPortrait = autoCast.some((c) => !c.avatar);
  if (autoCast.length)
    html +=
      '<div class="studio-cast">' +
      autoCast
        .map(
          (c) =>
            '<label title="' +
            esc(c.description) +
            '">' +
            (c.avatar ? '<img alt="" src="' + esc(c.avatar) + '">' : "") +
            esc(c.name) +
            "</label>",
        )
        .join("") +
      "</div>";
  html +=
    '<div class="studio-actions">' +
    (v.comic && !done ? action("Tiếp tục dựng truyện tranh", "comic") : "") +
    (needsPortrait ? action("Vẽ ảnh nhân vật", "portraits") : "") +
    (v.status === "draft" && v.studio_source && !v.workshop
      ? action("Sửa bản nháp", "draft")
      : "") +
    (v.workshop && written < total
      ? action("Viết tiếp kịch bản", "write", false, "btn-primary")
      : "") +
    action(
      "Vẽ ảnh còn thiếu",
      "images",
      needsPortrait || !scenes.some((s) => s.prompt && !s.frame),
    ) +
    action("Duyệt ảnh đã xong", "images-ok", frames === approvedFrames) +
    action("👁️ Chấm đồng nhất", "vision-qc-all", !frames) +
    action(
      "Làm video",
      "clips",
      !scenes.some((s) => s.frame_approved && !s.clip),
    ) +
    action("Duyệt video đã xong", "clips-ok", clips === approvedClips) +
    action("Ghép clip · giữ tiếng AI", "compose", approvedClips !== total) +
    action("Dựng phim có lời kể", "narrated", !stillMode, "btn-primary") +
    action("Thiết lập", "settings") +
    action("Dựng lại", "rebuild", written !== total) +
    "</div>";
  if (busy)
    html += '<p role="status">' + esc(v.current_step || "Đang xử lý") + "</p>";
  if (v.continuity_warnings?.length)
    html +=
      '<details open><summary>Lưu ý đồng nhất · ' +
      v.continuity_warnings.length +
      '</summary><div class="ms-warn-head"><span>Cảnh báo nhảy bối cảnh hoặc thời gian giữa các cảnh:</span>' +
      action("✨ Chèn cảnh cầu nối tự động", "bridge", written !== total) +
      "</div>" +
      v.continuity_warnings.map((t) => "<p>" + esc(t) + "</p>").join("") +
      "</details>";
  if (v.error) html += '<p class="studio-error">' + esc(v.error) + "</p>";
  html += "</section>";
  if (chapter)
    html +=
      '<section class="ms-film"><header><h4>Tiến trình dựng</h4>' +
      action("AI nhấn nhá giọng", "emphasis", !written) +
      "</header><p>" +
      progress.filter((x) => x[1]).length +
      '/7 bước hoàn tất</p><div class="ms-progress">' +
      progress
        .map(
          ([t, ok]) =>
            '<span class="' +
            (ok ? "ok" : "") +
            '">' +
            (ok ? "✓ " : "○ ") +
            t +
            "</span>",
        )
        .join("") +
      "</div><p>" +
      anchored +
      "/" +
      total +
      " ảnh cảnh đã dùng ảnh tham chiếu.</p></section>";
  html +=
    '<div class="studio-tabs">' +
    Object.entries(editor.filters)
      .map(
        ([k, t]) =>
          '<button type="button" data-ms-filter="' +
          k +
          '" aria-pressed="' +
          (filter === k) +
          '">' +
          t +
          " " +
          scenes.filter((s) => listFilter(s, k)).length +
          "</button>",
      )
      .join("") +
    "</div>";
  const shot = (s) => {
    const index = Number(s.index),
      state = s.error
        ? "error"
        : s.approved
          ? "approved"
          : s.clip
            ? "clip"
            : s.frame_approved
              ? "image-approved"
              : s.frame
                ? "image"
                : "empty",
      st = s.error
        ? "Có lỗi"
        : s.approved
          ? "Video đã duyệt"
          : s.clip
            ? "Chờ duyệt video"
            : s.frame_approved
              ? "Ảnh đã duyệt"
              : s.frame
                ? "Chờ duyệt ảnh"
                : "Chưa có ảnh";
    return (
      '<article class="ms-shot" data-state="' +
      state +
      '" data-index="' +
      index +
      '"><header><span class="ms-index">' +
      index +
      "</span><div><strong>" +
      esc(s.title || "Cảnh " + index) +
      "</strong><small>" +
      Number(s.duration) +
      ' giây</small></div><span class="ms-badge">' +
      st +
      "</span></header>" +
      '<div class="ms-pair"><div><div class="ms-media">' +
      (s.frame
        ? '<img loading="lazy" src="' +
          mediaUrl(v, s, "frame") +
          '" alt="Ảnh mở đầu cảnh ' +
          index +
          '">'
        : "<span>Ảnh mở đầu</span>") +
      "</div>" +
      '<div class="ms-tools">' +
      action(
        s.frame_approved ? "Bỏ duyệt" : "Duyệt ảnh",
        "image-ok",
        !s.frame,
      ) +
      action(
        s.frame ? "Vẽ lại" : "Vẽ ảnh",
        "image",
        needsPortrait || !s.prompt,
      ) +
      "</div></div>" +
      '<div><div class="ms-media">' +
      (s.clip
        ? '<video controls playsinline preload="metadata" src="' +
          mediaUrl(v, s, "clip") +
          '"></video>'
        : "<span>" +
          (s.frame_approved ? "Sẵn sàng tạo clip" : "Duyệt ảnh trước") +
          "</span>") +
      "</div>" +
      '<div class="ms-tools">' +
      (s.clip
        ? action(s.approved ? "Bỏ duyệt" : "Duyệt clip", "clip-ok")
        : "") +
      action(s.clip ? "Tạo lại" : "Làm video", "clip", !s.frame_approved) +
      "</div></div></div>" +
      (s.stale_join
        ? '<p class="studio-error">Khung cuối cảnh trước đã đổi. Xem lại mối nối.</p>'
        : "") +
      (s.qc?.passed === false
        ? '<p class="studio-error">QC · ' +
          esc((s.qc.issues || []).join(" · ")) +
          "</p>"
        : "") +
      (s.visual_qc && Number.isFinite(Number(s.visual_qc.score))
        ? '<div class="ms-vqc-badge ' +
          (s.visual_qc.passed !== false ? "ok" : "warn") +
          '">' +
          (s.visual_qc.passed !== false ? "👁️ Vision QC · " : "⚠️ Vision QC · ") +
          Number(s.visual_qc.score) +
          "/100" +
          (s.visual_qc.issues?.length
            ? " — " + esc(s.visual_qc.issues.join(" · "))
            : "") +
          "</div>"
        : "") +
      (s.reference_source
        ? "<small>Ảnh đã gửi · " +
          esc(
            {
              anchor: "Ảnh neo gốc",
              last_frame: "Khung cuối cảnh trước",
              fallback_image: "Ảnh neo dự phòng",
              independent: "Tạo từ mô tả",
            }[s.reference_source],
          ) +
          "</small>"
        : "") +
      (s.previous_take?.clip
        ? '<details><summary>So sánh với bản trước</summary><video controls src="' +
          mediaUrl(v, s, "previous_clip") +
          "&take=" +
          encodeURIComponent(s.previous_take.clip) +
          '"></video>' +
          action("Khôi phục bản trước", "restore") +
          action("Bỏ bản trước", "discard_previous") +
          "</details>"
        : "") +
      "<p>" +
      esc(s.voiceover || "Đang chờ kịch bản") +
      "</p>" +
      (s.speech
        ? '<audio controls preload="none" src="' +
          mediaUrl(v, s, "speech") +
          '"></audio>'
        : "") +
      '<div class="ms-tools">' +
      action("Sửa", "edit", !s.prompt) +
      action(s.speech ? "Đọc lại" : "Đọc thử", "speech", !s.voiceover) +
      action("Thay ảnh", "replace", !s.prompt) +
      action("👁️ Chấm đồng nhất", "vision_qc", !s.frame) +
      (s.frame_approved
        ? '<a class="btn btn-ghost btn-sm" download href="/api/muse-studio/' +
          encodeURIComponent(v.id) +
          "/scenes/" +
          index +
          '/handoff">Gói bàn giao cảnh</a>'
        : "") +
      "</div>" +
      (s.error ? '<p class="studio-error">' + esc(s.error) + "</p>" : "") +
      "<details><summary>Prompt của cảnh</summary><p>" +
      esc(s.used_prompt || s.prompt || "") +
      "</p></details></article>"
    );
  };
  const checks = [
    ["Có hình", !!report.width],
    ["Có âm thanh", !!report.has_audio],
    ["Có thời lượng", report.duration_s > 0],
    ["Đúng số cảnh", approvedClips === total || stillMode],
    ...(v.include_subtitles === false ? [] : [["Có phụ đề", !!v.srt_url]]),
    ["Âm lượng lời kể", report.lufs >= -24 && report.lufs <= -10],
    ["Đỉnh âm thanh", Number.isFinite(report.peak) && report.peak <= -1],
  ];
  const output =
    '<aside class="ms-output"><h4>Bản dựng</h4>' +
    (done
      ? '<video controls src="' +
        prefix +
        "/video?v=" +
        v.updated_at +
        '"></video>' +
        '<div class="ms-thumb-box"><img loading="lazy" src="/api/muse-studio/' +
        encodeURIComponent(v.id) +
        "/thumbnail?v=" +
        v.updated_at +
        '" alt="Smart Thumbnail" onerror="this.closest(\'.ms-thumb-box\').hidden=true"></div>' +
        '<div class="studio-actions"><a class="btn btn-ghost" href="' +
        prefix +
        '/video" download>Tải MP4</a>' +
        '<a class="btn btn-ghost" href="/api/muse-studio/' +
        encodeURIComponent(v.id) +
        '/thumbnail" download="' +
        esc(v.id) +
        '_thumbnail.jpg">Tải Thumbnail</a>' +
        (v.srt_url ? '<a class="btn btn-ghost" href="' + prefix + '/subtitles" download>Phụ đề SRT</a>' : "") +
        '<a class="btn btn-ghost" href="/api/factory/publish-pack/' +
        encodeURIComponent(v.id) +
        '" download>Gói đăng bài</a>' +
        action("Sửa trong trình dựng", "timeline") +
        "</div>" +
        "<h4>Kiểm định thành phẩm</h4><p>" +
        checks.filter((x) => x[1]).length +
        "/" + checks.length + " mục đạt</p><dl><dt>Thời lượng</dt><dd>" +
        timeLabel(report.duration_s) +
        "</dd><dt>Khung hình</dt><dd>" +
        esc(report.width || "—") +
        " × " +
        esc(report.height || "—") +
        "</dd><dt>Độ to</dt><dd>" +
        (report.lufs ?? "—") +
        " LUFS</dd><dt>Đỉnh thực</dt><dd>" +
        (report.peak ?? "—") +
        " dBTP</dd></dl><details><summary>Kết quả đo</summary>" +
        checks
          .map(([t, ok]) => "<p>" + (ok ? "✓ " : "○ ") + t + "</p>")
          .join("") +
        "</details>"
      : "<p>Phim xem trước và kết quả kiểm định sẽ hiện sau khi ghép.</p>") +
    "</aside>";
  html +=
    '<div class="' +
    (chapter ? "ms-chapter" : "ms-board-layout") +
    '"><div class="ms-grid">' +
    (rows.map(shot).join("") || "<p>Chưa có cảnh trong bộ lọc này.</p>") +
    "</div>" +
    output +
    "</div>";
  board.innerHTML = html;
  board.querySelectorAll("video,audio").forEach((n) => {
    const old = preserved.get(n.getAttribute("src"));
    if (old) n.replaceWith(old);
  });
  board.querySelectorAll("[data-ms-filter]").forEach(
    (b) =>
      (b.onclick = () => {
        studioState.filter = b.dataset.msFilter;
        drawMuseBoard();
      }),
  );
  board
    .querySelectorAll("[data-do]")
    .forEach(
      (b) => (b.onclick = () => guidedAction(v, b).catch(reportFailure)),
    );
}
async function stage(v, action, indexes = [], extra = {}) {
  try {
    await editorApi(
      "/api/muse-studio/" + encodeURIComponent(v.id) + "/actions",
      "POST",
      {
        action,
        indexes,
        version: v.updated_at,
        ...(action === "image" && {
          max_concurrent: Math.max(1, Math.min(12, parseInt(el("cfgConcurrent").value, 10) || 6)),
        }),
        ...extra,
      },
    );
  } finally {
    await refreshMuseBoard();
  }
}
async function guidedAction(v, b) {
  const op = b.dataset.do,
    scenes = v.scenes,
    i = Number(b.closest("[data-index]")?.dataset.index),
    s = scenes.find((x) => x.index === i);
  const targets = (f) => scenes.filter(f).map((x) => x.index);
  if (op === "layout") {
    editor.chapterLayout = !editor.chapterLayout;
    studioState.stamp = "";
    drawMuseBoard();
    return;
  }
  if (op === "draft") return editStudioDraft(v);
  if (op === "rebuild") return copyToReview(v);
  if (op === "timeline") return openCurrentBatchInShotDesk(v.id);
  if (op === "settings") return chapterSettings(v);
  if (op === "edit") {
    return dialog(
      "Sửa cảnh " + i,
      '<label>Prompt và góc máy<textarea name="prompt" required maxlength="5000">' +
        esc(s.prompt) +
        "</textarea></label>" +
        '<label>Lời kể<textarea name="voiceover" required maxlength="3000">' +
        esc(s.voiceover) +
        '</textarea></label><label>Giọng kể<select name="voice">' +
        (v.cast || [])
          .filter((c) => c.voice)
          .map(
            (c) =>
              '<option value="' +
              esc(c.voice) +
              '">Giọng ' +
              esc(c.name) +
              "</option>",
          )
          .join("") +
        voiceSelectHtml(s.voice || v.voice, false) +
        "</select></label>" +
        '<label>Nhân vật xuất hiện (tên trong thư viện, cách nhau bằng dấu phẩy)<input name="characters" value="' +
        esc((s.characters || []).join(", ")) +
        '"></label>' +
        '<label>Bối cảnh và thời điểm<input name="setting" maxlength="500" value="' +
        esc(s.setting || "") +
        '"></label>' +
        '<label>Thoại nhân vật (mỗi dòng ghi tên và lời thoại)<textarea name="dialogue">' +
        esc(s.dialogue || "") +
        "</textarea></label>" +
        '<label><span><input name="new_segment" type="checkbox" ' +
        (s.new_segment ? "checked" : "") +
        "> Bắt đầu phân đoạn mới</span></label>" +
        '<label>Chuyển sang cảnh sau<select name="transition_out"><option value="dissolve">Hòa tan</option><option value="cut" ' +
        (s.transition_out === "cut" ? "selected" : "") +
        ">Cắt thẳng</option></select></label>" +
        '<label>Thời lượng (giây)<input name="duration" type="number" min="1" max="60" required value="' +
        Number(s.duration) +
        '"></label>',
      (data) =>
        stage(v, "edit", [i], {
          ...Object.fromEntries(data),
          characters: String(data.get("characters") || "")
            .split(",")
            .map((n) => n.trim())
            .filter(Boolean),
          new_segment: data.has("new_segment"),
          duration: Number(data.get("duration")),
        }),
    );
  }
  if (
    op === "clip-ok" &&
    !s.approved &&
    (s.stale_join || s.qc?.passed === false)
  )
    return dialog(
      "Duyệt cảnh có cảnh báo QC",
      "<p>" +
        esc(
          [
            ...(s.qc?.issues || []),
            ...(s.stale_join ? ["Khung cuối cảnh trước đã đổi."] : []),
          ].join(" · "),
        ) +
        "</p>",
      () => stage(v, "approve_clip", [i], { approved: true, accept_qc: true }),
      "Đã xem cảnh, chấp nhận",
    );
  if (op === "replace")
    return dialog(
      "Thay ảnh mở đầu",
      '<label>Ảnh của cảnh<input required type="file" name="file" accept="image/png,image/jpeg,image/webp"></label>',
      async (data) =>
        stage(v, "replace", [i], {
          upload: await uploadStudioFile(data.get("file")),
        }),
    );
  if (op === "bridge") {
    b.disabled = true;
    try {
      const res = await editorApi(
        "/api/muse-studio/" + encodeURIComponent(v.id) + "/bridge-shots",
        "POST",
        { version: v.updated_at },
      );
      await refreshMuseBoard();
      if (res.inserted > 0)
        showToast(
          "Đã chèn " +
            res.inserted +
            " cảnh cầu nối tự động (tổng " +
            res.scenes +
            " cảnh).",
          "success",
        );
      else
        showToast("Không còn bước nhảy bối cảnh/thời gian cần chèn cầu nối.", "info");
    } finally {
      b.disabled = false;
    }
    return;
  }
  if (op === "clips") {
    const pending = targets((s) => s.frame_approved && !s.clip);
    if (!pending.length) return;
    if (pending.length === 1) return stage(v, "clip", pending);
    const defaultMode = v.clip_parallel_mode || "sequential";
    const defaultWorkers = Math.max(
      2,
      Math.min(6, parseInt(el("cfgConcurrent")?.value, 10) || 3),
    );
    return dialog(
      "Làm video AI (" + pending.length + " cảnh)",
      '<label>Chế độ dựng video AI (Tính năng #6)<select name="clip_parallel_mode">' +
        Object.entries(clipParallelModeLabels)
          .map(
            ([k, t]) =>
              '<option value="' +
              esc(k) +
              '" ' +
              (k === defaultMode ? "selected" : "") +
              ">" +
              esc(t) +
              "</option>",
          )
          .join("") +
        "</select></label>" +
        clipParallelNoteHtml +
        '<label>Số luồng phân đoạn song song (chỉ áp dụng khi chọn Song song theo phân đoạn)<input name="max_concurrent" type="number" min="1" max="12" value="' +
        defaultWorkers +
        '"></label>',
      (data) =>
        stage(v, "clip", pending, {
          clip_parallel_mode:
            data.get("clip_parallel_mode") || "sequential",
          max_concurrent: Math.max(
            1,
            Math.min(12, Number(data.get("max_concurrent")) || defaultWorkers),
          ),
        }),
      "Bắt đầu làm video",
    );
  }
  const multi = {
    emphasis: ["emphasis", targets((s) => s.voiceover)],
    "vision-qc-all": ["vision_qc", targets((s) => !!s.frame)],
    images: ["image", targets((s) => s.prompt && !s.frame)],
    "images-ok": [
      "approve_image",
      targets((s) => s.frame && !s.frame_approved),
    ],
    clips: ["clip", targets((s) => s.frame_approved && !s.clip)],
    "clips-ok": [
      "approve_clip",
      targets(
        (s) => s.clip && !s.approved && !s.stale_join && s.qc?.passed !== false,
      ),
    ],
  };
  b.disabled = true;
  try {
    if (multi[op]) await stage(v, ...multi[op]);
    else if (op === "image-ok" || op === "clip-ok")
      await stage(
        v,
        op === "image-ok" ? "approve_image" : "approve_clip",
        [i],
        { approved: !(op === "image-ok" ? s.frame_approved : s.approved) },
      );
    else if (op === "compose" || op === "narrated") {
      const narrated = op === "narrated";
      const useStills = narrated && scenes.some((scene) => !scene.approved);
      const note = useStills
        ? "<p>Các cảnh chưa có clip sẽ dùng ảnh đã duyệt có chuyển động nhẹ.</p>"
        : "";
      return dialog(
        narrated ? "Dựng phim có lời kể" : "Ghép clip · giữ tiếng AI",
        note + '<label>Phụ đề<select name="subtitles"><option value="yes">' +
          (narrated ? "Gắn phụ đề và xuất SRT" : "Xuất tệp SRT riêng") +
          '</option><option value="no">Không phụ đề</option></select></label>',
        (data) => stage(v, op, [], {
          include_subtitles: data.get("subtitles") === "yes",
          allow_stills: useStills,
        }),
        "Xuất phim",
      );
    } else await stage(v, op, s ? [i] : []);
  } finally {
    b.disabled = false;
  }
}
