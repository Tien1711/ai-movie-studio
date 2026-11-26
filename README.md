# 🎬 AI Movie Studio & Affiliate Video Creator

> **Nền tảng Tự Động Hóa Sản Xuất Phim Ngắn & Video Tiếp Thị Liên Kết (Affiliate Marketing) Bằng Trí Tuệ Nhân Tạo**
> Tối ưu hóa chuyển đổi cho TikTok Shop, Shopee Video, Facebook Reels và YouTube Shorts (Dọc 9:16 & Ngang 16:9).

---

## 🌟 Tính Năng Nổi Bật

### 1. 🛍️ Studio Tiếp Thị Liên Kết (Affiliate Video Engine)
- **4 Kiểu quay chuyên biệt (Shooting Styles)**:
  - 👤 **Người mẫu không lộ mặt**: Mẫu mặc/dùng sản phẩm, chỉ quay từ cổ trở xuống hoặc từ phía sau, loại bỏ hoàn toàn lỗi biến dạng mặt.
  - ✋ **Chỉ quay tay**: Cận cảnh đôi tay thao tác, đập hộp bóc hộp, xoay 360° trên nền bàn gỗ/đá cao cấp.
  - 📦 **Chỉ quay sản phẩm**: Quảng cáo studio 3D điện ảnh, xoay mâm 360°, hoàn toàn không có người hoặc tay.
  - 🧑 **KOC lộ mặt đầy đủ**: KOC review trực tiếp, biểu cảm hài hước, khóa chặt nhận diện khuôn mặt đồng nhất 100%.
- **4 Hình mẫu KOC Persona**:
  - 🧑 *Tuấn Bựa*: Nam 25 tuổi, hài hước bựa dí dỏm, đồng bộ giọng đọc `VieNeu-TTS: Adam bựa`.
  - 👩 *Trúc Linh*: Nữ 23 tuổi, tươi tắn rạng rỡ, giọng điệu ngọt ngào, review mỹ phẩm/gia dụng.
  - 👩‍💼 *Mai Phương*: Nữ chuyên gia 29 tuổi, thanh lịch uy tín, sản phẩm sức khỏe & cao cấp.
  - 🧔 *Hoàng Nam*: Nam reviewer 30 tuổi, lịch lãm nam tính, công nghệ & phong cách sống.
- **5 Framework Bán Hàng Triệu View**:
  - `PAS` (Problem - Agitate - Solution)
  - `Hài Hước / Bựa Troll` (Tối ưu riêng cho ⭐ Adam bựa)
  - `Review KOC & Đập Hộp Chân Thực`
  - `Before / After` (Biến hóa ngoạn mục)
  - `AIDA` (Attention - Interest - Desire - Action)

### 2. 🎙️ Hệ Thống Lồng Tiếng Đa Tầng (Multi-Engine TTS Manager)
- **VieNeu-TTS**: Giọng đọc nơ-ron tiếng Việt cảm xúc cao, hỗ trợ preset *Adam bựa*, *Trúc Ly*, *Mai Anh*.
- **OmniVoice**: Mô hình TTS đa ngôn ngữ (>600 ngôn ngữ) với voice design và zero-shot voice cloning.
- **Microsoft Edge-TTS**: Giọng đọc đám mây mượt mà (Hoài My, Nam Minh).
- Tự động ngắt nhịp, tạo phụ đề SRT đồng bộ mili-giây.

### 3. 🎞️ Bộ Hậu Kỳ Điện Ảnh Cao Cấp (Video Composer & Pipeline)
- **FFmpeg Concat Filter**: Ghép nối các phân cảnh mượt mà dứt khoát, loại bỏ 100% lỗi đứng hình/đóng băng frame.
- **Burn-in Subtitles**: Khắc phụ đề trực tiếp vào video, hỗ trợ font chữ lớn, viền đen sắc nét, chống lỗi ký tự đặc biệt và emoji.
- **Audio Ducking**: Tự động giảm âm lượng nhạc nền khi có tiếng thoại và nâng lên khi hết thoại.
- **Server-Sent Events (SSE)**: Theo dõi tiến trình render thời gian thực theo từng phân cảnh.

