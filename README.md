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

---

## 🚀 Hướng Dẫn Cài Đặt & Khởi Chạy

### 1. Yêu cầu hệ thống
- Python 3.10+
- FFmpeg (Tự động tải qua `imageio-ffmpeg` hoặc cài đặt hệ thống)
- Gateway Muse2API đang chạy tại `http://localhost:18610`

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
MUSE2API_KEY=m2a_studio_secret_2026
MAX_CONCURRENT=12
PORT=8000
```

### 4. Khởi chạy Studio
```bash
python app.py
```
Mở trình duyệt truy cập: **`http://localhost:8000`**

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

---

## 📄 Bản Quyền
Phát triển cho hệ sinh thái sáng tạo nội dung số tự động hóa.
