import os, subprocess
from composer import FFMPEG

if __name__ == "__main__":
    srt_dir = os.path.abspath(r"workspace/_shot_desk_projects/tan_kiem_huu_tinh_1bf776/final")
    clip13 = os.path.abspath(r"workspace/_shot_desk_projects/tan_kiem_huu_tinh_1bf776/clips/shot_013_clip.mp4")
    in_rel = os.path.relpath(clip13, srt_dir).replace("\\", "/")

    # Match the user's desired bottom subtitle:
    # Single line, neat font, positioned at the bottom margin (MarginV=10)
    style = "FontName=Arial,FontSize=10,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=1.2,Shadow=0.5,Alignment=2,MarginV=10"
    vf = f"subtitles=test_shot13.srt:force_style='{style}'"

    out_img = os.path.join(srt_dir, "test_perfect_bottom3.jpg")
    subprocess.run([
        FFMPEG, "-y",
        "-i", in_rel,
        "-vf", vf,
        "-ss", "00:00:03",
        "-vframes", "1",
        out_img
    ], cwd=srt_dir, check=True)
    print("Rendered successfully:", out_img)

