"""
affiliate_engine.py – AI Movie Studio
--------------------------------------
Chuyên biệt sản xuất Video Tiếp Thị Liên Kết (Affiliate Marketing)
Dành cho TikTok Shop, Shopee Video, Facebook Reels, YouTube Shorts.

Hỗ trợ các công thức kịch bản chuyển đổi cao:
1. PAS (Problem - Agitate - Solution - Demo - CTA)
2. Hài Hước / Bựa Troll (Tối ưu cho giọng ⭐ Adam bựa)
3. Review KOC / Đập Hộp Chân Thực (Tối ưu cho Trúc Ly / Hoài My)
4. Before / After (Chuyển hoá trước và sau khi dùng)
5. AIDA (Attention - Interest - Desire - Action)

Tự động sinh:
- Hook 3 giây giữ chân người xem chống lướt qua
- Visual Prompt chuẩn Commercial Advertising 9:16
- Voiceover tiếng Việt tự nhiên, ngắt nghỉ nhịp nhàng cho TTS
- Badge / Sticker kêu gọi bấm giỏ hàng góc trái
"""

import os
import re
import json
import httpx
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field

# ── 1. Danh mục sản phẩm & Mẫu gợi ý (Presets) ──────────────────────────────

AFFILIATE_CATEGORIES = {
    "gadgets": {
        "id": "gadgets",
        "name": "Công nghệ & Phụ kiện",
        "icon": "📱",
        "sample_products": [
            "Giá đỡ điện thoại xoay 360 độ hợp kim nhôm",
            "Tai nghe Bluetooth chống ồn chủ động ANC",
            "Sạc dự phòng mini không dây hít từ tính",
            "Cáp sạc nhanh 3 trong 1 tự ngắt thông minh",
            "Đèn bàn LED chống cận cảm ứng điều chỉnh nhiệt độ màu"
        ],
        "default_pain": "Dây nhợ lằng nhằng, dùng phụ kiện rẻ tiền mau hỏng, nóng máy chai pin",
        "default_features": "Thiết kế kim loại cao cấp, sạc siêu nhanh chuẩn an toàn, nhỏ gọn bỏ túi mang đi bất cứ đâu",
        "default_voice": "vieneu:AdamBua",
        "style_tag": "modern tech gadgets, futuristic sleek studio aesthetic, clean neon accent"
    },
    "home": {
        "id": "home",
        "name": "Gia dụng thông minh",
        "icon": "🏠",
        "sample_products": [
            "Nồi chiên không dầu điện tử 8L kính trong suốt",
            "Máy hút bụi cầm tay không dây lực hút siêu mạnh",
            "Máy xay sinh tố cầm tay mini sạc pin tiện lợi",
            "Robot lau dọn nhà thông minh cảm biến laser",
            "Cây lau nhà tự vắt xoay 360 độ thế hệ mới"
        ],
        "default_pain": "Dọn dẹp nấu nướng mệt mỏi, tốn hàng giờ mỗi ngày, dầu mỡ bám bẩn ám mùi",
        "default_features": "Tự động hóa hoàn toàn, công nghệ hiện đại giúp tiết kiệm thời gian dọn dẹp, lau sạch bóng dễ dàng",
        "default_voice": "vieneu:AdamBua",
        "style_tag": "cozy modern Scandinavian home, bright warm sunlight, pristine clean kitchen and living room"
    },
    "beauty": {
        "id": "beauty",
        "name": "Mỹ phẩm & Chăm sóc da",
        "icon": "💄",
        "sample_products": [
            "Kem chống nắng nâng tone kiềm dầu kiềm mồ hôi 12h",
            "Serum Vitamin C mờ thâm sáng da ngừa lão hóa",
            "Son dưỡng môi collagen cấp ẩm căng mọng tự nhiên",
            "Sữa rửa mặt Amino Acid dịu nhẹ sạch sâu không khô da",
            "Kem dưỡng phục hồi B5 làm dịu mẩn đỏ nhẹ nhàng"
        ],
        "default_pain": "Da dầu mụn bết dính, nám sạm xỉn màu, dùng nhiều loại mỹ phẩm đắt tiền mà không thấy hiệu quả",
        "default_features": "Thành phần thiên nhiên lành tính, thẩm thấu nhanh, cho da sáng hồng rạng rỡ",
        "default_voice": "vieneu:TrucLy",
        "style_tag": "high-end cosmetic skincare commercial, soft pastel aesthetics, water splash, luminous dewy skin glow"
    },
    "fashion": {
        "id": "fashion",
        "name": "Thời trang & Phụ kiện",
        "icon": "👗",
        "sample_products": [
            "Áo thun Form rộng Oversize Cotton 100% thoáng mát",
            "Quần suông ống rộng cạp cao hack dáng siêu đỉnh",
            "Giày Sneaker độn đế 5cm êm chân tôn dáng",
            "Kính mát thời trang chống tia UV400 phân cực",
            "Túi xách kẹp nách da mềm cao cấp phong cách Hàn Quốc"
        ],
        "default_pain": "Mặc đồ dìm dáng, bí bách đổ mồ hôi, giặt vài lần là nhăn nhúm bạc màu",
        "default_features": "Chất vải cotton co giãn 4 chiều mềm mịn, form hack dáng chuẩn từng centimet, giặt máy không lo bai xù",
        "default_voice": "vi-VN-HoaiMyNeural",
        "style_tag": "fashion runway editorial, urban chic street style, dynamic model movement, vibrant sunlight"
    },
    "food": {
        "id": "food",
        "name": "Ăn vặt & Ẩm thực",
        "icon": "🍿",
        "sample_products": [
            "Khô bò que miếng giòn tẩm vị đậm đà",
            "Bánh tráng phơi sương sốt bơ trứng muối béo ngậy",
            "Rong biển kẹp hạt dinh dưỡng giòn rụm ít calo",
            "Trà sữa nướng đóng lon đậm vị trà ngọt thanh",
            "Hạt điều rang củi Bình Phước loại A nguyên hạt"
        ],
        "default_pain": "Đang làm việc hay cày phim buồn miệng mà không biết ăn gì, sợ đồ ăn dầu mỡ mất vệ sinh",
        "default_features": "Nguyên liệu tươi sạch chuẩn an toàn vệ sinh, hương vị thơm ngon bùng nổ vị giác, ăn hoài không ngán",
        "default_voice": "vieneu:AdamBua",
        "style_tag": "mouthwatering commercial food cinematography, macro crispy texture, appetizing steam, rich golden lighting"
    },
    "health": {
        "id": "health",
        "name": "Sức khỏe & Thể thao",
        "icon": "🧘",
        "sample_products": [
            "Bình nước giữ nhiệt 2L có ống hút tiện lợi",
            "Thảm tập Yoga định tuyến chống trơn trượt 8mm",
            "Gối công thái học nâng đỡ cổ vai gáy êm ái",
            "Kẹo dẻo thảo mộc tự nhiên Melatonin hỗ trợ giấc ngủ",
            "Máy massage mắt làm ấm thư giãn vùng mắt"
        ],
        "default_pain": "Cơ thể hay mệt mỏi, ăn uống thất thường, muốn chăm sóc sức khỏe mà bận rộn không có thời gian",
        "default_features": "Thiết kế tiện lợi, hỗ trợ chăm sóc sức khỏe mỗi ngày, dùng đơn giản tại nhà",
        "default_voice": "vi-VN-NamMinhNeural",
        "style_tag": "healthy zen lifestyle, peaceful serene ambient light, comfortable relaxation vibe"
    }
}

AFFILIATE_FRAMEWORKS = {
    "pas": {
        "id": "pas",
        "name": "PAS (Problem - Agitate - Solution)",
        "badge": "Chuyển Đổi Cao Nhất",
        "desc": "Khơi gợi nỗi đau bực bội thường gặp -> Thổi bùng sự khó chịu -> Giới thiệu sản phẩm như vị cứu tinh -> Chốt giỏ hàng."
    },
    "hai_bua": {
        "id": "hai_bua",
        "name": "Hài Hước / Bựa Troll (⭐ Adam bựa)",
        "badge": "Siêu Viral TikTok",
        "desc": "Tình huống tréo ngoe cười nghiêng ngả, giọng điệu dí dỏm cà khịa cuốn hút người xem xem hết clip mà vẫn chốt đơn ầm ầm."
    },
    "review_koc": {
        "id": "review_koc",
        "name": "Review KOC & Đập Hộp Chân Thực",
        "badge": "Tạo Niềm Tin Lớn",
        "desc": "Bóc hộp trên tay, thử trực tiếp tính năng trước ống kính, đánh giá ưu nhược điểm khách quan và tặng kèm deal giảm giá."
    },
    "before_after": {
        "id": "before_after",
        "name": "Before / After (Biến Hóa Ngoạn Mục)",
        "badge": "Thị Giác Bắt Mắt",
        "desc": "So sánh tương phản rõ rệt: Cảnh trước đây khốn khổ, bất tiện vs Cảnh sau khi dùng xịn sò, nhàn tênh rạng rỡ."
    },
    "aida": {
        "id": "aida",
        "name": "AIDA (Attention - Interest - Desire - Action)",
        "badge": "Kinh Điển Marketing",
        "desc": "3s đầu gây chấn động tò mò -> Trình diễn công năng kích thích thèm muốn -> Cam kết bảo hành uy tín -> Kêu gọi mua ngay."
    }
}

# ── 1.1. Hình tượng KOC Đại Diện (Khóa Nhân Vật Đồng Nhất 100%) ──────────────

KOC_PERSONAS = {
    "nam_bua_hai": {
        "id": "nam_bua_hai",
        "name": "🧑 Tuấn Bựa (Nam KOC 25t · Hài hước bựa dí dỏm)",
        "badge": "⭐ Chuẩn Cặp Bài Trùng Adam Bựa",
        "gender": "male",
        "voice": "vieneu:AdamBua",
        "desc": "Chàng trai 25 tuổi hóm hỉnh, phong cách review hài hước, phản ứng chân thực và biểu cảm cực dính.",
        "prompt_dna": "a charismatic 25-year-old Vietnamese male tech reviewer with trendy side-part black hair, modern glasses, wearing a casual navy polo shirt, expressive comedic facial reactions, consistent identical face and clothing"
    },
    "nu_tre_trung": {
        "id": "nu_tre_trung",
        "name": "👩 Trúc Linh (Nữ KOC 23t · Tươi tắn rạng ngời)",
        "badge": "💄 Mỹ Phẩm & Gia Dụng Số 1",
        "gender": "female",
        "voice": "vieneu:TrucLy",
        "desc": "Cô gái 23 tuổi xinh xắn, nụ cười rạng rỡ, giọng điệu ngọt ngào, tạo thiện cảm lớn khi đập hộp.",
        "prompt_dna": "a beautiful 23-year-old Vietnamese female content creator with sleek shoulder-length black hair, bright expressive eyes, wearing a stylish minimalist beige pastel sweater, radiant warm smile, consistent identical face and clothing"
    },
    "chuyen_gia_nu": {
        "id": "chuyen_gia_nu",
        "name": "👩‍💼 Mai Phương (Nữ Chuyên Gia 29t · Thanh lịch uy tín)",
        "badge": "🏆 Uy Tín & Đẳng Cấp",
        "gender": "female",
        "voice": "vieneu:MaiAnh",
        "desc": "Phụ nữ 29 tuổi thanh lịch, trang nhã, tạo dựng niềm tin tuyệt đối cho các sản phẩm sức khỏe, cao cấp.",
        "prompt_dna": "a poised 29-year-old Vietnamese female specialist with elegant low ponytail hair, clean white smart-casual blazer, trustworthy confident gaze, consistent identical face and clothing"
    },
    "nam_lich_lam": {
        "id": "nam_lich_lam",
        "name": "🧔 Hoàng Nam (Nam Reviewer 30t · Lịch lãm nam tính)",
        "badge": "📱 Công Nghệ & Đời Sống",
        "gender": "male",
        "voice": "vi-VN-NamMinhNeural",
        "desc": "Nam reviewer 30 tuổi chững chạc, phong cách hiện đại tối giản, phù hợp đồ công nghệ, thời trang.",
        "prompt_dna": "a handsome 30-year-old Vietnamese male reviewer with neat groomed hair, sharp jawline, wearing a charcoal minimalist crewneck, confident masculine aura, consistent identical face and clothing"
    }
}

COMMON_PRODUCT_KEYWORDS = {
    "giá đỡ": "360-degree rotating phone stand holder",
    "kẹp điện thoại": "phone mount holder",
    "tai nghe": "wireless bluetooth noise-canceling headphones",
    "sạc": "fast charging power bank adapter",
    "cáp sạc": "fast charging braided cable",
    "nồi chiên": "air fryer cooker with glass viewing window",
    "máy hút bụi": "cordless handheld vacuum cleaner",
    "máy xay": "portable smoothie blender",
    "cây lau nhà": "self-wringing spin mop",
    "robot": "smart robot vacuum cleaner",
    "kem chống nắng": "sunscreen lotion cream tube",
    "serum": "skincare vitamin serum dropper bottle",
    "son": "lip balm lipstick tube",
    "sữa rửa mặt": "facial cleanser foaming pump bottle",
    "áo": "oversized cotton t-shirt",
    "quần": "trendy wide-leg trousers",
    "giày": "stylish sneaker running shoes",
    "kính": "polarized fashion sunglasses",
    "túi": "minimalist aesthetic shoulder handbag",
    "khô bò": "crispy beef jerky snack pack",
    "bánh tráng": "flavorful rice paper snack",
    "bình nước": "insulated thermal water bottle",
    "thảm": "alignment yoga mat",
    "gối": "ergonomic memory foam cervical pillow"
}

# Bảng transliterate dấu tiếng Việt thủ công (dùng khi thư viện `unidecode`
# không có sẵn trong requirements — KHÔNG strip dấu thành ký tự rác).
_VI_DIACRITICS_MAP = str.maketrans({
    "à": "a", "á": "a", "ả": "a", "ã": "a", "ạ": "a",
    "ă": "a", "ằ": "a", "ắ": "a", "ẳ": "a", "ẵ": "a", "ặ": "a",
    "â": "a", "ầ": "a", "ấ": "a", "ẩ": "a", "ẫ": "a", "ậ": "a",
    "À": "A", "Á": "A", "Ả": "A", "Ã": "A", "Ạ": "A",
    "Ă": "A", "Ằ": "A", "Ắ": "A", "Ẳ": "A", "Ẵ": "A", "Ặ": "A",
    "Â": "A", "Ầ": "A", "Ấ": "A", "Ẩ": "A", "Ẫ": "A", "Ậ": "A",
    "đ": "d", "Đ": "D",
    "è": "e", "é": "e", "ẻ": "e", "ẽ": "e", "ẹ": "e",
    "ê": "e", "ề": "e", "ế": "e", "ể": "e", "ễ": "e", "ệ": "e",
    "È": "E", "É": "E", "Ẻ": "E", "Ẽ": "E", "Ẹ": "E",
    "Ê": "E", "Ề": "E", "Ế": "E", "Ể": "E", "Ễ": "E", "Ệ": "E",
    "ì": "i", "í": "i", "ỉ": "i", "ĩ": "i", "ị": "i",
    "Ì": "I", "Í": "I", "Ỉ": "I", "Ĩ": "I", "Ị": "I",
    "ò": "o", "ó": "o", "ỏ": "o", "õ": "o", "ọ": "o",
    "ô": "o", "ồ": "o", "ố": "o", "ổ": "o", "ỗ": "o", "ộ": "o",
    "ơ": "o", "ờ": "o", "ớ": "o", "ở": "o", "ỡ": "o", "ợ": "o",
    "Ò": "O", "Ó": "O", "Ỏ": "O", "Õ": "O", "Ọ": "O",
    "Ô": "O", "Ồ": "O", "Ố": "O", "Ổ": "O", "Ỗ": "O", "Ộ": "O",
    "Ơ": "O", "Ờ": "O", "Ớ": "O", "Ở": "O", "Ỡ": "O", "Ợ": "O",
    "ù": "u", "ú": "u", "ủ": "u", "ũ": "u", "ụ": "u",
    "ư": "u", "ừ": "u", "ứ": "u", "ử": "u", "ữ": "u", "ự": "u",
    "Ù": "U", "Ú": "U", "Ủ": "U", "Ũ": "U", "Ụ": "U",
    "Ư": "U", "Ừ": "U", "Ứ": "U", "Ử": "U", "Ữ": "U", "Ự": "U",
    "ỳ": "y", "ý": "y", "ỷ": "y", "ỹ": "y", "ỵ": "y",
    "Ỳ": "Y", "Ý": "Y", "Ỷ": "Y", "Ỹ": "Y", "Ỵ": "Y",
})

try:
    from unidecode import unidecode as _unidecode
except Exception:  # unidecode không có trong requirements -> dùng bảng tay
    _unidecode = None


def _transliterate_vi(text: str) -> str:
    """Bỏ dấu tiếng Việt mà không làm mất chữ: 'Đèn ngủ' -> 'Den ngu'.

    Ưu tiên unidecode nếu có sẵn, nếu không dùng bảng mapping thủ công.
    """
    if _unidecode is not None:
        return _unidecode(text)
    return text.translate(_VI_DIACRITICS_MAP)


def translate_product_to_en(vi_name: str) -> str:
    """Chuyển đổi tên sản phẩm tiếng Việt sang mô tả tiếng Anh để Muse AI nhận diện chuẩn 100%."""
    low = vi_name.lower().strip()
    for kw, en_trans in COMMON_PRODUCT_KEYWORDS.items():
        if kw in low:
            return f"sleek {en_trans} ({vi_name})"
    # KHÔNG dùng re.sub(r'[^a-zA-Z0-9\\s]', ' ', ...) trực tiếp lên tiếng Việt
    # có dấu: nó xóa dấu và biến "Đèn ngủ" thành "n ng". Transliterate trước,
    # giữ nguyên tên gốc có dấu trong ngoặc để prompt không mất thông tin.
    clean = re.sub(r'[^a-zA-Z0-9\s]', ' ', _transliterate_vi(vi_name)).strip()
    clean = re.sub(r'\s+', ' ', clean)
    return f"sleek commercial product ({clean} / {vi_name})" if clean else "sleek lifestyle product"


