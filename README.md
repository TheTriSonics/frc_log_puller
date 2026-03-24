# FRC Log Puller

Desktop app that automatically pulls FRC match log files from a roboRIO via SFTP and downloads corresponding match videos from YouTube via [The Blue Alliance](https://www.thebluealliance.com/) API.

Built for FRC Team 4003 (TriSonics), but works with any team number.

## Features

- Automatically detects and downloads new match logs (`.wpilog` files) from the roboRIO over SFTP
- Looks up match videos on The Blue Alliance and downloads them from YouTube
- Supports qualification and elimination matches (including double-elimination brackets)
- Resumes partial downloads — designed for flaky event WiFi
- Saves settings between sessions (`~/.frc_log_puller/config.json`)
- Only downloads event match logs — practice and test logs are ignored

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Your laptop must be on the same network as the roboRIO (e.g., connected to the robot's radio or the field network)

## Installation

```bash
git clone <repo-url>
cd frc_log_puller
uv sync
```

## Usage

```bash
uv run python log_puller.py
```

This opens a GUI window with the following settings:

| Setting | Description |
|---------|-------------|
| **Team #** | Your FRC team number. Used to determine the roboRIO IP address (`10.XX.YY.2`). |
| **Save to** | Local directory where log files and videos are saved. |
| **TBA Key** | Your [The Blue Alliance API key](https://www.thebluealliance.com/account). Required for video downloads. Optional if you only want log files. |

Once configured, the app will:

1. Continuously attempt to connect to the roboRIO via SFTP
2. Show a green dot when connected, red when disconnected
3. List all match logs found on the robot
4. Automatically download any new match logs to your save directory
5. If a TBA API key is set, look up and download the YouTube video for each match

No manual triggering is needed — just leave it running during an event and it will pull logs and videos as matches are played.

## Network Notes

At FRC events, internet access (needed for TBA and YouTube) is often unavailable on the field network. You may need to:

- Use a VPN to route internet traffic
- Or download videos later when you have internet access

The app will show a reminder dialog if it can't reach TBA or YouTube.

## Building a Windows Executable

On a Windows machine:

```bash
uv sync --group dev
uv run python build.py
```

This produces a standalone `dist/FRC_Log_Puller.exe` using PyInstaller.

## How It Works

The app connects to the roboRIO at `10.XX.YY.2` (where XX.YY is derived from the team number) as the `lvuser` user with no password, which is the default FRC roboRIO configuration.

It polls the `/home/lvuser/logs` directory for files matching the pattern `FRC_{date}_{time}_{EVENT}_{Q|E}{num}.wpilog`. When a new match log is found, it downloads it using a `.part` suffix for atomicity — if a download is interrupted, it won't leave a corrupt file behind.

For video downloads, the app maps the event code and match number from the log filename to a TBA match key, fetches the associated YouTube video IDs, and downloads the best available MP4 using yt-dlp.

## License

This project is not currently published under a formal license.
