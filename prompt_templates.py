"""
prompt_templates.py – AI Movie Studio
-------------------------------------
Thư viện Prompt Mẫu, Kịch bản Chuẩn & Cẩm nang Từ điển Điện ảnh dành cho người dùng.
Giúp người mới bắt đầu dễ dàng copy/paste kịch bản từ ChatGPT, Claude, DeepSeek
với định dạng chuẩn để công cụ AI Movie Studio tự động bóc tách 100% không lỗi.
"""

import copy
from typing import Dict, Any, List

# ── 1. Prompt Master dành riêng cho ChatGPT / Claude / DeepSeek ──────────────
CHATGPT_MASTER_PROMPT = """Bạn là một Đạo diễn điện ảnh và Biên kịch gia Hollywood chuyên nghiệp.
Nhiệm vụ của bạn là viết kịch bản phân cảnh (Storyboard Shot-by-Shot) chuẩn xác để nạp thẳng vào phần mềm làm phim AI Movie Studio.

QUY TẮC CẤU TRÚC BẮT BUỘC (Để công cụ AI tự động bóc tách từng cảnh, nhân vật, prompt thị giác và giọng đọc):

1. Dòng đầu tiên là Tên phim:
🎬 TÊN PHIM: [Điền tên phim tại đây]

2. Mỗi cảnh được viết theo đúng khối mẫu sau (ngăn cách nhau bởi dòng trống hoặc dấu ---):
🎥 SHOT [Số cảnh] – [Tiêu đề cảnh ngắn gọn]
Duration: [Thời lượng từ 3 đến 8]s

PROMPT:
[Mô tả góc máy, ánh sáng, màu sắc, trang phục và hành động của nhân vật bằng TIẾNG ANH điện ảnh Hollywood. TUYỆT ĐỐI KHÔNG dùng từ bạo lực máu me. Luôn kết thúc bằng: shot on ARRI Alexa 35, volumetric lighting, photorealistic 8k, masterpiece, no text, no watermark]

🎙️ Hội thoại tiếng Việt:
[Tên nhân vật]: "[Lời thoại hoặc câu dẫn chuyện tiếng Việt đặt trong ngoặc kép]"

---

VÍ DỤ MẪU CHUẨN 2 CẢNH ĐỂ BẠN LÀM THEO:

🎬 TÊN PHIM: Huyền Thoại Kiếm Sĩ

🎥 SHOT 1 – Bước Chân Rừng Trúc
Duration: 5s

PROMPT:
Cinematic low-angle tracking shot of a solitary swordsman in dark hanfu robe walking slowly through misty bamboo forest, soft moonlight god rays filtering through bamboo leaves, atmospheric fog, shot on ARRI Alexa 35, anamorphic lens, 8k, masterpiece, no text, no watermark

🎙️ Hội thoại tiếng Việt:
Lương Phong: "Mười năm ẩn tích, không ngờ nơi này vẫn vương mùi hàn khí..."

---

🎥 SHOT 2 – Cuộc Chạm Trán Dưới Trăng
Duration: 6s

PROMPT:
Cinematic medium over-the-shoulder shot, a mysterious female assassin in deep indigo silk robes and silver veil mask standing on bamboo branch, wind fluttering her cape, moonlight rim lighting, 85mm portrait lens, photorealistic 8k, masterpiece, no text, no watermark

🎙️ Hội thoại tiếng Việt:
Phụng Anh: "Lương Phong, đêm nay thanh kiếm của ngươi không thể rời khỏi rừng trúc này đâu!"

---
Hãy viết kịch bản theo đúng cấu trúc trên cho chủ đề sau: """


