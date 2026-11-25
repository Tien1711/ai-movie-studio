"""
batch_video_engine.py – AI Movie Studio
----------------------------------------
Module quản lý & sản xuất "Tạo video hàng loạt" (Batch Video Creator).
Hỗ trợ:
- Nhập danh sách prompt theo dòng, tự động sinh kịch bản phân cảnh (3-12 cảnh).
- Quản lý hàng đợi (Queue), bộ lọc trạng thái (Tất cả, Đang chạy, Nháp, Hoàn tất, Lỗi).
- Kho Mẫu Prompt phân loại theo chủ đề (Chuyện ma, Bài học cuộc sống, Sự thật thú vị, v.v.).
- Tự động sinh lồng tiếng TTS, ghép timeline video, khắc phụ đề chuẩn điện ảnh.
- Gói đăng bài (Social Bundle: Tiêu đề giật tít, Caption, Hashtag TikTok/Reels).
- Chuyển đổi 1-click sang Bàn Phân Cảnh (Shot Desk Studio) để chỉnh sửa sâu.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger("ai_movie_studio.batch_video")

# Thư mục lưu trữ các video hàng loạt (tự động phân giải đường dẫn tương đối theo dự án)
WORKSPACE_DIR = Path(__file__).resolve().parent / "workspace"
BATCH_DIR = WORKSPACE_DIR / "_batch_videos"
BATCH_DIR.mkdir(parents=True, exist_ok=True)
QUEUE_FILE = BATCH_DIR / "batch_queue.json"
QUEUE_BAK_FILE = BATCH_DIR / "batch_queue.json.bak"

_QUEUE_LOCK = threading.RLock()
_CANCELLED_VIDEOS = set()
_CANCELLED_LOCK = threading.Lock()

# ── 1. Thư viện Phong cách Hình ảnh (Visual Styles) ──────────────────────────
BATCH_VISUAL_STYLES: Dict[str, Dict[str, str]] = {
    "anime": {
        "id": "anime",
        "name": "Anime Nhật Bản (Anime)",
        "prompt_suffix": "Makoto Shinkai and Studio Ghibli anime style, vibrant beautiful colors, highly detailed anime scenery, cinematic anime visual, masterpiece, 8k, no text, no watermark",
        "aspect_hint": "anime cinematic"
    },
    "cinematic": {
        "id": "cinematic",
        "name": "Điện ảnh tả thực (Cinematic Realistic)",
        "prompt_suffix": "photorealistic 8k, shot on ARRI Alexa 35, anamorphic lens, dramatic volumetric cinematic lighting, photoreal depth of field, masterpiece, no text, no watermark",
        "aspect_hint": "hollywood realism"
    },
    "3d_pixar": {
        "id": "3d_pixar",
        "name": "3D Hoạt hình Pixar (3D Animation)",
        "prompt_suffix": "Pixar and Disney 3D animation style, adorable expressive characters, cute subsurface scattering, vibrant colorful lighting, octane 3D render, masterpiece, no text, no watermark",
        "aspect_hint": "3d animated"
    },
    "cyberpunk": {
        "id": "cyberpunk",
        "name": "Cyberpunk Viễn tưởng (Sci-Fi Neon)",
        "prompt_suffix": "cyberpunk futuristic neon style, glowing cyan and magenta neon lights, rainy street reflections, high-tech dystopian atmosphere, cinematic sci-fi, 8k, no text, no watermark",
        "aspect_hint": "cyberpunk neon"
    },
    "oil_painting": {
        "id": "oil_painting",
        "name": "Tranh sơn dầu cổ điển (Oil Painting)",
        "prompt_suffix": "classical oil painting style, rich artistic textured brushstrokes, dramatic Rembrandt lighting, vintage museum fine art, masterpiece, 8k, no text, no watermark",
        "aspect_hint": "classic art"
    },
    "donghua": {
        "id": "donghua",
        "name": "3D Tiên hiệp / Huyền huyễn (Donghua)",
        "prompt_suffix": "Chinese donghua 3D animation style, xianxia fantasy world, flowing robes and mystical aura, epic wuxia atmosphere, luminous spiritual energy, highly detailed fantasy scenery, masterpiece, 8k, no text, no watermark",
        "aspect_hint": "donghua fantasy"
    },
    "horror_dark": {
        "id": "horror_dark",
        "name": "Kinh dị & Ma mị (Horror / Dark)",
        "prompt_suffix": "dark horror cinematic style, eerie fog and shadows, ominous pale moonlight, unsettling haunted atmosphere, desaturated cold tones, suspenseful dread, masterpiece, 8k, no text, no watermark",
        "aspect_hint": "dark horror"
    },
    "comic": {
        "id": "comic",
        "name": "Truyện tranh Webtoon (Comic / Manhwa)",
        "prompt_suffix": "Korean webtoon manhwa comic style, bold clean line art, vibrant cel shading, dynamic comic panel composition, expressive characters, masterpiece, 8k, no text, no watermark",
        "aspect_hint": "webtoon comic"
    },
    "ink_wash": {
        "id": "ink_wash",
        "name": "Cổ phong Thủy mặc (Ink Wash)",
        "prompt_suffix": "traditional Chinese ink wash painting style, elegant black ink brushstrokes on rice paper, misty mountains and flowing water, minimalist ancient oriental aesthetic, masterpiece, no text, no watermark",
        "aspect_hint": "ink wash"
    },
    "photoreal": {
        "id": "photoreal",
        "name": "Ảnh chụp đời thực (Photoreal)",
        "prompt_suffix": "ultra realistic photography, shot on DSLR with 50mm lens, natural daylight, true to life colors and skin texture, sharp realistic details, masterpiece, 8k, no text, no watermark",
        "aspect_hint": "photorealistic"
    },
    "watercolor": {
        "id": "watercolor",
        "name": "Màu nước nghệ thuật (Watercolor)",
        "prompt_suffix": "soft watercolor painting style, gentle flowing pigments, dreamy pastel washes, delicate artistic paper texture, light airy atmosphere, masterpiece, no text, no watermark",
        "aspect_hint": "watercolor art"
    },
    "vintage_retro": {
        "id": "vintage_retro",
        "name": "Cổ điển Hoài niệm 90s (Vintage Retro)",
        "prompt_suffix": "90s vintage retro film aesthetic, warm nostalgic colors, subtle 35mm film grain, retro cinema look, emotional mood, masterpiece, no text, no watermark",
        "aspect_hint": "retro 90s"
    }
}

# ── 2. Thư viện Mẫu Prompt (Prompt Templates Catalog) ────────────────────────
BATCH_PROMPT_TEMPLATES: List[Dict[str, Any]] = [
    {
        "category": "chuyen_ma",
        "category_name": "👻 Chuyện Ma & Tâm Linh",
        "description": "Các chủ đề rùng rợn, bí ẩn hút người xem cao nhất về đêm",
        "items": [
            "Chuyện ma ở ngôi nhà cổ Đà Lạt trong đêm mưa bão",
            "Bóng trắng bí ẩn trên chuyến xe buýt số 13 cuối cùng",
            "Tiếng gõ cửa lúc 3 giờ sáng ở căn chung cư cũ",
            "Bí ẩn ngôi làng mất tích sau một đêm sương mù dày đặc",
            "Chiếc gương cổ trong phòng trọ và điều cấm kỵ lúc nửa đêm"
        ]
    },
    {
        "category": "bai_hoc_cuoc_song",
        "category_name": "💡 Bài Học & Triết Lý Sống",
        "description": "Chủ đề truyền cảm hứng, bài học nhân sinh sâu sắc",
        "items": [
            "3 bài học đắt giá từ người cha nghèo thay đổi cuộc đời tôi",
            "Vì sao người càng im lặng lại càng thấu suốt sự đời?",
            "Quy luật 5 giây giúp bạn vượt qua sự trì hoãn vĩnh viễn",
            "Bài học về sự buông bỏ: Khi mất đi cũng là lúc bạn nhận lại",
            "Đừng bao giờ kể 3 bí mật này cho bất kỳ ai, kể cả người thân"
        ]
    },
    {
        "category": "su_that_thu_vi",
        "category_name": "🌍 Sự Thật Kỳ Thú & Khoa Học",
        "description": "Nội dung tò mò, kích thích tương tác và bình luận tranh luận",
        "items": [
            "Điều gì sẽ xảy ra nếu toàn bộ loài người biến mất trong 24 giờ?",
            "5 sự thật kỳ lạ dưới đáy đại dương mà khoa học chưa thể giải mã",
            "Bí mật chưa kể bên trong Đại Kim Tự Tháp Giza cổ đại",
            "Sẽ ra sao nếu Trái Đất bất ngờ ngừng quay trong 1 giây?",
            "3 hiện tượng thiên văn kỳ bí sắp diễn ra khiến giới khoa học sửng sốt"
        ]
    },
    {
        "category": "lich_su_viet_nam",
        "category_name": "⚔️ Lịch Sử & Nhân Vật Hào Hùng",
        "description": "Hào khí dân tộc, truyền thống anh hùng và các trận đánh huyền thoại",
        "items": [
            "Chiến dịch Điện Biên Phủ — 56 ngày đêm chấn động địa cầu",
            "Huyền thoại Trần Hưng Đạo và 3 lần đại thắng giặc Nguyên Mông",
            "Bí ẩn chiến thuật cọc ngầm sông Bạch Đằng của Ngô Quyền",
            "Nữ tướng Hai Bà Trưng và cuộc khởi nghĩa vang dội ngàn thu",
            "Vị vua áo vải Quang Trung và cuộc hành quân thần tốc đánh tan 29 vạn quân Thanh"
        ]
    },
    {
        "category": "hai_huoc_troll",
        "category_name": "🤣 Hài Hước & Đời Sống",
        "description": "Châm biếm vui vẻ, bắt trend hài bựa TikTok",
        "items": [
            "Những pha xử lý tình huống đi vào lòng đất của sinh viên mới ra trường",
            "Khi người hướng nội bị bạn bè rủ đi tiệc tùng đông đúc",
            "Một ngày xui xẻo tột đỉnh của anh chàng văn phòng thứ hai đầu tuần",
            "Cách tồn tại qua bữa cơm họp mặt gia đình khi bị hỏi chuyện lấy vợ",
            "Những tình huống dở khóc dở cười khi lần đầu ra mắt nhà người yêu"
        ]
    }
]

# ── 3. Kho Nhịp Kịch Bản Chi Tiết (Narrative Beat Pools) ────────────────────
# Mỗi thể loại có 10 nhịp kịch bản tiến triển tự nhiên, đa dạng về bối cảnh,
# góc máy và cảm xúc để đảm bảo không cảnh nào bị trùng lặp thoại hay hình.
NARRATIVE_BEAT_POOLS: Dict[str, List[Tuple[str, str]]] = {
    "horror": [
        (
            "Càng tiến sâu vào bên trong, bầu không khí càng trở nên đặc quánh, lạnh lẽo đến thấu xương như có ai đó đang dõi theo từng cử chỉ.",
            "Suspenseful cinematic tracking shot, creeping shadows, haunting details emerging from the background, intense mysterious atmosphere"
        ),
        (
            "Một tiếng động lạ bất ngờ vang lên từ phía căn phòng cuối dãy, phá tan sự im lặng ma mị bao trùm suốt thời gian qua.",
            "Tense cinematic medium shot, weathered antique wooden door slightly ajar, ominous dark silhouette inside, flickering candle light, dramatic shadows"
        ),
        (
            "Khi ánh đèn pin rọi vào bức tường loang lổ, những vết tích kỳ bí xưa kia dần lộ rõ, kể lại một quá khứ đầy u uất và bi thương.",
            "Detailed close-up cinematic shot, beam of flashlight illuminating ancient cryptic symbols etched onto dusty decayed walls, high suspense"
        ),
        (
            "Cơn gió lạnh buốt thốc mạnh qua khung cửa sổ vỡ vụn, khiến chiếc bóng trên sàn nhà dường như đang tự chuyển động một cách dị thường.",
            "Atmospheric wide shot, stormy night, wind billowing ragged curtains through a shattered antique window, eerie moonlight casting surreal shadows"
        ),
        (
            "Đỉnh điểm của sự kinh hoàng ập đến khi chiếc gương cổ phản chiếu một hình bóng lạ, hoàn toàn không phải là của người đang đứng trước gương.",
            "Shocking cinematic climax shot, cracked vintage mirror reflecting an ominous ethereal face with glowing eerie eyes, high tension, cinematic horror visual"
        ),
        (
            "Nhịp tim đập loạn xạ, từng bước chân vội vã tháo chạy trong bóng tối mù mịt như muốn thoát khỏi bàn tay vô hình của số phận.",
            "Dynamic fast-paced cinematic tracking shot, fleeing through misty corridor, heavy shadows closing in, heart-pounding suspense"
        ),
        (
            "Khi cánh cửa gỗ nặng nề sập lại phía sau, cảm giác lạnh lẽo vẫn đeo bám không buông, như một lời nhắc nhở rằng bí ẩn vẫn còn nguyên vẹn.",
            "Dramatic low-angle cinematic shot of a massive locked vintage wooden door, dense fog seeping beneath the threshold, haunting moonlight"
        ),
        (
            "Thời gian trôi qua, nhưng những câu hỏi không lời đáp về nơi này vẫn tiếp tục ám ảnh tâm trí của bất cứ ai từng đặt chân tới.",
            "Cinematic moody silhouette standing outside the mysterious mansion under a cold starry sky, deep atmospheric dread, 8k cinematic"
        ),
        (
            "Người ta đồn rằng, vào những đêm trăng khuyết, tiếng thở than ai oán vẫn còn văng vẳng bên hiên nhà đổ nát.",
            "Eerie wide shot of the ruined mansion under a crescent moon, wisps of supernatural mist, hauntingly beautiful Gothic aesthetic"
        ),
        (
            "Liệu đó chỉ là sự tưởng tượng của con người, hay thực sự tồn tại một thế giới huyền bí đang song hành cùng chúng ta?",
            "Thoughtful cinematic composition, glowing antique lantern resting on wet cobblestones, mysterious mist fading into night"
        )
    ],
    "lesson": [
        (
            "Người thông tuệ hiểu rằng nghịch cảnh không sinh ra để đánh gục chúng ta, mà để tôi luyện ý chí và bộc lộ bản lĩnh tiềm ẩn.",
            "Cinematic reflective medium shot, thoughtful person looking out over a stormy sea from a cliff, resolute calm expression, golden backlight"
        ),
        (
            "Khi đứng trước những lựa chọn mang tính bước ngoặt, lòng kiên định với các giá trị cốt lõi chính là chiếc la bàn soi đường chuẩn xác nhất.",
            "Artistic cinematic visual of an antique brass compass lying on weathered parchment map, warm sunlight illuminating the needle pointing true north"
        ),
        (
            "Đừng so sánh sự khởi đầu của mình với vinh quang của người khác; mỗi bông hoa đều có một thời điểm riêng để bung nở rực rỡ nhất.",
            "Poetic cinematic macro shot of a delicate bud slowly blooming through cracked pavement towards golden sunbeams, metaphor of resilience"
        ),
        (
            "Giá trị thực sự của con người không đo bằng những gì họ sở hữu, mà bằng những phẩm chất họ gìn giữ khi đối diện với thử thách gian nan.",
            "Inspiring medium shot of an honest mentor conversing with a student, warm ambient library lighting, genuine emotional depth"
        ),
        (
            "Biết buông bỏ những điều không thuộc về mình cũng là một dạng trí tuệ, mở ra khoảng trống để đón nhận những duyên lành mới.",
            "Peaceful cinematic wide shot, person releasing autumn leaves into a flowing crystal-clear stream, soft sunset colors, serene atmosphere"
        ),
        (
            "Mỗi vết sẹo trên hành trình trưởng thành đều là một minh chứng kiêu hãnh cho sự kiên cường và lòng dũng cảm vượt qua giông bão.",
            "Cinematic hero shot, determined character looking toward the morning sun, warm golden hour illumination, uplifting triumphant mood"
        ),
        (
            "Lòng biết ơn chính là chiếc chìa khóa vạn năng, biến những điều bình dị thường ngày trở thành nguồn hạnh phúc vô tận.",
            "Warm cinematic scene of a family or friends gathered around a modest table smiling with gratitude, gentle warm lighting, cozy aesthetic"
        ),
        (
            "Khi bạn dốc hết tâm sức giúp đỡ người khác tỏa sáng, bạn cũng đang tự thắp lên ngọn đèn soi sáng cho chính cuộc đời mình.",
            "Touching cinematic visual of hands passing a glowing lantern forward in the dusk, symbolizing shared wisdom and warmth"
        ),
        (
            "Thành quả bền vững nhất luôn được tạo nên từ những bước đi kỷ luật và sự kiên trì âm thầm mỗi ngày mà không cần phô trương.",
            "Cinematic visual of a craftsperson meticulously carving intricate artwork in a quiet workshop, dust motes dancing in sunbeams"
        ),
        (
            "Hãy luôn giữ cho tâm mình sự thuần khiết và bình an, bởi đó chính là sức mạnh vô địch giúp bạn vượt qua mọi thăng trầm.",
            "Tranquil panoramic landscape of a calm zen lotus pond at sunrise, gentle ripples, golden morning mist, deep peace and clarity"
        )
    ],
    "history": [
        (
            "Khắp các nẻo đường, hào khí non sông sục sôi như dòng thác lũ, khi toàn thể quân dân một lòng hướng về tiền tuyến quyết chiến.",
            "Epic cinematic wide shot, vast army banners fluttering in the wind, ancient city walls, resolute warriors marching forward, golden dust"
        ),
        (
            "Những cuộc họp bàn cơ mật nơi quân doanh thâu đêm suốt sáng, nơi trí tuệ của các bậc danh tướng được đúc kết thành mưu lược tuyệt vời.",
            "Dramatic interior military council tent, strategists gathered around ancient hand-drawn battle map illuminated by flickering oil lamps"
        ),
        (
            "Từng trận địa chông cọc và phòng tuyến hiểm trở đã được xây dựng khéo léo, biến địa hình non nước thành thế trận bủa vây quân thù.",
            "Atmospheric landscape shot of historic strategic river estuary, wooden defense stakes rising from misty tidal waters at twilight"
        ),
        (
            "Khi tiếng tù và khai trận vang lên rúng động núi rừng, các cánh quân dũng mãnh đồng loạt xung phong làm rung chuyển cả đất trời.",
            "High adrenaline cinematic battle visual, charging cavalry through rolling mist, golden sunset backlighting, heroic momentum"
        ),
        (
            "Trận chiến quyết định diễn ra vô cùng khốc liệt, nơi ý chí sắt đá của dân tộc đã đè bẹp hoàn toàn mưu toan xâm lược của kẻ địch.",
            "Intense dramatic clash of arms, smoke billows across the valley, resolute flag-bearer holding national colors high, epic cinematic framing"
        ),
        (
            "Trước sức mạnh chính nghĩa và lòng dũng cảm phi thường, đội quân xâm lược hoảng loạn tan vỡ, tháo chạy trong nỗi khiếp đảm tột cùng.",
            "Cinematic wide angle showing retreating broken forces in distant smoke, victorious banners waving proudly on the ridge"
        ),
        (
            "Khúc khải hoàn vang vọng giữa đất trời sông núi, non sông thu về một mối trong niềm hân hoan rạng rỡ của muôn vạn đồng bào.",
            "Triumphant celebration in ancient historic capital, golden flower petals falling, radiant sunlight breaking through clouds, joyous citizens"
        ),
        (
            "Công lao to lớn của các vị anh hùng dân tộc đã được tạc vào bia đá, sống mãi cùng nghìn năm thịnh trị của đất nước Đại Việt.",
            "Majestic stone monument and ancient stele in sacred temple courtyard, dignified morning light, incense smoke drifting peacefully"
        ),
        (
            "Những bài học quân sự và tinh thần đoàn kết ấy vẫn mãi là kim chỉ nam vô giá, soi sáng cho các thế hệ con cháu mai sau.",
            "Cinematic wide shot of historic river at golden hour, serene waters reflecting towering green karsts, timeless pride of the nation"
        ),
        (
            "Mỗi tấc đất thiêng liêng hôm nay đều thấm đượm mồ hôi và xương máu của cha ông, nhắc nhở chúng ta sống xứng đáng với lịch sử oai hùng.",
            "Breathtaking aerial view of Vietnam countryside, terraced green fields and majestic mountains under brilliant sunbeams"
        )
    ],
    "science": [
        (
            "Bằng việc sử dụng các thiết bị quan sát tối tân, các nhà nghiên cứu đã lần đầu tiên ghi lại được những dữ liệu chấn động này.",
            "Futuristic observatory telescope dome opening toward a radiant galaxy, high-tech monitors streaming real-time cosmic data"
        ),
        (
            "Ở cấp độ nguyên tử, các hạt chuyển động với vận tốc kinh ngạc, tạo nên những cấu trúc hình học cân đối một cách hoàn mỹ.",
            "Breathtaking 3D macro simulation of spinning glowing atoms and energy orbital rings, deep navy blue and vibrant cyan particles"
        ),
        (
            "Điều kỳ diệu là hiện tượng này không chỉ xảy ra trên Trái Đất, mà còn lặp lại chuẩn xác ở nhiều góc xa xôi khác trong dải ngân hà.",
            "Expansive cosmic panorama featuring distant colorful nebulae, glowing celestial bodies, awe-inspiring depth of space"
        ),
        (
            "Các phép tính mô phỏng trên siêu máy tính đã chứng minh rằng một thay đổi siêu nhỏ cũng có thể kích hoạt chuỗi phản ứng dây chuyền khổng lồ.",
            "Dynamic holographic quantum grid pulsing with golden energy waves, cutting-edge futuristic laboratory atmosphere"
        ),
        (
            "Khám phá mang tính cách mạng này đã lật đổ những định kiến kéo dài hàng thế kỷ, mở toang cánh cửa dẫn vào tương lai mới của nhân loại.",
            "Dramatic shot of diverse visionary scientists celebrating a breakthrough, glowing blue laser hologram illuminating their faces with joy"
        ),
        (
            "Trong lòng đại dương sâu thẳm, các sinh vật kỳ lạ sở hữu khả năng phát quang sinh học độc đáo mà công nghệ con người vẫn chưa thể sao chép hoàn toàn.",
            "Mesmerizing deep sea shot of glowing translucent bioluminescent jellyfish and underwater coral reefs in deep midnight ocean"
        ),
        (
            "Sự liên kết mật thiết giữa tự nhiên và vũ trụ cho thấy hành tinh của chúng ta là một chỉnh thể sống động và kỳ diệu vô ngần.",
            "Breathtaking orbital view of Earth at twilight, city night lights sparkling like jewels while auroras dance across the atmosphere"
        ),
        (
            "Những ứng dụng thực tiễn của hiện tượng này đang mở đường cho nguồn năng lượng sạch vô tận phục vụ cho các thế hệ mai sau.",
            "Visionary concept of a utopian eco-friendly metropolis powered by glowing clean fusion reactors and lush vertical gardens"
        ),
        (
            "Mỗi câu hỏi được giải đáp lại mở ra hàng vạn bí ẩn mới, thôi thúc trí tò mò và khát vọng thám hiểm bất tận của loài người.",
            "An astronaut standing on the precipice of an alien planet looking toward a colossal ringed planet rising in the pastel sky"
        ),
        (
            "Khoa học không chỉ là những con số khô khan, mà chính là ngôn ngữ diệu kỳ mà vũ trụ dùng để trò chuyện cùng chúng ta.",
            "Artistic visual of human hand reaching toward a constellation of glowing stardust, poetic harmony of mind and cosmos"
        )
    ],
    "funny": [
        (
            "Mọi chuyện bắt đầu vô cùng êm đẹp cho tới khi nhân vật chính quyết định thể hiện kỹ năng thượng thừa trước mặt bàn dân thiên hạ.",
            "Comical anime protagonist smirking with supreme overconfidence, sparkles in eyes, dramatic comic background effects"
        ),
        (
            "Chỉ một tích tắc sơ sẩy, kịch bản trong mơ bỗng biến thành hiện thực không lối thoát với một cú trượt chân nhớ đời.",
            "Hilarious mid-action freeze frame of comical stumble, exaggerated physics, playful anime slapstick visual, bright colors"
        ),
        (
            "Những ánh mắt ngơ ngác của mọi người xung quanh như đang cùng đặt ra một câu hỏi: Chuyện gì vừa mới xảy ra vậy?",
            "Funny group shot of bystanders with blank stunned expressions, comically wide eyes, awkward silence anime humor"
        ),
        (
            "Nhưng đỉnh cao chưa dừng lại ở đó; nỗ lực cứu vãn tình thế của anh chàng lại vô tình kích hoạt thêm một chuỗi thảm họa mới.",
            "Domino-effect comedic chaos scene, objects tumbling playfully, comical anime panicked expression with motion speedlines"
        ),
        (
            "Gương mặt giả vờ bình tĩnh nhưng trong lòng đang khóc thét, chỉ ước gì có một cái hố để chui xuống ngay lúc này.",
            "Extreme close-up of a hilariously forced polite smile while nervous sweat drips down, bright comedy anime style"
        ),
        (
            "Cả khán phòng bỗng chốc vỡ òa trong tràng cười nghiêng ngả, biến sự cố hy hữu thành kỷ niệm hài hước không thể nào quên.",
            "Joyful colorful scene of friends rolling on the floor laughing heartily, warm cheerful lighting, lively cartoon aesthetic"
        ),
        (
            "Sau tất cả, bài học sâu sắc rút ra là: đừng bao giờ gáy sớm nếu bạn không muốn trở thành meme của cả nhóm suốt tuần sau.",
            "Playful shot of the character holding a mock trophy of clumsiness, wearing sunglasses indoors, funny self-deprecating triumph"
        ),
        (
            "Những khoảnh khắc ngáo ngơ ngộ nghĩnh này chính là gia vị ngọt ngào giúp cuộc sống mỗi ngày thêm tràn ngập tiếng cười.",
            "Whimsical uplifting anime montage of cheerful friends sharing funny memories over milk tea, bright sunny aesthetic"
        ),
        (
            "Ai trong chúng ta cũng từng có những pha xử lý cồng kềnh như thế, nhưng quan trọng nhất là vẫn phải cười thật tươi đón nhận.",
            "Charming cute cartoon character giving two thumbs up with an endearing goofy smile, vibrant pastel palette"
        ),
        (
            "Nếu bạn thấy hình bóng của chính mình hoặc đứa bạn thân trong câu chuyện này, thì đừng ngần ngại chia sẻ ngay nhé!",
            "Playful closing visual of cute cartoon avatars winking and pointing forward, festive confetti and fun vibrant vibes"
        )
    ],
    "generic": [
        (
            "Từ những chi tiết tưởng chừng nhỏ bé ban đầu, câu chuyện dần hé lộ một bức tranh toàn cảnh vô cùng hấp dẫn và cuốn hút.",
            "Engaging cinematic establishing wide shot, vibrant layered scenery, dynamic lighting setting an intriguing narrative tone"
        ),
        (
            "Từng diễn biến tiếp theo liên tục đưa người xem đi từ bất ngờ này sang bất ngờ khác với nhịp độ cuốn hút không thể rời mắt.",
            "Dynamic cinematic tracking shot focusing on meaningful storytelling elements, rich colors and expressive visual depth"
        ),
        (
            "Sự kết hợp tinh tế giữa bối cảnh và cảm xúc nhân vật tạo nên một trải nghiệm nghệ thuật chân thực và đầy lay động.",
            "Cinematic medium shot with soft atmospheric lighting, artistic character composition, high emotional resonance"
        ),
        (
            "Điểm nhấn độc đáo xuất hiện đúng vào thời điểm ít ai ngờ tới nhất, mang đến một góc nhìn hoàn toàn mới mẻ và sâu sắc.",
            "Dramatic visual highlight, captivating play of light and shadow illuminating a pivotal story element, 8k cinematic"
        ),
        (
            "Khoảnh khắc thăng hoa đưa thông điệp chính lan tỏa mạnh mẽ, chạm đến trái tim và khơi gợi sự đồng cảm nơi người xem.",
            "Uplifting triumphant visual scene, golden sunbeams casting radiant glows across the landscape, masterpiece quality"
        ),
        (
            "Mọi chi tiết được kết nối một cách hoàn hảo, khép lại hành trình đầy cảm xúc nhưng vẫn mở ra những suy ngẫm khó quên.",
            "Harmonious wide composition, scenic horizon bathed in soft sunset tones, peaceful and deeply satisfying visual resolution"
        ),
        (
            "Những giá trị tinh hoa được đúc kết sẽ tiếp tục truyền cảm hứng và mang lại nguồn năng lượng tích cực cho cuộc sống.",
            "Inspiring cinematic landscape with radiant golden lighting, symbol of renewal, hope and positive inspiration"
        ),
        (
            "Mỗi lần nhìn lại, chúng ta lại phát hiện thêm những tầng ý nghĩa mới mẻ được gửi gắm khéo léo trong từng thước phim.",
            "Artistic cinematic composition with warm depth of field, poetic visual metaphor, elegant lighting and aesthetic mastery"
        ),
        (
            "Một hành trình trọn vẹn không chỉ đem lại niềm vui tức thì, mà còn lưu lại những dư ba ngọt ngào trong tâm hồn.",
            "Serene twilight view with gentle ambient glowing lanterns, tranquil reflection on still water, calm emotional depth"
        ),
        (
            "Đó chính là sức mạnh kỳ diệu của nghệ thuật kể chuyện — nơi mọi giới hạn được xóa nhòa để cảm xúc được tự do thăng hoa.",
            "Stunning artistic cinematic shot with starry cosmic sky blending into a magnificent landscape, awe-inspiring beauty"
        )
    ]
}



def _validate_image_file(p: Path) -> bool:
    """Kiểm tra file ảnh tồn tại, kích thước hợp lệ, đúng header magic bytes và giải mã thành công bằng PIL."""
    if not p.exists() or p.stat().st_size < 100:
        return False
    try:
        with open(p, "rb") as f:
            header = f.read(32)
        valid_magic = (
            (header.startswith(b"RIFF") and b"WEBP" in header[:16]) or
            header.startswith(b"\x89PNG\r\n\x1a\n") or
            header.startswith(b"\xff\xd8")
        )
        if not valid_magic:
            return False
        # Xác thực file có thể giải mã trọn vẹn, không bị cắt cụt hoặc hỏng dữ liệu
        try:
            from PIL import Image
            with Image.open(p) as img:
                img.verify()
        except Exception:
            return False
        return True
    except Exception:
        return False


def get_character_from_library(char_id: str) -> Optional[Dict[str, Any]]:
    """Tải hồ sơ nhân vật cụ thể từ thư viện _characters.json nếu tồn tại."""
    if not char_id:
        return None
    chars_file = WORKSPACE_DIR / "_characters.json"
    if not chars_file.exists():
        return None
    try:
        chars = json.loads(chars_file.read_text(encoding="utf-8"))
        for c in chars:
            if c.get("id") == char_id:
                char_desc = c.get("description") or c.get("prompt") or c.get("visual_summary") or c.get("name")
                return {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "prompt": char_desc,
                    "avatar": c.get("avatar")
                }
    except Exception as e:
        logger.warning("Lỗi đọc nhân vật từ thư viện: %s", e)
    return None

# ── 4. Bộ Tạo Kịch Bản Tự Động Theo Dòng Prompt ──────────────────────────────
def generate_batch_script_local(
    raw_prompt: str,
    scenes_count: int = 8,
    visual_style: str = "anime",
    aspect_ratio: str = "9:16",
    mode: str = "free",
    character_id: Optional[str] = None
) -> Dict[str, Any]:
    """Sinh kịch bản chi tiết gồm N cảnh dựa trên một dòng ý tưởng (Prompt).
    Mỗi cảnh có:
    - visual prompt: mô tả góc máy, hành động, ánh sáng, gắn hậu tố phong cách
    - voiceover: lời thuyết minh tiếng Việt tự nhiên, truyền cảm, tiến triển xuyên suốt không lặp lại
    - duration: thời lượng hợp lý (5-8 giây)
    - role: hook, buildup, climax, ending...
    - social bundle: title, caption, hashtags
    """
    clean_title = raw_prompt.strip()
    if not clean_title:
        clean_title = "Video ngắn không tiêu đề"

    scenes_count = max(3, min(12, int(scenes_count)))
    style_info = BATCH_VISUAL_STYLES.get(visual_style, BATCH_VISUAL_STYLES["anime"])
    style_suffix = style_info["prompt_suffix"]
    ratio_prompt = "9:16 vertical smartphone format" if aspect_ratio == "9:16" else ("16:9 widescreen format" if aspect_ratio == "16:9" else "1:1 square format")

    # Phân loại đề tài để sinh cấu trúc lời thoại phù hợp
    lower_title = clean_title.lower()
    is_horror = any(k in lower_title for k in ("ma", "rùng rợn", "bí ẩn", "đêm", "chết", "kinh dị", "quỷ", "u ám"))
    is_lesson = any(k in lower_title for k in ("bài học", "cuộc đời", "giàu", "nghèo", "thành công", "triết lý", "cha", "mẹ", "im lặng", "thói quen"))
    is_history = any(k in lower_title for k in ("lịch sử", "chiến", "huyền thoại", "vua", "tướng", "đại thắng", "điện biên", "nguyên mông", "trần hưng đạo"))
    is_science = any(k in lower_title for k in ("sự thật", "khoa học", "trái đất", "vũ trụ", "đại dương", "biến mất", "bí mật", "tại sao"))
    is_funny = any(k in lower_title for k in ("hài", "vui", "cười", "troll", "mắc cười", "bá đạo", "xui xẻo"))

    if is_horror:
        cat_key = "horror"
    elif is_lesson:
        cat_key = "lesson"
    elif is_history:
        cat_key = "history"
    elif is_science:
        cat_key = "science"
    elif is_funny:
        cat_key = "funny"
    else:
        cat_key = "generic"

    # Định danh nhân vật trung tâm: ưu tiên nhân vật từ thư viện người dùng, sau đó fallback theo archetype (Point 7)
    char_info = None
    char_prompt_prefix = ""
    if mode == "character":
        if character_id:
            lib_char = get_character_from_library(character_id)
            if lib_char:
                char_info = lib_char
                char_prompt_prefix = f"featuring {lib_char['prompt']}, "

        if not char_info:
            if cat_key == "history":
                char_name = "Tướng Quân Trẻ Tuổi"
                char_desc = "central protagonist brave young Vietnamese general, handsome resolute face, sharp dark eyes, traditional black hair in bun, wearing dark lacquer ceremonial armor with red silk sash, consistent facial features across all scenes"
            elif cat_key == "horror":
                char_name = "Nhà Thám Hiểm Trẻ"
                char_desc = "central protagonist curious young investigator in worn charcoal trench coat, observant amber eyes, dark hair, leather satchel, highly consistent recognizable facial structure and attire"
            elif cat_key == "lesson":
                char_name = "Chàng Trai Trầm Lặng"
                char_desc = "central protagonist thoughtful modern young adult in navy knitted sweater, gentle expressive brown eyes, neat black hair, highly consistent character appearance across scenes"
            elif cat_key == "science":
                char_name = "Nhà Khoa Học Tương Lai"
                char_desc = "central protagonist passionate astrophysicist researcher in sleek white and grey flight suit, futuristic luminous visor, consistent facial features and uniform across all shots"
            elif cat_key == "funny":
                char_name = "Cậu Bạn Nhí Nhảnh"
                char_desc = "central protagonist comical expressive character wearing oversized mustard yellow hoodie, funny expressive lively face, messy tousled hair, consistent animated character design"
            else:
                char_name = "Nhân Vật Chính"
                char_desc = "central protagonist attractive young lead with striking confident gaze, styled dark hair, wearing signature sapphire-blue jacket, highly consistent facial identity"
            char_info = {"name": char_name, "prompt": char_desc}
            char_prompt_prefix = f"featuring {char_desc}, "

    # Trích xuất từ khóa chủ đề cốt lõi từ prompt để lồng ghép tự nhiên vào diễn biến kịch bản (Point 8)
    topic_clean = re.sub(r'^(chuyện ma|bài học|lịch sử|sự thật|bí mật|huyền thoại|tại sao)\s*(ở|về|từ|trong|của)?\s*', '', clean_title, flags=re.IGNORECASE).strip()
    if not topic_clean:
        topic_clean = clean_title
    scenes: List[Dict[str, Any]] = []

    # Cảnh 1 luôn là HOOK giật tít giữ chân người xem
    if is_horror:
        hook_vo = f"Có những câu chuyện rùng rợn mà người ta chỉ dám thì thầm khi màn đêm buông xuống: {clean_title}."
        hook_prompt = f"Cinematic atmospheric opening shot, mysterious dark environment, ominous moody shadows, eerie glowing light, suspenseful tension, {ratio_prompt}, {style_suffix}"
    elif is_lesson:
        hook_vo = f"Nếu bạn đang cảm thấy bế tắc hay mất phương hướng, hãy lắng nghe câu chuyện về: {clean_title}."
        hook_prompt = f"Cinematic emotional opening shot, thoughtful silhouette sitting in deep contemplation, gentle ray of light breaking through darkness, {ratio_prompt}, {style_suffix}"
    elif is_history:
        hook_vo = f"Trang sử hào hùng được viết bằng máu và lòng kiên cường bất khuất: {clean_title}."
        hook_prompt = f"Epic grand cinematic opening shot, majestic historical landscape, dramatic stormy sky with golden sunlight breaking through clouds, {ratio_prompt}, {style_suffix}"
    elif is_science:
        hook_vo = f"Bạn có bao giờ tự hỏi điều không tưởng này thực sự diễn ra thì sẽ thế nào: {clean_title}?"
        hook_prompt = f"Stunning awe-inspiring cinematic visual of cosmic phenomena and earth mysteries, mind-bending scientific perspective, {ratio_prompt}, {style_suffix}"
    elif is_funny:
        hook_vo = f"Chuẩn bị tinh thần để cười nghiêng ngả với câu chuyện khó đỡ này nhé: {clean_title}!"
        hook_prompt = f"Lively energetic comedy opening shot, comical expressive protagonist, colorful bright setting, playful cinematic atmosphere, {ratio_prompt}, {style_suffix}"
    else:
        hook_vo = f"Chào mừng bạn đến với câu chuyện đặc biệt ngày hôm nay: {clean_title}."
        hook_prompt = f"Engaging cinematic establishing shot, dynamic visual composition, intriguing central subject, {ratio_prompt}, {style_suffix}"

    if char_prompt_prefix:
        hook_prompt = f"{char_prompt_prefix}{hook_prompt}"

    scenes.append({
        "index": 1,
        "role": "hook",
        "prompt": hook_prompt,
        "voiceover": hook_vo,
        "duration": 6
    })

    # Các cảnh giữa: lấy từ kho beat pool, trải đều theo số cảnh để đảm bảo TUYỆT ĐỐI KHÔNG TRÙNG LẶP
    mid_count = scenes_count - 2
    pool = NARRATIVE_BEAT_POOLS[cat_key]
    pool_len = len(pool)

    for i in range(mid_count):
        idx = i + 2
        # Tính toán chỉ mục phân bổ đều trong pool 10 beats
        if mid_count <= 1:
            beat_idx = pool_len // 2
        else:
            beat_idx = int(round(i * (pool_len - 1) / (mid_count - 1)))
        beat_idx = max(0, min(pool_len - 1, beat_idx))

        beat_vo, beat_pr_base = pool[beat_idx]
        is_climax = (i >= mid_count - 2) if mid_count >= 3 else (i == mid_count - 1)
        role = "climax" if is_climax else "buildup"

        # Khai triển sâu nội dung chủ đề cụ thể của prompt vào từng phân cảnh để kịch bản xuyên suốt, không chung chung
        lower_beat = (beat_vo[0].lower() + beat_vo[1:]) if beat_vo else ""
        if i == 0:
            scene_vo = f"Bắt đầu hành trình '{clean_title}', {lower_beat}"
        elif is_climax:
            scene_vo = f"Bước ngoặt lớn nhất của câu chuyện '{topic_clean}' xuất hiện: {lower_beat}"
        elif i == 1:
            scene_vo = f"Khi đi sâu vào câu chuyện '{topic_clean}', {lower_beat}"
        elif i == 2:
            scene_vo = f"Từng tình tiết liên quan đến '{topic_clean}' dần hé lộ: {lower_beat}"
        elif i == 3:
            scene_vo = f"Diễn biến xung quanh '{topic_clean}' ngày càng gay cấn: {lower_beat}"
        else:
            scene_vo = f"Tiếp nối diễn biến của '{topic_clean}', {lower_beat}"

        full_pr = f"{char_prompt_prefix}{beat_pr_base}, illustrating key event specifically related to '{clean_title}', {ratio_prompt}, {style_suffix}"

        scenes.append({
            "index": idx,
            "role": role,
            "prompt": full_pr,
            "voiceover": scene_vo,
            "duration": 6
        })

    # Cảnh cuối cùng là KẾT LUẬN & KÊU GỌI (Ending / CTA)
    outro_idx = scenes_count
    if is_horror:
        outro_vo = "Bạn có tin vào những điều kỳ bí này không? Hãy bình luận câu chuyện của bạn và bấm theo dõi để nghe tiếp tập sau!"
    elif is_lesson:
        outro_vo = "Cuộc sống là một hành trình dài, hãy trân trọng từng khoảnh khắc. Bấm theo dõi kênh để cùng lan tỏa những giá trị tích cực nhé!"
    elif is_history:
        outro_vo = "Tự hào dòng máu Lạc Hồng và lịch sử ngàn năm oanh liệt! Đừng quên thả tim và chia sẻ để tiếp nối ngọn lửa hào khí non sông!"
    elif is_science:
        outro_vo = "Khoa học luôn chứa đựng những điều kỳ diệu vượt ngoài sức tưởng tượng. Bấm đăng ký kênh để khám phá thêm nhiều bí ẩn thú vị!"
    elif is_funny:
        outro_vo = "Nếu thấy hài hước thì đừng tiếc một tim và chia sẻ cho đứa bạn thân cùng cười xả stress nhé!"
    else:
        outro_vo = "Cảm ơn bạn đã xem hết video! Hãy thả tim và bấm theo dõi để không bỏ lỡ những nội dung hấp dẫn tiếp theo nhé!"

    outro_prompt = f"Inspiring cinematic closing shot with warm aesthetic lighting, satisfying narrative resolution, uplifting atmosphere, {ratio_prompt}, {style_suffix}"
    if char_prompt_prefix:
        outro_prompt = f"{char_prompt_prefix}{outro_prompt}"

    scenes.append({
        "index": outro_idx,
        "role": "outro",
        "prompt": outro_prompt,
        "voiceover": outro_vo,
        "duration": 6
    })

    # Sinh gói đăng bài mạng xã hội (Social Media Bundle)
    hashtags = ["#fyp", "#xuhuong", "#viral", "#videoai"]
    if is_horror:
        hashtags.extend(["#chuyenma", "#tamlinh", "#kinhdi", "#khampha"])
        viral_title = f"{clean_title} 😱 (Chuyện Thật Rùng Rợn)"
    elif is_lesson:
        hashtags.extend(["#baihoccuocsong", "#trietly", "#thanhcong", "#dongluc"])
        viral_title = f"{clean_title} 💡 (Ngẫm Cực Thấm)"
    elif is_history:
        hashtags.extend(["#lichsuvietnam", "#haohung", "#dantoc", "#dienbienphu"])
        viral_title = f"Huyền Thoại: {clean_title} ⚔️"
    elif is_science:
        hashtags.extend(["#khoahoc", "#khampha", "#suthat", "#kyla"])
        viral_title = f"Bí Mật Chưa Kể: {clean_title} 🌍"
    else:
        hashtags.extend(["#phimngan", "#sangtao", "#giaitri"])
        viral_title = f"{clean_title} ✨"

    caption = (
        f"🎬 Video được sản xuất tự động hoàn toàn bằng AI Movie Studio Hollywood Edition.\n"
        f"Xem ngay để khám phá trọn vẹn câu chuyện nhé!"
    )

    return {
        "title": clean_title,
        "viral_title": viral_title,
        "caption": caption,
        "hashtags": hashtags,
        "scenes_count": scenes_count,
        "aspect_ratio": aspect_ratio,
        "visual_style": visual_style,
        "estimated_duration_s": scenes_count * 6,
        "character": char_info,
        "scenes": scenes
    }


def is_safe_video_id(vid: str) -> bool:
    """Kiểm tra video ID hợp lệ theo chuẩn định dạng và ngăn chặn triệt để path traversal."""
    if not vid or not isinstance(vid, str):
        return False
    if not re.match(r"^bv_[a-zA-Z0-9_\-]+$", vid):
        return False
    try:
        target_dir = (BATCH_DIR / vid).resolve()
        return target_dir.parent == BATCH_DIR.resolve()
    except Exception:
        return False


# ── 4. Quản Lý Hàng Đợi Tạo Video Hàng Loạt (Batch Video Queue Manager) ───────
class BatchVideoManager:
    """Quản trị danh sách hàng đợi các video hàng loạt trên đĩa."""

    def __init__(self):
        self._ensure_storage()
        self.recovered_on_init = self._recover_interrupted_jobs()

    def _ensure_storage(self):
        with _QUEUE_LOCK:
            if not QUEUE_FILE.exists():
                initial_data = {"videos": [], "updated_at": time.time()}
                QUEUE_FILE.write_text(json.dumps(initial_data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _recover_interrupted_jobs(self) -> int:
        """Khôi phục các job đang dở dang 'running' thành 'queued' khi khởi động server (Point 1)."""
        with _QUEUE_LOCK:
            videos = self._read_queue()
            recovered = 0
            for v in videos:
                if v.get("status") == "running":
                    v["status"] = "queued"
                    v["progress"] = 0
                    v["current_step"] = "Chờ chạy lại (Khôi phục sau khởi động server)"
                    v["error"] = None
                    recovered += 1
            if recovered > 0:
                logger.info(f"🔄 Đã tự động khôi phục {recovered} job 'running' dở dang về hàng đợi 'queued'.")
                self._write_queue(videos)
            return recovered

    def _read_queue(self) -> List[Dict[str, Any]]:
        with _QUEUE_LOCK:
            self._ensure_storage()
            # Thử đọc file chính
            try:
                data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
                return data.get("videos", [])
            except Exception as e:
                logger.error("Lỗi đọc file chính batch_queue.json: %s. Thử khôi phục từ .bak...", e)

            # Dự phòng file .bak nếu file chính bị lỗi do mất điện / crash giữa chừng
            if QUEUE_BAK_FILE.exists():
                try:
                    data = json.loads(QUEUE_BAK_FILE.read_text(encoding="utf-8"))
                    logger.info("Đã khôi phục thành công danh sách hàng đợi từ file sao lưu .bak!")
                    return data.get("videos", [])
                except Exception as e_bak:
                    logger.error("Lỗi đọc file dự phòng batch_queue.json.bak: %s", e_bak)

            return []

    def _write_queue(self, videos: List[Dict[str, Any]]):
        """Ghi file hàng đợi nguyên tử (atomic write) thông qua file tạm và sao lưu .bak."""
        with _QUEUE_LOCK:
            payload = {"videos": videos, "updated_at": time.time()}
            content = json.dumps(payload, ensure_ascii=False, indent=2)
            # 1. Ghi ra file tạm trước
            tmp_file = QUEUE_FILE.with_suffix(".tmp")
            tmp_file.write_text(content, encoding="utf-8")
            # 2. Thay thế nguyên tử file chính
            os.replace(tmp_file, QUEUE_FILE)
            # 3. Cập nhật bản sao lưu an toàn .bak
            try:
                QUEUE_BAK_FILE.write_text(content, encoding="utf-8")
            except Exception as e:
                logger.warning("Không thể ghi file sao lưu .bak: %s", e)

    def list_queue(
        self,
        search: str = "",
        status: str = "all",
        sort: str = "newest"
    ) -> Dict[str, Any]:
        """Lấy danh sách hàng đợi kèm bộ lọc & thống kê số lượng."""
        videos = self._read_queue()

        # Thống kê tổng số lượng theo từng trạng thái
        counts = {
            "all": len(videos),
            "running": len([v for v in videos if v.get("status") == "running"]),
            "draft": len([v for v in videos if v.get("status") in ("draft", "queued")]),
            "completed": len([v for v in videos if v.get("status") == "completed"]),
            "failed": len([v for v in videos if v.get("status") == "failed"]),
        }

        # Áp dụng tìm kiếm
        filtered = videos
        if search and search.strip():
            q = search.strip().lower()
            filtered = [
                v for v in filtered
                if q in (v.get("title") or "").lower()
                or q in (v.get("prompt") or "").lower()
                or q in str(v.get("order_num") or "")
            ]

        # Áp dụng bộ lọc trạng thái
        status = status.lower()
        if status == "running":
            filtered = [v for v in filtered if v.get("status") == "running"]
        elif status == "draft":
            filtered = [v for v in filtered if v.get("status") in ("draft", "queued")]
        elif status == "completed":
            filtered = [v for v in filtered if v.get("status") == "completed"]
        elif status == "failed":
            filtered = [v for v in filtered if v.get("status") == "failed"]

        # Sắp xếp
        if sort == "newest":
            filtered.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        elif sort == "oldest":
            filtered.sort(key=lambda x: x.get("created_at", 0))

        return {
            "counts": counts,
            "videos": filtered,
            "total_filtered": len(filtered)
        }

    def add_batch(
        self,
        prompts: List[str],
        aspect_ratio: str = "9:16",
        scenes_count: int = 8,
        visual_style: str = "anime",
        voice: str = "vi-VN-HoaiMyNeural",
        mode: str = "free",
        character_id: Optional[str] = None,
        auto_run: bool = True,
        scripts: Optional[List[Dict[str, Any]]] = None,
        extra: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Nhận danh sách prompt theo dòng, tự tạo các job vào hàng đợi.

        - scripts: kịch bản có sẵn cho từng video (dạng dict như kết quả của
          generate_batch_script_local: title/viral_title/caption/hashtags/
          scenes). Khi có thì dùng scenes cho sẵn, KHÔNG sinh lại script —
          dùng cho Truyện thành video (scenes đã adapt từ chương truyện).
          Video dùng scripts sẽ được gắn cờ script_locked để worker không
          tự làm mới kịch bản.
        - extra: dict gán thêm vào mỗi item (vd: story_source, story_project_id).
        """
        created_items: List[Dict[str, Any]] = []
        now = time.time()

        with _QUEUE_LOCK:
            videos = self._read_queue()
            max_order = max((v.get("order_num", 0) for v in videos), default=0)
            start_num = max_order + 1

            valid_idx = 0
            for idx, raw_line in enumerate(prompts):
                line = raw_line.strip()
                if not line:
                    continue

                vid_id = f"bv_{uuid.uuid4().hex[:8]}"
                v_dir = BATCH_DIR / vid_id
                v_dir.mkdir(parents=True, exist_ok=True)
                (v_dir / "clips").mkdir(exist_ok=True)
                (v_dir / "audio").mkdir(exist_ok=True)

                # Sinh kịch bản phân cảnh ngay cho video này — hoặc dùng
                # kịch bản cho sẵn (Truyện thành video: scenes đã adapt từ chương)
                preset = scripts[idx] if scripts and idx < len(scripts) and scripts[idx] else None
                if preset and preset.get("scenes"):
                    script_data = {
                        "title": preset.get("title") or line,
                        "viral_title": preset.get("viral_title") or preset.get("title") or line,
                        "caption": preset.get("caption") or "",
                        "hashtags": preset.get("hashtags") or [],
                        "scenes": preset.get("scenes"),
                        "character": preset.get("character"),
                    }
                else:
                    script_data = generate_batch_script_local(
                        raw_prompt=line,
                        scenes_count=scenes_count,
                        visual_style=visual_style,
                        aspect_ratio=aspect_ratio,
                        mode=mode,
                        character_id=character_id
                    )

                item = {
                    "id": vid_id,
                    "order_num": start_num + valid_idx,
                    "prompt": line,
                    "title": script_data["title"],
                    "status": "queued" if auto_run else "draft",
                    "progress": 0,
                    "current_step": "Chờ chạy" if auto_run else "Bản nháp",
                    "aspect_ratio": aspect_ratio,
                    "scenes_count": scenes_count,
                    "visual_style": visual_style,
                    "voice": voice,
                    "mode": mode,
                    "character_id": character_id,
                    "character": script_data.get("character"),
                    # Kịch bản khóa: worker không tự làm mới (xem _process_single_video)
                    "script_locked": bool(preset and preset.get("scenes")),
                    "created_at": now + valid_idx * 0.001,
                    "updated_at": now,
                    "scenes": script_data["scenes"],
                    "bundle": {
                        "title": script_data["viral_title"],
                        "caption": script_data["caption"],
                        "hashtags": script_data["hashtags"]
                    },
                    "final_video_url": None,
                    "error": None
                }
                if extra:
                    # Audit #6 (2026-11-25): extra khong duoc ghi de cac
                    # truong he thong (id/status/...) — chi bo sung metadata.
                    _reserved = {"id", "status", "order_num", "progress",
                                 "created_at", "updated_at"}
                    item.update({k: v for k, v in extra.items()
                                 if k not in _reserved})

                # Lưu metadata cục bộ của video an toàn
                try:
                    (v_dir / "project.json").write_text(
                        json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except Exception as e_meta:
                    logger.warning("Không thể lưu file project.json cho %s: %s", vid_id, e_meta)

                videos.append(item)
                created_items.append(item)
                valid_idx += 1

            self._write_queue(videos)

        if auto_run and created_items:
            start_batch_worker()

        return created_items

    def get_video(self, video_id: str) -> Optional[Dict[str, Any]]:
        # Audit #6 (2026-11-25): chan ID khong an toan ngay tu dau (nhu
        # delete_video / _process_single_video da lam).
        if not is_safe_video_id(video_id):
            return None
        videos = self._read_queue()
        for v in videos:
            if v.get("id") == video_id:
                return v
        return None

    def update_video(self, video_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # Audit #6: chan ID khong an toan — neu khong, BATCH_DIR/video_id
        # o duoi co the thoat ra ngoai workspace khi ghi project.json.
        if not is_safe_video_id(video_id):
            logger.warning("Bỏ qua cập nhật video ID không an toàn: %s", video_id)
            return None
        with _QUEUE_LOCK:
            videos = self._read_queue()
            target = None
            for v in videos:
                if v.get("id") == video_id:
                    v.update(updates)
                    v["updated_at"] = time.time()
                    target = v
                    break
            if target:
                self._write_queue(videos)
                v_dir = BATCH_DIR / video_id
                if v_dir.exists():
                    (v_dir / "project.json").write_text(
                        json.dumps(target, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
            return target

    def delete_video(self, video_id: str) -> bool:
        if not is_safe_video_id(video_id):
            logger.warning("Cố gắng xóa video với ID không an toàn: %s", video_id)
            return False

        with _QUEUE_LOCK:
            videos = self._read_queue()
            target = next((v for v in videos if v.get("id") == video_id), None)
            if not target:
                return False

            # Đưa vào tập hợp hủy nếu video đang running hoặc queued
            if target.get("status") in ("running", "queued"):
                with _CANCELLED_LOCK:
                    _CANCELLED_VIDEOS.add(video_id)

            videos = [v for v in videos if v.get("id") != video_id]
            self._write_queue(videos)

            v_dir = (BATCH_DIR / video_id).resolve()
            if v_dir.parent == BATCH_DIR.resolve() and v_dir.exists():
                shutil.rmtree(v_dir, ignore_errors=True)
            return True

    def bulk_run(self, video_ids: List[str]) -> int:
        """Kích hoạt chạy các video được chọn (trạng thái draft hoặc failed)."""
        target_ids = {vid for vid in video_ids if is_safe_video_id(vid)}
        if not target_ids:
            return 0

        count = 0
        with _QUEUE_LOCK:
            videos = self._read_queue()
            for v in videos:
                if v.get("id") in target_ids and v.get("status") in ("draft", "failed"):
                    v["status"] = "queued"
                    v["error"] = None
                    v["progress"] = 0
                    v["current_step"] = "Chờ chạy"
                    count += 1
            if count > 0:
                self._write_queue(videos)

        if count > 0:
            start_batch_worker()
        return count

    def bulk_delete(self, video_ids: List[str]) -> int:
        """Xóa nhiều video được chọn cùng lúc và hủy tác vụ nếu đang chạy (ngăn chặn triệt để path traversal)."""
        safe_ids = {vid for vid in video_ids if is_safe_video_id(vid)}
        if not safe_ids:
            return 0

        matched_items = []
        with _QUEUE_LOCK:
            videos = self._read_queue()
            # Hủy tác vụ cho các video đang running hoặc queued
            active_ids = {v["id"] for v in videos if v.get("id") in safe_ids and v.get("status") in ("running", "queued")}
            if active_ids:
                with _CANCELLED_LOCK:
                    _CANCELLED_VIDEOS.update(active_ids)

            matched_items = [v for v in videos if v.get("id") in safe_ids]
            if matched_items:
                videos = [v for v in videos if v.get("id") not in safe_ids]
                self._write_queue(videos)

        # Xóa thư mục trên đĩa hoàn toàn ngoài lock và chỉ cho các thư mục con an toàn của BATCH_DIR
        for item in matched_items:
            vid = item.get("id")
            if vid and is_safe_video_id(vid):
                v_dir = (BATCH_DIR / vid).resolve()
                if v_dir.parent == BATCH_DIR.resolve() and v_dir.exists():
                    shutil.rmtree(v_dir, ignore_errors=True)

        return len(matched_items)

    def retry_failed(self) -> int:
        """Đưa toàn bộ video bị lỗi về hàng đợi 'queued'."""
        count = 0
        with _QUEUE_LOCK:
            videos = self._read_queue()
            for v in videos:
                if v.get("status") == "failed":
                    v["status"] = "queued"
                    v["error"] = None
                    v["progress"] = 0
                    v["current_step"] = "Chờ chạy lại"
                    count += 1
            if count > 0:
                self._write_queue(videos)

        if count > 0:
            start_batch_worker()
        return count

    def run_all_queued(self) -> int:
        """Kích hoạt tất cả video đang ở trạng thái 'draft' hoặc 'queued'."""
        count = 0
        with _QUEUE_LOCK:
            videos = self._read_queue()
            for v in videos:
                if v.get("status") in ("draft", "queued"):
                    v["status"] = "queued"
                    v["current_step"] = "Đang trong hàng đợi"
                    count += 1
            if count > 0:
                self._write_queue(videos)

        if count > 0:
            start_batch_worker()
        return count

    def run_video(self, video_id: str) -> bool:
        """Đưa 1 video cụ thể về hàng đợi 'queued' và khởi động worker."""
        if not is_safe_video_id(video_id):
            return False

        found = False
        with _QUEUE_LOCK:
            videos = self._read_queue()
            for v in videos:
                if v.get("id") == video_id:
                    v["status"] = "queued"
                    v["error"] = None
                    v["progress"] = 0
                    v["current_step"] = "Chờ chạy"
                    found = True
                    break
            if found:
                self._write_queue(videos)

        if found:
            start_batch_worker()
            return True
        return False

    def convert_to_shot_desk(self, video_id: str) -> Optional[Dict[str, Any]]:
        """Chuyển đổi video hàng loạt sang một dự án Bàn Phân Cảnh (Shot Desk Studio) hoàn chỉnh."""
        v = self.get_video(video_id)
        if not v:
            return None

        from shot_desk import save_shot_desk_project, to_slug

        proj_slug = to_slug(v.get("title") or "video_hang_loat")
        proj_base = f"{proj_slug}_{v['id'].replace('bv_', '')}"
        # Audit #6 (2026-11-25): convert 2 lan cung video khong duoc ghi
        # de project cu (mat prompt/voice/transition/approval nguoi dung
        # da sua) — tao proj_id moi voi hau to tang dan.
        proj_id = proj_base
        _suffix = 2
        while (WORKSPACE_DIR / "_shot_desk_projects" / proj_id / "project.json").exists():
            proj_id = f"{proj_base}_{_suffix}"
            _suffix += 1
        proj_dir = WORKSPACE_DIR / "_shot_desk_projects" / proj_id
        proj_dir.mkdir(parents=True, exist_ok=True)
        (proj_dir / "clips").mkdir(exist_ok=True)
        (proj_dir / "audio").mkdir(exist_ok=True)
        (proj_dir / "shots").mkdir(exist_ok=True)
        (proj_dir / "final").mkdir(exist_ok=True)

        v_dir = BATCH_DIR / video_id
        default_voice = v.get("voice") or "vi-VN-HoaiMyNeural"
        if default_voice in ("ai_voice", "native", "none", "no_dub"):
            default_voice = "vi-VN-HoaiMyNeural"
        aspect_ratio = v.get("aspect_ratio", "9:16")
        shots = []
        for i, s in enumerate(v.get("scenes", [])):
            s_idx = int(s.get("index") or (i + 1))
            src_clip_name = f"clip_{s_idx}.mp4"
            src_audio_name = f"scene_{s_idx}.mp3"
            dst_clip_rel = f"clips/shot_{s_idx:03d}_clip.mp4"
            dst_audio_rel = f"audio/shot_{s_idx:03d}_tts.mp3"
            shot_dir = proj_dir / "shots" / f"shot_{s_idx:03d}"
            shot_dir.mkdir(parents=True, exist_ok=True)

            # Sao chép clip/audio/ảnh theo đúng chuẩn file & URL của Shot Desk
            target_clip = None
            video_url = None
            audio_url = None
            first_frame = None
            first_frame_url = None
            shot_status = "ready"

            src_clip = v_dir / "clips" / src_clip_name
            if src_clip.exists() and src_clip.stat().st_size > 500:
                dst_clip = proj_dir / dst_clip_rel
                try:
                    shutil.copy2(src_clip, dst_clip)
                    target_clip = dst_clip_rel
                    video_url = f"/api/shot-desk/projects/{proj_id}/shots/{s_idx}/video"
                    shot_status = "approved"
                except Exception as e:
                    logger.warning("Không thể copy clip sang shot desk: %s", e)

            src_audio = v_dir / "audio" / src_audio_name
            if src_audio.exists() and src_audio.stat().st_size > 100:
                dst_audio = proj_dir / dst_audio_rel
                try:
                    shutil.copy2(src_audio, dst_audio)
                    audio_url = f"/api/shot-desk/projects/{proj_id}/shots/{s_idx}/audio"
                except Exception:
                    pass

            src_img = v_dir / "images" / f"scene_{s_idx}.webp"
            if src_img.exists() and src_img.stat().st_size > 500:
                dst_img = shot_dir / "first_frame.webp"
                try:
                    shutil.copy2(src_img, dst_img)
                    first_frame = f"shots/shot_{s_idx:03d}/first_frame.webp"
                    first_frame_url = f"/api/shot-desk/projects/{proj_id}/shots/{s_idx}/frames/first_frame.webp"
                except Exception:
                    pass

            dur_val = int(round(float(s.get("actual_duration") or s.get("duration") or 6)))
            shots.append({
                "index": s_idx,
                "title": f"Cảnh {s_idx} ({s.get('role', 'scene')})",
                "prompt": s.get("prompt", ""),
                "compliance_prompt": None,
                "risk_level": "low",
                "voiceover": s.get("voiceover", ""),
                "voice_mode": "narration",
                "speaker": "",
                "voice": default_voice,
                "lip_sync": False,
                "duration": max(2, dur_val),
                "first_frame": first_frame,
                "middle_frame": None,
                "last_frame": None,
                "first_frame_url": first_frame_url,
                "middle_frame_url": None,
                "last_frame_url": None,
                "transition_in": None,
                "transition_out": None,
                "transition_note": "",
                "characters": [v["character"].get("name")] if v.get("character") and v["character"].get("name") else [],
                "status": shot_status,
                "clip_path": target_clip,
                "video_url": video_url,
                "audio_url": audio_url,
                "used_prompt": s.get("prompt", "") if target_clip else None,
                "used_compliance": False,
                "has_middle_frame": False,
                "error": None,
                "audit": {
                    "character_check": "Đã đồng bộ từ Batch Video" if target_clip else "Chờ render",
                    "aspect_ratio_check": aspect_ratio,
                    "watermark_clean": True,
                    "continuity_note": "",
                },
            })

        project_obj = {
            "id": proj_id,
            "format_version": "1.0",
            "title": v.get("title", "Video Hàng Loạt"),
            "genre": "Video ngắn",
            "logline": v.get("prompt", ""),
            "aspect_ratio": aspect_ratio,
            "visual_style": v.get("visual_style", "anime"),
            "default_voice": default_voice,
            "default_voice_mode": "narration",
            "total_duration": sum(sh["duration"] for sh in shots),
            "shot_duration": 6,
            "shots": shots,
            "characters": [v["character"]] if v.get("character") else [],
            "timeline_settings": {
                "music_volume": 0.25,
                "voice_volume": 1.0,
                "subtitle_mode": "burn_in",
                "export_quality": "720p"
            },
            "created_at": time.time(),
            "updated_at": time.time(),
            "final_video": None
        }

        save_shot_desk_project(project_obj)
        return {"project_id": proj_id, "title": project_obj["title"]}


# ── 5. Worker Xử Lý Video Chạy Nền Tự Động ───────────────────────────────────

_worker_wake_event = threading.Event()
_worker_thread: Optional[threading.Thread] = None
_worker_lock = threading.Lock()


def _resolve_character_ref_data_uri(ref_str: Optional[str]) -> Optional[str]:
    """Chuẩn hóa avatar nhân vật thành URL/data URI an toàn (chỉ cho phép file trong workspace/)."""
    raw = (ref_str or "").strip()
    if not raw:
        return None
    if raw.startswith(("http://", "https://", "data:image/")):
        return raw
    import base64
    import mimetypes
    from batch_engine import _resolve_safe_reference
    safe_path = _resolve_safe_reference(raw)
    if not safe_path or not safe_path.is_file():
        return None
    try:
        mime = mimetypes.guess_type(str(safe_path))[0] or "image/png"
        b64 = base64.b64encode(safe_path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception:
        return None


def _generate_scene_image(
    prompt: str,
    aspect_ratio: str,
    out_img_path: Path,
    reference_image: Optional[str] = None,
) -> bool:
    """Tạo ảnh tĩnh độ phân giải cao cho phân cảnh thông qua Muse2API."""
    from batch_engine import generate_keyframe_image
    base_url = os.getenv("MUSE2API_URL", "http://localhost:18610")
    api_key = os.getenv("MUSE2API_KEY") or os.getenv("MUSE_API_KEY")
    if not api_key:
        logger.warning("MUSE2API_KEY / MUSE_API_KEY chưa được cấu hình trong môi trường, bỏ qua gọi Muse AI.")
        return False

    ref_payload = _resolve_character_ref_data_uri(reference_image)
    try:
        asyncio.run(generate_keyframe_image(
            prompt=prompt,
            base_url=base_url,
            api_key=api_key,
            output_path=str(out_img_path),
            aspect_ratio=aspect_ratio,
            reference_image=ref_payload,
        ))
        if out_img_path.exists() and out_img_path.stat().st_size > 1000:
            return True
    except Exception as e:
        logger.warning("Không thể sinh ảnh Muse AI cho prompt '%s': %s", prompt[:60], e)

    # Nếu truyền reference_image lỗi, thử lại không kèm reference_image
    if ref_payload:
        try:
            asyncio.run(generate_keyframe_image(
                prompt=prompt,
                base_url=base_url,
                api_key=api_key,
                output_path=str(out_img_path),
                aspect_ratio=aspect_ratio,
            ))
            return out_img_path.exists() and out_img_path.stat().st_size > 1000
        except Exception as e2:
            logger.warning("Fallback sinh ảnh không reference cũng thất bại: %s", e2)
    return False


def _convert_image_and_audio_to_clip(
    img_path: Path,
    audio_path: Optional[Path],
    duration: float,
    aspect_ratio: str,
    out_clip_path: Path
) -> bool:
    """Chuyển đổi ảnh AI + âm thanh TTS thành video clip MP4 mượt mà với hiệu ứng chuyển động Ken Burns."""
    from composer import _resolve_ffmpeg
    ffmpeg_exe = _resolve_ffmpeg()

    if aspect_ratio == "16:9":
        width, height = 1280, 720
        scale_w, scale_h = 1920, 1080
    elif aspect_ratio == "1:1":
        width, height = 720, 720
        scale_w, scale_h = 1080, 1080
    else:  # 9:16
        width, height = 720, 1280
        scale_w, scale_h = 1080, 1920

    dur = max(2.5, float(duration))
    frames_count = max(50, int(dur * 25))
    has_audio = audio_path and audio_path.exists() and audio_path.stat().st_size > 100

    # Hiệu ứng Ken Burns zoom chậm điện ảnh
    zoom_filter = f"scale={scale_w}:{scale_h},zoompan=z='min(zoom+0.0008,1.12)':d={frames_count}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps=25,format=yuv420p"

    cmd = [
        ffmpeg_exe, "-y",
        "-loop", "1",
        "-i", str(img_path)
    ]
    if has_audio:
        cmd.extend(["-i", str(audio_path)])
    else:
        cmd.extend(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"])

    cmd.extend([
        "-t", f"{dur:.2f}",
        "-vf", zoom_filter,
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "192k", "-shortest",
        str(out_clip_path)
    ])

    try:
        res = subprocess.run(cmd, capture_output=True, timeout=90)
        if res.returncode == 0 and out_clip_path.exists() and out_clip_path.stat().st_size > 1000:
            return True
        logger.warning("Ken Burns zoompan gặp sự cố, chuyển sang filter scale tĩnh...")
    except Exception as e:
        logger.warning("Lỗi chạy Ken Burns clip: %s", e)

    # Fallback: scale + crop tĩnh nếu zoompan không thành công
    static_filter = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},format=yuv420p"
    cmd_fallback = [
        ffmpeg_exe, "-y",
        "-loop", "1",
        "-i", str(img_path)
    ]
    if has_audio:
        cmd_fallback.extend(["-i", str(audio_path)])
    else:
        cmd_fallback.extend(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"])

    cmd_fallback.extend([
        "-t", f"{dur:.2f}",
        "-vf", static_filter,
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "192k", "-shortest",
        str(out_clip_path)
    ])

    try:
        res2 = subprocess.run(cmd_fallback, capture_output=True, timeout=60)
        return res2.returncode == 0 and out_clip_path.exists() and out_clip_path.stat().st_size > 1000
    except Exception as e:
        logger.error("Lỗi tạo clip tĩnh từ ảnh: %s", e)
        return False


def _generate_fallback_clip(
    out_clip_path: Path,
    audio_path: Optional[Path],
    duration: float,
    aspect_ratio: str,
    title_text: str,
    scene_idx: int,
    total_scenes: int
) -> bool:
    """Tạo video phân cảnh chất lượng cao kèm âm thanh TTS bằng FFmpeg."""
    from composer import _resolve_ffmpeg
    ffmpeg_exe = _resolve_ffmpeg()

    # Xác định độ phân giải theo khung hình
    if aspect_ratio == "16:9":
        width, height = 1280, 720
    elif aspect_ratio == "1:1":
        width, height = 720, 720
    else:  # 9:16
        width, height = 720, 1280

    dur_str = f"{max(2.0, duration):.2f}"

    # Màu nền gradient cinema sang trọng
    color_schemes = [
        "0x0f172a",  # Deep slate
        "0x111827",  # Dark zinc
        "0x1e1b4b",  # Midnight indigo
        "0x172554",  # Ocean navy
        "0x1c1917"   # Warm dark stone
    ]
    bg_color = color_schemes[(scene_idx - 1) % len(color_schemes)]

    cmd = [
        ffmpeg_exe, "-y",
        "-f", "lavfi", "-i", f"color=c={bg_color}:s={width}x{height}:d={dur_str}:r=30"
    ]

    has_audio = audio_path and audio_path.exists() and audio_path.stat().st_size > 100
    if has_audio:
        cmd.extend(["-i", str(audio_path)])
    else:
        # Audit #12 (2026-11-25): input thu hai (anullsrc) phai dung
        # TRUOC moi option output nhu -vf — truoc day -vf dung giua 2
        # lenh -i nen ffmpeg hieu -vf la option cua input thu hai va loi
        # 100% o nhanh khong-audio.
        cmd.extend(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"])

    # Video filters: Tạo hiệu ứng zoom nhẹ + vignette + mờ viền
    v_filter = "format=yuv420p"
    cmd.extend(["-vf", v_filter])

    if has_audio:
        cmd.extend(["-c:a", "aac", "-b:a", "192k", "-shortest"])
    else:
        cmd.extend(["-c:a", "aac", "-shortest"])

    cmd.extend([
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
        "-pix_fmt", "yuv420p",
        str(out_clip_path)
    ])

    try:
        res = subprocess.run(cmd, capture_output=True, timeout=60)
        return res.returncode == 0 and out_clip_path.exists() and out_clip_path.stat().st_size > 1000
    except Exception as e:
        logger.error("Lỗi chạy ffmpeg fallback clip: %s", e)
        return False


def _process_single_video(video_id: str):
    """Xử lý render toàn bộ các cảnh của 1 video trong hàng đợi."""
    if not is_safe_video_id(video_id):
        logger.warning("Bỏ qua xử lý video ID không an toàn: %s", video_id)
        return

    # Toàn bộ quá trình chuẩn bị & render được bọc trong try...except...finally
    try:
        # Kiểm tra nếu job đã bị hủy hoặc xóa trước khi bắt đầu
        with _CANCELLED_LOCK:
            if video_id in _CANCELLED_VIDEOS:
                logger.info("🚫 Video %s đã bị hủy bỏ bởi người dùng trước khi bắt đầu.", video_id)
                return

        v = BATCH_MANAGER.get_video(video_id)
        if not v:
            logger.info("Video %s không còn tồn tại trong hàng đợi, bỏ qua.", video_id)
            return

        # Chỉ xử lý nếu video đang thực sự ở trạng thái queued
        if v.get("status") != "queued":
            logger.info("Video %s có trạng thái '%s', không phải 'queued', bỏ qua.", video_id, v.get("status"))
            return

        logger.info("🎬 Bắt đầu sản xuất batch video: %s ('%s')", video_id, v.get("title"))
        BATCH_MANAGER.update_video(video_id, {
            "status": "running",
            "progress": 5,
            "current_step": "Khởi tạo kịch bản & thư mục sản xuất..."
        })

        v_dir = (BATCH_DIR / video_id).resolve()
        if v_dir.parent != BATCH_DIR.resolve():
            raise RuntimeError(f"Đường dẫn video {video_id} không an toàn")

        clips_dir = v_dir / "clips"
        audio_dir = v_dir / "audio"
        images_dir = v_dir / "images"
        clips_dir.mkdir(parents=True, exist_ok=True)
        audio_dir.mkdir(parents=True, exist_ok=True)
        images_dir.mkdir(parents=True, exist_ok=True)

        import batch_engine
        voice = v.get("voice") or "vi-VN-HoaiMyNeural"
        if voice in ("ai_voice", "native", "none", "no_dub"):
            logger.warning("Video %s cấu hình voice='%s' không tạo TTS cho ảnh tĩnh; tự động chuyển về vi-VN-HoaiMyNeural.", video_id, voice)
            voice = "vi-VN-HoaiMyNeural"
        aspect_ratio = v.get("aspect_ratio", "9:16")
        mode = v.get("mode", "free")
        character_id = v.get("character_id")
        scenes = v.get("scenes", [])

        # Tự động làm mới kịch bản nếu phát hiện các cảnh cũ bị lặp lại giọng đọc
        # (KHÔNG áp dụng cho video có script_locked — vd: Truyện thành video,
        # scenes đã adapt từ chương truyện, worker phải giữ nguyên)
        vo_set = set(s.get("voiceover", "") for s in scenes)
        if not v.get("script_locked") and len(scenes) >= 3 and (len(vo_set) <= 3 or len(vo_set) < len(scenes) * 0.8):
            logger.info("Phát hiện kịch bản cũ bị lặp thoại, tự động nâng cấp sang narrative beat pool đa dạng...")
            new_script = generate_batch_script_local(
                raw_prompt=v.get("prompt") or v.get("title", ""),
                scenes_count=len(scenes),
                visual_style=v.get("visual_style", "anime"),
                aspect_ratio=aspect_ratio,
                mode=mode,
                character_id=character_id
            )
            scenes = new_script["scenes"]
            BATCH_MANAGER.update_video(video_id, {"scenes": scenes, "character": new_script.get("character")})

        total_scenes = len(scenes)
        scene_clips: List[Path] = []
        ai_images_count = 0
        fallback_scenes_count = 0

        # Bước 1: Xử lý từng cảnh (TTS -> AI Image -> Ken Burns Clip)
        for idx, sc in enumerate(scenes, start=1):
            # Kiểm tra hủy bỏ giữa các cảnh
            with _CANCELLED_LOCK:
                if video_id in _CANCELLED_VIDEOS:
                    logger.info(f"🚫 Video {video_id} đã bị hủy bỏ giữa chừng ở cảnh {idx}.")
                    _CANCELLED_VIDEOS.discard(video_id)
                    shutil.rmtree(v_dir, ignore_errors=True)
                    return

            vo_text = sc.get("voiceover", "").strip() or sc.get("prompt", "")
            audio_path = audio_dir / f"scene_{idx}.mp3"
            img_path = images_dir / f"scene_{idx}.webp"
            clip_path = clips_dir / f"clip_{idx}.mp4"

            base_pct = int(10 + ((idx - 1) / total_scenes) * 65)

            # 1.1 Lồng tiếng TTS (Có retry, nếu lỗi dứt khoát báo failed, KHÔNG tạo video câm im lặng)
            BATCH_MANAGER.update_video(video_id, {
                "progress": base_pct,
                "current_step": f"Đang thu âm lồng tiếng cảnh {idx}/{total_scenes}..."
            })
            if not audio_path.exists() or audio_path.stat().st_size < 100:
                tts_err = None
                for attempt in range(2):
                    try:
                        dur, is_fallback, err_detail = asyncio.run(
                            batch_engine.generate_tts_with_status(vo_text, voice, str(audio_path))
                        )
                        if is_fallback:
                            if audio_path.exists():
                                try:
                                    audio_path.unlink()
                                except Exception:
                                    pass
                            tts_err = RuntimeError(f"TTS trả về audio im lặng dự phòng: {err_detail or 'lỗi không rõ'}")
                            logger.warning("Lần %d lồng tiếng cảnh %d dùng silent fallback: %s", attempt + 1, idx, err_detail)
                        elif audio_path.exists() and audio_path.stat().st_size > 100:
                            sc["actual_duration"] = dur
                            tts_err = None
                            break
                        else:
                            tts_err = RuntimeError("File audio không được tạo hoặc kích thước rỗng")
                    except Exception as e:
                        tts_err = e
                        logger.warning("Lần %d lồng tiếng cảnh %d lỗi: %s", attempt + 1, idx, e)
                    time.sleep(0.5)

                if tts_err or not audio_path.exists() or audio_path.stat().st_size < 100:
                    raise RuntimeError(f"Lỗi tạo giọng đọc (TTS) cảnh {idx}: {tts_err or 'Không thể lưu file âm thanh'}")
            else:
                try:
                    sc["actual_duration"] = batch_engine.get_media_duration(str(audio_path))
                except Exception:
                    sc["actual_duration"] = sc.get("duration", 6.0)

            dur = sc.get("actual_duration") or sc.get("duration", 6.0)

            # 1.2 Sinh ảnh tĩnh AI qua Muse2API
            step_img_pct = min(75, base_pct + int(30 / total_scenes))
            BATCH_MANAGER.update_video(video_id, {
                "progress": step_img_pct,
                "current_step": f"Đang vẽ hình ảnh AI cảnh {idx}/{total_scenes}..."
            })
            if not _validate_image_file(img_path):
                if img_path.exists():
                    try:
                        img_path.unlink()
                    except Exception:
                        pass
                prompt_to_gen = sc.get("prompt", "")
                char_ref = None
                if mode == "character":
                    cur_v = BATCH_MANAGER.get_video(video_id) or v
                    char_obj = cur_v.get("character") or {}
                    char_ref = char_obj.get("avatar") if isinstance(char_obj, dict) else None
                _generate_scene_image(
                    prompt=prompt_to_gen,
                    aspect_ratio=aspect_ratio,
                    out_img_path=img_path,
                    reference_image=char_ref,
                )

            # Đánh giá kết quả tạo ảnh AI thông qua kiểm tra magic bytes và PIL giải mã (Point 2 & 7)
            has_ai_image = _validate_image_file(img_path)
            if has_ai_image:
                ai_images_count += 1
                sc["image_status"] = "ai_generated"
            else:
                fallback_scenes_count += 1
                sc["image_status"] = "fallback"
                if img_path.exists():
                    try:
                        img_path.unlink()
                    except Exception:
                        pass

            # 1.3 Dựng video clip (ưu tiên ảnh AI + Ken Burns motion)
            step_clip_pct = min(78, base_pct + int(60 / total_scenes))
            BATCH_MANAGER.update_video(video_id, {
                "progress": step_clip_pct,
                "current_step": f"Đang render chuyển động cảnh {idx}/{total_scenes}..."
            })

            clip_success = False
            if has_ai_image:
                clip_success = _convert_image_and_audio_to_clip(
                    img_path=img_path,
                    audio_path=audio_path,
                    duration=dur,
                    aspect_ratio=aspect_ratio,
                    out_clip_path=clip_path
                )

            if not clip_success or not clip_path.exists() or clip_path.stat().st_size < 1000:
                logger.info("Chạy fallback clip cho cảnh %d...", idx)
                clip_success = _generate_fallback_clip(
                    out_clip_path=clip_path,
                    audio_path=audio_path,
                    duration=dur,
                    aspect_ratio=aspect_ratio,
                    title_text=v.get("title", ""),
                    scene_idx=idx,
                    total_scenes=total_scenes
                )
                # Nếu ảnh tồn tại nhưng render clip thất bại và phải dùng fallback clip, điều chỉnh chính xác số lượng cảnh fallback (Point 2)
                if has_ai_image:
                    ai_images_count = max(0, ai_images_count - 1)
                    fallback_scenes_count += 1
                    sc["image_status"] = "fallback"

            if not clip_success:
                raise RuntimeError(f"Không thể tạo video clip cho cảnh {idx}")

            scene_clips.append(clip_path)

        # Kiểm tra chất lượng hình ảnh AI: Nếu toàn bộ cảnh không thể sinh ảnh AI, báo lỗi failed rõ ràng
        if ai_images_count == 0:
            raise RuntimeError("Toàn bộ các cảnh đều không thể tạo hình ảnh AI qua Muse2API. Vui lòng kiểm tra lại trạng thái dịch vụ AI vẽ ảnh.")

        # Bước 2: Ghép nối thô timeline
        BATCH_MANAGER.update_video(video_id, {
            "progress": 80,
            "current_step": "Đang ghép nối timeline các cảnh..."
        })

        from composer import create_srt, burn_subtitles, _resolve_ffmpeg
        ffmpeg_exe = _resolve_ffmpeg()

        raw_concat_mp4 = v_dir / "_raw_concat.mp4"
        final_mp4 = v_dir / "final.mp4"
        concat_txt = v_dir / "concat.txt"
        srt_file = v_dir / "subtitles.srt"

        # Tạo file danh sách nối cảnh (escape ' theo chuan ffmpeg — audit
        # #6, 2026-11-25: f-string tho vo khi username dang O'Brien).
        from composer import _concat_list_line
        lines = [_concat_list_line(str(p.resolve())) for p in scene_clips]
        concat_txt.write_text("\n".join(lines), encoding="utf-8")

        # Ghép re-encode để đồng bộ âm thanh và khung hình 100%
        cmd_reencode = [
            ffmpeg_exe, "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_txt),
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "aac", "-b:a", "192k",
            str(raw_concat_mp4)
        ]
        res_re = subprocess.run(cmd_reencode, capture_output=True, timeout=180)
        if res_re.returncode != 0 or not raw_concat_mp4.exists():
            raise RuntimeError(f"FFmpeg ghép video thất bại: {res_re.stderr.decode(errors='replace')}")

        # Bước 3: Tạo file phụ đề SRT và khắc phụ đề điện ảnh vào video (Burn-in Subtitles)
        BATCH_MANAGER.update_video(video_id, {
            "progress": 88,
            "current_step": "Đang đồng bộ và khắc phụ đề tự động (Burn-in Subtitles)..."
        })

        # Tạo file phụ đề SRT (Point 3: Báo lỗi nếu thất bại, không âm thầm bỏ qua phụ đề)
        try:
            create_srt(
                results=scenes,
                output_srt=str(srt_file),
                clip_durations=[sc.get("actual_duration") or sc.get("duration", 6.0) for sc in scenes],
                media_paths=[str(p) for p in scene_clips]
            )
        except Exception as e_srt:
            logger.error(f"Lỗi tạo file phụ đề SRT cho {video_id}: {e_srt}")
            raise RuntimeError(f"Lỗi tạo phụ đề video (SRT): {e_srt}")

        if not srt_file.exists() or srt_file.stat().st_size < 10:
            raise RuntimeError("File phụ đề SRT bị rỗng hoặc không thể tạo được.")

        # Khắc phụ đề vào video thành phẩm (Point 3)
        try:
            burn_subtitles(
                input_video=str(raw_concat_mp4),
                srt_file=str(srt_file),
                output_video=str(final_mp4),
                font_size=12,
                raise_on_error=True
            )
        except Exception as e_burn:
            logger.error(f"Burn-in phụ đề gặp sự cố: {e_burn}")
            raise RuntimeError(f"Lỗi khắc phụ đề vào video: {e_burn}")

        if not final_mp4.exists() or final_mp4.stat().st_size < 1000:
            raise RuntimeError("File video cuối cùng sau khi khắc phụ đề không tồn tại hoặc bị lỗi.")

        # Dọn dẹp file tạm concat
        if raw_concat_mp4.exists():
            try:
                raw_concat_mp4.unlink()
            except Exception:
                pass

        # Xác định thông báo hoàn tất & cảnh báo fallback nếu có
        warning_msg = None
        completion_step = "Hoàn tất video"
        if fallback_scenes_count > 0:
            warning_msg = f"Có {fallback_scenes_count}/{total_scenes} cảnh phải dùng hình nền dự phòng do AI vẽ ảnh quá tải."
            completion_step = f"Hoàn tất ({ai_images_count}/{total_scenes} cảnh AI)"

        # Hoàn tất thành công! (Lưu lại đầy đủ metadata scenes bao gồm actual_duration và image_status)
        BATCH_MANAGER.update_video(video_id, {
            "status": "completed",
            "progress": 100,
            "current_step": completion_step,
            "scenes": scenes,
            "final_video_url": f"/api/batch-videos/{video_id}/video",
            "srt_url": f"/api/batch-videos/{video_id}/subtitles",
            "has_fallback_images": fallback_scenes_count > 0,
            "warning": warning_msg,
            "error": None
        })
        logger.info("✅ Hoàn tất sản xuất batch video: %s (AI images: %d/%d, Subtitles: OK)", video_id, ai_images_count, total_scenes)

    except Exception as e:
        logger.error("❌ Lỗi sản xuất batch video %s: %s", video_id, e, exc_info=True)
        try:
            if BATCH_MANAGER.get_video(video_id):
                BATCH_MANAGER.update_video(video_id, {
                    "status": "failed",
                    "scenes": scenes if 'scenes' in locals() else [],
                    "error": str(e),
                    "current_step": f"Lỗi: {e}"
                })
        except Exception as e_fail:
            logger.error("Không thể cập nhật trạng thái failed cho %s: %s", video_id, e_fail)
    finally:
        # Luôn đảm bảo dọn sạch cờ hủy sau khi kết thúc xử lý (Point 6)
        with _CANCELLED_LOCK:
            _CANCELLED_VIDEOS.discard(video_id)


def _worker_loop():
    """Vòng lặp lấy các video 'queued' và xử lý lần lượt, có cơ chế chờ và chống race condition."""
    global _worker_thread
    idle_rounds = 0
    while True:
        target_id = None
        with _QUEUE_LOCK:
            videos = BATCH_MANAGER._read_queue()
            for v in videos:
                if v.get("status") == "queued":
                    target_id = v.get("id")
                    break

        if target_id:
            idle_rounds = 0
            _process_single_video(target_id)
            time.sleep(0.5)
        else:
            idle_rounds += 1
            if idle_rounds >= 2:
                # Kiểm tra lần cuối trong _worker_lock trước khi kết thúc luồng (Point 5: loại bỏ hoàn toàn race condition)
                with _worker_lock:
                    with _QUEUE_LOCK:
                        has_queued = any(v.get("status") == "queued" for v in BATCH_MANAGER._read_queue())
                    if not has_queued:
                        _worker_thread = None
                        return
            _worker_wake_event.wait(timeout=2.0)
            _worker_wake_event.clear()


def start_batch_worker():
    """Khởi động luồng worker nền nếu chưa chạy hoặc đánh thức worker đang chờ."""
    global _worker_thread
    with _worker_lock:
        _worker_wake_event.set()
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(target=_worker_loop, daemon=True, name="BatchVideoWorker")
            _worker_thread.start()
            logger.info("🚀 Đã khởi động BatchVideoWorker daemon thread.")


def start_pending_batch_worker():
    """Resume both already queued jobs and jobs recovered from an interruption."""
    if any(video.get("status") == "queued" for video in BATCH_MANAGER._read_queue()):
        start_batch_worker()


BATCH_MANAGER = BatchVideoManager()
try:
    start_pending_batch_worker()
except Exception as _e_rec:
    logger.warning("Không thể tự động khởi động worker sau recovery: %s", _e_rec)

