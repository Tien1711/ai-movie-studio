"""normalize_clips.py — Chuẩn hoá audio các clip Shot Desk về 44.1kHz stereo.

Chạy:  python normalize_clips.py [--dir <thư mục project>] [--pattern 'shot_*_clip.mp4']
        (mặc định: thư mục hiện tại, hoặc env NORMALIZE_CLIPS_DIR)

Audit §1.16 (đã sửa):
- Toàn bộ logic nằm trong main() + `if __name__ == "__main__"` — import file
  này không còn chạy normalize nữa.
- Không còn hardcode đường dẫn Windows C:\\Users\\Admin\\... — thư mục lấy
  từ --dir / env NORMALIZE_CLIPS_DIR / thư mục hiện tại.
- Resume được: sau mỗi clip xong, trạng thái (path + size + mtime sau khi
  normalize) được ghi atomic vào <clips_dir>/.normalize_state.json; chạy lại
  sẽ skip các clip đã xong và hợp lệ. Dùng --force để làm lại từ đầu.
- Clip không có audio stream thì log WARNING rõ ràng (không bỏ qua lặng lẽ).
- Mọi subprocess đều có timeout; lỗi 1 clip không crash toàn bộ tiến trình.
"""

import argparse
import glob
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from composer import FFMPEG

PROBE_TIMEOUT = 60
NORMALIZE_TIMEOUT = 600
STATE_FILENAME = ".normalize_state.json"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("normalize_clips")


def _load_state(state_path: Path) -> dict:
    try:
        if state_path.is_file():
            return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"Không đọc được state file {state_path} ({exc}) — làm lại từ đầu.")
    return {}


def _save_state_atomic(state_path: Path, state: dict) -> None:
    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, state_path)


def _probe_audio(clip: str) -> tuple[bool, bool]:
    """Trả về (has_audio, needs_norm)."""
    info_cmd = [FFMPEG, "-i", clip]
    res = subprocess.run(info_cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    has_audio = False
    needs_norm = False
    for line in res.stderr.split("\n"):
        if "Audio:" in line:
            has_audio = True
            if "24000 Hz" in line or "mono" in line or "stereo" not in line or "44100 Hz" not in line:
                needs_norm = True
            break
    return has_audio, needs_norm


def _normalize_one(clip: str) -> bool:
    tmp_clip = clip + ".norm.mp4"
    cmd = [
        FFMPEG, "-y",
        "-i", clip,
        "-c:v", "copy",
        "-af", "aresample=44100:async=1,aformat=sample_fmts=fltp:channel_layouts=stereo",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        tmp_clip,
    ]
    subprocess.run(cmd, capture_output=True, check=True, timeout=NORMALIZE_TIMEOUT)
    os.replace(tmp_clip, clip)
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Chuẩn hoá audio các clip về 44.1kHz stereo.")
    parser.add_argument(
        "--dir", dest="proj_dir", default=None,
        help="Thư mục project chứa thư mục clips/ (mặc định: env NORMALIZE_CLIPS_DIR hoặc thư mục hiện tại)",
    )
    parser.add_argument(
        "--pattern", default="shot_*_clip.mp4",
        help="Glob pattern tên clip trong clips/ (mặc định: shot_*_clip.mp4)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Normalize lại cả các clip đã ghi nhận xong trong state file.",
    )
    args = parser.parse_args(argv)

    proj_dir = Path(
        args.proj_dir or os.environ.get("NORMALIZE_CLIPS_DIR") or os.getcwd()
    ).expanduser().resolve()
    clips_dir = proj_dir / "clips"
    if not clips_dir.is_dir():
        logger.error(f"Không tìm thấy thư mục clips: {clips_dir}")
        return 2

    clips = sorted(glob.glob(str(clips_dir / args.pattern)))
    logger.info(f"Normalizing audio for {len(clips)} clips in {clips_dir}...")
    t0 = time.time()

    state_path = clips_dir / STATE_FILENAME
    state = _load_state(state_path)
    done = state.setdefault("done", {})  # clip path -> {"size": int, "mtime": float}

    ok = skipped = failed = no_audio = 0
    for clip in clips:
        name = os.path.basename(clip)
        try:
            st = os.stat(clip)
        except OSError as exc:
            logger.error(f"{name}: không stat được file ({exc}) — bỏ qua.")
            failed += 1
            continue

        # Resume: clip đích đã tồn tại và hợp lệ (khớp size/mtime đã ghi) thì skip.
        if not args.force:
            rec = done.get(clip)
            if rec and rec.get("size") == st.st_size and rec.get("mtime") == st.st_mtime:
                logger.info(f"Skipped (đã normalize trước đó): {name}")
                skipped += 1
                continue

        try:
            has_audio, needs_norm = _probe_audio(clip)
        except subprocess.TimeoutExpired:
            logger.error(f"{name}: ffprobe timeout sau {PROBE_TIMEOUT}s — bỏ qua, chạy lại sẽ thử tiếp.")
            failed += 1
            continue
        except Exception as exc:
            logger.error(f"{name}: lỗi probe ({exc}) — bỏ qua.")
            failed += 1
            continue

        if not has_audio:
            # Cảnh báo rõ ràng thay vì bỏ qua lặng lẽ (§1.16): clip không có
            # audio có thể làm concat sau này fail.
            logger.warning(
                f"{name}: KHÔNG có audio stream — giữ nguyên clip, "
                "kiểm tra lại nguồn render (concat sau này có thể fail vì thiếu audio)."
            )
            no_audio += 1
            continue

        if not needs_norm:
            done[clip] = {"size": st.st_size, "mtime": st.st_mtime}
            _save_state_atomic(state_path, state)
            skipped += 1
            continue

        try:
            _normalize_one(clip)
        except subprocess.TimeoutExpired:
            logger.error(f"{name}: normalize timeout sau {NORMALIZE_TIMEOUT}s — bỏ qua, chạy lại sẽ thử tiếp.")
            failed += 1
            continue
        except subprocess.CalledProcessError as exc:
            logger.error(f"{name}: ffmpeg normalize lỗi (rc={exc.returncode}) — bỏ qua, chạy lại sẽ thử tiếp.")
            failed += 1
            continue
        except Exception as exc:
            logger.error(f"{name}: lỗi không ngờ ({exc}) — bỏ qua.")
            failed += 1
            continue

        st2 = os.stat(clip)
        done[clip] = {"size": st2.st_size, "mtime": st2.st_mtime}
        _save_state_atomic(state_path, state)
        logger.info(f"Normalized {name}")
        ok += 1

    logger.info(
        f"Xong trong {time.time() - t0:.2f}s: {ok} normalized, {skipped} skipped, "
        f"{no_audio} không có audio, {failed} lỗi."
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