# ── 1.2. Kiểu quay Video (Shooting Styles) ───────────────────────────────────

AFFILIATE_SHOOTING_STYLES = {
    "faceless": {
        "id": "faceless",
        "name": "Người mẫu không lộ mặt",
        "badge": "👤 Bí Ẩn & Sang Trọng",
        "desc": "Mẫu mặc/dùng sản phẩm, chỉ quay từ cổ trở xuống hoặc từ phía sau",
        "icon": "👤",
        "tip": "Mẫu mặc/dùng sản phẩm, góc máy từ cổ trở xuống hoặc từ lưng, tinh tế và không lo biến dạng mặt.",
        "shot_concept": "Aesthetic faceless model shot strictly from neck down or from behind, no face visible"
    },
    "hands": {
        "id": "hands",
        "name": "Chỉ quay tay",
        "badge": "✋ Trực Quan & Thao Tác",
        "desc": "Đôi tay cầm, xoay, thao tác với sản phẩm trên nền đẹp",
        "icon": "✋",
        "tip": "Cận cảnh đôi tay đập hộp, xoay 360 độ trên mặt bàn gỗ/đá cao cấp, chuẩn review phụ kiện, đồ công nghệ.",
        "shot_concept": "Extreme close-up macro hands manipulating product on aesthetic tabletop, zero face"
    },
    "product": {
        "id": "product",
        "name": "Chỉ quay sản phẩm",
        "badge": "📦 Studio Quảng Cáo 3D",
        "desc": "Không có người — sản phẩm được quay như quảng cáo",
        "icon": "📦",
        "tip": "Sản phẩm xoay trên mâm xoay studio, ánh sáng điện ảnh, hoàn toàn không có người hoặc tay, chuẩn TVC.",
        "shot_concept": "Pure cinematic commercial product showcase with strictly ZERO HUMANS and NO HANDS"
    },
    "koc_face": {
        "id": "koc_face",
        "name": "KOC lộ mặt đầy đủ",
        "badge": "🧑 Gương Mặt Đồng Nhất",
        "desc": "KOC trực tiếp review, biểu cảm và nói chuyện trước ống kính",
        "icon": "🧑",
        "tip": "Khóa chặt đặc điểm khuôn mặt và trang phục của KOC đồng nhất 100% xuyên suốt 5 cảnh quay.",
        "shot_concept": "Charismatic KOC speaking and gesturing directly to camera with consistent facial identity"
    }
}

# Chuẩn affiliate (đặt trước Data Models vì AffiliateScene dùng làm default).
# Clip video API (muse2api /v1/videos) chỉ nhận các mốc 5/6/8/10s — KHÔNG nhận
# số giây tùy ý. Vì vậy mỗi cảnh lấy mốc nhỏ nhất phủ được thoại của nó
# (thoại ngắn → clip ngắn, thoại dài → clip dài); thoại dài hơn 10s vẫn lấy
# mốc 10 và video loop để đọc kịp thoại (hành vi compose có sẵn, không cắt thoại).
AFFILIATE_SCENE_DURATION = 10  # giây/cảnh — mốc tối đa của clip API (mặc định)
AFFILIATE_CLIP_DURATIONS = (5, 6, 8, 10)  # các mốc clip API /v1/videos chấp nhận
AFFILIATE_VOICEOVER_CHARS_PER_SEC = 11  # ước lượng TTS tiếng Việt (cùng quy ước story_engine)
AFFILIATE_VOICEOVER_MAX_WORDS = 30  # ~25-30 từ tiếng Việt đọc vừa trong 10s
PRODUCT_LOCK_MARKER = "PRODUCT CONTINUITY LOCK"
FIDELITY_MARKER = "PRODUCT FIDELITY LOCK"

# Khối khóa trung thực ảnh sản phẩm — dán NGUYÊN VĂN vào cuối prompt MỌI cảnh
# affiliate (cùng cơ chế lock hiện tại). Quy tắc cứng của Tiến 2026-11-24:
# tuyệt đối giữ ảnh sản phẩm 100% — không đổi màu sắc, hình dạng, kích thước.
FIDELITY_BLOCK = (
    f"[{FIDELITY_MARKER} — the product in this video MUST look EXACTLY like "
    "the product in the reference image, 100% identical: same colors, same "
    "shape, same size and proportions, same packaging, same logo, same label "
    "text and artwork. Do NOT redraw, recolor, restyle, resize, reshape or "
    "redesign the product in any way. Do NOT add, remove or change any detail "
    "of the product. If the reference image shows only the outer box or "
    "packaging, show ONLY the box/packaging — NEVER invent or imagine what "
    "is inside the box.]"
)


def build_fidelity_block() -> str:
    """Trả về khối khóa trung thực ảnh sản phẩm (nguyên văn, dùng cho mọi cảnh)."""
    return FIDELITY_BLOCK


# Cụm từ trong prompt cho phép AI thay đổi sản phẩm (validator gắn cờ).
# Quét không phân biệt hoa/thường; khớp theo cụm từ tiếng Anh.
_FIDELITY_RISK_PATTERNS = (
    "redesign",
    "redesigned",
    "recolor",
    "recolour",
    "restyle",
    "restyled",
    "makeover",
    "new look",
    "new design",
    "different color",
    "different colour",
    "different shape",
    "change the color",
    "change the colour",
    "change the shape",
    "in a different color",
    "in a different colour",
    "another color",
    "another colour",
    "redraw",
    "reimagine",
)


# Các câu bắt buộc trong FIDELITY_BLOCK (chuẩn hóa: chữ thường + gộp khoảng
# trắng). Kiểm nội dung khối thật thay vì chỉ marker (fix audit #15):
# prompt chỉ chứa marker, hoặc block bị cắt/paraphrase thiếu câu then chốt,
# đều được coi là chưa có block -> append lại toàn bộ khối chuẩn.
_FIDELITY_REQUIRED_SENTENCES = (
    "must look exactly like the product in the reference image",
    "100% identical",
    "same colors, same shape, same size and proportions",
    "do not redraw, recolor, restyle, resize, reshape or redesign",
    "do not add, remove or change any detail",
    "show only the box/packaging",
    "never invent or imagine what is inside",
)


def _normalize_fidelity_text(text: str) -> str:
    """Chuẩn hóa để so khớp khối fidelity: chữ thường, gộp khoảng trắng."""
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def fidelity_block_missing_sentences(prompt: str) -> List[str]:
    """Các câu bắt buộc của FIDELITY_BLOCK còn thiếu trong prompt ([] = đủ)."""
    norm = _normalize_fidelity_text(prompt)
    return [s for s in _FIDELITY_REQUIRED_SENTENCES if s not in norm]


def fidelity_block_complete(prompt: str) -> bool:
    """Prompt có đủ toàn bộ khối fidelity chuẩn (không chỉ marker)."""
    return not fidelity_block_missing_sentences(prompt)


# Cụm phủ định + danh sách từ cấm (fix audit #15): khối fidelity viết lại
# ("Do NOT redraw, recolor, restyle...") không được gắn cờ oan. Chỉ strip
# đúng dạng "do not / don't / never + <từ cấm>" theo danh sách liệt kê —
# "not" trần không tính, cụm khẳng định vẫn bị gắn cờ như cũ.
_FIDELITY_NEGATED_LIST_RX = re.compile(
    r"\b(?:do\s+not|don't|dont|never)\b"
    r"(?:\s*,?\s*(?:or|and)?\s*(?:" + "|".join(
        re.escape(p) for p in sorted(_FIDELITY_RISK_PATTERNS, key=len, reverse=True)
    ) + r")\b)+",
    re.I)


def check_prompt_fidelity(prompt: str) -> List[str]:
    """Quét prompt, trả về các cụm từ cho phép thay đổi sản phẩm (nếu có).

    Trả [] khi prompt sạch. Dùng cho _normalize warnings + validate script.
    Chính khối FIDELITY BLOCK cũng nhắc tới các từ này ở dạng phủ định
    ("Do NOT redesign / recolor / restyle...") nên phải loại khối block ra
    trước khi quét, nếu không mọi cảnh sẽ bị gắn cờ oan. Loại cả dạng viết
    lại của khối ("Do NOT redraw, recolor...") — chỉ strip đúng dạng phủ
    định do not/don't/never + từ cấm, cụm khẳng định vẫn bị gắn cờ.
    """
    text = (prompt or "").replace(FIDELITY_BLOCK, "")
    text = _FIDELITY_NEGATED_LIST_RX.sub("", text)
    out: List[str] = []
    for pat in _FIDELITY_RISK_PATTERNS:
        if re.search(r"\b" + re.escape(pat) + r"\b", text, flags=re.I):
            out.append(pat)
    return out


def estimate_voiceover_seconds(text: str) -> float:
    """Ước lượng độ dài đọc thoại tiếng Việt (giây): ~11 ký tự/giây."""
    n = len((text or "").strip())
    return n / AFFILIATE_VOICEOVER_CHARS_PER_SEC if n > 0 else 0.0


def snap_clip_duration(est_seconds: float) -> int:
    """Chọn mốc clip API nhỏ nhất phủ được độ dài thoại ước lượng.

    Thoại ≤5s → 5; ≤6s → 6; ≤8s → 8; còn lại (kể cả dài hơn 10s) → 10.
    """
    try:
        est = float(est_seconds)
    except (TypeError, ValueError):
        return AFFILIATE_SCENE_DURATION
    for d in AFFILIATE_CLIP_DURATIONS:
        if est <= d:
            return d
    return AFFILIATE_CLIP_DURATIONS[-1]


# ── 2. Data Models ──────────────────────────────────────────────────────────

class ProductAffiliateRequest(BaseModel):
    product_name: str
    category: str = "gadgets"
    target_audience: Optional[str] = "Người dùng trẻ, dân văn phòng, học sinh sinh viên"
    price_original: Optional[str] = ""
    price_sale: Optional[str] = ""
    discount_percent: Optional[str] = ""  # Mặc định trống: KHÔNG bịa "50%" khi user chưa nhập.
    promotion_deal: Optional[str] = ""  # Mặc định trống: KHÔNG bịa freeship/quà tặng khi user chưa nhập.
    key_features: Optional[str] = ""
    pain_points: Optional[str] = ""
    framework: str = "pas" # pas | hai_bua | review_koc | before_after | aida
    shooting_style: Optional[str] = "faceless" # faceless | hands | product | koc_face
    koc_persona: Optional[str] = "nam_bua_hai" # nam_bua_hai | nu_tre_trung | chuyen_gia_nu | nam_lich_lam
    koc_image: Optional[str] = None # Ảnh chân dung người mẫu KOC đại diện
    tone: str = "energetic" # hai_bua | energetic | honest | luxury | urgent
    voice: str = "vieneu:AdamBua"
    aspect_ratio: str = "9:16"
    reference_image: Optional[str] = None
    target_platform: str = "tiktok" # tiktok | shopee | reels | shorts

class AffiliateScene(BaseModel):
    index: int
    title: str
    role: str # hook | pain | demo | proof | cta
    duration: int = AFFILIATE_SCENE_DURATION  # chuẩn 10s = clip API trả về
    prompt: str
    voiceover: str
    voice: str = ""
    badge: str = ""
    reference_image: Optional[str] = None

class AffiliateScriptResponse(BaseModel):
    title: str
    framework: str
    target_platform: str
    aspect_ratio: str
    recommended_voice: str
    hook_strength: str = "99/100"
    marketing_notes: List[str] = []
    scenes: List[AffiliateScene]
    # Cảnh báo tuân thủ (fix PR #26 c/d): cùng nội dung với các warning trong
    # marketing_notes nhưng tách riêng để UI hiện hộp cảnh báo rõ ràng.
    warnings: List[str] = []
    # Câu khuyến cáo bắt buộc cho danh mục Sức khỏe / thực phẩm bổ sung —
    # append vào MÔ TẢ video, KHÔNG đọc trong thoại từng cảnh.
    compliance_disclaimer: Optional[str] = None


# ── 2.5. Product DNA Lock + thời lượng theo thoại ───────────────────────────
# Mỗi cảnh dài theo thoại của nó (clip 5/6/8/10s — mốc API chấp nhận): thoại
# ngắn thì clip ngắn (không thừa giây hình đứng yên), thoại dài thì clip dài
# (không bị cắt thoại). Thoại dài hơn 10s vẫn lấy clip 10s và video loop để
# đọc kịp (hành vi compose có sẵn). Mọi prompt đều dán NGUYÊN VĂN cùng một
# Product DNA lock để tay/sản phẩm/bối cảnh không trôi giữa các cảnh (cùng
# nguyên tắc Character Block của phim dài).



def build_product_dna_lock(
    product_name: str,
    category: str = "gadgets",
    key_features: str = "",
    koc_persona_id: str = "nam_bua_hai",
    shooting_style: str = "faceless",
) -> str:
    """Khối khóa liên tục của sản phẩm — dán NGUYÊN VĂN vào MỌI prompt cảnh.

    Gồm: tên sản phẩm (EN + giữ tên gốc), tính năng chính, neo liên tục theo
    kiểu quay (tay/bối cảnh/trang phục/KOC) và style_tag của ngành hàng.
    Không chứa từ cấm (no text/watermark nằm ở prompt gốc, không lặp ở đây).
    """
    cat_cfg = AFFILIATE_CATEGORIES.get(category, AFFILIATE_CATEGORIES["gadgets"])
    persona_cfg = KOC_PERSONAS.get(koc_persona_id, KOC_PERSONAS["nam_bua_hai"])
    p_name_en = translate_product_to_en(product_name)
    feats = re.sub(r"\s+", " ", _transliterate_vi((key_features or "").strip())).strip()
    if len(feats) > 160:
        feats = feats[:160].rsplit(" ", 1)[0].strip() + "..."
    if shooting_style == "hands":
        anchor = ("the EXACT SAME pair of clean well-groomed aesthetic hands, "
                  "the EXACT SAME clean aesthetic minimalist oak wood desk tabletop "
                  "with soft morning daylight and pastel props")
    elif shooting_style == "product":
        anchor = ("the EXACT SAME luxury minimalist studio showcase with rotating "
                  "motorized matte turntable, volumetric softbox lighting and clean "
                  "gradient background, no people, no hands")
    elif shooting_style == "koc_face":
        anchor = f"the EXACT SAME {persona_cfg['prompt_dna']}"
    else:  # faceless
        wardrobe = ("stylish modern neutral oversized streetwear"
                    if persona_cfg.get("gender") == "male"
                    else "elegant modern minimalist pastel outfit")
        anchor = (f"the EXACT SAME aesthetic faceless model wearing {wardrobe}, "
                  "strictly shot from neck down or from behind, "
                  "absolutely no facial features visible")
    feat_part = f", key features: {feats}" if feats else ""
    return (
        f"[{PRODUCT_LOCK_MARKER} — identical in EVERY scene, do not change anything: "
        f"the EXACT SAME {p_name_en}{feat_part}; {anchor}; "
        f"{cat_cfg['style_tag']}. Match white balance, light direction, lens perspective "
        "and product scale across cuts. Keep the label readable in the hero shot. "
        "Frame only details visible in the reference when supplied.]"
    )


def build_product_dna_lock_for_request(req: "ProductAffiliateRequest") -> str:
    """Tiện ích: dựng lock trực tiếp từ request kịch bản."""
    return build_product_dna_lock(
        req.product_name,
        req.category,
        req.key_features or "",
        getattr(req, "koc_persona", "nam_bua_hai") or "nam_bua_hai",
        getattr(req, "shooting_style", "faceless") or "faceless",
    )


