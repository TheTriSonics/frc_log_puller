"""SFTP client for connecting to the roboRIO and downloading log files."""

import os
import re
import logging
import threading
import time
from pathlib import Path

import paramiko

logger = logging.getLogger(__name__)

# Only match event log files: FRC_{date}_{time}_{EVENT}_{Q|E}{num}.wpilog
# Optionally with a suffix like -with-drift-detect
MATCH_LOG_RE = re.compile(
    r"^FRC_(\d{8})_(\d{6})_([A-Z0-9]+)_(Q|E)(\d+)(?:-.+)?\.wpilog$",
    re.IGNORECASE,
)


def team_to_ip(team_number: int) -> str:
    """Convert an FRC team number to the roboRIO IP address.

    Format: 10.XX.YY.2 where XXYY comes from the team number.
    Team 4003  -> 10.40.03.2
    Team 11001 -> 10.110.01.2
    """
    xx = team_number // 100
    yy = team_number % 100
    return f"10.{xx}.{yy}.2"


def is_match_log(filename: str) -> bool:
    """Return True if the filename matches the event match log pattern."""
    return MATCH_LOG_RE.match(filename) is not None


def parse_match_log(filename: str) -> dict | None:
    """Parse a match log filename into its components.

    Returns dict with keys: date, time, event, match_type, match_number, year
    or None if the filename doesn't match.
    """
    m = MATCH_LOG_RE.match(filename)
    if not m:
        return None
    date_str, time_str, event, match_type, match_num = m.groups()
    return {
        "date": date_str,
        "time": time_str,
        "event": event,
        "match_type": match_type.upper(),
        "match_number": int(match_num),
        "year": int(date_str[:4]),
        "filename": filename,
    }


class SFTPClient:
    """Manages SSH/SFTP connection to the roboRIO."""

    REMOTE_LOG_DIR = "/home/lvuser/logs"
    USERNAME = "lvuser"
    CONNECT_TIMEOUT = 5
    # roboRIO lvuser has no password by default
    PASSWORD = ""

    def __init__(self, team_number: int):
        self.team_number = team_number
        self.ip = team_to_ip(team_number)
        self._ssh: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None
        self._lock = threading.Lock()

    @property
    def connected(self) -> bool:
        with self._lock:
            if self._ssh is None or self._sftp is None:
                return False
            try:
                self._sftp.stat(self.REMOTE_LOG_DIR)
                return True
            except Exception:
                self._close_unlocked()
                return False

    def connect(self) -> bool:
        """Attempt to connect to the roboRIO. Returns True on success."""
        with self._lock:
            if self._ssh is not None:
                try:
                    self._sftp.stat(self.REMOTE_LOG_DIR)
                    return True
                except Exception:
                    self._close_unlocked()

            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(
                    self.ip,
                    username=self.USERNAME,
                    password=self.PASSWORD,
                    timeout=self.CONNECT_TIMEOUT,
                    allow_agent=False,
                    look_for_keys=False,
                    banner_timeout=self.CONNECT_TIMEOUT,
                    auth_timeout=self.CONNECT_TIMEOUT,
                )
                self._ssh = ssh
                self._sftp = ssh.open_sftp()
                logger.info("Connected to roboRIO at %s", self.ip)
                return True
            except Exception as e:
                logger.debug("Connection failed to %s: %s", self.ip, e)
                self._close_unlocked()
                return False

    def disconnect(self):
        with self._lock:
            self._close_unlocked()

    def _close_unlocked(self):
        if self._sftp:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None
        if self._ssh:
            try:
                self._ssh.close()
            except Exception:
                pass
            self._ssh = None

    def list_match_logs(self) -> list[dict]:
        """List match log files on the roboRIO.

        Returns a list of parsed match log dicts, sorted by filename.
        Only includes files matching the event match pattern.
        """
        with self._lock:
            if self._sftp is None:
                return []
            try:
                entries = self._sftp.listdir_attr(self.REMOTE_LOG_DIR)
            except Exception as e:
                logger.error("Failed to list remote logs: %s", e)
                self._close_unlocked()
                return []

        results = []
        for entry in entries:
            parsed = parse_match_log(entry.filename)
            if parsed:
                parsed["size"] = entry.st_size or 0
                parsed["mtime"] = entry.st_mtime or 0
                results.append(parsed)

        results.sort(key=lambda x: x["filename"])
        return results

    def download_file(
        self,
        remote_filename: str,
        local_dir: str | Path,
        progress_callback=None,
    ) -> Path:
        """Download a file from the roboRIO logs directory.

        Uses a .part suffix during download for atomicity.
        Calls progress_callback(bytes_transferred, total_bytes) if provided.

        Returns the final local path on success.
        Raises on failure.
        """
        local_dir = Path(local_dir)
        local_dir.mkdir(parents=True, exist_ok=True)
        final_path = local_dir / remote_filename
        part_path = local_dir / f"{remote_filename}.part"
        remote_path = f"{self.REMOTE_LOG_DIR}/{remote_filename}"

        with self._lock:
            if self._sftp is None:
                raise ConnectionError("Not connected to roboRIO")
            try:
                self._sftp.get(
                    remote_path,
                    str(part_path),
                    callback=progress_callback,
                )
            except Exception:
                # Clean up partial file on failure
                if part_path.exists():
                    part_path.unlink()
                self._close_unlocked()
                raise

        # Atomic rename on success
        part_path.rename(final_path)
        logger.info("Downloaded %s", remote_filename)
        return final_path
