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
        "default_features": "Tự động hóa hoàn toàn, công nghệ hiện đại tiết kiệm 80% thời gian, lau sạch bóng chỉ sau 1 lần",
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
            "Kem dưỡng phục hồi B5 làm dịu mẩn đỏ tức thì"
        ],
        "default_pain": "Da dầu mụn bết dính, nám sạm xỉn màu, dùng nhiều loại mỹ phẩm đắt tiền mà không thấy hiệu quả",
        "default_features": "Thành phần thiên nhiên lành tính, thẩm thấu siêu nhanh sau 3 giây, nâng tone sáng hồng rạng rỡ",
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
            "Gối công thái học chống đau mỏi cổ vai gáy",
            "Kẹo dẻo ngủ ngon thảo mộc tự nhiên Melatonin",
            "Máy massage mắt làm ấm thư giãn giảm thâm quầng"
        ],
        "default_pain": "Làm việc văn phòng mỏi nhừ cổ gáy, ngủ chập chờn mất ngủ, lười uống nước cả ngày",
        "default_features": "Thiết kế chuẩn công thái học nâng đỡ toàn diện, thư giãn giảm căng thẳng tức thì chỉ sau 10 phút sử dụng",
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

def translate_product_to_en(vi_name: str) -> str:
    """Chuyển đổi tên sản phẩm tiếng Việt sang mô tả tiếng Anh để Muse AI nhận diện chuẩn 100%."""
    low = vi_name.lower().strip()
    for kw, en_trans in COMMON_PRODUCT_KEYWORDS.items():
        if kw in low:
            return f"sleek {en_trans} ({vi_name})"
    clean = re.sub(r'[^a-zA-Z0-9\s]', ' ', vi_name).strip()
    return f"sleek commercial product ({clean})" if clean else "sleek lifestyle product"


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

# ── 2. Data Models ──────────────────────────────────────────────────────────

class ProductAffiliateRequest(BaseModel):
    product_name: str
    category: str = "gadgets"
    target_audience: Optional[str] = "Người dùng trẻ, dân văn phòng, học sinh sinh viên"
    price_original: Optional[str] = ""
    price_sale: Optional[str] = ""
    discount_percent: Optional[str] = "50%"
    promotion_deal: Optional[str] = "Freeship 0đ toàn quốc, tặng kèm phụ kiện giới hạn"
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
    duration: int = 5
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


# ── 3. Prompt Builder (Sinh Visual Prompt Tiếng Anh chuẩn Commercial) ─────────

