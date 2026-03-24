"""YouTube video downloader using yt-dlp."""

import logging
import threading
from pathlib import Path

import yt_dlp

logger = logging.getLogger(__name__)


class VideoDownloader:
    """Downloads YouTube videos using yt-dlp with retry and resume support."""

    def __init__(self):
        self._cancel = threading.Event()

    def cancel(self):
        """Signal cancellation of the current download."""
        self._cancel.set()

    def download(
        self,
        url: str,
        output_dir: str | Path,
        output_name: str,
        progress_callback=None,
    ) -> Path | None:
        """Download a YouTube video to the specified directory.

        Args:
            url: YouTube video URL
            output_dir: Directory to save the video
            output_name: Base filename (without extension) for the output
            progress_callback: Called with (status_str) for progress updates

        Returns:
            Path to the downloaded file, or None on failure.

        Raises:
            Exception on network or download errors (caller should handle).
        """
        self._cancel.clear()
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # yt-dlp will add the extension based on format
        output_template = str(output_dir / f"{output_name}.%(ext)s")

        def progress_hook(d):
            if self._cancel.is_set():
                raise yt_dlp.utils.DownloadCancelled("Download cancelled")

            if progress_callback and d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                if total > 0:
                    pct = downloaded / total * 100
                    progress_callback(
                        f"Downloading video: {pct:.0f}% "
                        f"({downloaded // 1024 // 1024}MB / "
                        f"{total // 1024 // 1024}MB)"
                    )
                else:
                    progress_callback(
                        f"Downloading video: {downloaded // 1024 // 1024}MB"
                    )
            elif progress_callback and d.get("status") == "finished":
                progress_callback("Download complete, processing...")

        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "outtmpl": output_template,
            "progress_hooks": [progress_hook],
            "quiet": True,
            "no_warnings": True,
            # Retry and robustness options for flaky connections
            "retries": 10,
            "fragment_retries": 10,
            "retry_sleep_functions": {"http": lambda n: min(2 ** n, 30)},
            "socket_timeout": 30,
            "extractor_retries": 5,
            # Continue partial downloads
            "continuedl": True,
            # Don't overwrite completed downloads
            "nooverwrites": True,
            "merge_output_format": "mp4",
        }

        logger.info("Starting video download: %s -> %s", url, output_name)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # Find the actual output file
            if info:
                ext = info.get("ext", "mp4")
                final_path = output_dir / f"{output_name}.{ext}"
                if final_path.exists():
                    logger.info("Video saved: %s", final_path)
                    return final_path
                # Try mp4 specifically since we request merge to mp4
                mp4_path = output_dir / f"{output_name}.mp4"
                if mp4_path.exists():
                    return mp4_path

        return None
