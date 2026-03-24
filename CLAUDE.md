# CLAUDE.md — FRC Log Puller

## Project Overview

Desktop app (tkinter) that automatically pulls FRC match log files from a roboRIO via SFTP and downloads corresponding match videos from YouTube via The Blue Alliance API.

Built for FRC Team 4003 (TriSonics) but works with any team number.

## Common Commands

```bash
# Install dependencies
uv sync

# Run the app
uv run python log_puller.py

# Build Windows EXE (must run on Windows)
uv sync --group dev
uv run python build.py
# Output: dist/FRC_Log_Puller.exe
```

## Architecture

- **log_puller.py** — Main GUI app. Tkinter-based. Settings persistence in `~/.frc_log_puller/config.json`. Background threads for SFTP polling, log downloads, and video downloads.
- **sftp_client.py** — SSH/SFTP connection to roboRIO. Team number → IP conversion (`10.XX.YY.2`). Match log filename parsing via regex. Atomic downloads using `.part` suffix.
- **tba_client.py** — The Blue Alliance API client. Maps FIRST event codes to TBA event keys, builds match keys (including double-elimination mapping), fetches YouTube video IDs. Retry with exponential backoff.
- **video_downloader.py** — yt-dlp wrapper. Best quality MP4, resume support, 10 retries with backoff for flaky event WiFi.
- **build.py** — PyInstaller build script for single-file Windows EXE.

## Key Design Decisions

- Only downloads match logs matching `FRC_{date}_{time}_{EVENT}_{Q|E}{num}.wpilog`. Practice/test logs are ignored.
- Auto-downloads new logs when detected — no manual trigger needed.
- Internet errors (TBA, YouTube) show a dialog reminding the user to check VPN, since event venues often lack direct internet.
- roboRIO connection uses `lvuser` with no password (default FRC config).

## Dependencies

- `paramiko` — SFTP
- `requests` — TBA API
- `yt-dlp` — YouTube downloads
- `pyinstaller` (dev) — Windows EXE packaging
