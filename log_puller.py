"""FRC Log Puller — desktop app to pull match logs from roboRIO and match videos
from YouTube via The Blue Alliance API."""

import json
import logging
import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

from sftp_client import SFTPClient, is_match_log, parse_match_log, team_to_ip
from tba_client import TBAClient
from video_downloader import VideoDownloader

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".frc_log_puller"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "team_number": 4003,
    "download_dir": "",
    "tba_api_key": "",
}

POLL_INTERVAL_CONNECTED = 5  # seconds between log checks when connected
POLL_INTERVAL_DISCONNECTED = 5  # seconds between reconnect attempts


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            # Merge with defaults for any missing keys
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


class LogPullerApp:
    def __init__(self):
        self.config = load_config()
        self.sftp: SFTPClient | None = None
        self.tba: TBAClient | None = None
        self.video_dl = VideoDownloader()

        # Track what we've already downloaded or are downloading
        self._downloaded: set[str] = set()
        self._download_queue: list[dict] = []
        self._downloading = False
        self._video_queue: list[dict] = []
        self._downloading_video = False

        # Threads
        self._poll_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        self._build_gui()
        self._apply_config()
        self._scan_existing_downloads()
        self._start_polling()

    def _build_gui(self):
        self.root = tk.Tk()
        self.root.title("FRC Log Puller")
        self.root.geometry("850x600")
        self.root.minsize(700, 450)

        # --- Settings frame ---
        settings_frame = ttk.LabelFrame(self.root, text="Settings", padding=8)
        settings_frame.pack(fill=tk.X, padx=8, pady=(8, 4))

        # Row 0: Team number + IP display
        ttk.Label(settings_frame, text="Team #:").grid(
            row=0, column=0, sticky=tk.W, padx=(0, 4)
        )
        self.team_var = tk.StringVar(value=str(self.config["team_number"]))
        team_entry = ttk.Entry(settings_frame, textvariable=self.team_var, width=8)
        team_entry.grid(row=0, column=1, sticky=tk.W)
        team_entry.bind("<FocusOut>", self._on_team_changed)
        team_entry.bind("<Return>", self._on_team_changed)

        self.ip_label = ttk.Label(settings_frame, text="", foreground="gray")
        self.ip_label.grid(row=0, column=2, sticky=tk.W, padx=(8, 0))

        # Row 0: Connection status (right side)
        self.status_canvas = tk.Canvas(
            settings_frame, width=16, height=16, highlightthickness=0
        )
        self.status_canvas.grid(row=0, column=3, padx=(16, 4))
        self.status_dot = self.status_canvas.create_oval(2, 2, 14, 14, fill="red")
        self.status_text = ttk.Label(settings_frame, text="Disconnected")
        self.status_text.grid(row=0, column=4, sticky=tk.W)

        # Row 1: Download dir
        ttk.Label(settings_frame, text="Save to:").grid(
            row=1, column=0, sticky=tk.W, padx=(0, 4), pady=(4, 0)
        )
        self.dir_var = tk.StringVar(value=self.config["download_dir"])
        dir_entry = ttk.Entry(settings_frame, textvariable=self.dir_var, width=50)
        dir_entry.grid(row=1, column=1, columnspan=3, sticky=tk.EW, pady=(4, 0))
        ttk.Button(settings_frame, text="Browse...", command=self._browse_dir).grid(
            row=1, column=4, padx=(4, 0), pady=(4, 0)
        )

        # Row 2: TBA API key
        ttk.Label(settings_frame, text="TBA Key:").grid(
            row=2, column=0, sticky=tk.W, padx=(0, 4), pady=(4, 0)
        )
        self.tba_var = tk.StringVar(value=self.config["tba_api_key"])
        tba_entry = ttk.Entry(settings_frame, textvariable=self.tba_var, width=50)
        tba_entry.grid(row=2, column=1, columnspan=3, sticky=tk.EW, pady=(4, 0))
        tba_entry.bind("<FocusOut>", self._on_tba_changed)
        tba_entry.bind("<Return>", self._on_tba_changed)

        settings_frame.columnconfigure(3, weight=1)

        # --- Log file list ---
        list_frame = ttk.LabelFrame(self.root, text="Match Logs on Robot", padding=8)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        columns = ("filename", "event", "match", "size", "status")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings")
        self.tree.heading("filename", text="Filename")
        self.tree.heading("event", text="Event")
        self.tree.heading("match", text="Match")
        self.tree.heading("size", text="Size")
        self.tree.heading("status", text="Status")
        self.tree.column("filename", width=350)
        self.tree.column("event", width=80)
        self.tree.column("match", width=60)
        self.tree.column("size", width=80)
        self.tree.column("status", width=150)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # --- Status bar ---
        status_bar = ttk.Frame(self.root, padding=(8, 4))
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)
        self.activity_label = ttk.Label(status_bar, text="Ready")
        self.activity_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _apply_config(self):
        """Apply current config values to the UI and internal state."""
        try:
            team = int(self.team_var.get())
        except ValueError:
            team = 4003
        ip = team_to_ip(team)
        self.ip_label.config(text=f"Robot IP: {ip}")

        # Rebuild TBA client if key changed
        tba_key = self.tba_var.get().strip()
        if tba_key:
            self.tba = TBAClient(tba_key)
        else:
            self.tba = None

    def _save_current_config(self):
        try:
            team = int(self.team_var.get())
        except ValueError:
            team = 4003
        self.config["team_number"] = team
        self.config["download_dir"] = self.dir_var.get().strip()
        self.config["tba_api_key"] = self.tba_var.get().strip()
        save_config(self.config)

    def _on_team_changed(self, event=None):
        self._apply_config()
        self._save_current_config()
        # Reconnect with new team number
        if self.sftp:
            self.sftp.disconnect()
            self.sftp = None

    def _on_tba_changed(self, event=None):
        self._apply_config()
        self._save_current_config()

    def _browse_dir(self):
        d = filedialog.askdirectory(
            title="Select Download Directory",
            initialdir=self.dir_var.get() or str(Path.home()),
        )
        if d:
            self.dir_var.set(d)
            self._save_current_config()
            self._scan_existing_downloads()

    def _scan_existing_downloads(self):
        """Scan the download directory for already-downloaded log files."""
        self._downloaded.clear()
        dl_dir = self.dir_var.get().strip()
        if not dl_dir or not Path(dl_dir).is_dir():
            return
        for f in Path(dl_dir).iterdir():
            if f.suffix == ".wpilog" and is_match_log(f.name):
                self._downloaded.add(f.name)

    def _set_status(self, connected: bool):
        """Update the connection status indicator (must be called from main thread)."""
        if connected:
            self.status_canvas.itemconfig(self.status_dot, fill="#22c55e")
            self.status_text.config(text="Connected")
        else:
            self.status_canvas.itemconfig(self.status_dot, fill="#ef4444")
            self.status_text.config(text="Disconnected")

    def _set_activity(self, text: str):
        """Update the status bar text (must be called from main thread)."""
        self.activity_label.config(text=text)

    def _update_tree(self, logs: list[dict]):
        """Update the treeview with the current log list (main thread)."""
        # Remember existing items
        existing_ids = set(self.tree.get_children())
        current_filenames = set()

        for log in logs:
            fname = log["filename"]
            current_filenames.add(fname)
            match_str = f"{log['match_type']}{log['match_number']}"
            size_mb = log["size"] / (1024 * 1024)
            size_str = f"{size_mb:.1f} MB"

            if fname in self._downloaded:
                status = "Downloaded"
            elif fname in [d["filename"] for d in self._download_queue]:
                status = "Queued..."
            else:
                status = "New"

            values = (fname, log["event"], match_str, size_str, status)

            if fname in existing_ids:
                self.tree.item(fname, values=values)
            else:
                self.tree.insert("", tk.END, iid=fname, values=values)

        # Remove items no longer on the robot
        for iid in existing_ids:
            if iid not in current_filenames:
                self.tree.delete(iid)

    def _start_polling(self):
        """Start the background thread that polls the roboRIO."""
        self._stop_event.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def _poll_loop(self):
        """Background loop: connect, list logs, trigger downloads."""
        while not self._stop_event.is_set():
            try:
                team = int(self.team_var.get())
            except ValueError:
                time.sleep(POLL_INTERVAL_DISCONNECTED)
                continue

            # Create/recreate SFTP client if team changed
            if self.sftp is None or self.sftp.team_number != team:
                if self.sftp:
                    self.sftp.disconnect()
                self.sftp = SFTPClient(team)

            connected = self.sftp.connect()
            self.root.after(0, self._set_status, connected)

            if connected:
                logs = self.sftp.list_match_logs()
                self.root.after(0, self._update_tree, logs)

                # Find new files to download
                dl_dir = self.dir_var.get().strip()
                if dl_dir:
                    new_logs = [
                        log for log in logs
                        if log["filename"] not in self._downloaded
                        and log["filename"] not in [
                            d["filename"] for d in self._download_queue
                        ]
                    ]
                    if new_logs:
                        self._download_queue.extend(new_logs)
                        for log in new_logs:
                            self.root.after(
                                0,
                                self._set_activity,
                                f"New log found: {log['filename']}",
                            )
                        self._maybe_start_download()

                self._stop_event.wait(POLL_INTERVAL_CONNECTED)
            else:
                self._stop_event.wait(POLL_INTERVAL_DISCONNECTED)

    def _maybe_start_download(self):
        """Start downloading the next file in the queue if not already downloading."""
        if self._downloading or not self._download_queue:
            return
        self._downloading = True
        t = threading.Thread(target=self._download_worker, daemon=True)
        t.start()

    def _download_worker(self):
        """Worker thread that processes the download queue."""
        while self._download_queue and not self._stop_event.is_set():
            log = self._download_queue.pop(0)
            fname = log["filename"]
            dl_dir = self.dir_var.get().strip()

            if not dl_dir:
                self.root.after(
                    0, self._set_activity, "No download directory set!"
                )
                break

            self.root.after(0, self._set_activity, f"Downloading {fname}...")
            self.root.after(0, self._update_item_status, fname, "Downloading...")

            def progress_cb(transferred, total, _fname=fname):
                if total > 0:
                    pct = transferred / total * 100
                    self.root.after(
                        0, self._update_item_status, _fname, f"{pct:.0f}%"
                    )

            try:
                self.sftp.download_file(fname, dl_dir, progress_callback=progress_cb)
                self._downloaded.add(fname)
                self.root.after(
                    0, self._update_item_status, fname, "Downloaded"
                )
                self.root.after(
                    0, self._set_activity, f"Download complete: {fname}"
                )
                # Queue video download if TBA is configured
                if self.tba:
                    self._video_queue.append(log)
                    self._maybe_start_video_download()
            except Exception as e:
                logger.error("Download failed for %s: %s", fname, e)
                self.root.after(
                    0, self._update_item_status, fname, "Failed"
                )
                self.root.after(
                    0,
                    self._set_activity,
                    f"Download failed: {fname} — {e}",
                )
                # Re-queue at end for retry on next poll cycle
                # Don't re-queue immediately to avoid tight loop
                self._downloaded.discard(fname)

        self._downloading = False

    def _maybe_start_video_download(self):
        """Start video download worker if not already running."""
        if self._downloading_video or not self._video_queue:
            return
        self._downloading_video = True
        t = threading.Thread(target=self._video_download_worker, daemon=True)
        t.start()

    def _video_download_worker(self):
        """Worker thread that downloads match videos from YouTube."""
        while self._video_queue and not self._stop_event.is_set():
            log = self._video_queue.pop(0)
            fname = log["filename"]
            dl_dir = self.dir_var.get().strip()

            if not dl_dir or not self.tba:
                break

            # Build video output name to match log file
            base_name = Path(fname).stem  # e.g., FRC_20260321_153505_MIBKN_Q5
            video_path = Path(dl_dir) / f"{base_name}.mp4"
            if video_path.exists():
                self.root.after(
                    0, self._set_activity, f"Video already exists: {base_name}.mp4"
                )
                continue

            self.root.after(
                0, self._set_activity, f"Looking up match video for {fname}..."
            )

            try:
                urls = self.tba.get_video_urls_for_log(log)
            except Exception as e:
                error_msg = str(e)
                self.root.after(
                    0,
                    lambda em=error_msg, fn=fname: self._show_internet_error(
                        f"TBA API error for {fn}", em
                    ),
                )
                continue

            if not urls:
                self.root.after(
                    0,
                    self._set_activity,
                    f"No video found on TBA for {fname}",
                )
                continue

            # Download the first (best) video
            url = urls[0]
            self.root.after(
                0, self._set_activity, f"Downloading video for {base_name}..."
            )

            def video_progress(status_str):
                self.root.after(0, self._set_activity, status_str)

            try:
                result = self.video_dl.download(
                    url, dl_dir, base_name, progress_callback=video_progress
                )
                if result:
                    self.root.after(
                        0,
                        self._set_activity,
                        f"Video downloaded: {result.name}",
                    )
                else:
                    self.root.after(
                        0,
                        self._set_activity,
                        f"Video download produced no output for {base_name}",
                    )
            except Exception as e:
                error_msg = str(e)
                self.root.after(
                    0,
                    lambda em=error_msg, bn=base_name: self._show_internet_error(
                        f"YouTube download error for {bn}", em
                    ),
                )

        self._downloading_video = False

    def _show_internet_error(self, title: str, detail: str):
        """Show an error dialog with technical details and VPN reminder."""
        messagebox.showerror(
            title,
            f"{detail}\n\n"
            "-------\n"
            "Reminder: At FRC events you may need to connect to a VPN "
            "to access internet services like The Blue Alliance API "
            "and YouTube. Check your VPN connection and try again.",
        )

    def _update_item_status(self, filename: str, status: str):
        """Update the status column for a specific file in the treeview."""
        try:
            values = list(self.tree.item(filename, "values"))
            values[4] = status
            self.tree.item(filename, values=values)
        except tk.TclError:
            pass  # Item may not exist yet

    def _on_close(self):
        """Clean shutdown."""
        self._stop_event.set()
        self.video_dl.cancel()
        if self.sftp:
            self.sftp.disconnect()
        self._save_current_config()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    app = LogPullerApp()
    app.run()


if __name__ == "__main__":
    main()
