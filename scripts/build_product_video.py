"""Build the TopAI Real Estate Tools cartoon explainer video."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import wave
from pathlib import Path

import edge_tts
import imageio_ffmpeg
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS_SRC = ROOT / "static" / "product" / "video-frames"
WORK = ROOT / "build" / "product_video"
OUT_DIR = ROOT / "static" / "product"
OUT_VIDEO = OUT_DIR / "topai-how-it-works.mp4"
OUT_POSTER = OUT_DIR / "topai-how-it-works-poster.jpg"

VOICE = "en-US-AndrewMultilingualNeural"
WIDTH, HEIGHT = 1280, 720
FPS = 30

SCENES = [
    {
        "image": "video-01-problem.png",
        "text": (
            "Real estate agents juggle listings, follow-ups, and social media every day. "
            "Too many marketing tasks. Not enough time."
        ),
    },
    {
        "image": "video-02-solution.png",
        "text": (
            "TopAI Real Estate Tools is your AI-powered solution for sourcing more leads "
            "and growing opportunities to sell your services."
        ),
    },
    {
        "image": "video-03-listing-social.png",
        "text": (
            "Enter a property once, and instantly create polished listing descriptions, "
            "prospect emails, and posts ready for Instagram, Facebook, and X."
        ),
    },
    {
        "image": "video-04-scripts.png",
        "text": (
            "Need to prospect? Generate natural cold call scripts, objection handlers, "
            "and voicemail messages in seconds."
        ),
    },
    {
        "image": "video-05-calling.png",
        "text": (
            "Then put your AI Calling Assistant to work. It makes consent-based follow-up calls, "
            "qualifies interest, and helps unlock more appointment opportunities."
        ),
    },
    {
        "image": "video-06-cta.png",
        "text": (
            "TopAI Real Estate Tools helps you market your services across the platforms that matter, "
            "capture more leads, and win more opportunities — so you can spend less time writing "
            "and more time closing."
        ),
    },
]


def prepare_dirs() -> None:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)


def prepare_images() -> list[Path]:
    frames = []
    for index, scene in enumerate(SCENES, start=1):
        src = ASSETS_SRC / scene["image"]
        if not src.exists():
            raise FileNotFoundError(f"Missing storyboard image: {src}")
        img = Image.open(src).convert("RGB")
        img = fit_cover(img, WIDTH, HEIGHT)
        dest = WORK / f"scene_{index:02d}.png"
        img.save(dest, format="PNG", optimize=True)
        frames.append(dest)
        if index == 1:
            img.save(OUT_POSTER, format="JPEG", quality=90, optimize=True)
    return frames


def fit_cover(img: Image.Image, width: int, height: int) -> Image.Image:
    src_ratio = img.width / img.height
    dest_ratio = width / height
    if src_ratio > dest_ratio:
        new_height = height
        new_width = int(height * src_ratio)
    else:
        new_width = width
        new_height = int(width / src_ratio)
    resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
    left = (new_width - width) // 2
    top = (new_height - height) // 2
    return resized.crop((left, top, left + width, top + height))


async def synthesize_scene_audio(index: int, text: str) -> Path:
    dest = WORK / f"scene_{index:02d}.mp3"
    communicate = edge_tts.Communicate(text, VOICE, rate="-5%", pitch="-2Hz")
    await communicate.save(str(dest))
    return dest


async def synthesize_all_audio() -> list[Path]:
    tasks = [synthesize_scene_audio(i, scene["text"]) for i, scene in enumerate(SCENES, start=1)]
    return await asyncio.gather(*tasks)


def audio_duration_seconds(path: Path) -> float:
    # Convert mp3 to wav temporarily for accurate duration, then read frames.
    wav_path = path.with_suffix(".wav")
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [ffmpeg, "-y", "-i", str(path), str(wav_path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with wave.open(str(wav_path), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
    return max(frames / float(rate), 1.0)


def write_concat_list(entries: list[tuple[Path, float]]) -> Path:
    concat_path = WORK / "scenes.txt"
    lines = []
    for image_path, duration in entries:
        # ffmpeg concat demuxer needs escaped single quotes on Windows paths carefully.
        safe = str(image_path).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{safe}'")
        lines.append(f"duration {duration:.3f}")
    # Repeat last file for concat demuxer correctness.
    last = str(entries[-1][0]).replace("\\", "/").replace("'", "'\\''")
    lines.append(f"file '{last}'")
    concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return concat_path


def concat_audio(audio_files: list[Path]) -> Path:
    list_path = WORK / "audio.txt"
    lines = []
    for audio in audio_files:
        safe = str(audio).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = WORK / "narration.mp3"
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            str(out),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return out


def render_video(frames: list[Path], audio_files: list[Path], narration: Path) -> None:
    durations = [audio_duration_seconds(path) + 0.35 for path in audio_files]
    concat_list = write_concat_list(list(zip(frames, durations)))
    silent = WORK / "silent.mp4"
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-vf",
            f"fps={FPS},format=yuv420p",
            "-pix_fmt",
            "yuv420p",
            str(silent),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(silent),
            "-i",
            str(narration),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(OUT_VIDEO),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> None:
    prepare_dirs()
    frames = prepare_images()
    audio_files = asyncio.run(synthesize_all_audio())
    narration = concat_audio(audio_files)
    render_video(frames, audio_files, narration)
    print(f"Wrote {OUT_VIDEO}")
    print(f"Wrote {OUT_POSTER}")


if __name__ == "__main__":
    main()