### 4. 🏭 Xưởng tự động (Auto Factory)
- **Một cú bấm ra nhiều video hoàn chỉnh**: chọn nguồn nội dung, số video (1–10), số cảnh mỗi video (3–8), phong cách hình ảnh (11 style), giọng đọc — bấm **Bắt đầu sản xuất**, xưởng tự viết kịch bản, đọc giọng, vẽ ảnh, dựng video và làm sẵn gói đăng bài cho từng video.
- **Nguồn nội dung tự động, không cần key mới**:
  - 📈 **Ý tưởng đang hot**: Google Trends Việt Nam (RSS công khai).
  - 📰 **Tin tức mới**: VnExpress RSS, tự động fallback sang Tuổi Trẻ nếu feed chính lỗi.
  - ✏️ **Tự nhập chủ đề**: mỗi dòng là một video (tối đa 10).
  - Kết quả RSS được cache 30 phút; nguồn lỗi thì tự dùng pool ý tưởng có sẵn và báo rõ trên giao diện.
- **Gói đăng bài (.zip) cho mỗi video hoàn tất**: video `final.mp4`, `thumbnail.jpg` (trích frame đầu, đúng khung hình), `title.txt`, `description.txt` (tiêu đề + mô tả + hashtag tự sinh), `subtitles.srt` (nếu có), `metadata.json`.
- **Giới hạn**: chưa đăng thẳng lên YouTube/TikTok (cần OAuth, ngoài phạm vi) — nút **Đăng bài** mở gói đăng bài để sao chép tiêu đề/mô tả hoặc tải file `.zip` về đăng tay.

### 5. 📖 Truyện thành video (Story-to-Video)
- **Truyện dài → video từng chương**: mỗi chương thành 1 tập video (9:16 / 16:9 / 1:1, kèm phụ đề), chạy qua hàng đợi batch có sẵn — không pipeline dựng mới.
- **Nguồn truyện** (6 tab): kho tác phẩm có sẵn, tìm trên web (Wikipedia/Wikisource tiếng Việt, không cần key), nhập từ link, tải file lên (TXT/MD/DOCX/EPUB, tối đa 60MB), dán văn bản (tối đa 500.000 ký tự), xào nấu AI.
- **Tách chương**: theo tiêu đề Chương/Hồi/Phần (số Ả Rập, La Mã hoặc chữ), không có tiêu đề thì cắt theo độ dài (mặc định 6.000 ký tự/chương, cấu hình được).
- **Xào nấu AI — chuyển thể deterministic, không dùng LLM ngoài**: 5 chế độ (Video Ngắn 60s / ma mị kiểu Nguyễn Ngọc Ngạn / Hồi hộp & Gay cấn / Triết lý & Chữa lành / Tóm tắt nhanh). Lời thoại lấy từ câu văn gốc đã gọn lại, không bịa thêm tình tiết; kịch bản sửa trực tiếp được ở UI trước khi tạo video.
- **Giới hạn**: kho có sẵn chỉ gồm 4 truyện dân gian bản kể lại ngắn (Tấm Cám, Thạch Sanh, Ăn Khế Trả Vàng, Bí Ẩn Con Ma Nhà Họ Hứa) — toàn văn truyện dài lấy qua web/file/dán văn bản. PDF chưa hỗ trợ — chuyển sang DOCX/TXT trước khi tải lên.

---

## 🚀 Hướng Dẫn Cài Đặt & Khởi Chạy

### 1. Yêu cầu hệ thống
- Python 3.12 (phiên bản dùng để chạy CI)
- FFmpeg (Tự động tải qua `imageio-ffmpeg` hoặc cài đặt hệ thống)
- Gateway Muse2API tại `http://localhost:18610` hoặc gateway Browser theo mục bên dưới.

### 2. Cài đặt thư viện
```bash
git clone https://github.com/Tien1711/ai-movie-studio.git
cd ai-movie-studio

# Cài đặt các gói phụ thuộc
pip install -r requirements.txt
```

### 3. Cấu hình môi trường
Sao chép file `.env.example` thành `.env` và điều chỉnh thông số:
```bash
cp .env.example .env
```

Nội dung `.env`:
```env
MUSE2API_URL=http://localhost:18610
MUSE2API_KEY=
STUDIO_HOST=127.0.0.1
STUDIO_API_KEY=
MAX_CONCURRENT=12
PORT=8000
```

