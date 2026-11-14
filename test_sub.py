import os, subprocess
from composer import FFMPEG

if __name__ == "__main__":
    fin = r"workspace/_shot_desk_projects/tan_kiem_huu_tinh_1bf776/final/tan_kiem_huu_tinh_1792505744.mp4"
    srt = r"workspace/_shot_desk_projects/tan_kiem_huu_tinh_1bf776/final/tan_kiem_huu_tinh_1792505744.srt"

    srt_dir = os.path.dirname(os.path.abspath(srt))
    srt_name = os.path.basename(srt)
    in_rel = os.path.relpath(os.path.abspath(fin), srt_dir).replace("\\", "/")

    # In libass:
    # BorderStyle=1: Outline with drop shadow (standard cinema subtitle, no clumsy background box)
    # Alignment=2: Bottom-center
    # MarginV=25: Clean margin at the bottom
    # FontSize=13: In ASS default 384x288 coordinate space, 12-14 scales to ~50px on 1280h, perfectly legible single-line bottom subtitle
    style = "FontName=Arial,FontSize=13,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=25"
    vf = f"subtitles={srt_name}:force_style='{style}'"

    # Let's test on the merged video before sub burn-in, or raw shot 13
    subprocess.run([
        FFMPEG, "-y", "-ss", "00:02:02",
        "-i", in_rel,
        "-vf", vf,
        "-vframes", "1",
        os.path.join(srt_dir, "test_sub_fix.jpg")
    ], cwd=srt_dir, check=True)
    print("Saved test_sub_fix.jpg")

