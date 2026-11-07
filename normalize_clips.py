import os, glob, subprocess, time
from composer import FFMPEG

proj_dir = r"C:\Users\Admin\.gemini\antigravity\scratch\ai_movie_studio\workspace\_shot_desk_projects\tan_kiem_huu_tinh_1bf776"
clips_dir = os.path.join(proj_dir, "clips")
clips = sorted(glob.glob(os.path.join(clips_dir, "shot_*_clip.mp4")))

print(f"Normalizing audio for {len(clips)} clips...")
t0 = time.time()

for clip in clips:
    # Check if clip has audio and what sample rate / channels
    info_cmd = [FFMPEG, "-i", clip]
    res = subprocess.run(info_cmd, capture_output=True, text=True)
    needs_norm = False
    for line in res.stderr.split("\n"):
        if "Audio:" in line:
            if "24000 Hz" in line or "mono" in line or "stereo" not in line or "44100 Hz" not in line:
                needs_norm = True
            break
    
    if needs_norm:
        tmp_clip = clip + ".norm.mp4"
        cmd = [
            FFMPEG, "-y",
            "-i", clip,
            "-c:v", "copy",
            "-af", "aresample=44100:async=1,aformat=sample_fmts=fltp:channel_layouts=stereo",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            tmp_clip
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        os.replace(tmp_clip, clip)
        print(f"Normalized {os.path.basename(clip)}")

print(f"All clips normalized in {time.time() - t0:.2f}s!")