### 4. Khởi chạy Studio
```bash
python app.py
```
Mở [Studio trên máy này](http://127.0.0.1:8000). Điền `MUSE2API_KEY` do gateway cấp để dùng các tính năng render.

Để truy cập Studio từ máy khác, đặt `STUDIO_HOST=0.0.0.0` và cấp một `STUDIO_API_KEY` riêng. Dùng HTTPS khi triển khai qua mạng. Trình duyệt dùng phiên đăng nhập có thời hạn 12 giờ sau khi nhập khóa. Các công cụ gọi API có thể tiếp tục gửi header `X-API-Key`.

### 5. Kết nối qua Chrome hoặc Edge

Studio mặc định dùng `MUSE_PROVIDER=muse2api`. Chế độ `browser` dùng phiên Muse đã đăng nhập để tạo kịch bản, ảnh và video. Cửa sổ riêng sử dụng driver [Muse-Chat-MCP](https://github.com/duclm1x1/Muse-Chat-MCP). Profile Chrome đã ghép nối sử dụng tiện ích của Studio.

1. Cài Node.js 22 và Chrome hoặc Edge. Clone [Muse-Chat-MCP](https://github.com/duclm1x1/Muse-Chat-MCP) vào thư mục riêng và chạy `npm install` tại đó. Studio đã kiểm tra giao diện driver tại commit `6c00e8c`.
2. Trong `.env` của Studio, điền đường dẫn tuyệt đối vào `MUSE_CHAT_MCP_DIR` và tạo một khóa ngẫu nhiên riêng cho `MUSE_BROWSER_KEY`. Gateway dùng cổng `MUSE_BROWSER_PORT` (mặc định `18787`). Với Edge, thêm `MUSE_CHANNEL=msedge`.
3. Từ thư mục Studio, chạy lệnh sau và giữ gateway chạy. Đăng nhập Muse trong cửa sổ do gateway mở nếu bạn chọn dùng cửa sổ riêng. Đặt `MUSE_BROWSER_OPEN_SEPARATE=0` nếu chỉ dùng profile Chrome đã ghép nối.

   ```bash
   node --env-file=.env scripts/muse_browser_gateway.mjs
   ```

4. Vào **Cài đặt → Kết nối Muse**, chọn **Muse Browser (Chrome / Edge)** và bấm **Kiểm tra kết nối**. Bấm **Lưu chế độ** rồi khởi động lại Studio để áp dụng. Để quay lại API, chọn **Muse2API** trong cùng menu, lưu và khởi động lại. Studio ưu tiên lựa chọn đã lưu trên giao diện. `MUSE_PROVIDER` trong `.env` dùng làm mặc định trước khi lưu.

Browser xử lý một yêu cầu tại một thời điểm. Khi dùng ảnh tham chiếu, tải ảnh lên Studio. Gateway chờ ảnh hoặc video của lượt hiện tại kể cả khi phần văn bản đã hoàn thành. Gateway hỗ trợ liên kết chia sẻ và media nội tuyến như ảnh WebP, video MP4 hoặc WebM. Cửa sổ riêng dùng `downloadMedia` của Muse-Chat-MCP để lấy tệp nội tuyến. Tiện ích gửi tệp từ profile đã ghép nối sang gateway theo từng gói. Tệp nội tuyến được giữ tối đa một giờ, nên tải clip Quick Video về nếu cần lưu lâu hơn. Các luồng dựng phim tải media vào workspace.

Trong **Cài đặt → Kết nối Muse → Cuộc trò chuyện Muse**, chọn **Chỉ dùng chat chính** để gửi yêu cầu vào cuộc trò chuyện chính của tài khoản Muse. Các yêu cầu chạy sau khi lưu dùng lựa chọn mới. Với profile Chrome đã ghép nối, chế độ này cần tiện ích **AI Movie Studio – Muse Profile** phiên bản 1.2.0 trở lên.

Tỷ lệ khung hình và thời lượng là yêu cầu trong prompt. Kiểm tra tệp thực tế để xác nhận thông số đầu ra. [Thông số đo bằng ffprobe trong Muse Manual](https://github.com/duclm1x1/Muse-Chat-MCP/blob/6c00e8c/manual/MUSE_MANUAL.md)

#### Dùng profile Chrome đã đăng nhập Muse

1. Mở profile cần dùng từ Chrome. Trong profile đó, vào `chrome://extensions`, bật **Chế độ dành cho nhà phát triển**, rồi chọn **Tải tiện ích đã giải nén** và trỏ tới thư mục `chrome-muse-profile` của Studio.
2. Trong **Cài đặt → Kết nối Muse → Muse Browser**, bấm **Tạo mã ghép nối**. Mở tiện ích **AI Movie Studio – Muse Profile** trong profile Chrome đích, nhập tên để nhận biết profile và dán mã. Tiện ích mở một tab Muse dành cho Studio, dùng phiên đang đăng nhập trong profile này. Điền đúng cổng gateway nếu bạn đã thay cổng mặc định.
3. Bấm **Tải lại profile** để xem trạng thái. Gateway tự chọn profile đầu tiên khi người dùng chưa chọn chế độ riêng. Khi có nhiều profile, chọn profile muốn dùng trong menu. Lựa chọn có hiệu lực ngay khi gateway rảnh.

Giữ tab Muse của tiện ích mở khi render. Nếu Studio báo Muse đang chờ xác nhận, xem yêu cầu và bấm xác nhận trong tab đó để tiếp tục. Sau khi cập nhật tiện ích, bấm **Tải lại** tại `chrome://extensions`, rồi bấm **Mở tab Muse cho Studio** trong tiện ích. Profile đã ghép nối được lưu lại. Nếu phiên Muse hết hạn, đăng nhập lại trong profile đang dùng.

---

## 📁 Cấu Trúc Dự Án

```text
ai_movie_studio/
├── app.py                     # Máy chủ FastAPI chính, REST APIs, SSE streams
├── affiliate_engine.py        # Động cơ Tiếp Thị Liên Kết: Kịch bản, KOC, 4 Kiểu quay
├── batch_engine.py            # Động cơ render song song đa luồng qua Muse2API
├── composer.py                # Xử lý hậu kỳ FFmpeg: Ghép clip, trộn nhạc, khắc phụ đề
├── tts_manager.py             # Quản lý âm thanh đa engine: VieNeu, OmniVoice, Edge-TTS
├── prompt_templates.py        # Thư viện mẫu prompt điện ảnh cho phim AI
├── shot_desk.py               # Bảng điều khiển phân cảnh (Shot Desk)
├── static/
│   └── index.html             # Giao diện Web Studio hiện đại (Dark theme điện ảnh)
├── requirements.txt           # Danh mục thư viện Python
├── .env.example               # Mẫu cấu hình môi trường
├── .gitignore                 # Quy tắc loại trừ tệp rác / tệp media lớn
└── test_affiliate_suite.py    # Bộ kiểm thử tự động toàn diện module Affiliate
```

---

## 🧪 Kiểm Thử Hệ Thống (Test Suite)

Để kiểm tra tính toàn vẹn của hệ thống:
```bash
python test_affiliate_suite.py
```
Kết quả kiểm thử tự động xác nhận 6/6 tính năng hoạt động hoàn hảo:
```text
✅ test_templates_catalog PASSED (Categories + Frameworks + KOC + 4 Shooting Styles)
✅ test_shooting_styles_prompts PASSED (Faceless + Hands + Product + KOC Face)
✅ test_local_script_generator_with_styles PASSED
✅ test_local_script_generator_hai_bua PASSED
✅ test_api_generate_script_endpoint PASSED (HTTP faceless shooting style)
✅ test_api_render_affiliate_endpoint PASSED (Job ID: d6ebaf615951)
🎉 TẤT CẢ 6/6 TESTS AFFILIATE MARKETING ĐÃ PASS 100%!
```

### CI (GitHub Actions)

Workflow `.github/workflows/ci.yml` chạy kiểm thử tự động trên Ubuntu và Windows với Python 3.12 khi push hoặc mở pull request vào `main`. Cài Node.js để chạy các bài kiểm thử JavaScript.

```bash
python -m pip install -r requirements.txt pytest==8.3.4
node --check static/js/app.js
node --check static/js/ui.js
node --test scripts/test_muse_browser_gateway.mjs
python -m pytest -q --ignore=test_audit_fixes.py --ignore=test_new_audit_fixes.py --ignore=test_e2e_video_subtitles.py --ignore=test_tts_multi_engine.py
```

Chạy riêng bốn tệp kiểm thử tích hợp khi đã cấu hình Studio, Muse2API và dịch vụ TTS cần thiết.

---

## 📄 Bản Quyền
Phát triển cho hệ sinh thái sáng tạo nội dung số tự động hóa.
