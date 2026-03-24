"""Build script to create a standalone Windows EXE using PyInstaller.

Run on Windows:
    uv sync --group dev
    uv run python build.py
"""

import PyInstaller.__main__
import sys

PyInstaller.__main__.run([
    "log_puller.py",
    "--onefile",
    "--windowed",
    "--name=FRC_Log_Puller",
    "--icon=NONE",
    # Pull in the submodules
    "--hidden-import=sftp_client",
    "--hidden-import=tba_client",
    "--hidden-import=video_downloader",
    # yt-dlp needs these
    "--collect-all=yt_dlp",
    "--noconfirm",
    "--clean",
])
