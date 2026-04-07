from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "www.youtu.be",
}


@dataclass(frozen=True)
class YouTubeDownloadResult:
    media_path: Path
    video_id: str
    title: str


def _is_youtube_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower().strip()
    return host in YOUTUBE_HOSTS


def download_youtube_media(
    youtube_url: str,
    *,
    output_dir: str | Path,
    output_stem: str | None = None,
) -> YouTubeDownloadResult:
    url = youtube_url.strip()
    if not _is_youtube_url(url):
        raise ValueError("youtube_url must be a valid YouTube link")

    try:
        import yt_dlp  # type: ignore
    except ImportError as exc:
        raise RuntimeError("yt-dlp is not installed. Add it to requirements and install dependencies.") from exc

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    suffix = output_stem.strip() if output_stem else "%(id)s"
    outtmpl = str(out_dir / f"{suffix}.%(ext)s")

    options = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise RuntimeError("yt-dlp did not return metadata")

        downloaded = Path(ydl.prepare_filename(info))
        if not downloaded.exists():
            raise RuntimeError("yt-dlp reported success but no media file was downloaded")

        video_id = str(info.get("id") or downloaded.stem).strip()
        title = str(info.get("title") or downloaded.stem).strip()

    return YouTubeDownloadResult(media_path=downloaded, video_id=video_id, title=title)