def validate_affiliate_script(
    scenes: List[Any], product_name: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Kiểm tra cảnh nào thiếu Product DNA lock (dùng cho test + log warning).

    Trả về danh sách {"index": int, "missing": ["product_lock" | "product_name"]}.
    Chấp nhận cả AffiliateScene lẫn dict.
    """
    issues: List[Dict[str, Any]] = []
    core_name = ""
    if product_name:
        core_name = translate_product_to_en(product_name).split(" (")[0].strip().lower()
    for s in scenes:
        prompt = s.get("prompt", "") if isinstance(s, dict) else getattr(s, "prompt", "")
        idx = s.get("index", 0) if isinstance(s, dict) else getattr(s, "index", 0)
        missing: List[str] = []
        if PRODUCT_LOCK_MARKER not in (prompt or ""):
            missing.append("product_lock")
        if core_name and core_name not in (prompt or "").lower():
            missing.append("product_name")
        if check_prompt_fidelity(prompt or ""):
            missing.append("fidelity_risk")
        if missing:
            issues.append({"index": idx, "missing": missing})
    return issues


# ── 2.6. Tuân thủ chính sách bán hàng (quy tắc cứng 2026-11-24) ─────────────
# TikTok Shop / Shopee cấm quảng cáo: tuyên bố chữa bệnh/khỏi bệnh, cam kết
# tuyệt đối, kết quả phi thực tế, giảm giá/khan hiếm giả, từ tuyệt đối không
# có chứng cứ. Thực phẩm bổ sung/dinh dưỡng (vd sữa Ensure) chỉ được nói
# "hỗ trợ dinh dưỡng/sức khỏe", KHÔNG được nói "chữa/khỏi bệnh".
#
# - Vi phạm NẶNG (severe): viết lại tự động sang cách nói tuân thủ + warning.
# - Vi phạm NHẸ (light): giữ nguyên câu + warning bắt người dùng xác nhận
#   trước khi đăng (giá/khuyến mãi/số lượng phải là số thật của shop).

# (regex, replacement, nhãn) — áp dụng theo thứ tự: cụm dài/trường hợp riêng
# trước, cụm ngắn sau (vd "100% khỏi bệnh" phải xử lý trước "100% khỏi").
# QUAN TRỌNG (fix PR #26): mọi pattern chứa "100%" phải đứng TRƯỚC các pattern
# "cam kết/chắc chắn/đảm bảo khỏi bệnh" — nếu không, "cam kết khỏi bệnh 100%"
# sẽ bị thay cụm "cam kết khỏi bệnh" trước, còn trơ "100%" thành tuyên bố
# tuyệt đối mới ("hỗ trợ sức khỏe 100%").
_COMPLIANCE_SEVERE = (
    # "100%" đi kèm tuyên bố khỏi bệnh/hiệu quả — xử lý TRƯỚC hết.
    # Dạng có động từ cam kết đi trước ("cam kết 100% khỏi bệnh", "bao khỏi
    # 100%") phải xử lý trước pattern ngắn, nếu không sẽ còn trơ lại động từ
    # cam kết ("bao hỗ trợ hiệu quả") — audit #3.
    (re.compile(r"(cam\s*kết|đảm\s*bảo|chắc\s*chắn|bao)\s+100%\s*khỏi\s*bệnh", re.I),
     "hỗ trợ sức khỏe", "cam kết khỏi bệnh tuyệt đối"),
    (re.compile(r"(cam\s*kết|đảm\s*bảo|chắc\s*chắn|bao)\s+khỏi\s*bệnh\s*100%", re.I),
     "hỗ trợ sức khỏe", "cam kết khỏi bệnh tuyệt đối"),
    (re.compile(r"(cam\s*kết|đảm\s*bảo|chắc\s*chắn|bao)\s+100%\s*khỏi", re.I),
     "hỗ trợ hiệu quả", "cam kết khỏi bệnh tuyệt đối"),
    (re.compile(r"(cam\s*kết|đảm\s*bảo|chắc\s*chắn|bao)\s+khỏi\s*100%", re.I),
     "hỗ trợ hiệu quả", "cam kết khỏi bệnh tuyệt đối"),
    (re.compile(r"100%\s*hiệu\s*quả", re.I), "hỗ trợ hiệu quả",
     "cam kết hiệu quả 100% tuyệt đối"),
    (re.compile(r"hiệu\s*quả\s*100%", re.I), "hỗ trợ hiệu quả",
     "cam kết hiệu quả 100% tuyệt đối"),
    (re.compile(r"100%\s*khỏi\s+bệnh", re.I), "hỗ trợ sức khỏe",
     "tuyên bố khỏi bệnh tuyệt đối"),
    (re.compile(r"khỏi\s+bệnh\s*100%", re.I), "hỗ trợ sức khỏe",
     "tuyên bố khỏi bệnh tuyệt đối"),
    (re.compile(r"100%\s*khỏi", re.I), "hỗ trợ hiệu quả",
     "cam kết khỏi bệnh tuyệt đối"),
    (re.compile(r"khỏi\s*100%", re.I), "hỗ trợ hiệu quả",
     "cam kết khỏi bệnh tuyệt đối"),
    (re.compile(r"cam\s*kết\s+khỏi\s+bệnh", re.I), "hỗ trợ sức khỏe",
     "cam kết khỏi bệnh"),
    (re.compile(r"chắc\s+chắn\s+khỏi\s+bệnh", re.I), "hỗ trợ sức khỏe",
     "cam kết khỏi bệnh"),
    (re.compile(r"đảm\s+bảo\s+khỏi\s+bệnh", re.I), "hỗ trợ sức khỏe",
     "cam kết khỏi bệnh"),
    (re.compile(r"bao\s+khỏi\s+bệnh", re.I), "hỗ trợ sức khỏe",
     "cam kết khỏi bệnh"),
    (re.compile(r"chữa\s+khỏi\s+bệnh", re.I), "hỗ trợ cải thiện sức khỏe",
     "tuyên bố chữa khỏi bệnh"),
    (re.compile(r"trị\s+khỏi\s+bệnh", re.I), "hỗ trợ cải thiện sức khỏe",
     "tuyên bố trị khỏi bệnh"),
    (re.compile(r"chữa\s+khỏi", re.I), "hỗ trợ cải thiện",
     "tuyên bố chữa khỏi bệnh"),
    (re.compile(r"trị\s+khỏi", re.I), "hỗ trợ cải thiện",
     "tuyên bố trị khỏi bệnh"),
    (re.compile(r"chữa\s+bệnh", re.I), "hỗ trợ sức khỏe",
     "tuyên bố chữa bệnh"),
    (re.compile(r"chữa\s+lành", re.I), "chăm sóc",
     "tuyên bố chữa lành (tác dụng trị liệu)"),
    (re.compile(r"đặc\s+trị", re.I), "hỗ trợ chuyên biệt",
     "tuyên bố đặc trị"),
    # "Trị dứt điểm" / "thần dược" — tuyên bố trị liệu tuyệt đối (audit #3).
    (re.compile(r"(trị|chữa)\s+dứt\s+điểm", re.I), "hỗ trợ cải thiện dần dần",
     "tuyên bố trị dứt điểm"),
    (re.compile(r"dứt\s+điểm", re.I), "dần dần",
     "tuyên bố dứt điểm (kết quả tuyệt đối)"),
    (re.compile(r"thần\s+dược", re.I), "sản phẩm hỗ trợ sức khỏe",
     "tuyên bố 'thần dược'"),
    (re.compile(r"khỏi\s+bệnh", re.I), "khỏe mạnh hơn",
     "tuyên bố khỏi bệnh"),
    (re.compile(r"khỏi\s+hẳn", re.I), "cải thiện rõ rệt",
     "tuyên bố khỏi hẳn"),
    # "Khỏi hoàn toàn" — tuyên bố khỏi bệnh tuyệt đối (audit #3).
    (re.compile(r"khỏi\s+hoàn\s+toàn", re.I), "cải thiện rõ rệt",
     "tuyên bố khỏi hoàn toàn"),
    (re.compile(r"hoàn\s+toàn\s+khỏi", re.I), "cải thiện rõ rệt",
     "tuyên bố khỏi hoàn toàn"),
    (re.compile(r"cam\s*kết\s+khỏi", re.I), "hỗ trợ",
     "cam kết khỏi bệnh"),
    (re.compile(r"chắc\s+chắn\s+khỏi", re.I), "hỗ trợ",
     "cam kết khỏi bệnh"),
    (re.compile(r"đảm\s+bảo\s+khỏi", re.I), "hỗ trợ",
     "cam kết khỏi bệnh"),
    (re.compile(r"bao\s+khỏi", re.I), "hỗ trợ",
     "cam kết khỏi bệnh"),
    (re.compile(r"hiệu\s+quả\s+tuyệt\s+đối", re.I), "hỗ trợ hiệu quả",
     "cam kết hiệu quả tuyệt đối"),
    # Cam kết tuyệt đối về hiệu quả, không kèm "100%" (audit #3): "cam kết
    # hiệu quả" trần từng lọt checker sau rewrite. Có cả dạng "cam kết hỗ
    # trợ hiệu quả" — dang dở do pattern 100% thay trước.
    (re.compile(r"(cam\s*kết|đảm\s*bảo|chắc\s*chắn|bao)\s+(hỗ\s+trợ\s+)?hiệu\s*quả",
                re.I), "hỗ trợ hiệu quả", "cam kết hiệu quả tuyệt đối"),
    # Hiệu quả tức thì + mốc thời gian ngắn (fix PR #26 — trước đây checker
    # trả [] cho chính câu mặc định của app "tức thì chỉ sau 10 phút").
    # Audit #3: mở rộng đơn vị thời gian sang tuần/tháng — "giảm 5kg trong
    # 1 tuần" / "trắng sau 7 ngày" trước đây lọt vì chỉ khớp "ngày"/"lần".
    (re.compile(r"tức\s+thì", re.I), "dần dần",
     "hiệu quả tức thì (cần thời gian sử dụng)"),
    (re.compile(r"chỉ\s+sau\s+\d+\s*(phút|giờ|ngày|tuần|tháng)(\s+sử\s+dụng)?", re.I),
     "sau một thời gian sử dụng", "hiệu quả nhanh sau mốc thời gian ngắn"),
    (re.compile(r"ngay\s+sau\s+\d+\s*(lần|ngày|tuần)", re.I),
     "sau một thời gian sử dụng", "hiệu quả ngay sau vài lần dùng"),
    (re.compile(r"ngay\s+lần\s+đầu\s+(dùng|sử\s+dụng)", re.I),
     "sau một thời gian sử dụng", "hiệu quả ngay lần đầu dùng"),
    (re.compile(r"(hiệu\s+quả|kết\s+quả)[^.]{0,40}?ngay\s+lần\s+đầu", re.I),
     "hỗ trợ cải thiện dần dần", "hiệu quả ngay lần đầu dùng"),
    # Tuyên bố trị liệu / ngừa bệnh / phục hồi sau bệnh / tăng cân nhanh
    # (fix PR #26 — đúng kiểu câu nhà bán lẻ hay viết cho sữa Ensure).
    (re.compile(r"chống\s+đau\s+mỏi", re.I), "hỗ trợ thư giãn",
     "tuyên bố trị liệu 'chống đau mỏi'"),
    (re.compile(r"giảm\s+đau(\s+nhức)?", re.I), "hỗ trợ thư giãn",
     "tuyên bố trị liệu 'giảm đau'"),
    (re.compile(
        r"(ngừa|phòng\s+ngừa|phòng\s+chống)\s+(loãng\s+xương|tiểu\s+đường|ung\s+thư"
        r"|tim\s+mạch|đột\s+quỵ|cao\s+huyết\s+áp|mỡ\s+máu|gút|viêm\s+khớp"
        r"|thoái\s+hóa|béo\s+phì|cận\s+thị|sâu\s+răng|bệnh)", re.I),
     "hỗ trợ sức khỏe", "tuyên bố ngừa bệnh"),
    (re.compile(r"phục\s+hồi\s+(nhanh\s+)?sau\s+(bệnh|ốm)", re.I),
     "hỗ trợ phục hồi sức khỏe", "tuyên bố phục hồi sau bệnh"),
    (re.compile(r"tăng\s+cân\s+nhanh", re.I), "hỗ trợ tăng cân từ từ",
     "kết quả phi thực tế (tăng cân nhanh)"),
    # "Giảm/tăng cân cấp tốc" (audit #3). Chỉ khớp khi đi kèm động từ kết
    # quả (giảm/tăng cân/kg) để không bắt nhầm "giao hàng cấp tốc".
    # Replacement dùng \1 để giữ nguyên động từ khi viết lại.
    (re.compile(r"((?:giảm|tăng)\s+(?:\d+\s*(?:kg|cm)|cân|mỡ|kg))\s+cấp\s+tốc",
                re.I), r"\1 dần dần", "kết quả phi thực tế (cấp tốc)"),
    (re.compile(r"tăng\s+\d+\s*kg\s+trong\s+\d+\s*(ngày|tuần|tháng)", re.I),
     "hỗ trợ tăng cân dần dần", "kết quả phi thực tế (tăng cân cấp tốc)"),
    # Kết quả phi thực tế: số đo + thời gian ngắn không tưởng.
    (re.compile(r"giảm\s+\d+\s*kg\s+trong\s+\d+\s*(ngày|tuần|tháng)", re.I),
     "hỗ trợ giảm cân dần dần", "kết quả phi thực tế (giảm cân cấp tốc)"),
    (re.compile(r"giảm\s+\d+\s*cm\s+trong\s+\d+\s*(ngày|tuần|tháng)", re.I),
     "hỗ trợ thon gọn dần dần", "kết quả phi thực tế"),
    (re.compile(r"trắng\s+(?:\S+\s+)?(ngay\s+)?sau\s+\d+\s*(lần|ngày|tuần)", re.I),
     "hỗ trợ làm sáng da dần dần", "kết quả phi thực tế (trắng sau vài lần)"),
    (re.compile(r"hết\s+(mụn|nám|thâm|tàn\s+nhang)\s+sau\s+\d+\s*(lần|ngày|tuần)", re.I),
     "hỗ trợ cải thiện da dần dần", "kết quả phi thực tế"),
    (re.compile(r"trắng\s+ngay\s+lần\s+đầu", re.I),
     "hỗ trợ làm sáng da dần dần", "kết quả phi thực tế (trắng ngay lần đầu)"),
    (re.compile(r"hết\s+(mụn|nám|thâm)\s+ngay\s+lần\s+đầu", re.I),
     "hỗ trợ cải thiện da dần dần", "kết quả phi thực tế"),
    # Tiếng Anh (prompt thương mại).
    (re.compile(r"\bmiracle\s+cure\b", re.I), "effective support",
     "tuyên bố chữa bệnh (tiếng Anh 'miracle cure')"),
    (re.compile(r"\bcure[s]?\b", re.I), "support",
     "tuyên bố chữa bệnh (tiếng Anh 'cure')"),
)

# (regex, nhãn) — vi phạm NHẸ: giữ nguyên câu, chỉ gắn warning xác nhận.
_COMPLIANCE_LIGHT = (
    (re.compile(r"tốt\s+nhất", re.I), "từ tuyệt đối 'tốt nhất' (cần chứng cứ)"),
    (re.compile(r"số\s*1\b", re.I), "từ tuyệt đối 'số 1' (cần chứng cứ)"),
    (re.compile(r"số\s+một\b", re.I), "từ tuyệt đối 'số một' (cần chứng cứ)"),
    (re.compile(r"duy\s+nhất\s+hôm\s+nay", re.I),
     "khan hiếm 'duy nhất hôm nay' (cần đúng thời gian thật)"),
    (re.compile(r"duy\s+nhất", re.I), "từ tuyệt đối 'duy nhất' (cần chứng cứ)"),
    (re.compile(r"100%\s*chính\s+hãng", re.I),
     "tuyên bố '100% chính hãng' (cần chứng cứ)"),
    (re.compile(r"chỉ\s+còn\s+\d+", re.I),
     "khan hiếm 'chỉ còn ...' (cần đúng số lượng thật)"),
    (re.compile(r"chỉ\s+hôm\s+nay", re.I),
     "khan hiếm 'chỉ hôm nay' (cần đúng thời gian thật)"),
    (re.compile(r"hết\s+hạn", re.I), "giục mua 'hết hạn' (cần đúng hạn thật)"),
    (re.compile(r"hốt\s+sạch", re.I), "khan hiếm giả 'hốt sạch'"),
    (re.compile(r"sắp\s+hết\s+hàng", re.I),
     "khan hiếm 'sắp hết hàng' (cần đúng tồn kho thật)"),
    (re.compile(r"quà\s+tặng\s+giới\s+hạn", re.I),
     "quà tặng 'giới hạn' (cần đúng số lượng thật)"),
    (re.compile(r"giảm\s+\d+\s*%", re.I),
     "mức giảm giá % (cần đúng giá gốc thật)"),
    # Khuyến mãi/khan hiếm giả — audit #3: "sale 50%" lọt vì pattern trên
    # đòi chữ "giảm"; "mua 1 tặng 1" không có pattern nào.
    (re.compile(r"sale\s+\d+\s*%", re.I),
     "khuyến mãi 'sale N%' (cần đúng giá thật)"),
    (re.compile(r"mua\s+\d+\s+tặng\s+\d+", re.I),
     "khuyến mãi 'mua N tặng N' (cần đúng ưu đãi thật)"),
    (re.compile(r"only\s+\d+\s+left", re.I),
     "khan hiếm 'only N left' (cần đúng số lượng thật)"),
    (re.compile(r"limited\s+time", re.I),
     "khuyến mãi 'limited time' (cần đúng thời gian thật)"),
    (re.compile(r"tiết\s+kiệm\s+\d+\s*%", re.I),
     "mức tiết kiệm % (cần đúng số liệu thật)"),
    (re.compile(r"sale\s+sốc", re.I), "khuyến mãi 'sale sốc' (cần đúng giá thật)"),
    (re.compile(r"giá\s+sốc", re.I), "khuyến mãi 'giá sốc' (cần đúng giá thật)"),
    (re.compile(r"số\s+lượng\s+(flash\s+sale\s+)?có\s+hạn", re.I),
     "khan hiếm 'số lượng có hạn' (cần đúng số lượng thật)"),
    (re.compile(r"hàng\s+ngàn\s+lượt\s+đánh\s+giá\s+5\s+sao", re.I),
     "số liệu đánh giá chưa kiểm chứng"),
    (re.compile(r"chấm\s+điểm\s+\d+(\.\d+)?\s*(/|trên)\s*10", re.I),
     "điểm đánh giá tự chấm (cần nguồn thật)"),
    # Tiếng Anh (prompt thương mại).
    (re.compile(r"\bbest in the world\b", re.I),
     "absolute 'best in the world' (needs evidence)"),
    # "best ... in the world" có từ chen giữa (audit #3) — đặt SAU pattern
    # khớp chính xác để span dài không nuốt span ngắn sai thứ tự.
    (re.compile(r"\bbest\b.{0,30}?\bin the world\b", re.I),
     "absolute 'best ... in the world' (needs evidence)"),
    (re.compile(r"\bnumber\s*1\b", re.I), "absolute 'number 1' (needs evidence)"),
    (re.compile(r"\bonly today\b", re.I),
     "urgency 'only today' (needs a real deadline)"),
)


def check_affiliate_compliance(text: str) -> List[Dict[str, Any]]:
    """Quét text, trả về các phát hiện vi phạm chính sách bán hàng.

    Mỗi phát hiện: {"level": "severe"|"light", "category": str,
    "matched": str, "message": str}. Không sửa text. Khớp không phân biệt
    hoa/thường; trùng lặp theo span (cụm dài nuốt cụm ngắn) bị loại bỏ.
    """
    findings: List[Dict[str, Any]] = []
    taken: List[tuple] = []  # các span đã ghi nhận (để loại trùng lặp)

    def _add(level: str, rx, category: str) -> None:
        for m in rx.finditer(text or ""):
            span = (m.start(), m.end())
            if any(s <= span[0] and span[1] <= e for s, e in taken):
                continue
            taken.append(span)
            findings.append({
                "level": level,
                "category": category,
                "matched": m.group(0),
                "message": category,
            })

    for rx, _rep, category in _COMPLIANCE_SEVERE:
        _add("severe", rx, category)
    for rx, category in _COMPLIANCE_LIGHT:
        _add("light", rx, category)
    return findings


def rewrite_affiliate_compliance(text: str) -> tuple[str, List[Dict[str, Any]]]:
    """Áp dụng viết lại cho các mẫu vi phạm NẶNG sang cách nói tuân thủ.

    Trả (text_mới, changes) với changes = [{"matched", "replacement",
    "category"}]. Chỉ thay cụm từ vi phạm, không bịa con số mới, không làm
    mất ý bán hàng. Vi phạm nhẹ không được viết lại ở đây (chỉ warning).
    Sau khi viết lại, hàm quét LẠI kết quả (fail-closed, fix audit #3):
    nếu vẫn còn vi phạm NẶNG thì ghi thêm entry cảnh báo
    {"remaining_violation": True, "replacement": ""} vào changes — caller
    phải bắt người dùng sửa tay, không được im lặng cho qua.
    """
    out = text or ""
    changes: List[Dict[str, Any]] = []
    for rx, replacement, category in _COMPLIANCE_SEVERE:
        def _sub(m, rx=rx, replacement=replacement, category=category):
            expanded = m.expand(replacement)
            changes.append({
                "matched": m.group(0),
                "replacement": expanded,
                "category": category,
            })
            return expanded
        out = rx.sub(_sub, out)
    # Lưới an toàn (fix PR #26): nếu sau khi thay vẫn còn "100%" lơ lửng đi
    # kèm cụm hiệu quả/hỗ trợ ("hỗ trợ sức khỏe 100%") thì bỏ "100%" — tránh
    # tạo tuyên bố tuyệt đối mới từ chính phần đã viết lại.
    _leftover_100 = re.compile(
        r"(\b(?:hỗ\s+trợ|hiệu\s+quả|cải\s+thiện|khỏe\s+mạnh)[^.,!]{0,30}?)\s*100%",
        re.I)
    m = _leftover_100.search(out)
    if m:
        changes.append({
            "matched": m.group(0).strip(),
            "replacement": m.group(1).strip(),
            "category": "tuyên bố hiệu quả 100% còn sót sau viết lại",
        })
        out = _leftover_100.sub(lambda mm: mm.group(1), out)
    # Lưới an toàn bổ sung (fix audit #3): "100%" đứng TRƯỚC cụm hỗ trợ
    # ("100% cam kết hiệu quả" -> sau viết lại còn "100% hỗ trợ hiệu quả")
    # cũng là tuyên bố tuyệt đối — bỏ "100%".
    _leftover_100_prefix = re.compile(
        r"100%\s+(?=(?:hỗ\s+trợ|hiệu\s+quả|cải\s+thiện|khỏe\s+mạnh)\b)", re.I)
    m = _leftover_100_prefix.search(out)
    if m:
        changes.append({
            "matched": m.group(0).strip(),
            "replacement": "",
            "category": "tuyên bố hiệu quả 100% còn sót sau viết lại",
        })
        out = _leftover_100_prefix.sub("", out)
    # Re-check fail-closed (fix audit #3): quét lại kết quả sau viết lại.
    # Còn vi phạm NẶNG -> ghi cảnh báo vào changes, KHÔNG tự ý xóa câu
    # (người dùng sửa tay). Vi phạm nhẹ không ghi ở đây (warning ở tầng
    # normalize như cũ).
    for f in check_affiliate_compliance(out):
        if f["level"] == "severe":
            changes.append({
                "matched": f["matched"],
                "replacement": "",
                "category": f"vẫn còn vi phạm sau viết lại: {f['category']}",
                "remaining_violation": True,
            })
    return out, changes


# Câu khuyến cáo bắt buộc cho thực phẩm bảo vệ sức khỏe / thực phẩm bổ sung
# trên TikTok Shop (fix PR #26): phải có trong mô tả video, KHÔNG đọc trong
# thoại từng cảnh. Trang bán Ensure Gold cũng in câu này trong mục Cảnh báo.
HEALTH_DISCLAIMER = (
    "Sản phẩm này không phải là thuốc và không có tác dụng thay thế "
    "thuốc chữa bệnh."
)

_SUPPLEMENT_KEYWORDS = (
    "sữa", "ensure", "vitamin", "thực phẩm bổ sung", "dinh dưỡng",
    "whey", "protein", "thực phẩm chức năng", "men vi sinh",
)


def _needs_health_disclaimer(req: "ProductAffiliateRequest") -> bool:
    """Danh mục Sức khỏe hoặc tên/tính năng gợi ý thực phẩm dinh dưỡng/bổ sung."""
    if (req.category or "").strip().lower() == "health":
        return True
    hay = f"{req.product_name or ''} {req.key_features or ''}".lower()
    return any(k in hay for k in _SUPPLEMENT_KEYWORDS)


# Mẫu giá/khuyến mãi bịa — chỉ dùng khi ô giá của request ĐANG TRỐNG
# (fix PR #26: live test 08:38 — ô giá trống nhưng kịch bản viết
# "đang giảm chỉ còn Chỉ 99k", "trợ giá tụt quần", "Freeship đơn 0đ +
# Quà tặng kèm"). Gốc rò rỉ: JS gửi fallback "Chỉ 99k"/"Freeship..." khi ô
# trống + model default promotion_deal cũ + template local tự điền deal.
_FABRICATED_PRICE_PATTERNS = (
    (re.compile(r"chỉ\s+còn\s+[^.,]{0,40}?\d[\d.,]*\s*k\b", re.I),
     "nhắc giá 'chỉ còn ...k'"),
    (re.compile(r"giảm\s+\d+\s*%", re.I), "nhắc mức giảm %"),
    (re.compile(r"trợ\s+giá", re.I), "nhắc 'trợ giá'"),
    (re.compile(r"\b\d[\d.,]*\s*k\b", re.I), "nhắc giá '...k'"),
)
_FABRICATED_DEAL_PATTERNS = (
    (re.compile(r"freeship", re.I), "nhắc 'freeship'"),
    (re.compile(r"quà\s+tặng", re.I), "nhắc 'quà tặng'"),
)


def _check_fabricated_price(text: str, include_bare: bool = True) -> List[Dict[str, Any]]:
    """Quét giá bịa trong text (chỉ dùng khi request KHÔNG có giá thật).

    include_bare=False cho prompt tiếng Anh: bỏ pattern giá trần "...k" vì
    prompt thương mại luôn chứa "8k" (độ phân giải video), không phải giá.
    """
    patterns = _FABRICATED_PRICE_PATTERNS
    if not include_bare:
        patterns = tuple(p for p in patterns if "nhắc giá '...k'" not in p[1])
    return _check_fabricated_patterns(text, patterns)


def _check_fabricated_deal(text: str) -> List[Dict[str, Any]]:
    """Quét ưu đãi bịa trong text (chỉ dùng khi request KHÔNG có ưu đãi thật)."""
    return _check_fabricated_patterns(text, _FABRICATED_DEAL_PATTERNS)


def _check_fabricated_patterns(text: str, patterns) -> List[Dict[str, Any]]:
    """Quét text theo tuple pattern. Mọi match đều là bịa (chưa có căn cứ)."""
    out: List[Dict[str, Any]] = []
    taken: List[tuple] = []
    for rx, category in patterns:
        for m in rx.finditer(text or ""):
            span = (m.start(), m.end())
            if any(s <= span[0] and span[1] <= e for s, e in taken):
                continue
            taken.append(span)
            out.append({
                "level": "severe",
                "category": f"giá/khuyến mãi bịa ({category})",
                "matched": m.group(0),
                "message": f"giá/khuyến mãi bịa ({category})",
            })
    return out


# ── Làm sạch từ khóa tìm ảnh (fix PR #26) ────────────────────────────────────
# Tên sản phẩm dạng "COMBO 4 LON SỮA ENSURE GOLD DẠNG BỘT HƯƠNG VANI 800G"
# làm Bing hiểu "COMBO" = combo đồ ăn -> trả burger/xe ô tô. Bỏ cụm đóng gói/
# số lượng, giữ thương hiệu + loại sản phẩm: "SỮA ENSURE GOLD".
_PACKAGING_TOKENS = (
    r"\bCOMBO\b",
    r"\bSET\b",
    r"\bBỘ\b",
    r"\b\d+\s*(LON|HỘP|GÓI|CHAI|TÚI|LỌ|VIÊN|GÓI|HŨ|BỊCH|THÙNG|CAN|CÁI|CHIẾC|PACK|PCS|PIECES)\b",
    r"\b(LON|HỘP|GÓI|CHAI|TÚI|LỌ|HŨ|BỊCH|THÙNG)\b",
    r"\bDẠNG\s+\w+",
    r"\bHƯƠNG\s+\w+",
    r"\b\d+\s*(G|KG|ML|L|MG)\b",
    r"\bX\d+\b",
    r"\b\d+\s*\+\s*\d+\b",
)


def _clean_product_search_query(name: str) -> str:
    """Bỏ từ đóng gói/số lượng khỏi tên sản phẩm để tìm ảnh đúng hơn."""
    q = (name or "").strip()
    for pat in _PACKAGING_TOKENS:
        q = re.sub(pat, " ", q, flags=re.I)
    q = re.sub(r"\s+", " ", q).strip(" -")
    return q or (name or "").strip()


def _product_search_keywords(cleaned_query: str) -> List[str]:
    """Từ khóa lọc ảnh: token >= 3 ký tự của query đã làm sạch (bỏ dấu)."""
    words = []
    for w in _strip_vi_diacritics(cleaned_query or "").lower().split():
        w = re.sub(r"[^a-z0-9]+", "", w)
        if len(w) >= 3 and w not in words:
            words.append(w)
    # "sữa" -> thêm "milk" (ảnh nước ngoài hay đặt tên milk).
    if "sua" in words and "milk" not in words:
        words.append("milk")
    return words


def _image_url_matches_product(url: str, keywords: List[str]) -> bool:
    """Ảnh web chỉ giữ khi URL chứa ít nhất 2 từ khóa sản phẩm, HOẶC chứa
    từ khóa thương hiệu (từ dài nhất — vd "ensure" trong
    ["sua", "ensure", "gold", "milk"]).

    Audit #6 (2026-11-25): truoc day chi can 1 tu khoa — URL
    gold-necklace.jpg dau loc cho Ensure Gold chi vi chua "gold".
    Anh nền tảng gốc (Shopee/TikTok) KHÔNG qua lọc này — chúng lấy từ chính
    trang sản phẩm nên đáng tin. Lọc chỉ áp dụng cho kết quả tìm kiếm web.
    """
    if not keywords:
        return True
    hay = _strip_vi_diacritics(url or "").lower()
    hits = [k for k in keywords if k in hay]
    if len(hits) >= 2:
        return True
    brand = max(keywords, key=len)
    return brand in hay


def _normalize_affiliate_script(
    scenes: List[AffiliateScene], req: "ProductAffiliateRequest", *, keep_muse_durations: bool = False
) -> tuple[List[AffiliateScene], List[str]]:
    """Chuẩn hóa hậu xử lý cho kịch bản affiliate (dùng chung local + AI).

    - Tuân thủ chính sách bán hàng (quy tắc cứng 2026-11-24): thoại vi phạm
      NẶNG (chữa bệnh/khỏi bệnh, cam kết tuyệt đối, kết quả phi thực tế) thì
      VIẾT LẠI sang cách nói tuân thủ + warning nêu rõ đã sửa gì; vi phạm NHẸ
      (từ tuyệt đối, khan hiếm/giảm giá chưa kiểm chứng) thì giữ nguyên câu +
      warning bắt người dùng xác nhận trước khi đăng.
    - Giữ thời lượng hợp lệ do Muse chọn. Kịch bản mẫu và cảnh thiếu thời
      lượng hợp lệ dùng độ dài thoại sau khi viết lại để chọn mốc clip.
    - Prompt nào thiếu lock thì APPEND lock + fidelity block (không viết lại
      prompt của LLM). Prompt chứa cụm cho phép đổi sản phẩm thì warning.
    - Thoại dài hơn AFFILIATE_VOICEOVER_MAX_WORDS từ thì chỉ ghi warning,
      KHÔNG cắt lặng lẽ (cắt thoại làm mất ý bán hàng).
    Trả về (scenes, warnings).
    """
    lock = build_product_dna_lock_for_request(req)
    warnings: List[str] = []
    has_price = bool((req.price_sale or "").strip())
    has_deal = bool((req.promotion_deal or "").strip())
    # Danh mục Sức khỏe / thực phẩm bổ sung: nhắc thêm câu khuyến cáo bắt buộc
    # vào MÔ TẢ video (không đọc trong thoại từng cảnh) — fix PR #26 (c).
    if _needs_health_disclaimer(req):
        warnings.append(
            "Danh mục Sức khỏe: nhớ thêm câu khuyến cáo vào mô tả video trước "
            f"khi đăng: '{HEALTH_DISCLAIMER}'"
        )
    for s in scenes:
        # 1. Tuân thủ chính sách: viết lại thoại vi phạm nặng, warning nhẹ.
        new_vo, _changes = rewrite_affiliate_compliance(s.voiceover or "")
        for ch in _changes:
            if ch.get("remaining_violation"):
                # Fail-closed (fix audit #3): rewrite không xử lý hết — cảnh
                # báo riêng, bắt sửa tay, không ghi như "đã viết lại".
                warnings.append(
                    f"Cảnh {s.index}: [CẢNH BÁO] vẫn còn vi phạm chính sách bán "
                    f"hàng sau khi viết lại: '{ch['matched']}' ({ch['category']}) "
                    "— sửa tay trước khi đăng."
                )
            else:
                warnings.append(
                    f"Cảnh {s.index}: đã viết lại câu vi phạm chính sách bán hàng: "
                    f"'{ch['matched']}' → '{ch['replacement']}' ({ch['category']})."
                )
        if new_vo != (s.voiceover or ""):
            s.voiceover = new_vo
        for f in check_affiliate_compliance(s.voiceover or ""):
            if f["level"] == "light":
                warnings.append(
                    f"Cảnh {s.index}: kiểm tra lại trước khi đăng — {f['message']}: "
                    f"'{f['matched']}'."
                )
        # Giá bịa (fix PR #26 d): ô giá/ưu đãi trống mà thoại/prompt nhắc giá/
        # giảm giá/freeship/quà tặng -> NẶNG, bắt sửa tay (không tự bịa số khác).
        # Giá và ưu đãi kiểm riêng: nhập deal thật nhưng chưa nhập giá thì
        # nhắc freeship/quà tặng KHÔNG bị gắn cờ oan.
        if not has_price:
            for f in _check_fabricated_price(s.voiceover or ""):
                warnings.append(
                    f"Cảnh {s.index}: [GIÁ BỊA — CẦN SỬA] thoại nhắc "
                    f"{f['message']}: '{f['matched']}' nhưng ô giá đang trống — "
                    "xóa câu này hoặc nhập giá thật trước khi đăng."
                )
            for f in _check_fabricated_price(s.prompt or "", include_bare=False):
                warnings.append(
                    f"Cảnh {s.index}: [GIÁ BỊA — CẦN SỬA] prompt nhắc "
                    f"{f['message']}: '{f['matched']}' nhưng ô giá đang trống — "
                    "xóa câu này hoặc nhập giá thật trước khi render."
                )
        if not has_deal:
            for f in _check_fabricated_deal(s.voiceover or ""):
                warnings.append(
                    f"Cảnh {s.index}: [GIÁ BỊA — CẦN SỬA] thoại nhắc "
                    f"{f['message']}: '{f['matched']}' nhưng ô ưu đãi đang trống — "
                    "xóa câu này hoặc nhập ưu đãi thật trước khi đăng."
                )
            for f in _check_fabricated_deal(s.prompt or ""):
                warnings.append(
                    f"Cảnh {s.index}: [GIÁ BỊA — CẦN SỬA] prompt nhắc "
                    f"{f['message']}: '{f['matched']}' nhưng ô ưu đãi đang trống — "
                    "xóa câu này hoặc nhập ưu đãi thật trước khi render."
                )
        # Prompt: chỉ gắn warning (không viết lại prompt tự động).
        for f in check_affiliate_compliance(s.prompt or ""):
            if f["level"] == "severe":
                warnings.append(
                    f"Cảnh {s.index}: prompt chứa cụm vi phạm chính sách bán hàng: "
                    f"'{f['matched']}' ({f['category']}) — sửa tay trước khi render."
                )
            else:
                warnings.append(
                    f"Cảnh {s.index}: prompt chứa {f['message']}: "
                    f"'{f['matched']}' — kiểm tra lại trước khi đăng."
                )
        for risky in check_prompt_fidelity(s.prompt or ""):
            warnings.append(
                f"Cảnh {s.index}: prompt chứa cụm có thể làm AI đổi sản phẩm: "
                f"'{risky}' — kiểm tra lại hình sản phẩm trước khi render."
            )
        if not keep_muse_durations or "duration" not in s.model_fields_set:
            s.duration = snap_clip_duration(estimate_voiceover_seconds(s.voiceover))
        if PRODUCT_LOCK_MARKER not in (s.prompt or ""):
            s.prompt = f"{(s.prompt or '').strip()} {lock}".strip()
        # Kiểm nội dung khối thật (fix audit #15): chỉ có marker, hoặc block
        # bị cắt/paraphrase thiếu câu then chốt, thì append lại khối chuẩn.
        if not fidelity_block_complete(s.prompt or ""):
            s.prompt = f"{(s.prompt or '').strip()} {FIDELITY_BLOCK}".strip()
        words = len((s.voiceover or "").split())
        if words > AFFILIATE_VOICEOVER_MAX_WORDS:
            warnings.append(
                f"Cảnh {s.index}: thoại {words} từ (>{AFFILIATE_VOICEOVER_MAX_WORDS} từ) "
                f"— dài hơn clip tối đa 10s, video sẽ loop để đọc kịp thoại; nên rút ngắn thoại."
            )
    return scenes, warnings



# ── 3. Prompt Builder (Sinh Visual Prompt Tiếng Anh chuẩn Commercial) ─────────

def _build_commercial_prompt_base(
    role: str,
    product_name: str,
    category: str,
    key_feature: str,
    koc_persona_id: str = "nam_bua_hai",
    aspect_ratio: str = "9:16",
    shooting_style: str = "faceless"
) -> str:
    """Prompt gốc theo từng style/role (CHƯA có Product DNA lock).

    Tạo prompt tiếng Anh thương mại điện ảnh cực nét theo 4 Kiểu quay:
    1. 'faceless': Người mẫu không lộ mặt (quay từ cổ trở xuống hoặc từ phía sau)
    2. 'hands': Chỉ quay tay (cầm, xoay, thao tác trên nền đẹp, macro tabletop)
    3. 'product': Chỉ quay sản phẩm (quảng cáo studio, hoàn toàn không người)
    4. 'koc_face': KOC lộ mặt đầy đủ (khóa ADN nhân vật đồng nhất 100%)

    Hàm public build_commercial_prompt() bọc hàm này và append lock nguyên văn.
    """
    cat_cfg = AFFILIATE_CATEGORIES.get(category, AFFILIATE_CATEGORIES["gadgets"])
    style_tag = cat_cfg["style_tag"]
    persona_cfg = KOC_PERSONAS.get(koc_persona_id, KOC_PERSONAS["nam_bua_hai"])
    char_dna = persona_cfg["prompt_dna"]
    p_name_en = translate_product_to_en(product_name)

    # Khung hình theo đúng aspect_ratio truyền vào (trước đây hardcode vertical
    # 9:16 cho mọi tỉ lệ). Giá trị lạ -> fallback vertical 9:16 + giữ nguyên.
    _framing_map = {
        "16:9": "horizontal 16:9 framing",
        "9:16": "vertical 9:16 framing",
        "1:1": "square 1:1 framing",
        "4:3": "horizontal 4:3 framing",
        "21:9": "ultrawide 21:9 framing",
    }
    framing = _framing_map.get((aspect_ratio or "").strip(), "vertical 9:16 framing")

    # ─────────────────────────────────────────────────────────────
    # STYLE 1: Người mẫu không lộ mặt (Faceless Model)
    # Mẫu mặc/dùng sản phẩm, chỉ quay từ cổ trở xuống hoặc từ phía sau
    # ─────────────────────────────────────────────────────────────
    if shooting_style == "faceless":
        gender_wardrobe = "stylish modern neutral oversized streetwear" if persona_cfg.get("gender") == "male" else "elegant modern minimalist pastel outfit"
        faceless_tag = f"aesthetic faceless model wearing {gender_wardrobe}, strictly shot from neck down or from behind, absolutely no facial features visible"

        if role == "hook":
            return (
                f"High-energy commercial video hook, {faceless_tag}, enthusiastically lifting and presenting the {p_name_en} close to the camera, "
                f"framed strictly from chest down, dynamic subtle push-in camera movement, dramatic warm studio rim lighting, "
                f"{framing}, photorealistic 8k, crisp depth of field, {style_tag}, luxury commercial advertising masterpiece, strictly no face, no head portrait, no text, no watermark"
            )
        elif role == "pain":
            return (
                f"Relatable everyday lifestyle scene, {faceless_tag}, viewed from behind or shoulders down, showing authentic restless body language "
                f"struggling with common hassle before discovering {p_name_en}, moody ambient indoor lighting, medium close-up shot from neck down, "
                f"consistent identical clothing, {framing}, hyper-realistic 8k, cinematic realism, no face visible, no text, no watermark"
            )
        elif role == "demo":
            return (
                f"Cinematic product in action shot, {faceless_tag}, shot from torso down effortlessly wearing and operating the {p_name_en}, "
                f"showcasing {key_feature or 'smooth premium functionality'}, softbox diffused studio lighting, pristine clean reflections, "
                f"{framing}, ultra-sharp textile and product detail, 60fps smooth movement, commercial product cinematography, 8k, no face, no text, no watermark"
            )
        elif role == "proof":
            return (
                f"Aesthetic modern lifestyle result, {faceless_tag}, viewed gracefully from behind or waist down posing with the {p_name_en}, "
                f"relaxed confident posture, warm ambient golden hour lighting, clean aesthetic minimalist room, "
                f"{framing}, identical stylish outfit, authentic satisfaction, 8k photorealistic masterpiece, no face, no text, no watermark"
            )
        elif role == "cta":
            return (
                f"High conversion commercial finale, {faceless_tag}, holding {p_name_en} prominently in front of the vertical camera, "
                f"hand gesturing excitedly pointing down towards the bottom-left corner of the vertical screen, "
                f"studio spotlight, sparkling particle bokeh, dramatic rim light, {framing}, strictly shot from neck down, "
                f"high conversion e-commerce aesthetic, 8k resolution, luxury commercial advertising masterpiece, no face, no text, no watermark"
            )
        else:
            return (
                f"Commercial advertising shot, {faceless_tag} with {p_name_en}, {framing}, studio lighting, "
                f"8k resolution, cinematic color grading, photorealistic masterpiece, strictly no face, no text, no watermark"
            )

    # ─────────────────────────────────────────────────────────────
    # STYLE 2: Chỉ quay tay (Hands Only)
    # Đôi tay cầm, xoay, thao tác với sản phẩm trên nền đẹp
    # ─────────────────────────────────────────────────────────────
    elif shooting_style == "hands":
        tabletop_env = "clean aesthetic minimalist oak wood desk with soft morning daylight and pastel props"

        if role == "hook":
            return (
                f"Eye-catching macro unboxing hook, clean well-groomed aesthetic hands lifting and revealing the {p_name_en} from a sleek minimalist gift box, "
                f"placed on a {tabletop_env}, dynamic 45-degree macro angle, crisp focus on the product, warm softbox lighting, "
                f"{framing}, 8k resolution, commercial advertising masterpiece, strictly only hands and product, no person, no face, no text, no watermark"
            )
        elif role == "pain":
            return (
                f"Macro close-up demonstration, aesthetic hands struggling with an inferior old broken alternative, then gently pushing it aside for the new {p_name_en}, "
                f"on a {tabletop_env}, moody dramatic lighting, extreme macro detail, {framing}, "
                f"crisp tactile details, strictly hands only, no human face, 8k, cinematic product ad, no text, no watermark"
            )
        elif role == "demo":
            return (
                f"Satisfying macro hands-on operation shot, pair of elegant hands effortlessly rotating 360 degrees, pressing buttons and demonstrating the {p_name_en}, "
                f"showcasing {key_feature or 'smooth mechanical precision'}, on a {tabletop_env}, soft diffused studio light, pristine product reflections, "
                f"{framing}, ultra-sharp macro lens, 60fps smooth movement, commercial product cinematography, strictly hands only, zero face, 8k, no text, no watermark"
            )
        elif role == "proof":
            return (
                f"Satisfying macro showcase, aesthetic hands gently presenting the finished perfect result with the {p_name_en}, "
                f"resting hands next to the product on a {tabletop_env}, soft golden hour rim light, beautiful creamy depth of field bokeh, "
                f"{framing}, close-up hands only, no face, no torso, 8k photorealistic commercial, no text, no watermark"
            )
        elif role == "cta":
            return (
                f"Commercial close-up finale, aesthetic hands placing the {p_name_en} heroically in center frame, "
                f"one index finger pointing clearly down towards the bottom-left corner of the vertical screen, "
                f"sparkling dust particles bokeh, dynamic studio spotlight, {framing}, strictly hands and product only, "
                f"high conversion e-commerce commercial, 8k resolution, no person, no face, no text, no watermark"
            )
        else:
            return (
                f"Macro commercial advertising shot, aesthetic hands interacting with {p_name_en} on a {tabletop_env}, "
                f"{framing}, studio lighting, 8k resolution, strictly hands only, no face, no text, no watermark"
            )

    # ─────────────────────────────────────────────────────────────
    # STYLE 3: Chỉ quay sản phẩm (Product Only / Commercial Showcase)
    # Không có người — sản phẩm được quay như quảng cáo
    # ─────────────────────────────────────────────────────────────
    elif shooting_style == "product":
        studio_env = "luxury minimalist studio showcase with rotating motorized matte turntable, volumetric softbox lighting, clean gradient background"

        if role == "hook":
            return (
                f"High-end 3D commercial advertising hook, pure product shot with STRICTLY ZERO HUMANS and NO HANDS, {p_name_en} floating dynamically "
                f"or rising dramatically on a {studio_env}, dramatic volumetric rim lighting, cinematic slow-motion rotation, "
                f"crisp 8k product reflections, metallic and glass highlights, {framing}, luxury TV commercial grade advertising, masterpiece, strictly no people, no text, no watermark"
            )
        elif role == "pain":
            return (
                f"Cinematic mood shot, the {p_name_en} displayed with dramatic high-contrast moody lighting on a textured dark granite pedestal, "
                f"extreme macro detail, dramatic shallow depth of field, showcasing durable robust build, pure product showcase with ZERO HUMANS, "
                f"{framing}, 8k resolution, cinematic commercial color grading, no person, no hands, no text, no watermark"
            )
        elif role == "demo":
            return (
                f"Pristine commercial demonstration, slow orbiting camera gliding smoothly 360 degrees around the {p_name_en}, "
                f"demonstrating {key_feature or 'cutting-edge design and flawless functionality'}, mechanical precision, crystal clear reflections, "
                f"luxury commercial cinematography, {studio_env}, STRICTLY NO PEOPLE, NO HANDS, {framing}, 8k resolution, advertising masterpiece, no text, no watermark"
            )
        elif role == "proof":
            return (
                f"Gleaming luxury advertising beauty shot, the {p_name_en} resting on a premium minimalist white stone podium with soft pastel shadows and ambient light rays, "
                f"glowing halo rim effect, 8k advertising masterpiece, pure clean product cinematography, zero people, zero hands, "
                f"{framing}, photorealistic commercial, no text, no watermark"
            )
        elif role == "cta":
            return (
                f"Explosive commercial finale, the {p_name_en} in centered hero pose on a reflective glossy pedestal, dynamic spotlight flare, "
                f"sparkling particle bokeh, dramatic camera tilt-up, high converting e-commerce commercial banner, "
                f"STRICTLY NO HUMANS, NO HANDS, {framing}, 8k resolution, commercial advertising masterpiece, no text, no watermark"
            )
        else:
            return (
                f"Commercial product advertising shot, {p_name_en} on {studio_env}, {framing}, studio lighting, "
                f"8k resolution, strictly no people, no hands, photorealistic masterpiece, no text, no watermark"
            )

    # ─────────────────────────────────────────────────────────────
    # STYLE 4: KOC lộ mặt đầy đủ (KOC Face Model)
    # Khóa chặt ĐỒNG NHẤT 100% nhân vật KOC xuyên suốt toàn bộ phân cảnh
    # ─────────────────────────────────────────────────────────────
    else:
        if role == "hook":
            return (
                f"High-energy commercial video hook, {char_dna}, looking directly at camera with an excited shocked expression, "
                f"holding up the {p_name_en} enthusiastically, dynamic subtle push-in camera movement, dramatic studio rim lighting, "
                f"{framing}, photorealistic 8k, crisp macro focus, depth of field, {style_tag}, masterpiece, commercial advertising, no text, no watermark"
            )
        elif role == "pain":
            return (
                f"Relatable everyday lifestyle scene, the EXACT SAME {char_dna}, showing relatable funny frustration and distress "
                f"while struggling with everyday annoyance before having {p_name_en}, cinematic moody indoor lighting, medium close-up shot, "
                f"consistent facial identity, same hair and same outfit, {framing}, hyper-realistic 8k, masterpiece, no text, no watermark"
            )
        elif role == "demo":
            return (
                f"Macro product demonstration shot, the hands of the SAME {char_dna} effortlessly operating the {p_name_en}, "
                f"showcasing {key_feature or 'smooth functionality'}, softbox diffused studio lighting, pristine clean reflections, "
                f"{framing}, ultra-sharp detail, 60fps smooth movement, commercial product cinematography, 8k, masterpiece, no text, no watermark"
            )
        elif role == "proof":
            return (
                f"Bright joyful lifestyle result, the EXACT SAME {char_dna}, smiling radiantly with a satisfied nod and giving a confident thumbs up "
                f"next to the {p_name_en}, modern aesthetic background, warm ambient indoor lighting, {framing}, "
                f"identical facial features, same clothing, authentic satisfaction, cinematic color grading, 8k, photorealistic, masterpiece, no text, no watermark"
            )
        elif role == "cta":
            return (
                f"High conversion commercial finale, the EXACT SAME {char_dna}, holding the {p_name_en} with a big winning smile, "
                f"pointing finger excitedly down towards the bottom-left corner of the vertical screen, "
                f"studio spotlight, sparkling particle bokeh, dramatic rim light, {framing}, identical character face and attire, "
                f"high conversion e-commerce aesthetic, 8k resolution, luxury commercial advertising masterpiece, no text, no watermark"
            )
        else:
            return (
                f"Commercial advertising shot, {char_dna} with {p_name_en}, {framing}, studio lighting, "
                f"8k resolution, cinematic color grading, photorealistic masterpiece, no text, no watermark"
            )


def build_commercial_prompt(
    role: str,
    product_name: str,
    category: str,
    key_feature: str,
    koc_persona_id: str = "nam_bua_hai",
    aspect_ratio: str = "9:16",
    shooting_style: str = "faceless"
) -> str:
    """Prompt thương mại cho 1 cảnh — LUÔN kèm Product DNA lock nguyên văn.

    Bọc _build_commercial_prompt_base() và append build_product_dna_lock()
    vào cuối prompt để tay/sản phẩm/bối cảnh không trôi giữa các cảnh.
    """
    base = _build_commercial_prompt_base(
        role, product_name, category, key_feature,
        koc_persona_id, aspect_ratio, shooting_style,
    )
    out = base
    if PRODUCT_LOCK_MARKER not in out:
        out = (
            f"{out} "
            f"{build_product_dna_lock(product_name, category, key_feature, koc_persona_id, shooting_style)}"
        ).strip()
    # Khóa trung thực ảnh sản phẩm (quy tắc cứng 2026-11-24): sản phẩm phải
    # giống y hệt ảnh reference — không vẽ lại/đổi màu/đổi hình/đổi kích thước.
    # Kiểm nội dung khối thật (fix audit #15): chỉ có marker, hoặc block bị
    # cắt/paraphrase thiếu câu then chốt, thì append lại khối chuẩn.
    if not fidelity_block_complete(out):
        out = f"{out} {FIDELITY_BLOCK}".strip()
    return out


# ── 4. Bộ Sinh Kịch Bản Tiếp Thị Ngoại Tuyến Siêu Tốc (Local Generator) ────────

def generate_affiliate_script_local(req: ProductAffiliateRequest) -> AffiliateScriptResponse:
    """
    Bộ sinh kịch bản Tiếp thị liên kết cục bộ thông minh, chuẩn quy luật tiếp thị hiện đại.
    Đảm bảo 100% hoạt động ngay lập tức ngay cả khi không có kết nối internet hoặc API ngoài.
    """
    p_name = req.product_name.strip() or "Sản phẩm thông minh"
    cat_cfg = AFFILIATE_CATEGORIES.get(req.category, AFFILIATE_CATEGORIES["gadgets"])
    p_pain = req.pain_points.strip() if req.pain_points else cat_cfg["default_pain"]
    p_features = req.key_features.strip() if req.key_features else cat_cfg["default_features"]
    p_sale = (req.price_sale or "").strip()
    p_deal = (req.promotion_deal or "").strip()
    # Giá/ưu đãi: KHÔNG tự bịa khi user chưa nhập (fix PR #26 d — live test
    # 08:38: ô giá trống nhưng kịch bản viết "Chỉ 99k"/"Freeship..."). Chỉ
    # dùng giá trị thật; khi trống, các câu phụ thuộc giá dùng biến thể trung
    # tính (không giá, không giảm giá, không freeship, không quà tặng).
    has_price = bool(p_sale)
    has_deal = bool(p_deal)
    framework = req.framework.lower()
    voice = req.voice or cat_cfg["default_voice"]
    ref_img = req.koc_image or req.reference_image

    shooting_style = getattr(req, "shooting_style", "faceless") or "faceless"
    koc_persona_id = getattr(req, "koc_persona", "nam_bua_hai") or "nam_bua_hai"

    def _prompt(r: str) -> str:
        return build_commercial_prompt(
            role=r,
            product_name=p_name,
            category=req.category,
            key_feature=p_features,
            koc_persona_id=koc_persona_id,
            aspect_ratio=req.aspect_ratio,
            shooting_style=shooting_style,
        )


    scenes: List[AffiliateScene] = []
    title = f"🔥 Review Siêu Phẩm {p_name} — Đừng Mua Nếu Chưa Xem Clip Này!"
    notes = [
        "Hook 3s đầu sử dụng câu hỏi đảo hoặc khẳng định gây sốc để kéo CTR retention > 65%.",
        "Lời thoại được tối ưu ngắt câu ngắn, tự nhiên cho bộ đọc TTS, không vấp từ.",
        "Đoạn kết CTA luôn gắn chặt vào hành động 'bấm giỏ hàng góc trái'."
    ]

    # --- KHUNG 1: HÀI HƯỚC / BỰA TROLL (Tối ưu riêng cho ⭐ Adam bựa) ---
    if framework == "hai_bua" or req.tone == "hai_bua":
        title = f"🤣 [BỰA REVIEW] {p_name} — Thề luôn, không xem phí cả nửa đời người!"
        # Tôn trọng giọng user đã chọn: chỉ dùng giọng mặc định của framework
        # (AdamBua) khi user chưa chọn giọng khác. Override thì log rõ ràng.
        _default_voice = ProductAffiliateRequest.model_fields["voice"].default
        if voice and voice != _default_voice:
            print(f"[Affiliate Local] Warning: framework 'hai_bua' khuyến nghị giọng vieneu:AdamBua nhưng tôn trọng giọng user đã chọn ({voice}).")
        else:
            voice = "vieneu:AdamBua"
        scenes = [
            AffiliateScene(
                index=1,
                title="Cảnh 1: Hook Bựa 3 Giây Chặn Lướt",
                role="hook",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("hook"),
                voiceover=f"Dừng lại 3 giây! Thề với các bác, ai mà lướt qua cái clip này thì chỉ có nước tiếc đứt ruột thôi!",
                voice=voice,
                badge="XEM NGAY KẺO TIẾC 🔥",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=2,
                title="Cảnh 2: Tình Huống Cay Đắng Hàng Ngày",
                role="pain",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("pain"),
                voiceover=f"Bình thường ở nhà toàn dính quả {p_pain}, vừa tốn tiền oan lại còn rước bực vào người, cay không chịu nổi!",
                voice=voice,
                badge="VẤN ĐỀ NHỨC NHỐI 😡",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=3,
                title="Cảnh 3: Cứu Tinh Xuất Hiện Đỉnh Chóp",
                role="demo",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("demo"),
                voiceover=f"Cho đến khi tôi vớ được em {p_name} này! Úi chùi ui, nó đỉnh của chóp luôn các bác ạ. {p_features}, dùng sướng tê người!",
                voice=voice,
                badge="ĐỈNH CỦA CHÓP ⭐",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=4,
                title="Cảnh 4: Deal Hời Tụt Quần",
                role="proof",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("proof"),
                voiceover=(f"Đã thế hôm nay đang có giá ưu đãi {p_sale}"
                           f"{f', lại còn {p_deal}' if has_deal else ''} nữa chứ!"
                           if has_price else
                           "Đã thế em này đang được nhiều người săn đón, bấm vào giỏ hàng góc trái xem giá thật của shop là rõ ngay!"),
                voice=voice,
                badge="SALE SỐC HÔM NAY ⚡",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=5,
                title="Cảnh 5: Chốt Đơn Gấp Trong Giỏ Hàng",
                role="cta",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("cta"),
                voiceover=f"Nhanh cái tay lẹ cái mắt bấm ngay vào giỏ hàng màu vàng bên góc trái để múc luôn nha!",
                voice=voice,
                badge="GIỎ HÀNG GÓC TRÁI 👇",
                reference_image=ref_img
            )
        ]

    # --- KHUNG 2: REVIEW KOC / ĐẬP HỘP CHÂN THỰC ---
    elif framework == "review_koc":
        title = f"📦 Đập Hộp & Test Thực Tế {p_name} — Có Thực Sự Đáng Mua Không?"
        # Giọng user khác default thì giữ nguyên; chỉ khi user để giọng mặc
        # định mới đổi sang TrucLy (giọng hợp framework này) và log rõ ràng.
        if req.voice and req.voice != ProductAffiliateRequest.model_fields["voice"].default:
            voice = req.voice
        else:
            print("[Affiliate Local] Warning: framework 'review_koc' đổi giọng mặc định sang vieneu:TrucLy (hợp format đập hộp/review).")
            voice = "vieneu:TrucLy"
        scenes = [
            AffiliateScene(
                index=1,
                title="Cảnh 1: Mở Đầu Đập Hộp Trực Tiếp",
                role="hook",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("hook"),
                voiceover=f"Cùng mình đập hộp và test thử em {p_name} đang làm mưa làm gió mấy ngày nay xem có xịn thật không nha!",
                voice=voice,
                badge="REVIEW CHÂN THỰC 📦",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=2,
                title="Cảnh 2: Cảm Quan & Chất Lượng Hoàn Thiện",
                role="pain",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("pain"),
                voiceover=f"Trước đây mình rất ngại mua trên mạng vì sợ hàng đểu mau hỏng, nhưng cầm em này trên tay cảm giác chắc chắn cực kỳ.",
                voice=voice,
                badge="CHÍNH HÃNG ✨",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=3,
                title="Cảnh 3: Test Tính Năng Cốt Lõi Tại Chỗ",
                role="demo",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("demo"),
                voiceover=f"Mình test luôn công năng thực tế cho mọi người xem nè. Điểm mình ưng nhất là {p_features}, hoạt động siêu mượt mà.",
                voice=voice,
                badge="TEST TẠI CHỖ 🔍",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=4,
                title="Cảnh 4: Đánh Giá 10/10 & Giá Khuyến Mãi",
                role="proof",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("proof"),
                voiceover=(f"Mình đánh giá rất cao em này luôn nha! Hiện tại đang có giá ưu đãi {p_sale}"
                           f"{f' và kèm {p_deal}' if has_deal else ''}."
                           if has_price else
                           "Mình đánh giá rất cao em này luôn nha! Bấm vào giỏ hàng góc trái xem giá thật của shop nhé!"),
                voice=voice,
                badge="ĐÁNH GIÁ CAO 🌟",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=5,
                title="Cảnh 5: Hướng Dẫn Săn Deal Góc Trái",
                role="cta",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("cta"),
                voiceover=f"Mọi người muốn mua hàng chuẩn thì bấm ngay vào giỏ hàng màu vàng bên góc trái màn hình để nhận voucher nhé!",
                voice=voice,
                badge="GIỎ HÀNG CHÍNH HÃNG 👇",
                reference_image=ref_img
            )
        ]

    # --- KHUNG 3: BEFORE / AFTER (CHUYỂN HOÁ NGOẠN MỤC) ---
    elif framework == "before_after":
        title = f"🔄 Sự Khác Biệt Một Trời Một Vực Trước & Sau Khi Có {p_name}!"
        scenes = [
            AffiliateScene(
                index=1,
                title="Cảnh 1: Hook Biến Hóa Trước Sau",
                role="hook",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("hook"),
                voiceover=f"Sự khác biệt một trời một vực trước và sau khi mình biết đến em {p_name} này, xem mà ngỡ ngàng luôn!",
                voice=voice,
                badge="BEFORE & AFTER 🔄",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=2,
                title="Cảnh 2: Trước Khi Dùng (Vất Vả, Khó Chịu)",
                role="pain",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("pain"),
                voiceover=f"Trước đây mỗi lần gặp cảnh {p_pain} là mình chỉ muốn bỏ cuộc, vừa mất thời gian vừa bực mình cả ngày.",
                voice=voice,
                badge="TRƯỚC KHI DÙNG ❌",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=3,
                title="Cảnh 3: Sau Khi Dùng (Nhàn Tênh, Thư Thái)",
                role="demo",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("demo"),
                voiceover=f"Còn bây giờ thì nhàn tênh! Nhờ có {p_features}, chỉ cần vài giây là mọi thứ xong xuôi gọn gàng.",
                voice=voice,
                badge="SAU KHI DÙNG ✅",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=4,
                title="Cảnh 4: Giá Trị Vượt Xa Mong Đợi",
                role="proof",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("proof"),
                voiceover=(f"Đúng là đầu tư thông minh nhất tháng này, vừa tiết kiệm công sức mà giá sale hôm nay {p_sale}."
                           if has_price else
                           "Đúng là đầu tư thông minh nhất tháng này, vừa tiết kiệm công sức lại tiện lợi mỗi ngày."),
                voice=voice,
                badge="TIẾT KIỆM THỜI GIAN ⏱️",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=5,
                title="Cảnh 5: Nhanh Tay Nhận Voucher",
                role="cta",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("cta"),
                voiceover=(f"Voucher giảm giá và {p_deal} mình để sẵn ở giỏ hàng góc trái, cả nhà nhanh tay chốt ngay kẻo hết hạn nha!"
                           if has_deal else
                           "Mình để sẵn link mua ở giỏ hàng góc trái, cả nhà bấm vào xem giá thật của shop rồi chốt nhé!"),
                voice=voice,
                badge="MUA NGAY HÔM NAY 👇",
                reference_image=ref_img
            )
        ]

    # --- KHUNG 4: AIDA (ATTENTION - INTEREST - DESIRE - ACTION) ---
    elif framework == "aida":
        title = f"⚡ Bí Quyết Sở Hữu {p_name} Với Giá Rẻ Bất Ngờ!"
        scenes = [
            AffiliateScene(
                index=1,
                title="Cảnh 1: Attention - Cảnh Báo Gây Sốc",
                role="hook",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("hook"),
                voiceover=f"Cảnh báo: Đây là món đồ mà bạn ước gì mình đã mua sớm hơn, xem ngay để không bị mua đắt nhé!",
                voice=voice,
                badge="CẢNH BÁO HOT ⚠️",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=2,
                title="Cảnh 2: Interest - Tính Năng Bất Ngờ",
                role="pain",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("pain"),
                voiceover=f"Đừng để việc {p_pain} làm phiền bạn thêm một ngày nào nữa, bởi giải pháp đã nằm trọn trong chiếc hộp này.",
                voice=voice,
                badge="GIẢI PHÁP ĐỘT PHÁ 💡",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=3,
                title="Cảnh 3: Desire - Khao Khát Sở Hữu",
                role="demo",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("demo"),
                voiceover=f"Sở hữu {p_name} với công nghệ {p_features}. Thiết kế sang xịn mịn, ai nhìn qua cũng phải hỏi địa chỉ mua.",
                voice=voice,
                badge="THIẾT KẾ ĐẲNG CẤP ✨",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=4,
                title="Cảnh 4: Cam Kết Uy Tín & Deal",
                role="proof",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("proof"),
                voiceover=(f"Đang có deal hời với giá {p_sale}"
                           f"{f' kèm {p_deal}' if has_deal else ''}, bấm vào giỏ hàng góc trái xem ngay nhé!"
                           if has_price else
                           "Bấm vào giỏ hàng góc trái xem giá thật và ưu đãi của shop nhé!"),
                voice=voice,
                badge="UY TÍN HÀNG ĐẦU 🏆",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=5,
                title="Cảnh 5: Action - Chốt Đơn Lập Tức",
                role="cta",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("cta"),
                voiceover=(f"Số lượng flash sale có hạn, chạm ngay vào giỏ hàng màu vàng góc dưới bên trái để nhận ưu đãi ngay hôm nay!"
                           if has_deal else
                           "Chạm ngay vào giỏ hàng màu vàng góc dưới bên trái để xem giá thật và đặt mua ngay hôm nay!"),
                voice=voice,
                badge="BẤM VÀO GIỎ HÀNG 👇",
                reference_image=ref_img
            )
        ]

    # --- KHUNG 5: PAS (PROBLEM - AGITATE - SOLUTION - MẶC ĐỊNH) ---
    else:
        title = f"🎯 Đừng Để {p_pain} Làm Phiền Bạn — Khám Phá Ngay {p_name}!"
        scenes = [
            AffiliateScene(
                index=1,
                title="Cảnh 1: Hook 3s Đánh Trúng Tâm Lý",
                role="hook",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("hook"),
                voiceover=f"Nếu bạn đang phát bực vì {p_pain} thì dừng lại 3 giây, đây chính là chân ái dành riêng cho bạn!",
                voice=voice,
                badge="CHÂN ÁI XUẤT HIỆN 🔥",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=2,
                title="Cảnh 2: Thổi Bùng Nỗi Đau Khó Chịu",
                role="pain",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("pain"),
                voiceover=f"Cứ tưởng tượng mỗi ngày đi làm về mệt mỏi mà vẫn phải chịu đựng cảnh này, vừa tốn thời gian lại hại sức khỏe.",
                voice=voice,
                badge="NỖI ĐAU THƯỜNG GẶP 😫",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=3,
                title="Cảnh 3: Giải Pháp & Trình Diễn Đột Phá",
                role="demo",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("demo"),
                voiceover=f"Nhưng mọi thứ được giải quyết êm ru nhờ {p_name}! Điểm ăn tiền nhất là {p_features}, tiện lợi vô cùng.",
                voice=voice,
                badge="GIẢI PHÁP TOÀN DIỆN 🚀",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=4,
                title="Cảnh 4: Kết Quả & Giá Siêu Ưu Đãi",
                role="proof",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("proof"),
                voiceover=(f"Giá hôm nay {p_sale}"
                           f"{f', lại còn được {p_deal}' if has_deal else ''}."
                           if has_price else
                           "Bấm vào giỏ hàng góc trái xem giá thật của shop nhé!"),
                voice=voice,
                badge="GIÁ SỐC HÔM NAY 🏷️" if has_price else "GIÁ TRONG GIỎ HÀNG 🏷️",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=5,
                title="Cảnh 5: Kêu Gọi Mua Hàng Góc Trái",
                role="cta",
                duration=AFFILIATE_SCENE_DURATION,
                prompt=_prompt("cta"),
                voiceover=(f"Bấm ngay vào giỏ hàng màu vàng ở góc trái màn hình để săn deal hời trước khi hết mã freeship nhé!"
                           if has_deal else
                           "Bấm ngay vào giỏ hàng màu vàng ở góc trái màn hình để xem giá thật và đặt mua nhé!"),
                voice=voice,
                badge="GIỎ HÀNG GÓC TRÁI 👇",
                reference_image=ref_img
            )
        ]

    # Chuẩn hóa thời lượng theo thoại + lock (phòng khi framework nào đó quên):
    # duration mỗi cảnh = mốc clip API nhỏ nhất phủ được thoại (5/6/8/10s),
    # append lock nếu thiếu, cảnh báo thoại dài quá 10s (không cắt lặng lẽ).
    scenes, _vo_warnings = _normalize_affiliate_script(scenes, req)
    notes = notes + [
        "Mỗi cảnh dài theo thoại (clip 5/6/8/10s — thoại ngắn clip ngắn, thoại dài clip dài, tối đa 10s); thoại dài hơn 10s thì video loop để đọc kịp.",
        "Render nối tiếp (sequential chain): cảnh sau dùng last frame cảnh trước để giữ tay/sản phẩm/bối cảnh đồng nhất.",
    ] + _vo_warnings

    return AffiliateScriptResponse(
        title=title,
        framework=framework,
        target_platform=req.target_platform,
        aspect_ratio=req.aspect_ratio,
        recommended_voice=voice,
        hook_strength="99/100",
        marketing_notes=notes,
        scenes=scenes,
        warnings=_vo_warnings,
        compliance_disclaimer=(
            HEALTH_DISCLAIMER if _needs_health_disclaimer(req) else None),
    )


# ── 5. AI Script Generator (Gọi Muse2API hoặc LLM có Fallback) ───────────────

def _extract_json_object(raw_text: str) -> Optional[Dict[str, Any]]:
    """Bóc JSON object khỏi phản hồi LLM một cách cân bằng (không dùng regex greedy).

    Tìm dấu '{' đầu tiên rồi dùng json.JSONDecoder().raw_decode để parse đúng
    tới dấu '}' đóng tương ứng — LLM trả thêm text sau JSON cũng không fail.
    """
    start = raw_text.find("{")
    if start == -1:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(raw_text[start:])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


async def generate_affiliate_script_ai(
    req: ProductAffiliateRequest,
    muse_api_url: Optional[str] = None,
    muse_api_key: Optional[str] = None
) -> AffiliateScriptResponse:
    """
    Tạo kịch bản video tiếp thị liên kết bằng AI LLM thông minh qua muse2api /v1/chat/completions.
    Nếu Muse bận render hoặc không phản hồi, tự động fallback sang bộ tạo cục bộ siêu tốc.

    Lấy kết nối từ cấu hình provider khi caller không truyền đầy đủ.
    Thiếu key thì fallback local ngay và log rõ ràng (không gọi AI với key
    sai rồi mới fallback).
    """
    if not muse_api_url or not muse_api_key:
        from muse_connection import active_muse_provider, muse_connection
        _, configured_url, configured_key = muse_connection(active_muse_provider())
        muse_api_url = muse_api_url or configured_url
        muse_api_key = muse_api_key or configured_key
    if not muse_api_key:
        print("[Affiliate AI] Thiếu khóa cho provider Muse: bỏ qua gọi AI, dùng bộ tạo cục bộ.")
        return generate_affiliate_script_local(req)
    # Product DNA lock: yêu cầu LLM dán nguyên văn vào cuối prompt MỌI cảnh;
    # hậu xử lý vẫn append lại nếu LLM quên (không viết lại prompt của LLM).
    _dna_lock = build_product_dna_lock_for_request(req)
    _ar = (req.aspect_ratio or "9:16").strip()
    _ar_desc = (
        "dọc 9:16" if _ar == "9:16"
        else "vuông 1:1" if _ar == "1:1"
        else f"ngang {_ar}"
    )
    _ar_en = (
        "vertical 9:16" if _ar == "9:16"
        else "square 1:1" if _ar == "1:1"
        else f"widescreen {_ar}"
    )
    sys_instruction = (
        "Bạn là Chuyên gia Marketing Tiếp thị Liên kết hàng đầu (TikTok Shop & Shopee Top Creator).\n"
        f"Nhiệm vụ: Viết kịch bản video ngắn (15s-30s) {_ar_desc} bán hàng bùng nổ chuyển đổi theo đúng định dạng JSON.\n"
        "Quy tắc bắt buộc:\n"
        "1. Phải có đúng 5 cảnh: Scene 1 (Hook 3s), Scene 2 (Pain point/Vấn đề), Scene 3 (Demo sản phẩm/Giải pháp), Scene 4 (Proof/Deal hời), Scene 5 (CTA giỏ hàng góc trái).\n"
        f"2. Prompt cho mỗi cảnh phải là TIẾNG ANH thương mại điện ảnh cực kỳ chi tiết, tỷ lệ {_ar_en}, studio lighting, 8k, photorealistic, no text, no watermark.\n"
        "3. Voiceover phải là tiếng Việt tự nhiên, có dấu chấm câu ngắt nghỉ rõ ràng để máy đọc TTS chuẩn xác.\n"
        "4. Trả về DUY NHẤT một khối JSON hợp lệ theo schema:\n"
        "{\n"
        '  "title": "Tên kịch bản giật tít",\n'
        '  "scenes": [\n'
        '    {"index": 1, "title": "...", "role": "hook", "duration": 10, "prompt": "...", "voiceover": "...", "badge": "..."},\n'
        "    ...\n"
        "  ]\n"
        "}\n"
        "5. Mỗi cảnh duration là một trong 5/6/8/10 (clip API video chỉ nhận các mốc này): "
        "chọn mốc nhỏ nhất phủ được voiceover — thoại ngắn (~5s) lấy 5, thoại dài (~10s) lấy 10; "
        "voiceover mỗi cảnh khoảng 25-30 từ tiếng Việt.\n"
        "6. Dán NGUYÊN VĂN khối PRODUCT CONTINUITY LOCK sau vào CUỐI prompt của MỌI cảnh, không sửa một chữ:\n"
        f"{_dna_lock}\n"
        "7. Dán NGUYÊN VĂN khối PRODUCT FIDELITY LOCK sau vào CUỐI prompt của MỌI cảnh, ngay sau PRODUCT CONTINUITY LOCK, không sửa một chữ "
        "(sản phẩm phải giống y hệt ảnh tham chiếu — không vẽ lại/đổi màu/đổi hình/đổi kích thước):\n"
        f"{FIDELITY_BLOCK}\n"
        "8. TUÂN THỦ CHÍNH SÁCH BÁN HÀNG (TikTok Shop/Shopee) — tuyệt đối không viết: tuyên bố chữa bệnh/khỏi bệnh ('chữa', 'trị khỏi', 'đặc trị', 'khỏi bệnh'), "
        "cam kết tuyệt đối ('100% khỏi', 'hiệu quả tuyệt đối', 'chắc chắn khỏi'), kết quả phi thực tế ('giảm 5kg trong 3 ngày', 'trắng sau 1 lần'), "
        "giảm giá/khan hiếm giả khi không có căn cứ ('giảm 90%', 'chỉ còn 1 suất', 'duy nhất hôm nay'), từ tuyệt đối không chứng cứ ('tốt nhất', 'số 1', 'duy nhất'). "
        "Với thực phẩm bổ sung/dinh dưỡng (vd sữa) chỉ dùng cách nói 'hỗ trợ dinh dưỡng/sức khỏe', không dùng 'chữa/khỏi bệnh'.\n"
        "9. GIÁ/ƯU ĐÃI: chỉ nhắc giá, mức giảm, freeship, quà tặng, trợ giá khi user_prompt dưới đây ghi GIÁ/ƯU ĐÃI THẬT. "
        "Nếu user_prompt ghi 'CHƯA CÓ' thì tuyệt đối KHÔNG được tự bịa bất kỳ con số giá/mức giảm nào — chỉ nói 'xem giá trong giỏ hàng'.\n"
    )

    # Giá/ưu đãi: chỉ đưa số thật vào prompt; khi trống thì ghi rõ CHƯA CÓ
    # kèm lệnh cấm bịa (fix PR #26 d — live test 08:38: ô giá trống nhưng LLM
    # viết "đang giảm chỉ còn Chỉ 99k" vì prompt cũ để trống không cấm).
    _price_line = (
        f"Giá khuyến mãi: {req.price_sale}\n" if (req.price_sale or "").strip()
        else "Giá khuyến mãi: CHƯA CÓ — tuyệt đối KHÔNG được tự bịa bất kỳ con số giá/mức giảm nào; chỉ nói 'xem giá trong giỏ hàng'.\n"
    )
    _deal_line = (
        f"Ưu đãi đi kèm: {req.promotion_deal}\n" if (req.promotion_deal or "").strip()
        else "Ưu đãi đi kèm: KHÔNG CÓ — tuyệt đối KHÔNG được nhắc freeship/quà tặng/trợ giá/giảm giá.\n"
    )

    user_prompt = (
        f"Sản phẩm: {req.product_name}\n"
        f"Ngành hàng: {req.category}\n"
        f"Đối tượng nhắm tới: {req.target_audience}\n"
        f"Vấn đề/Nỗi đau: {req.pain_points}\n"
        f"Tính năng nổi bật: {req.key_features}\n"
        f"{_price_line}"
        f"{_deal_line}"
        f"Công thức kịch bản: {req.framework}\n"
        f"Kiểu quay (shooting_style): {getattr(req, 'shooting_style', 'faceless') or 'faceless'}\n"
        f"Giọng điệu/Tone: {req.tone}\n"
        f"PRODUCT LOCK (dán nguyên văn vào cuối prompt của mọi cảnh): {_dna_lock}"
    )

    try:
        # Allow the browser to finish a long storyboard and return its JSON.
        async with httpx.AsyncClient(timeout=httpx.Timeout(150.0, connect=5.0)) as client:
            resp = await client.post(
                f"{muse_api_url.rstrip('/')}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {muse_api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "muse-spark",
                    "timeout": 120,
                    "messages": [
                        {"role": "system", "content": sys_instruction},
                        {"role": "user", "content": user_prompt}
                    ],
                    "temperature": 0.7
                }
            )
            if resp.status_code == 200:
                raw_text = resp.json()["choices"][0]["message"]["content"]
                # Bóc tách JSON từ phản hồi LLM (parse cân bằng, chịu được
                # text thừa sau JSON — regex greedy cũ làm json.loads fail).
                parsed = _extract_json_object(raw_text)
                if parsed is not None:
                    scenes_list = []
                    _base_ref = req.koc_image or req.reference_image
                    _shoot = getattr(req, "shooting_style", "faceless") or "faceless"
                    for item in parsed.get("scenes", []):
                        _role = item.get("role", "demo")
                        duration = item.get("duration")
                        if isinstance(duration, str):
                            duration = next((d for d in AFFILIATE_CLIP_DURATIONS if duration.strip() == str(d)), None)
                        scenes_list.append(AffiliateScene(
                            index=int(item.get("index", len(scenes_list) + 1)),
                            title=item.get("title", f"Cảnh {len(scenes_list) + 1}"),
                            role=_role,
                            **({"duration": duration} if duration in AFFILIATE_CLIP_DURATIONS else {}),
                            prompt=item.get(
                                "prompt",
                                build_commercial_prompt(
                                    _role,
                                    req.product_name,
                                    req.category,
                                    req.key_features or "",
                                    koc_persona_id=req.koc_persona or "nam_bua_hai",
                                    aspect_ratio=_ar,
                                    shooting_style=_shoot,
                                ),
                            ),
                            voiceover=item.get("voiceover", ""),
                            voice=req.voice,
                            badge=item.get("badge", "GIỎ HÀNG GÓC TRÁI 👇"),
                            reference_image=_base_ref,
                        ))
                    if len(scenes_list) >= 3:
                        scenes_list, _vo_warnings = _normalize_affiliate_script(
                            scenes_list, req, keep_muse_durations=True)
                        return AffiliateScriptResponse(
                            title=parsed.get("title", f"🔥 Video Tiếp Thị {req.product_name}"),
                            framework=req.framework,
                            target_platform=req.target_platform,
                            aspect_ratio=req.aspect_ratio,
                            recommended_voice=req.voice,
                            hook_strength="99/100",
                            marketing_notes=[
                                "Được tạo bởi AI Muse Chat Model với tối ưu chuyển đổi trực tuyến.",
                                "Thời lượng clip theo kịch bản Muse (5/6/8/10s). Cảnh thiếu thời lượng hợp lệ được ước tính theo lời thoại.",
                                "Render nối tiếp (sequential chain): cảnh sau dùng last frame cảnh trước để giữ tay/sản phẩm/bối cảnh đồng nhất.",
                            ] + _vo_warnings,
                            scenes=scenes_list,
                            warnings=_vo_warnings,
                            compliance_disclaimer=(
                                HEALTH_DISCLAIMER
                                if _needs_health_disclaimer(req) else None),
                        )
    except Exception as e:
        print(f"[Affiliate AI] Muse chat unavailable or returned parse error ({e}), activating local generator fallback...")

    fallback = generate_affiliate_script_local(req)
    fallback.warnings.insert(0, "Studio đang hiển thị kịch bản mẫu vì chưa nhận được kịch bản Muse hợp lệ.")
    return fallback


# ── 6. Nhập link sản phẩm (Shopee / TikTok) ──────────────────────────────────
# Dán link sản phẩm -> bấm 1 cái -> prefill form affiliate (tên, danh mục,
# giá, ảnh, shop) + ảnh sản phẩm làm reference cho Product DNA lock.
#
# KẾT QUẢ NGHIÊN CỨU BẰNG MẠNG THẬT (2026-11-23, thử từ máy kiểm chứng):
# - TikTok oEmbed (www.tiktok.com/oembed?url=...): 200, nhưng payload TỐI GIẢN
#   (chỉ video_id + iframe embed + kích thước) — KHÔNG có title/author/thumbnail.
# - Trang video TikTok: JS shell, không có og tags, không có JSON nhúng.
# - Trang sản phẩm Shopee: JS shell (+ dấu hiệu captcha), không có og tags.
# - Shopee api/v4/item/get: 403 (chống bot). Endpoint PDP mới: 404.
# => Server-side CHỈ lấy chắc được: (1) tên sản phẩm từ slug URL Shopee
# (slug chính là tên sản phẩm), shopid/itemid từ URL; (2) xác nhận video
# TikTok tồn tại + video_id qua oEmbed. Giá/ảnh/shop chỉ lấy được khi API
# Shopee bất ngờ trả 200 (đã code sẵn đường này). Khi không lấy được gì thì
# trả fetched=False + message rõ ràng, UI vẫn cho nhập tay (không kẹt).
#
# BỔ SUNG 2026-11-24 (link thật của Tiến: vt.tiktok.com -> TikTok Shop):
# - Link chia sẻ TikTok SHOP redirect tới /view/product/<id>?...&og_info=
#   <JSON đã mã hóa URL> — trong og_info có SẴN title + image sản phẩm.
#   Đây là nguồn đáng tin nhất cho link TikTok Shop (trang sản phẩm chỉ là
#   JS shell 5KB, không og tags, không giá, không dữ liệu nhúng).
# - Giá KHÔNG có trong link TikTok Shop -> nhập tay.
# - httpx mặc định đọc proxy từ biến môi trường; NO_PROXY chứa IPv6 dạng
#   ngoặc như [::1] làm httpx lỗi 'Invalid port' — đã dùng trust_env=False.

AFFILIATE_LINK_TIMEOUT_S = 15.0  # giây cho mỗi lượt tải
AFFILIATE_LINK_MAX_BYTES = 2 * 1024 * 1024  # tối đa 2MB nội dung tải về
AFFILIATE_LINK_HOSTS = {
    "shopee.vn", "www.shopee.vn", "shopee.com", "www.shopee.com",
    "tiktok.com", "www.tiktok.com",
}
AFFILIATE_LINK_SHORT_HOSTS = {"shope.ee", "vt.tiktok.com"}
_AFFILIATE_LINK_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# Từ khóa đoán danh mục từ tên sản phẩm (best-effort; trả "" nếu không đoán được).
_CATEGORY_KEYWORDS = {
    "gadgets": ("giá đỡ", "tai nghe", "sạc", "cáp", "đèn", "ốp lưng", "loa", "chuột", "bàn phím", "đồng hồ", "ốp"),
    "home": ("nồi", "máy hút", "máy xay", "robot", "cây lau", "chảo", "bếp", "quạt", "máy lọc", "nồi chiên"),
    "beauty": ("kem", "serum", "son", "sữa rửa mặt", "mặt nạ", "toner", "phấn", "dưỡng", "chống nắng"),
    "fashion": ("áo", "quần", "giày", "kính", "túi", "váy", "đầm", "dép"),
    "food": ("khô bò", "bánh tráng", "rong biển", "trà sữa", "hạt điều", "khô gà", "bánh", "kẹo"),
    "health": ("bình nước", "thảm", "gối", "kẹo dẻo", "máy massage", "yoga", "vitamin", "sữa", "ensure", "dinh dưỡng", "whey", "protein"),
}


def _strip_vi_diacritics(s: str) -> str:
    """Bỏ dấu tiếng Việt để so khớp từ khóa (slug URL Shopee không có dấu)."""
    import unicodedata

    s = unicodedata.normalize("NFD", s or "")
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return s.replace("đ", "d").replace("Đ", "D")


def guess_affiliate_category(product_name: str) -> str:
    """Đoán danh mục từ tên sản phẩm (best-effort, trả "" nếu không đoán được).

    So khớp không phân biệt dấu (tên lấy từ slug Shopee không có dấu).
    """
    low = _strip_vi_diacritics((product_name or "").lower())
    if not low.strip():
        return ""
    for cat, kws in _CATEGORY_KEYWORDS.items():
        if any(_strip_vi_diacritics(k.lower()) in low for k in kws):
            return cat
    return ""


class _LinkResponse:
    """Response tối giản cho fetch link (đủ cho parse + test mock)."""

    def __init__(self, status_code: int, url: str, text: str = "", content: bytes = b""):
        self.status_code = status_code
        self.url = url
        self.text = text
        self.content = content

    def json(self):
        return json.loads(self.text)


def _default_link_http_get(url: str, **kwargs) -> _LinkResponse:
    """GET bằng IP pinning, tự theo redirect từng hop (mỗi hop đều validate), giới hạn 2MB.

    Chống SSRF (audit #1): TẮT follow_redirects; mỗi hop redirect đều qua
    _validate_and_resolve_url của story_engine (chặn IP nội bộ/CGNAT + pin
    IP chống DNS-rebinding) TRƯỚC khi tải body. Dùng chung cơ chế với story.
    Hàm riêng để test mock được; trust_env=False (xem ghi chú cũ dưới đây
    về NO_PROXY chứa [::1]).

    trust_env=False: KHÔNG đọc proxy từ biến môi trường — NO_PROXY chứa
    IPv6 dạng ngoặc như [::1] làm httpx lỗi 'Invalid port' (gặp thật trên
    máy kiểm chứng 2026-11-24). Studio chạy local, không cần proxy hệ thống.
    """
    from urllib.parse import urlencode, urljoin

    from story_engine import _pinned_fetch

    timeout = kwargs.pop("timeout", AFFILIATE_LINK_TIMEOUT_S)
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("User-Agent", _AFFILIATE_LINK_UA)
    params = kwargs.pop("params", None)
    if kwargs:
        raise TypeError(f"_default_link_http_get: tham số không hỗ trợ: {sorted(kwargs)}")
    current = url
    if params:
        sep = "&" if "?" in current else "?"
        current = current + sep + urlencode(params, doseq=True)
    for _ in range(6):
        status, resp_headers, body = _pinned_fetch(
            current, headers, timeout, AFFILIATE_LINK_MAX_BYTES,
            "Nội dung tải về vượt quá 2MB",
        )
        if status in (301, 302, 303, 307, 308):
            loc = resp_headers.get("location")
            if not loc:
                raise ValueError("Link chuyển hướng thiếu header Location")
            current = urljoin(current, loc)
            continue
        text = body.decode("utf-8", errors="replace")
        return _LinkResponse(status, current, text=text, content=body)
    raise ValueError("Link chuyển hướng quá nhiều lần (tối đa 5 lần)")


def _og_tags(html: str) -> Dict[str, str]:
    """Bóc og:title / og:image / og:description / product:price:amount từ HTML."""
    out: Dict[str, str] = {}
    for prop in ("og:title", "og:image", "og:description", "og:url",
                 "product:price:amount", "product:original_price:amount"):
        m = re.search(
            r'<meta[^>]+property=["\']' + re.escape(prop) + r'["\'][^>]+content=["\']([^"\']+)',
            html or "",
        ) or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']' + re.escape(prop) + r'["\']',
            html or "",
        )
        if m:
            out[prop] = m.group(1).strip()
    return out


def _shopee_ids_from_url(url: str) -> Dict[str, str]:
    """Tách shopid/itemid từ URL Shopee dạng i.<shopid>.<itemid> hoặc /product/<shopid>/<itemid>."""
    m = re.search(r"[.-]i\.(\d+)\.(\d+)(?:[/?#]|$)", url or "")
    if not m:
        m = re.search(r"/product/(\d+)/(\d+)(?:[/?#]|$)", url or "")
    if not m:
        return {}
    return {"shop_id": m.group(1), "item_id": m.group(2)}


def _shopee_api_item(shop_id: str, item_id: str, get, referer: str = "https://shopee.vn/") -> Dict[str, Any]:
    """Gọi Shopee api/v4/item/get, trả dict 'data' khi thành công, {} khi lỗi.

    API này thường trả 403 chống bot — best-effort, KHÔNG raise. Dùng chung
    cho import-link (lấy tên/giá/ảnh) và find-product-images (lấy mảng ảnh).
    `get` là hàm http_get đã inject (cùng chữ ký _default_link_http_get).
    """
    try:
        api_url = (
            "https://shopee.vn/api/v4/item/get"
            f"?itemid={item_id}&shopid={shop_id}"
        )
        r = get(api_url, headers={"Referer": referer})
        if r.status_code != 200:
            return {}
        data = (r.json().get("data") or {}) if hasattr(r, "json") else {}
        return data if isinstance(data, dict) and data.get("name") else {}
    except Exception:
        return {}


def _shopee_image_urls(images: Any) -> List[str]:
    """Đổi mảng images của Shopee API thành URL CDN đầy đủ (tối đa 8, lọc trùng)."""
    out: List[str] = []
    for img in (images or []):
        if not img:
            continue
        url = "https://down-vn.img.susercontent.com/file/" + str(img)
        if url not in out:
            out.append(url)
        if len(out) >= 8:
            break
    return out


def _shopee_name_from_slug(url: str) -> str:
    """Tên sản phẩm từ slug URL Shopee (slug chính là tên sản phẩm).

    VD: .../Gia-do-dien-thoai-xoay-360-do-...-i.1057167833.28821306303
    -> "Gia do dien thoai xoay 360 do ..." (giữ nguyên dấu nếu slug có mã hóa).
    """
    from urllib.parse import unquote, urlparse

    try:
        path = urlparse(url or "").path
    except Exception:
        return ""
    seg = path.rsplit("/", 1)[-1] if path else ""
    seg = re.sub(r"-i\.\d+\.\d+$", "", seg)
    # URL dạng /product/<shopid>/<itemid> không có slug: đoạn cuối chỉ là số
    # -> không phải tên sản phẩm, trả rỗng (đừng lấy itemid làm tên).
    if not seg or seg.isdigit():
        return ""
    name = unquote(seg).replace("-", " ").strip()
    name = re.sub(r"\s+", " ", name)
    return name


def _tiktok_video_from_url(url: str) -> Dict[str, str]:
    """Tách @tác giả + video_id từ URL TikTok dạng @user/video/<id>."""
    m = re.search(r"tiktok\.com/@([^/]+)/video/(\d+)", url or "")
    if m:
        return {"author": m.group(1), "video_id": m.group(2)}
    m = re.search(r"tiktok\.com/.*/video/(\d+)", url or "")
    if m:
        return {"video_id": m.group(1)}
    return {}


def _tiktok_product_from_url(url: str) -> Dict[str, str]:
    """Tách product_id từ URL TikTok Shop dạng /view/product/<id>."""
    m = re.search(r"tiktok\.com/view/product/(\d+)", url or "")
    if m:
        return {"product_id": m.group(1)}
    return {}


def _tiktok_og_info_from_url(url: str) -> Dict[str, str]:
    """Bóc tên + ảnh sản phẩm từ tham số og_info trên URL chia sẻ TikTok Shop.

    Link chia sẻ TikTok Shop (vt.tiktok.com) redirect tới
    /view/product/<id>?...&og_info=<JSON đã mã hóa URL> — trong đó có sẵn
    title + image của sản phẩm. Đây là nguồn ĐÁNG TIN NHẤT cho link TikTok
    Shop vì trang sản phẩm chỉ là JS shell (không og tags, không giá).
    Trả {} nếu không có og_info hoặc parse lỗi.
    """
    from urllib.parse import parse_qs, unquote, urlparse

    try:
        qs = parse_qs(urlparse(url or "").query)
        raw = (qs.get("og_info") or [""])[0]
        if not raw:
            return {}
        data = json.loads(unquote(raw))
        if not isinstance(data, dict):
            return {}
        out: Dict[str, str] = {}
        if data.get("title"):
            out["title"] = str(data["title"]).strip()
        if data.get("image"):
            out["image"] = str(data["image"]).strip()
        return out
    except Exception:
        return {}


def _shopee_price_vnd(raw: Any) -> int:
    """Giá Shopee API trả theo đơn vị 1/100000 VNĐ -> đổi ra VNĐ."""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 0
    if v <= 0:
        return 0
    return v // 100000 if v >= 100000 else v


def _format_vnd(vnd: int) -> str:
    """123000 -> '123.000₫'."""
    try:
        v = int(vnd)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return ""
    return f"{v:,}".replace(",", ".") + "₫"


def import_affiliate_product_link(
    url: str, http_get: Optional[Any] = None
) -> Dict[str, Any]:
    """Nhập thông tin sản phẩm từ link Shopee/TikTok (không cần key).

    Trả về dict prefill cho form affiliate. Luôn trả 1 dict (không raise) trừ
    2 trường hợp: URL sai/host không hỗ trợ -> ValueError (endpoint map 422);
    tải link thất bại hoàn toàn (timeout/mạng) -> RuntimeError (map 502).
    Khi link hợp lệ nhưng không lấy được chi tiết thì fetched=False + message
    rõ ràng, UI vẫn cho nhập tay.
    """
    raw_url = (url or "").strip()
    if not raw_url:
        raise ValueError("Vui lòng dán link sản phẩm Shopee hoặc TikTok.")
    if not re.match(r"^https?://", raw_url, re.IGNORECASE):
        raise ValueError("Link phải bắt đầu bằng http:// hoặc https://.")
    from urllib.parse import urlparse

    try:
        host = (urlparse(raw_url).hostname or "").lower()
    except Exception:
        raise ValueError("Link không hợp lệ.")
    if host not in AFFILIATE_LINK_HOSTS and host not in AFFILIATE_LINK_SHORT_HOSTS:
        raise ValueError(
            f"Chỉ hỗ trợ link Shopee (shopee.vn, shope.ee) và TikTok "
            f"(tiktok.com, vt.tiktok.com); host '{host}' chưa hỗ trợ."
        )

    get = http_get or _default_link_http_get
    try:
        first = get(raw_url)
    except ValueError:
        raise
    except Exception as e:
        raise RuntimeError(f"Không tải được link ({e}). Kiểm tra lại mạng rồi thử lại.")

    final_url = str(getattr(first, "url", raw_url) or raw_url)
    try:
        final_host = (urlparse(final_url).hostname or "").lower()
    except Exception:
        final_host = ""
    if final_host not in AFFILIATE_LINK_HOSTS:
        raise ValueError(
            f"Link rút gọn dẫn tới host '{final_host or '?'}' — chỉ hỗ trợ "
            f"Shopee và TikTok sau khi mở link."
        )

    platform = "shopee" if "shopee" in final_host else "tiktok"
    result: Dict[str, Any] = {
        "platform": platform,
        "url": final_url,
        "product_name": "",
        "price_sale": "",
        "price_original": "",
        "image_url": "",
        "image_urls": [],
        "shop_name": "",
        "author": "",
        "category": "",
        "item_id": "",
        "shop_id": "",
        "video_id": "",
        "product_id": "",
        "source": "url_only",
        "fetched": False,
        "message": "",
    }

    if platform == "shopee":
        ids = _shopee_ids_from_url(final_url)
        result.update(ids)
        slug_name = _shopee_name_from_slug(final_url)
        # (1) Thử API Shopee (thường 403 chống bot — best-effort).
        api_ok = False
        if ids.get("item_id") and ids.get("shop_id"):
            data = _shopee_api_item(ids["shop_id"], ids["item_id"], get, referer=final_url)
            if data:
                api_ok = True
                result["product_name"] = str(data["name"]).strip()
                price = _shopee_price_vnd(data.get("price"))
                price_max = _shopee_price_vnd(data.get("price_max"))
                if price:
                    result["price_sale"] = f"Chỉ {_format_vnd(price)}"
                if price_max and price_max > price:
                    result["price_original"] = _format_vnd(price_max)
                result["image_urls"] = _shopee_image_urls(data.get("images"))
                if result["image_urls"]:
                    result["image_url"] = result["image_urls"][0]
                shop = data.get("shop") or {}
                if isinstance(shop, dict) and shop.get("name"):
                    result["shop_name"] = str(shop["name"])
                if data.get("description"):
                    result["description"] = str(data["description"])[:500]
                result["source"] = "shopee_api"
        # (2) og tags trên trang sản phẩm (trang Shopee là JS shell — thường trống).
        if not api_ok and getattr(first, "status_code", 0) == 200:
            og = _og_tags(getattr(first, "text", "") or "")
            if og.get("og:title"):
                result["product_name"] = og["og:title"]
                _og_vimg = (og.get("og:image") or "").strip()
                result["image_url"] = _og_vimg if (_og_vimg and _is_image_url(_og_vimg)) else ""
                result["source"] = "og_tags"
                api_ok = True
        # (3) Fallback chắc chắn nhất: tên từ slug URL.
        if not api_ok and slug_name:
            result["product_name"] = slug_name
            result["source"] = "shopee_slug"
            result["fetched"] = True
            result["message"] = (
                "Đã lấy tên sản phẩm từ link (Shopee chặn tải chi tiết nên "
                "chưa có giá/ảnh — nhập tay giá và tải ảnh sản phẩm nếu cần)."
            )
        elif api_ok:
            result["fetched"] = True
            result["message"] = "Đã lấy thông tin sản phẩm từ Shopee."
        if not result["fetched"]:
            result["message"] = (
                "Không lấy được thông tin từ link Shopee này (Shopee chặn tải "
                "chi tiết, link không có tên trong địa chỉ) — vui lòng nhập tay."
            )
    else:  # tiktok
        vid: Dict[str, str] = {}
        # (0) Link TikTok SHOP (/view/product/<id>, thường từ vt.tiktok.com):
        # tên + ảnh có sẵn trong tham số og_info của URL sau redirect — lấy
        # ngay từ URL, không cần tải trang (trang sản phẩm chỉ là JS shell,
        # không og tags, không giá). Giá không có trong link -> nhập tay.
        prod = _tiktok_product_from_url(final_url)
        if prod:
            result.update(prod)
            og_info = _tiktok_og_info_from_url(final_url)
            if og_info.get("title"):
                result["product_name"] = og_info["title"]
                _og_img = (og_info.get("image") or "").strip()
                if _og_img and _is_image_url(_og_img):
                    result["image_url"] = _og_img
                    result["image_urls"] = [_og_img]
                else:
                    result["image_url"] = ""
                result["source"] = "tiktok_og_info"
                result["fetched"] = True
                result["message"] = (
                    "Đã lấy tên + ảnh sản phẩm từ link TikTok Shop "
                    "(giá không có trong link — nhập tay giá bán)."
                )
            else:
                result["message"] = (
                    "Đây là link sản phẩm TikTok Shop nhưng link không kèm "
                    "tên sản phẩm — nhập tên sản phẩm tay để tạo video."
                )
        if not result["fetched"] and not prod:
            vid = _tiktok_video_from_url(final_url)
            result.update(vid)
        # oEmbed xác nhận video tồn tại + lấy video_id (không có title/author/thumbnail).
        oembed_ok = False
        if vid.get("video_id"):
            try:
                from urllib.parse import quote

                r = get(
                    "https://www.tiktok.com/oembed?url=" + quote(final_url, safe=""),
                    headers={"Accept": "application/json"},
                )
                if r.status_code == 200:
                    data = r.json() if hasattr(r, "json") else {}
                    if data.get("embed_product_id") or data.get("type") == "video":
                        oembed_ok = True
                        result["source"] = "tiktok_oembed"
            except Exception:
                oembed_ok = False
        # og tags trên trang video (trang TikTok là JS shell — thường trống).
        if getattr(first, "status_code", 0) == 200:
            og = _og_tags(getattr(first, "text", "") or "")
            if og.get("og:title"):
                result["product_name"] = og["og:title"]
                _og_vimg = (og.get("og:image") or "").strip()
                result["image_url"] = _og_vimg if (_og_vimg and _is_image_url(_og_vimg)) else ""
                result["source"] = "og_tags"
                result["fetched"] = True
                result["message"] = "Đã lấy thông tin video TikTok."
        if not result["fetched"]:
            if oembed_ok:
                result["message"] = (
                    "Đã xác nhận video TikTok tồn tại, nhưng TikTok không trả tên "
                    "sản phẩm/ảnh qua link — nhập tên sản phẩm tay để tạo video."
                )
            else:
                result["message"] = (
                    "Không lấy được thông tin từ link TikTok này — kiểm tra lại link "
                    "hoặc nhập tên sản phẩm tay."
                )

    if result.get("product_name"):
        result["category"] = guess_affiliate_category(result["product_name"])
    return result


# ── 7. Tìm ảnh sản phẩm chuẩn (khi ảnh chỉ có vỏ hộp) ───────────────────────
# Quy tắc cứng 2026-11-24: nếu ảnh sản phẩm chỉ là thùng/vỏ (không thấy bên
# trong) thì tự tìm kiếm để lấy ảnh sản phẩm chuẩn xác.
#
# Nguồn theo thứ tự ưu tiên (best-effort, KHÔNG thêm dependency):
#   (1) ảnh từ nền tảng gốc khi có link: Shopee API trả mảng images (nhiều
#       ảnh), TikTok og_info chỉ 1 ảnh;
#   (2) DuckDuckGo image search (lấy vqd từ trang kết quả rồi gọi i.js);
#   (3) Bing image search (bóc &quot;murl&quot; trong HTML).
#
# KẾT QUẢ THỬ BẰNG MẠNG THẬT (2026-11-24, từ máy kiểm chứng qua proxy):
# - DuckDuckGo: lấy được vqd (HTTP 202) nhưng i.js trả botnet_challenge
#   (response_type "botnet_challenge") — bị chặn từ IP egress này; từ máy
#   người dùng (IP nhà mạng) có thể qua được, không chắc chắn.
# - Bing images HTML: 200, bóc được 36 murl (URL ảnh trực tiếp) — CHẠY ĐƯỢC.
# - html.duckduckgo.com/html: 200 nhưng trả kết quả WEB (link trang), không
#   phải ảnh — không dùng làm nguồn ảnh.
# => Code thử DuckDuckGo trước, rớt thì dùng Bing; cả hai đều rớt thì trả lỗi
# rõ, UI vẫn cho tải ảnh tay. Việc tự động PHÁT HIỆN "ảnh chỉ có vỏ hộp"
# không làm được chắc chắn nên để người dùng bấm nút tìm khi cần.
#
# LƯU Ý BẢN QUYỀN: ảnh tìm được là ảnh trên web (ảnh shop/người bán đăng);
# người dùng tự chọn ảnh đúng sản phẩm của mình làm Product Lock. Không tự
# động lấy ảnh lạ làm ảnh đại diện khi chưa có người chọn.

_AFFILIATE_IMAGE_MAX = 8
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_DDG_UA = _AFFILIATE_LINK_UA


def _is_image_url(url: str) -> bool:
    """Chỉ nhận URL ảnh http(s) (jpg/png/webp, cho phép query string).

    Một số CDN ảnh nền tảng (Shopee down-vn.img.susercontent.com, TikTok
    *.ibyteimg.com) không gắn đuôi file vào URL nên chấp nhận thêm theo host.
    """
    try:
        from urllib.parse import urlparse

        p = urlparse(url or "")
        if p.scheme not in ("http", "https") or not p.netloc:
            return False
        if p.path.lower().endswith(_IMAGE_EXTS):
            return True
        host = p.netloc.lower()
        return host.endswith(("img.susercontent.com", "ibyteimg.com"))
    except Exception:
        return False


def _dedup_image_urls(urls: List[str], limit: int = _AFFILIATE_IMAGE_MAX) -> List[str]:
    out: List[str] = []
    for u in urls or []:
        u = (u or "").strip()
        if u and u not in out:
            out.append(u)
        if len(out) >= limit:
            break
    return out


def _duckduckgo_image_search(query: str, get) -> List[str]:
    """Ảnh từ DuckDuckGo: lấy vqd rồi gọi i.js. Trả [] khi bị chặn/lỗi."""
    from urllib.parse import quote

    try:
        page = get(
            "https://duckduckgo.com/?q=" + quote(query, safe="") + "&iax=images&ia=images",
            headers={"User-Agent": _DDG_UA},
        )
        if page.status_code != 200:
            return []
        m = re.search(r'vqd="([^"]+)"', getattr(page, "text", "") or "")
        if not m:
            m = re.search(r"vqd='([^']+)'", getattr(page, "text", "") or "")
        if not m:
            return []
        vqd = m.group(1)
        r = get(
            "https://duckduckgo.com/i.js?l=vn-vi&o=json&q=" + quote(query, safe="")
            + "&vqd=" + quote(vqd, safe="") + "&f=,,,&p=1",
            headers={"User-Agent": _DDG_UA, "Referer": "https://duckduckgo.com/"},
        )
        if r.status_code != 200:
            return []
        data = r.json() if hasattr(r, "json") else {}
        if not isinstance(data, dict) or data.get("response_type") == "botnet_challenge":
            return []
        out: List[str] = []
        for item in data.get("results", []) or []:
            img = (item or {}).get("image", "")
            if _is_image_url(img):
                out.append(img)
        return out
    except Exception:
        return []


def _bing_image_search(query: str, get) -> List[str]:
    """Ảnh từ Bing images HTML: bóc &quot;murl&quot; (URL ảnh trực tiếp, hỗ trợ query string chứa &amp;)."""
    import html as _html_mod
    from urllib.parse import quote

    try:
        r = get(
            "https://www.bing.com/images/search?q=" + quote(query, safe="")
            + "&form=HDRSC2",
            headers={"User-Agent": _DDG_UA},
        )
        if r.status_code != 200:
            return []
        html_text = getattr(r, "text", "") or ""
        raw = [_html_mod.unescape(m) for m in re.findall(r"&quot;murl&quot;:&quot;((?:(?!&quot;).)+)&quot;", html_text)]
        raw += re.findall(r'"murl":"([^"]+)"', html_text)
        return [u for u in raw if _is_image_url(u)]
    except Exception:
        return []


def find_product_images(
    product_name: str, source_url: Optional[str] = None, http_get: Optional[Any] = None
) -> Dict[str, Any]:
    """Tìm ảnh ứng viên cho sản phẩm (tối đa 8, để người dùng chọn làm Product Lock).

    Thứ tự: (1) ảnh nền tảng gốc từ source_url (Shopee API nhiều ảnh, TikTok
    og_info 1 ảnh); (2) DuckDuckGo; (3) Bing. Trả {"images": [{"url",
    "source", "title"}], "query", "message"}. Không raise khi nguồn lỗi —
    nguồn nào lỗi thì bỏ qua, hết nguồn thì message nêu rõ.
    """
    name = (product_name or "").strip()
    if not name:
        raise ValueError("Vui lòng nhập tên sản phẩm để tìm ảnh.")
    get = http_get or _default_link_http_get
    # Query tìm kiếm đã làm sạch: bỏ cụm đóng gói/số lượng ("COMBO 4 LON",
    # "DẠNG BỘT", "800G") — fix PR #26 (e): tên thô làm Bing hiểu "COMBO"
    # = combo đồ ăn -> trả burger/xe ô tô.
    query = _clean_product_search_query(name)
    keywords = _product_search_keywords(query)
    found: List[Dict[str, str]] = []
    seen: set = set()
    filtered_out = 0

    def _add(url: str, source: str, trusted: bool = False) -> None:
        nonlocal filtered_out
        if len(found) >= _AFFILIATE_IMAGE_MAX:
            return
        if not _is_image_url(url) or url in seen:
            return
        # Ảnh web phải chứa từ khóa sản phẩm trong URL (loại burger/xe ô tô);
        # ảnh nền tảng gốc (Shopee/TikTok) lấy từ chính trang sản phẩm nên
        # giữ nguyên không lọc.
        if not trusted and not _image_url_matches_product(url, keywords):
            filtered_out += 1
            return
        seen.add(url)
        found.append({"url": url, "source": source, "title": name})

    # (1) Ảnh nền tảng gốc (khi có link sản phẩm) — đáng tin, không lọc.
    if source_url:
        try:
            imported = import_affiliate_product_link(source_url, http_get=get)
            for u in imported.get("image_urls", []) or []:
                _add(u, "shopee" if imported.get("platform") == "shopee" else "tiktok",
                     trusted=True)
            if imported.get("image_url"):
                _add(imported["image_url"],
                     "shopee" if imported.get("platform") == "shopee" else "tiktok",
                     trusted=True)
        except Exception:
            pass

    # (2) DuckDuckGo, rớt thì (3) Bing — tìm bằng query đã làm sạch.
    if len(found) < _AFFILIATE_IMAGE_MAX:
        for u in _duckduckgo_image_search(query, get):
            _add(u, "duckduckgo")
    if len(found) < _AFFILIATE_IMAGE_MAX:
        for u in _bing_image_search(query, get):
            _add(u, "bing")

    if not found:
        return {
            "images": [],
            "query": query,
            "message": (
                "Không tìm được ảnh phù hợp cho sản phẩm này"
                + (f" (đã loại {filtered_out} ảnh không liên quan)" if filtered_out else "")
                + " — vui lòng tải ảnh sản phẩm lên tay."
            ),
        }
    return {
        "images": found,
        "query": query,
        "message": (
            f"Đã tìm được {len(found)} ảnh ứng viên"
            + (f" (đã loại {filtered_out} ảnh không liên quan)" if filtered_out else "")
            + " — bấm 1 ảnh để đặt làm ảnh Product Lock."
        ),
    }