# ── 2. Các Kịch Bản Mẫu Chuẩn Sẵn Sàng Nạp Vào Studio ─────────────────────────
SAMPLE_SCRIPTS: Dict[str, Dict[str, Any]] = {
    "kiem_hiep": {
        "id": "kiem_hiep",
        "title": "⚔️ Kiếm Hiệp – Cuộc Gặp Đêm Mưa Tuyết (Đối thoại Nam - Nữ)",
        "badge": "Điện ảnh 16:9",
        "aspect_ratio": "16:9",
        "description": "Kịch bản mẫu 3 cảnh kiếm hiệp đối thoại kịch tính giữa kiếm sĩ Lương Phong và nữ sát thủ Phụng Anh.",
        "text": """🎬 TÊN PHIM: Tuyết Lạc Kiếm Ca

🎥 SHOT 1 – Vị Khách Đêm Đông
Duration: 5s

PROMPT:
Cinematic wide establishing shot of a solitary swordsman wearing a weathered conical hat and dark hanfu cloak, walking through heavy blizzard snowfall towards a glowing ancient wooden tavern, volumetric lantern light in darkness, shot on ARRI Alexa 35, 35mm film grain, 8k, masterpiece, no text, no watermark

🎙️ Hội thoại tiếng Việt:
Lương Phong: "Tuyết rơi dày đặc thế này... e là đêm nay khó lòng yên tĩnh."

---

🎥 SHOT 2 – Ánh Mắt Sau Lớp Màn Lụa
Duration: 5s

PROMPT:
Cinematic extreme close-up shot of a mysterious beautiful woman with sharp intense gaze behind an indigo silk face veil, warm candlelight flickering across her eyes, macro 85mm portrait lens, shallow depth of field, photorealistic 8k, masterpiece, no text, no watermark

🎙️ Hội thoại tiếng Việt:
Phụng Anh: "Mười năm không gặp, kiếm của ngươi vẫn mang theo hơi lạnh như ngày nào."

---

🎥 SHOT 3 – Lưỡi Kiếm Ngân Vang
Duration: 6s

PROMPT:
Cinematic dynamic medium tracking shot of the swordsman grasping the silver hilt of his ancient blade, radiant cold blue reflections gleaming on the polished steel, snowflakes swirling dramatically in the wind, dramatic rim lighting, anamorphic bokeh, masterpiece, no text, no watermark

🎙️ Hội thoại tiếng Việt:
Lương Phong: "Nếu đã nhận lệnh triều đình, xin mời rút kiếm!"
"""
    },
    "cyberpunk": {
        "id": "cyberpunk",
        "title": "🌆 Cyberpunk 2099 – Điệp Vụ Neon (Hành động viễn tưởng)",
        "badge": "Khoa học viễn tưởng",
        "aspect_ratio": "16:9",
        "description": "Kịch bản khoa học viễn tưởng với ánh sáng neon tương phản mạnh mẽ, góc quay drone và tracking shot.",
        "text": """🎬 TÊN PHIM: Neo Saigon 2099

🎥 SHOT 1 – Đô Thị Tương Lai
Duration: 6s

PROMPT:
Epic sweeping cinematic drone establishing shot over futuristic neon-lit cyberpunk metropolis in heavy rain, holographic billboards reflecting on wet asphalt, flying spinners ascending between towering megastructures, moody cyan and magenta lighting, shot on ARRI Alexa 35, photorealistic 8k, masterpiece, no text, no watermark

🎙️ Hội thoại tiếng Việt:
Người dẫn chuyện: "Năm 2099, thành phố không bao giờ ngủ, và bóng tối là nơi duy nhất cất giấu sự thật."

---

🎥 SHOT 2 – Tín Hiệu Mã Hóa
Duration: 5s

PROMPT:
Intense cinematic medium close-up shot of a female cybernetic agent wearing high-tech glowing visor, rain trickling down her leather trench coat, fingertips tapping glowing blue holographic terminal in dark alleyway, neon rim lighting, 8k, masterpiece, no text, no watermark

🎙️ Hội thoại tiếng Việt:
Phụng Anh: "Mã bảo mật đã bị phá. Chúng ta chỉ có đúng sáu mươi giây trước khi hệ thống phòng thủ kích hoạt!"

---

🎥 SHOT 3 – Cuộc Đột Kích
Duration: 6s

PROMPT:
Dynamic low-angle action tracking shot of a heavy armored cyborg warrior dashing forward, cybernetic armor glowing with radiant orange warning lights, sparks flying in rainy neon alley, sweeping camera movement, volumetric mist, anamorphic lens, masterpiece, no text, no watermark

🎙️ Hội thoại tiếng Việt:
Giang Trần: "Không cần sáu mươi giây... Ba mươi giây là đủ để san phẳng nơi này!"
"""
    },
    "tiktok_shorts": {
        "id": "tiktok_shorts",
        "title": "📱 TikTok / Shorts 9:16 – Bí Ẩn Cổ Mộ (Nhịp nhanh giật gân)",
        "badge": "Dọc 9:16 TikTok",
        "aspect_ratio": "9:16",
        "description": "Kịch bản chuẩn tỉ lệ 9:16 dọc điện thoại, mở đầu bằng câu hook giật gân và nhịp cắt nhanh 4 giây.",
        "text": """🎬 TÊN PHIM: Bí Mật Cổ Mộ 15 Giây

🎥 SHOT 1 – Cảnh Báo Đầu Tiên
Duration: 4s

PROMPT:
Cinematic 9:16 vertical portrait shot, torchlight illuminating ancient stone doorway carved with glowing runes in dark underground tomb, dust particles floating in golden light rays, mysterious dark atmosphere, vertical composition, 8k, masterpiece, no text, no watermark

🎙️ Hội thoại tiếng Việt:
Người dẫn chuyện: "Tuyệt đối đừng bao giờ mở cánh cửa đá thứ ba khi chưa nghe thấy tiếng chuông đồng..."

---

🎥 SHOT 2 – Phát Hiện Kinh Hoàng
Duration: 4s

PROMPT:
Cinematic 9:16 vertical close-up shot of young male archaeologist holding vintage brass lantern, his eyes widening in shock and awe as golden light reflects on strange ancient artifacts, dramatic shadow contrast, photorealistic 8k, masterpiece, no text, no watermark

🎙️ Hội thoại tiếng Việt:
Lương Phong: "Đợi đã... Những ký tự này vừa mới tự dịch chuyển!"

---

🎥 SHOT 3 – Sự Thức Tỉnh
Duration: 5s

PROMPT:
Cinematic 9:16 vertical dramatic low-angle shot, an ancient golden mask sculpture opening its glowing sapphire eyes, blue radiant shockwave pulsing through the cavern, stone debris floating in anti-gravity, volumetric lighting, vertical framing, masterpiece, no text, no watermark

🎙️ Hội thoại tiếng Việt:
Người dẫn chuyện: "Quá muộn rồi. Lời nguyền ngàn năm... đã chính thức thức tỉnh!"
"""
    },
    "lich_su": {
        "id": "lich_su",
        "title": "📜 Sử Thi Hào Khí – Bạch Đằng Giang (Tài liệu sử thi)",
        "badge": "Sử thi hào hùng",
        "aspect_ratio": "16:9",
        "description": "Kịch bản sử thi cổ trang hào hùng với đại cảnh sông nước, cọc gỗ và thần thái tướng lĩnh oai phong.",
        "text": """🎬 TÊN PHIM: Bạch Đằng Thiên Thu

🎥 SHOT 1 – Sóng Cuộn Bạch Đằng
Duration: 6s

PROMPT:
Epic cinematic wide panoramic shot of Bach Dang river at dawn, dramatic misty morning fog rising from water, silhouette of ancient wooden defense stakes rising from tidal water, golden hour sunrise piercing through clouds, shot on ARRI Alexa 35, 35mm lens, 8k, masterpiece, no text, no watermark

🎙️ Hội thoại tiếng Việt:
Người dẫn chuyện: "Dòng Bạch Đằng cuồn cuộn sóng trào, nơi lưu giữ chiến tích oanh liệt ngàn thu của cha ông."

---

🎥 SHOT 2 – Thần Thái Tướng Lĩnh
Duration: 5s

PROMPT:
Cinematic low-angle heroic portrait shot of a majestic Vietnamese general standing on flagship prow, wearing traditional dragon-embossed bronze armor and scarlet battle cape fluttering in sea breeze, resolute heroic gaze, volumetric sunlight, anamorphic lens, masterpiece, no text, no watermark

🎙️ Hội thoại tiếng Việt:
Giang Trần: "Thủy triều đang rút! Toàn quân chuẩn bị nghênh chiến bảo vệ bờ cõi non sông!"
"""
    },
    "phim_1_phut": {
        "id": "phim_1_phut",
        "title": "⏱️ Phim Hoàn Chỉnh 60 Giây – Kẻ Du Hành Thời Gian 2099 (8 Cảnh)",
        "badge": "Chuẩn 1 Phút (60s)",
        "aspect_ratio": "16:9",
        "description": "Kịch bản chuẩn xác 60 giây gồm 8 cảnh theo cấu trúc 3 hồi điện ảnh: Mở màn thế giới, gặp gỡ định mệnh, đối thoại kịch tính, bùng nổ năng lượng và kết thúc mở.",
        "text": """🎬 TÊN PHIM: Kẻ Du Hành Thời Gian 2099

🎥 SHOT 1 – Bình Minh Tương Lai
Duration: 8s

PROMPT:
Cinematic epic wide establishing aerial drone shot gliding over futuristic cyberpunk metropolis at twilight, towering neon skyscrapers piercing through misty clouds, flying spinners cruising between holographic billboards, warm cyan and amber volumetric lighting, shot on ARRI Alexa 35, anamorphic lens, 8k resolution, masterpiece, no text, no watermark

🎙️ Hội thoại tiếng Việt:
Người dẫn chuyện: "Năm 2099, khi dòng thời gian bị bẻ cong, thành phố này chính là nơi lưu giữ những ký ức cuối cùng của nhân loại."

---

🎥 SHOT 2 – Kẻ Lạ Mặt Xuất Hiện
Duration: 8s

PROMPT:
Cinematic medium tracking shot of a weary time-traveler named Minh wearing a worn dark trench coat with subtle glowing cybernetic collar, walking slowly through a rainy neon alley, steam rising from grates, wet asphalt reflections, camera tracking backward smoothly, shallow depth of field, 8k, photorealistic, masterpiece, no text, no watermark

🎙️ Hội thoại tiếng Việt:
Minh: "Nếu thiết bị Chrono này đếm ngược về con số không, toàn bộ thực tại sẽ tan biến."

---

🎥 SHOT 3 – Cuộc Chạm Trán Định Mệnh
Duration: 7s

PROMPT:
Cinematic medium over-the-shoulder shot, a cybernetic female operative named Lan in sleek matte black armor with radiant blue visor standing atop metal staircase, rain dripping from her visor, neon rim lighting casting sharp blue highlights, intense gaze, cinematic bokeh, masterpiece, no text, no watermark

🎙️ Hội thoại tiếng Việt:
Lan: "Minh, anh không thể tiếp tục can thiệp vào dòng thời gian nữa. Dừng lại đi!"

---

🎥 SHOT 4 – Lời Cảnh Báo Nghẹt Thở
Duration: 8s

PROMPT:
Cinematic intense close-up shot of Minh looking up with determination, rain droplets trickling down his face, neon reflections dancing in his eyes, background neon city lights blurred into beautiful anamorphic bokeh, 85mm portrait lens, photorealistic 8k, masterpiece, no text, no watermark

🎙️ Hội thoại tiếng Việt:
Minh: "Em không hiểu sao, Lan? Nếu tôi không khởi động lại dòng thời gian, ngày mai em sẽ không bao giờ tồn tại!"

---

🎥 SHOT 5 – Kích Hoạt Thiết Bị Thời Gian
Duration: 8s

PROMPT:
Cinematic dynamic low-angle shot, Minh quickly pulls out a glowing hexagonal Chrono device, blue energetic plasma sparks pulsing between his fingers, holographic temporal rings expanding outward, volumetric blue lighting illuminating his face, high-speed camera movement, masterpiece, no text, no watermark

🎙️ Hội thoại tiếng Việt:
Lan: "Đừng chạm vào lõi phản ứng! Nó sắp quá tải rồi!"

---

🎥 SHOT 6 – Bùng Nổ Sóng Năng Lượng
Duration: 7s

PROMPT:
Epic cinematic wide shot, a massive dome of brilliant golden and cyan temporal energy erupting between the two characters in the alleyway, rain droplets freezing mid-air in slow motion, holographic shockwave distorting space around them, awe-inspiring VFX, ARRI Alexa 35, 8k, masterpiece, no text, no watermark

🎙️ Hội thoại tiếng Việt:
Minh: "Tin tôi một lần này thôi... Giữ chặt lấy tay tôi!"

---

🎥 SHOT 7 – Khoảnh Khắc Lắng Đọng
Duration: 7s

PROMPT:
Cinematic emotional close-up shot of Lan reaching out her robotic gloved hand, her glowing visor retracting to reveal her emotional tearful eyes, golden light illuminating her face amidst swirling frozen sparks, 85mm lens, shallow depth of field, photorealistic 8k, masterpiece, no text, no watermark

🎙️ Hội thoại tiếng Việt:
Lan: "Dù ở bất kỳ dòng thời gian nào... em cũng sẽ tìm thấy anh."

---

🎥 SHOT 8 – Cánh Cổng Khởi Đầu Mới
Duration: 7s

PROMPT:
Cinematic wide silhouette shot of Minh and Lan holding hands as the golden temporal vortex swallows the alleyway, camera sweeping upward into the neon sky filled with swirling starlight, peaceful yet awe-inspiring resolution, cinematic lighting, 8k, masterpiece, no text, no watermark

🎙️ Hội thoại tiếng Việt:
Người dẫn chuyện: "Dòng thời gian bắt đầu viết lại. Và cuộc hành trình xuyên không gian... chính thức bắt đầu."
"""
    }
}