def build_commercial_prompt(
    role: str,
    product_name: str,
    category: str,
    key_feature: str,
    koc_persona_id: str = "nam_bua_hai",
    aspect_ratio: str = "9:16",
    shooting_style: str = "faceless"
) -> str:
    """
    Tạo prompt tiếng Anh thương mại điện ảnh cực nét theo 4 Kiểu quay (Shooting Styles):
    1. 'faceless': Người mẫu không lộ mặt (quay từ cổ trở xuống hoặc từ phía sau, no face visible)
    2. 'hands': Chỉ quay tay (cầm, xoay, thao tác trên nền đẹp, macro tabletop, no face)
    3. 'product': Chỉ quay sản phẩm (quảng cáo studio, hoàn toàn không người, 360 turntable)
    4. 'koc_face': KOC lộ mặt đầy đủ (khóa ADN nhân vật đồng nhất 100%)
    """
    cat_cfg = AFFILIATE_CATEGORIES.get(category, AFFILIATE_CATEGORIES["gadgets"])
    style_tag = cat_cfg["style_tag"]
    persona_cfg = KOC_PERSONAS.get(koc_persona_id, KOC_PERSONAS["nam_bua_hai"])
    char_dna = persona_cfg["prompt_dna"]
    p_name_en = translate_product_to_en(product_name)

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
                f"vertical 9:16 framing, photorealistic 8k, crisp depth of field, {style_tag}, luxury commercial advertising masterpiece, strictly no face, no head portrait, no text, no watermark"
            )
        elif role == "pain":
            return (
                f"Relatable everyday lifestyle scene, {faceless_tag}, viewed from behind or shoulders down, showing authentic restless body language "
                f"struggling with common hassle before discovering {p_name_en}, moody ambient indoor lighting, medium close-up shot from neck down, "
                f"consistent identical clothing, vertical 9:16 framing, hyper-realistic 8k, cinematic realism, no face visible, no text, no watermark"
            )
        elif role == "demo":
            return (
                f"Cinematic product in action shot, {faceless_tag}, shot from torso down effortlessly wearing and operating the {p_name_en}, "
                f"showcasing {key_feature or 'smooth premium functionality'}, softbox diffused studio lighting, pristine clean reflections, "
                f"vertical 9:16 framing, ultra-sharp textile and product detail, 60fps smooth movement, commercial product cinematography, 8k, no face, no text, no watermark"
            )
        elif role == "proof":
            return (
                f"Aesthetic modern lifestyle result, {faceless_tag}, viewed gracefully from behind or waist down posing with the {p_name_en}, "
                f"relaxed confident posture, warm ambient golden hour lighting, clean aesthetic minimalist room, "
                f"vertical 9:16 framing, identical stylish outfit, authentic satisfaction, 8k photorealistic masterpiece, no face, no text, no watermark"
            )
        elif role == "cta":
            return (
                f"High conversion commercial finale, {faceless_tag}, holding {p_name_en} prominently in front of the vertical camera, "
                f"hand gesturing excitedly pointing down towards the bottom-left corner of the vertical screen, "
                f"studio spotlight, sparkling particle bokeh, dramatic rim light, vertical 9:16 framing, strictly shot from neck down, "
                f"high conversion e-commerce aesthetic, 8k resolution, luxury commercial advertising masterpiece, no face, no text, no watermark"
            )
        else:
            return (
                f"Commercial advertising shot, {faceless_tag} with {p_name_en}, vertical 9:16 framing, studio lighting, "
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
                f"vertical 9:16 framing, 8k resolution, commercial advertising masterpiece, strictly only hands and product, no person, no face, no text, no watermark"
            )
        elif role == "pain":
            return (
                f"Macro close-up demonstration, aesthetic hands struggling with an inferior old broken alternative, then gently pushing it aside for the new {p_name_en}, "
                f"on a {tabletop_env}, moody dramatic lighting, extreme macro detail, vertical 9:16 framing, "
                f"crisp tactile details, strictly hands only, no human face, 8k, cinematic product ad, no text, no watermark"
            )
        elif role == "demo":
            return (
                f"Satisfying macro hands-on operation shot, pair of elegant hands effortlessly rotating 360 degrees, pressing buttons and demonstrating the {p_name_en}, "
                f"showcasing {key_feature or 'smooth mechanical precision'}, on a {tabletop_env}, soft diffused studio light, pristine product reflections, "
                f"vertical 9:16 framing, ultra-sharp macro lens, 60fps smooth movement, commercial product cinematography, strictly hands only, zero face, 8k, no text, no watermark"
            )
        elif role == "proof":
            return (
                f"Satisfying macro showcase, aesthetic hands gently presenting the finished perfect result with the {p_name_en}, "
                f"resting hands next to the product on a {tabletop_env}, soft golden hour rim light, beautiful creamy depth of field bokeh, "
                f"vertical 9:16 framing, close-up hands only, no face, no torso, 8k photorealistic commercial, no text, no watermark"
            )
        elif role == "cta":
            return (
                f"Commercial close-up finale, aesthetic hands placing the {p_name_en} heroically in center frame, "
                f"one index finger pointing clearly down towards the bottom-left corner of the vertical screen, "
                f"sparkling dust particles bokeh, dynamic studio spotlight, vertical 9:16 framing, strictly hands and product only, "
                f"high conversion e-commerce commercial, 8k resolution, no person, no face, no text, no watermark"
            )
        else:
            return (
                f"Macro commercial advertising shot, aesthetic hands interacting with {p_name_en} on a {tabletop_env}, "
                f"vertical 9:16 framing, studio lighting, 8k resolution, strictly hands only, no face, no text, no watermark"
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
                f"crisp 8k product reflections, metallic and glass highlights, vertical 9:16 framing, luxury TV commercial grade advertising, masterpiece, strictly no people, no text, no watermark"
            )
        elif role == "pain":
            return (
                f"Cinematic mood shot, the {p_name_en} displayed with dramatic high-contrast moody lighting on a textured dark granite pedestal, "
                f"extreme macro detail, dramatic shallow depth of field, showcasing durable robust build, pure product showcase with ZERO HUMANS, "
                f"vertical 9:16 framing, 8k resolution, cinematic commercial color grading, no person, no hands, no text, no watermark"
            )
        elif role == "demo":
            return (
                f"Pristine commercial demonstration, slow orbiting camera gliding smoothly 360 degrees around the {p_name_en}, "
                f"demonstrating {key_feature or 'cutting-edge design and flawless functionality'}, mechanical precision, crystal clear reflections, "
                f"luxury commercial cinematography, {studio_env}, STRICTLY NO PEOPLE, NO HANDS, vertical 9:16 framing, 8k resolution, advertising masterpiece, no text, no watermark"
            )
        elif role == "proof":
            return (
                f"Gleaming luxury advertising beauty shot, the {p_name_en} resting on a premium minimalist white stone podium with soft pastel shadows and ambient light rays, "
                f"glowing halo rim effect, 8k advertising masterpiece, pure clean product cinematography, zero people, zero hands, "
                f"vertical 9:16 framing, photorealistic commercial, no text, no watermark"
            )
        elif role == "cta":
            return (
                f"Explosive commercial finale, the {p_name_en} in centered hero pose on a reflective glossy pedestal, dynamic spotlight flare, "
                f"sparkling particle bokeh, dramatic camera tilt-up, high converting e-commerce commercial banner, "
                f"STRICTLY NO HUMANS, NO HANDS, vertical 9:16 framing, 8k resolution, commercial advertising masterpiece, no text, no watermark"
            )
        else:
            return (
                f"Commercial product advertising shot, {p_name_en} on {studio_env}, vertical 9:16 framing, studio lighting, "
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
                f"vertical 9:16 framing, photorealistic 8k, crisp macro focus, depth of field, {style_tag}, masterpiece, commercial advertising, no text, no watermark"
            )
        elif role == "pain":
            return (
                f"Relatable everyday lifestyle scene, the EXACT SAME {char_dna}, showing relatable funny frustration and distress "
                f"while struggling with everyday annoyance before having {p_name_en}, cinematic moody indoor lighting, medium close-up shot, "
                f"consistent facial identity, same hair and same outfit, vertical 9:16 framing, hyper-realistic 8k, masterpiece, no text, no watermark"
            )
        elif role == "demo":
            return (
                f"Macro product demonstration shot, the hands of the SAME {char_dna} effortlessly operating the {p_name_en}, "
                f"showcasing {key_feature or 'smooth functionality'}, softbox diffused studio lighting, pristine clean reflections, "
                f"vertical 9:16 framing, ultra-sharp detail, 60fps smooth movement, commercial product cinematography, 8k, masterpiece, no text, no watermark"
            )
        elif role == "proof":
            return (
                f"Bright joyful lifestyle result, the EXACT SAME {char_dna}, smiling radiantly with a satisfied nod and giving a confident thumbs up "
                f"next to the {p_name_en}, modern aesthetic background, warm ambient indoor lighting, vertical 9:16 framing, "
                f"identical facial features, same clothing, authentic satisfaction, cinematic color grading, 8k, photorealistic, masterpiece, no text, no watermark"
            )
        elif role == "cta":
            return (
                f"High conversion commercial finale, the EXACT SAME {char_dna}, holding the {p_name_en} with a big winning smile, "
                f"pointing finger excitedly down towards the bottom-left corner of the vertical screen, "
                f"studio spotlight, sparkling particle bokeh, dramatic rim light, vertical 9:16 framing, identical character face and attire, "
                f"high conversion e-commerce aesthetic, 8k resolution, luxury commercial advertising masterpiece, no text, no watermark"
            )
        else:
            return (
                f"Commercial advertising shot, {char_dna} with {p_name_en}, vertical 9:16 framing, studio lighting, "
                f"8k resolution, cinematic color grading, photorealistic masterpiece, no text, no watermark"
            )


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
    p_sale = req.price_sale.strip() if req.price_sale else "giá siêu hời"
    p_deal = req.promotion_deal.strip() if req.promotion_deal else "Freeship đơn từ 0đ và quà tặng giới hạn"
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
        voice = "vieneu:AdamBua"
        scenes = [
            AffiliateScene(
                index=1,
                title="Cảnh 1: Hook Bựa 3 Giây Chặn Lướt",
                role="hook",
                duration=4,
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
                duration=5,
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
                duration=6,
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
                duration=5,
                prompt=_prompt("proof"),
                voiceover=f"Đã thế hôm nay TikTok Shop trợ giá tụt quần, đang giảm chỉ còn {p_sale}, lại còn {p_deal} nữa chứ!",
                voice=voice,
                badge="SALE SỐC HÔM NAY ⚡",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=5,
                title="Cảnh 5: Chốt Đơn Gấp Trong Giỏ Hàng",
                role="cta",
                duration=5,
                prompt=_prompt("cta"),
                voiceover=f"Nhanh cái tay lẹ cái mắt bấm ngay vào giỏ hàng màu vàng bên góc trái để múc luôn, chậm là người ta hốt sạch đấy nha!",
                voice=voice,
                badge="GIỎ HÀNG GÓC TRÁI 👇",
                reference_image=ref_img
            )
        ]

    # --- KHUNG 2: REVIEW KOC / ĐẬP HỘP CHÂN THỰC ---
    elif framework == "review_koc":
        title = f"📦 Đập Hộp & Test Thực Tế {p_name} — Có Thực Sự Đáng Mua Không?"
        voice = req.voice if req.voice != "vieneu:AdamBua" else "vieneu:TrucLy"
        scenes = [
            AffiliateScene(
                index=1,
                title="Cảnh 1: Mở Đầu Đập Hộp Trực Tiếp",
                role="hook",
                duration=4,
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
                duration=5,
                prompt=_prompt("pain"),
                voiceover=f"Trước đây mình rất ngại mua trên mạng vì sợ hàng đểu mau hỏng, nhưng cầm em này trên tay cảm giác chắc chắn cực kỳ.",
                voice=voice,
                badge="100% CHÍNH HÃNG ✨",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=3,
                title="Cảnh 3: Test Tính Năng Cốt Lõi Tại Chỗ",
                role="demo",
                duration=6,
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
                duration=5,
                prompt=_prompt("proof"),
                voiceover=f"Chấm điểm 9.5 trên 10 luôn nha! Hiện tại đang có giá ưu đãi chỉ {p_sale} và kèm {p_deal}.",
                voice=voice,
                badge="CHẤM ĐIỂM 9.5/10 🌟",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=5,
                title="Cảnh 5: Hướng Dẫn Săn Deal Góc Trái",
                role="cta",
                duration=5,
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
                duration=4,
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
                duration=5,
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
                duration=6,
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
                duration=5,
                prompt=_prompt("proof"),
                voiceover=f"Đúng là đầu tư thông minh nhất tháng này, vừa tiết kiệm công sức mà giá sale hôm nay chỉ còn {p_sale}.",
                voice=voice,
                badge="TIẾT KIỆM 80% THỜI GIAN ⏱️",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=5,
                title="Cảnh 5: Nhanh Tay Nhận Voucher",
                role="cta",
                duration=5,
                prompt=_prompt("cta"),
                voiceover=f"Voucher giảm giá và {p_deal} mình để sẵn ở giỏ hàng góc trái, cả nhà nhanh tay chốt ngay kẻo hết hạn nha!",
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
                duration=4,
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
                duration=5,
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
                duration=6,
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
                duration=5,
                prompt=_prompt("proof"),
                voiceover=f"Hàng ngàn lượt đánh giá 5 sao uy tín, đang có deal hời tụt đỉnh {p_sale} kèm {p_deal}.",
                voice=voice,
                badge="UY TÍN HÀNG ĐẦU 🏆",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=5,
                title="Cảnh 5: Action - Chốt Đơn Lập Tức",
                role="cta",
                duration=5,
                prompt=_prompt("cta"),
                voiceover=f"Số lượng flash sale có hạn, chạm ngay vào giỏ hàng màu vàng góc dưới bên trái để nhận ưu đãi ngay hôm nay!",
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
                duration=4,
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
                duration=5,
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
                duration=6,
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
                duration=5,
                prompt=_prompt("proof"),
                voiceover=f"Hiệu quả rõ rệt ngay lần đầu dùng! Giá hôm nay siêu sốc chỉ {p_sale}, lại còn được {p_deal}.",
                voice=voice,
                badge="GIÁ SỐC HÔM NAY 🏷️",
                reference_image=ref_img
            ),
            AffiliateScene(
                index=5,
                title="Cảnh 5: Kêu Gọi Mua Hàng Góc Trái",
                role="cta",
                duration=5,
                prompt=_prompt("cta"),
                voiceover=f"Bấm ngay vào giỏ hàng màu vàng ở góc trái màn hình để săn deal hời trước khi hết mã freeship nhé!",
                voice=voice,
                badge="GIỎ HÀNG GÓC TRÁI 👇",
                reference_image=ref_img
            )
        ]

    return AffiliateScriptResponse(
        title=title,
        framework=framework,
        target_platform=req.target_platform,
        aspect_ratio=req.aspect_ratio,
        recommended_voice=voice,
        hook_strength="99/100",
        marketing_notes=notes,
        scenes=scenes
    )


