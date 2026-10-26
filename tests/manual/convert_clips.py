"""Verify real clip conversion locally; no TV or network access."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from gnome_adb_tv_remote.core.local_media import ffmpeg_args, media_binary, probe_file


def main() -> None:
    """Convert eight seconds per clip and retain ffprobe/log evidence."""
    encoder = sys.argv[1] if len(sys.argv) > 1 else "libx264"
    root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp/tvremote-conversion")
    for name in ("PXL_20261025_182139252.mp4", "XiaoYing_Video_1790339249840 (2).mp4"):
        source = Path.home() / "Videos/tv-receiver-check" / name
        destination = root / encoder / source.stem
        destination.mkdir(parents=True, exist_ok=True)
        info = probe_file(source)
        args = ffmpeg_args(source, destination, info, encoder)
        args[args.index("-map") : args.index("-map")] = ["-t", "8"]
        (destination / "argv.json").write_text(json.dumps(args, indent=2))
        with (destination / "encoder.log").open("wb") as log:
            result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=log, timeout=120)
        if result.returncode:
            raise RuntimeError((destination / "encoder.log").read_text()[-3000:])
        data = subprocess.check_output(
            [
                media_binary("ffprobe"),
                "-v",
                "error",
                "-show_streams",
                "-of",
                "json",
                str(destination / "index.m3u8"),
            ]
        )
        (destination / "ffprobe.json").write_bytes(data)
        streams = json.loads(data)["streams"]
        video = next(stream for stream in streams if stream["codec_type"] == "video")
        assert (
            video["codec_name"] == "h264" and video["profile"] == "High" and video["level"] <= 41
        )
        assert video["width"] <= 1920 and video["height"] <= 1080
        assert not any(side.get("rotation") for side in video.get("side_data_list", []))
        assert "#EXT-X-ENDLIST" in (destination / "index.m3u8").read_text()
        assert "#EXT-X-PLAYLIST-TYPE:EVENT" in (destination / "index.m3u8").read_text()
        audio = next(stream for stream in streams if stream["codec_type"] == "audio")
        assert audio["codec_name"] == "aac" and audio["profile"] == "LC"
        assert audio["channels"] == 2 and audio["sample_rate"] == "48000"
        if name.startswith("PXL"):
            assert video["width"] == 608 and video["height"] == 1080
            subprocess.run(
                [
                    media_binary("ffmpeg"),
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(destination / "index.m3u8"),
                    "-frames:v",
                    "1",
                    str(destination / "preview.png"),
                ],
                check=True,
                timeout=20,
            )
        print(encoder, name, video["width"], video["height"], "OK", flush=True)


if __name__ == "__main__":
    main()