# ── 3. Từ Điển Từ Khóa Prompt Điện Ảnh (Cinematic Cheat Sheet) ───────────────
CINEMATIC_DICTIONARY: Dict[str, List[Dict[str, str]]] = {
    "camera_angles": [
        {
            "keyword": "Cinematic wide establishing shot, slow push in",
            "vn": "Toàn cảnh mở màn, camera tiến chậm",
            "desc": "Bao quát toàn bộ không gian, thiết lập bối cảnh hoành tráng mở đầu phim."
        },
        {
            "keyword": "Low-angle heroic upward shot",
            "vn": "Góc quay thấp hất lên oai phong",
            "desc": "Tạo cảm giác nhân vật quyền lực, mạnh mẽ và thần thái bất khả chiến bại."
        },
        {
            "keyword": "Dynamic medium tracking shot at waist level",
            "vn": "Máy quay bám theo chuyển động ngang hông",
            "desc": "Theo dõi bước chân chạy, đi dạo hoặc hành động của nhân vật rất mượt mà."
        },
        {
            "keyword": "Intense extreme close-up shot on eyes",
            "vn": "Cận cảnh đôi mắt kịch tính",
            "desc": "Lột tả trọn vẹn cảm xúc nội tâm, ánh mắt cảnh giác hoặc quyết tử."
        },
        {
            "keyword": "Over-the-shoulder dramatic conversation angle",
            "vn": "Góc nhìn qua vai đối thoại",
            "desc": "Góc máy chuẩn Hollywood cho các phân cảnh 2 nhân vật nói chuyện với nhau."
        },
        {
            "keyword": "Epic 360-degree orbital camera sweep",
            "vn": "Góc quay bay vòng cung 360 độ",
            "desc": "Camera lượn quanh nhân vật giữa bối cảnh hoành tráng, tạo cảm xúc cao trào."
        }
    ],
    "lighting": [
        {
            "keyword": "Cinematic volumetric lighting, god rays filtering through mist",
            "vn": "Tia sáng thể tích xuyên qua sương mờ (God rays)",
            "desc": "Tạo các luồng ánh sáng điện ảnh huyền ảo xuyên qua kẽ lá, cửa sổ hoặc sương đêm."
        },
        {
            "keyword": "Dramatic rim lighting, glowing character contour",
            "vn": "Ánh sáng viền nổi bật đường nét (Rim light)",
            "desc": "Đánh sáng từ phía sau để tách nhân vật nổi bật lên khỏi phông nền tối."
        },
        {
            "keyword": "Golden hour warm sunlight, soft anamorphic flare",
            "vn": "Nắng vàng hoàng hôn rực rỡ (Golden Hour)",
            "desc": "Ánh sáng ấm áp, lãng mạn và giàu cảm xúc của thời khắc mặt trời lặn."
        },
        {
            "keyword": "Cyberpunk neon rim lighting, dual-tone cyan and magenta",
            "vn": "Ánh sáng viền Neon xanh - hồng tương phản",
            "desc": "Tone màu khoa học viễn tưởng hiện đại rực rỡ và thời thượng."
        },
        {
            "keyword": "Moody chiaroscuro lighting, deep dramatic shadows",
            "vn": "Độ tương phản sáng tối ấn tượng (Chiaroscuro)",
            "desc": "Phong cách ánh sáng phim trinh thám Noir, bí ẩn và giàu chiều sâu."
        }
    ],
    "cameras_lenses": [
        {
            "keyword": "shot on ARRI Alexa 35, 35mm film grain, 4k UHD",
            "vn": "Máy quay ARRI Alexa 35, hạt phim 35mm",
            "desc": "Tiêu chuẩn vàng của máy quay Hollywood, cho màu da tự nhiên và độ chuyển màu êm ái."
        },
        {
            "keyword": "Zeiss Master Prime anamorphic lenses, 2.39:1 aspect ratio",
            "vn": "Ống kính Anamorphic Zeiss xóa phông điện ảnh",
            "desc": "Tạo vệt sáng ngang đặc trưng và bokeh bầu dục tuyệt đẹp như phim chiếu rạp."
        },
        {
            "keyword": "85mm portrait lens, shallow depth of field, creamy bokeh",
            "vn": "Ống kính chân dung 85mm xóa phông mờ ảo",
            "desc": "Làm nổi bật nhân vật chính, làm mờ hậu cảnh một cách êm dịu, sang trọng."
        },
        {
            "keyword": "photorealistic 8k, Unreal Engine 5 render aesthetic, masterpiece",
            "vn": "Độ nét siêu thực 8K, chất lượng kiệt tác",
            "desc": "Khóa độ sắc nét tối đa, kết xuất chi tiết chân thực không watermark."
        }
    ],
    "safe_action_replacements": [
        {
            "bad": "blood, bloody, bleeding, pool of blood",
            "good": "dramatic crimson rim light, swirling red embers",
            "reason": "Tránh từ khóa máu me gây khóa tài khoản; chuyển thành hiệu ứng ánh sáng đỏ điện ảnh."
        },
        {
            "bad": "stab, impale, pierce through body, slit throat",
            "good": "clashing dynamically against glowing artifact, swift martial parry",
            "reason": "Tránh đâm chém sát thương; chuyển thành pha đỡ đòn và va chạm binh khí tóe sáng."
        },
        {
            "bad": "smash down, pulverize, crush into dust",
            "good": "unleashing sweeping radiant shockwave, bursting with golden light",
            "reason": "Tránh hành động đập nát bạo lực; chuyển thành sóng xung kích và hào quang năng lượng."
        },
        {
            "bad": "kill, murder, execute, dead body, corpse",
            "good": "outmaneuver opponent with graceful martial technique, victorious pose",
            "reason": "Tránh yếu tố chết chóc; chuyển thành tư thế chiến thắng anh dũng."
        },
        {
            "bad": "gun, rifle, shooting bullets, explosions blast",
            "good": "channeling radiant energy beam, mystical light trail",
            "reason": "Tránh vũ khí nóng sát thương hiện đại; chuyển thành tia sáng ma pháp huyền bí."
        }
    ]
}