# ── 5. AI Script Generator (Gọi Muse2API hoặc LLM có Fallback) ───────────────

async def generate_affiliate_script_ai(
    req: ProductAffiliateRequest,
    muse_api_url: str = "http://localhost:18610",
    muse_api_key: str = "muse2api"
) -> AffiliateScriptResponse:
    """
    Tạo kịch bản video tiếp thị liên kết bằng AI LLM thông minh qua muse2api /v1/chat/completions.
    Nếu Muse bận render hoặc không phản hồi, tự động fallback sang bộ tạo cục bộ siêu tốc.
    """
    sys_instruction = (
        "Bạn là Chuyên gia Marketing Tiếp thị Liên kết hàng đầu (TikTok Shop & Shopee Top Creator).\n"
        "Nhiệm vụ: Viết kịch bản video ngắn (15s-30s) dọc 9:16 bán hàng bùng nổ chuyển đổi theo đúng định dạng JSON.\n"
        "Quy tắc bắt buộc:\n"
        "1. Phải có đúng 5 cảnh: Scene 1 (Hook 3s), Scene 2 (Pain point/Vấn đề), Scene 3 (Demo sản phẩm/Giải pháp), Scene 4 (Proof/Deal hời), Scene 5 (CTA giỏ hàng góc trái).\n"
        "2. Prompt cho mỗi cảnh phải là TIẾNG ANH thương mại điện ảnh cực kỳ chi tiết, tỷ lệ vertical 9:16, studio lighting, 8k, photorealistic, no text, no watermark.\n"
        "3. Voiceover phải là tiếng Việt tự nhiên, có dấu chấm câu ngắt nghỉ rõ ràng để máy đọc TTS chuẩn xác.\n"
        "4. Trả về DUY NHẤT một khối JSON hợp lệ theo schema:\n"
        "{\n"
        '  "title": "Tên kịch bản giật tít",\n'
        '  "scenes": [\n'
        '    {"index": 1, "title": "...", "role": "hook", "duration": 4, "prompt": "...", "voiceover": "...", "badge": "..."},\n'
        "    ...\n"
        "  ]\n"
        "}"
    )

    user_prompt = (
        f"Sản phẩm: {req.product_name}\n"
        f"Ngành hàng: {req.category}\n"
        f"Đối tượng nhắm tới: {req.target_audience}\n"
        f"Vấn đề/Nỗi đau: {req.pain_points}\n"
        f"Tính năng nổi bật: {req.key_features}\n"
        f"Giá khuyến mãi: {req.price_sale}\n"
        f"Ưu đãi đi kèm: {req.promotion_deal}\n"
        f"Công thức kịch bản: {req.framework}\n"
        f"Kiểu quay (shooting_style): {getattr(req, 'shooting_style', 'faceless') or 'faceless'}\n"
        f"Giọng điệu/Tone: {req.tone}"
    )

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"{muse_api_url.rstrip('/')}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {muse_api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "muse-spark",
                    "messages": [
                        {"role": "system", "content": sys_instruction},
                        {"role": "user", "content": user_prompt}
                    ],
                    "temperature": 0.7
                }
            )
            if resp.status_code == 200:
                raw_text = resp.json()["choices"][0]["message"]["content"]
                # Bóc tách JSON từ phản hồi LLM
                json_match = re.search(r'\{[\s\S]*\}', raw_text)
                if json_match:
                    parsed = json.loads(json_match.group(0))
                    scenes_list = []
                    for item in parsed.get("scenes", []):
                        scenes_list.append(AffiliateScene(
                            index=int(item.get("index", len(scenes_list) + 1)),
                            title=item.get("title", f"Cảnh {len(scenes_list) + 1}"),
                            role=item.get("role", "demo"),
                            duration=int(item.get("duration", 5)),
                            prompt=item.get("prompt", build_commercial_prompt("demo", req.product_name, req.category, req.key_features or "")),
                            voiceover=item.get("voiceover", ""),
                            voice=req.voice,
                            badge=item.get("badge", "GIỎ HÀNG GÓC TRÁI 👇"),
                            reference_image=req.reference_image
                        ))
                    if len(scenes_list) >= 3:
                        return AffiliateScriptResponse(
                            title=parsed.get("title", f"🔥 Video Tiếp Thị {req.product_name}"),
                            framework=req.framework,
                            target_platform=req.target_platform,
                            aspect_ratio=req.aspect_ratio,
                            recommended_voice=req.voice,
                            hook_strength="99/100",
                            marketing_notes=["Được tạo bởi AI Muse Chat Model với tối ưu chuyển đổi trực tuyến."],
                            scenes=scenes_list
                        )
    except Exception as e:
        print(f"[Affiliate AI] Muse chat unavailable or returned parse error ({e}), activating local generator fallback...")

    # Fallback ngoại tuyến đảm bảo 100% không bao giờ gián đoạn
    return generate_affiliate_script_local(req)
