"""Visual directions used by the studio picker and the actual Muse prompts."""

GROUPS = [
    (
        "Điện ảnh thực tế & TVC quảng cáo",
        [
            (
                "cinematic",
                "Điện ảnh 4K siêu thực",
                "Ánh sáng điện ảnh, chuyển động máy mượt",
                "Realistic cinematic photography, motivated lighting, smooth motivated camera moves.",
            ),
            (
                "review_product",
                "Review sản phẩm / TikTok Affiliate",
                "Cận sản phẩm, ánh sáng studio sáng",
                "Clean close product coverage, faithful scale, materials, packaging and label, soft studio daylight.",
            ),
            (
                "serial",
                "Kể chuyện liền mạch",
                "Giữ nhân vật và mạch hành động qua cảnh",
                "Match action, eyelines, screen direction, wardrobe and the spatial geography across cuts.",
            ),
            (
                "ugc",
                "Quay đời thường (UGC / Vlog)",
                "Góc nhìn gần gũi từ máy cầm tay",
                "Handheld smartphone coverage, natural window light, observational framing.",
            ),
            (
                "luxury",
                "TVC quảng cáo sang trọng",
                "Slow motion, chất liệu và ánh sáng viền",
                "Luxury commercial, restrained slow motion, sculpted rim light, precise material detail.",
            ),
        ],
    ),
    (
        "Người que, tí hon & hoạt hình độc đáo",
        [
            (
                "stick",
                "Người que hài hước",
                "Nét vẽ tối giản, động tác rõ",
                "Minimal expressive stick figures, consistent silhouettes, clean comedic timing.",
            ),
            (
                "miniature",
                "Người tí hon",
                "Thế giới thu nhỏ, macro tilt-shift",
                "Miniature people in detailed dioramas, macro tilt-shift photography.",
            ),
            (
                "forest_spirit",
                "Người rừng xanh tí hon",
                "Tinh linh rừng, ánh sáng sinh học",
                "Tiny forest spirits, moss textures, luminous woodland scenery.",
            ),
            (
                "3d_pixar",
                "Hoạt hình 3D chiếu rạp",
                "Nhân vật biểu cảm, ánh sáng mềm",
                "Theatrical 3D animation, expressive faces, soft bounce light.",
            ),
            (
                "anime",
                "Anime Nhật Bản thơ mộng",
                "Nét vẽ tay và nền trời nhiều lớp",
                "Hand-drawn anime, layered painted skies and expressive character animation.",
            ),
            (
                "clay",
                "Đất sét / Stop-motion",
                "Bề mặt đất nặn thủ công",
                "Handcrafted clay models, tactile stop-motion animation, miniature sets.",
            ),
        ],
    ),
    (
        "Cổ trang, viễn tưởng, thú cưng & xu hướng",
        [
            (
                "donghua",
                "Cổ trang / Tiên hiệp 3D",
                "Kiến trúc cổ, trang phục nhiều lớp",
                "Historical fantasy, layered silk costumes and misty mountains.",
            ),
            (
                "cyberpunk",
                "Cyberpunk / Viễn tưởng",
                "Đèn neon, thành phố về đêm",
                "Neon reflections, futuristic urban architecture and atmospheric night light.",
            ),
            (
                "pet",
                "Thú cưng nhân hoá đáng yêu",
                "Thú nhỏ biểu cảm, lông mềm",
                "Anthropomorphic pets, soft fur detail and expressive stylized gestures.",
            ),
            (
                "asmr",
                "ASMR cận cảnh siêu thực",
                "Chuyển động chậm, bề mặt sắc nét",
                "Macro textures, deliberate tactile movements, controlled shallow focus.",
            ),
            (
                "history",
                "Tài liệu / Lịch sử tái hiện",
                "Trang phục và đạo cụ đúng thời kỳ",
                "Observational historical reconstruction, period-appropriate sets, props and clothing.",
            ),
        ],
    ),
    (
        "Việt Nam, tâm linh & cảm xúc",
        [
            (
                "zen",
                "Thiền & Phật pháp an yên",
                "Chùa cổ, sương sớm, ánh nến",
                "Quiet Vietnamese temple courtyards, dawn mist, gentle meditative camera movement.",
            ),
            (
                "folk",
                "Cổ tích Việt Nam",
                "Làng quê và chất liệu dân gian",
                "Vietnamese folklore, village settings, traditional costumes and folk-art detail.",
            ),
            (
                "horror_dark",
                "Truyện ma / Bí ẩn",
                "Tối, sương mù, hồi hộp",
                "Low-key lighting, suspended mist, atmospheric mystery and slow reveals.",
            ),
            (
                "food",
                "Ẩm thực đường phố",
                "Món ăn bốc khói, cận tay chế biến",
                "Vietnamese street food, natural steam, lively ambient light, appetizing macro detail.",
            ),
        ],
    ),
]


def install(styles):
    for _, rows in GROUPS:
        for key, name, _, prompt in rows:
            styles.setdefault(
                key,
                {"id": key, "name": name, "prompt_suffix": prompt, "aspect_hint": name},
            )