# ── 4. Quy Tắc Định Dạng Cú Pháp Kịch Bản ─────────────────────────────────────
FORMAT_RULES = [
    {
        "name": "1. Format Chuẩn Hollywood Shot-by-Shot (Khuyên dùng nhất ⭐)",
        "syntax": """🎥 SHOT 1 – [Tiêu đề cảnh]
Duration: [Số giây]s

PROMPT:
[Mô tả hình ảnh bằng tiếng Anh điện ảnh]

🎙️ Hội thoại tiếng Việt:
[Tên nhân vật]: "[Câu thoại tiếng Việt]"
""",
        "benefit": "Tool bóc tách chính xác 100% thời lượng, hình ảnh, nhân vật, giọng đọc và hỗ trợ tách từng cảnh độc lập."
    },
    {
        "name": "2. Format Dòng Thời Gian (Timeline 0-5s, 5-10s)",
        "syntax": """0-5s: [Màu sắc: Tone vàng ấm] Cinematic wide shot of swordsman in forest... Lương Phong: "Đêm nay trăng sáng quá."
5-10s: [Màu sắc: Tone lạnh] Intense close up shot... Phụng Anh: "Ngươi hãy bảo trọng."
""",
        "benefit": "Rất tiện khi bạn viết nhanh kịch bản theo trục thời gian tính theo giây."
    },
    {
        "name": "3. Format Phân Cảnh Đơn Giản (Cảnh 1, Cảnh 2)",
        "syntax": """Cảnh 1 (5s): Góc rộng bãi biển hoàng hôn tuyệt đẹp. Lương Phong: "Thời gian trôi nhanh quá."
Cảnh 2 (6s): Góc cận mặt người phụ nữ mỉm cười. Phụng Anh: "Chúng ta sẽ gặp lại nhau."
""",
        "benefit": "Tự nhiên, dễ gõ, hệ thống sẽ tự động dịch prompt tiếng Việt sang tiếng Anh điện ảnh."
    }
]


def get_prompt_sheet_data() -> Dict[str, Any]:
    """Trả về toàn bộ dữ liệu cấu trúc của Thư viện Sheet Prompt Mẫu.

    Trả bản copy sâu (deepcopy) để caller sửa thoải mái mà không mutate
    các global SAMPLE_SCRIPTS / CINEMATIC_DICTIONARY / FORMAT_RULES.
    """
    return {
        "success": True,
        "chatgpt_master_prompt": CHATGPT_MASTER_PROMPT,
        "sample_scripts": copy.deepcopy(SAMPLE_SCRIPTS),
        "cinematic_dictionary": copy.deepcopy(CINEMATIC_DICTIONARY),
        "format_rules": copy.deepcopy(FORMAT_RULES),
    }
