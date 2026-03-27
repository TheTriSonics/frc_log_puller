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

        # Per-file video status: {filename: {"tba": str, "video_url": str|None,
        #   "dl": str, "log": dict}}
        # tba: "unchecked" | "checking" | "found" | "not_found" | "error"
        # dl:  "not_downloaded" | "downloading" | "downloaded" | "error"
        self._video_status: dict[str, dict] = {}

        # Threads
        self._poll_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        self._build_gui()
        self._apply_config()
        self._scan_existing_downloads()
        self._show_local_logs()
        self._queue_video_lookups()
        self._start_polling()

    def _build_gui(self):
        self.root = tk.Tk()
        self.root.title("FRC Log Puller")
        self.root.geometry("950x600")
        self.root.minsize(800, 450)

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
        list_frame = ttk.LabelFrame(self.root, text="Match Logs", padding=8)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        columns = ("filename", "event", "match", "size", "status", "tba_video", "video_dl")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings")
        self.tree.heading("filename", text="Filename")
        self.tree.heading("event", text="Event")
        self.tree.heading("match", text="Match")
        self.tree.heading("size", text="Size")
        self.tree.heading("status", text="Log Status")
        self.tree.heading("tba_video", text="TBA Video")
        self.tree.heading("video_dl", text="Video DL")
        self.tree.column("filename", width=300)
        self.tree.column("event", width=80)
        self.tree.column("match", width=60)
        self.tree.column("size", width=70)
        self.tree.column("status", width=100)
        self.tree.column("tba_video", width=110)
        self.tree.column("video_dl", width=110)

        # Row color tags for video status
        self.tree.tag_configure("video_complete", foreground="#16a34a")
        self.tree.tag_configure("video_ready", foreground="#2563eb")
        self.tree.tag_configure("video_unavailable", foreground="#dc2626")
        self.tree.tag_configure("video_error", foreground="#dc2626")

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # --- Status bar with retry button ---
        status_bar = ttk.Frame(self.root, padding=(8, 4))
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)
        self.activity_label = ttk.Label(status_bar, text="Ready")
        self.activity_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(
            status_bar, text="Retry Video Download", command=self._retry_video
        ).pack(side=tk.RIGHT)

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
            # Clear tree and reload from new directory
            for iid in self.tree.get_children():
                self.tree.delete(iid)
            self._scan_existing_downloads()
            self._show_local_logs()
            self._queue_video_lookups()

    def _scan_existing_downloads(self):
        """Scan the download directory for already-downloaded log and video files."""
        self._downloaded.clear()
        self._video_status.clear()
        dl_dir = self.dir_var.get().strip()
        if not dl_dir or not Path(dl_dir).is_dir():
            return

        video_stems = set()
        for f in Path(dl_dir).iterdir():
            if f.suffix == ".mp4":
                video_stems.add(f.stem)

        for f in Path(dl_dir).iterdir():
            if f.suffix == ".wpilog" and is_match_log(f.name):
                self._downloaded.add(f.name)
                log = parse_match_log(f.name)
                if log:
                    log["size"] = f.stat().st_size
                    has_video = f.stem in video_stems
                    self._video_status[f.name] = {
                        "tba": "found" if has_video else "unchecked",
                        "video_url": None,
                        "dl": "downloaded" if has_video else "not_downloaded",
                        "log": log,
                    }

    def _show_local_logs(self):
        """Populate the treeview with locally downloaded log files."""
        existing_ids = set(self.tree.get_children())
        dl_dir = self.dir_var.get().strip()

        for fname, vs in self._video_status.items():
            log = vs["log"]
            match_str = f"{log['match_type']}{log['match_number']}"
            size_mb = log.get("size", 0) / (1024 * 1024)
            size_str = f"{size_mb:.1f} MB"

            tba = vs.get("tba", "unchecked")
            dl = vs.get("dl", "not_downloaded")
            tba_text = self._tba_display(tba)
            dl_text = self._dl_display(dl)
            tag = self._video_row_tag(tba, dl)

            values = (fname, log["event"], match_str, size_str, "Downloaded",
                      tba_text, dl_text)

            if fname in existing_ids:
                self.tree.item(fname, values=values, tags=(tag,) if tag else ())
            else:
                self.tree.insert(
                    "", tk.END, iid=fname, values=values,
                    tags=(tag,) if tag else (),
                )

    def _queue_video_lookups(self):
        """Queue TBA lookups for local logs that don't have videos yet."""
        if not self.tba:
            return
        for fname, vs in self._video_status.items():
            if vs["dl"] != "downloaded" and vs["tba"] == "unchecked":
                self._video_queue.append(vs["log"])
        self._maybe_start_video_download()

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

    @staticmethod
    def _tba_display(status: str) -> str:
        """Return display text for a TBA video availability status."""
        if status == "found":
            return "\u25cf Found"
        if status == "not_found":
            return "\u25cf Not Found"
        if status == "error":
            return "\u25cf Error"
        if status == "checking":
            return "Checking..."
        return "\u2014"

    @staticmethod
    def _dl_display(status: str) -> str:
        """Return display text for a video download status."""
        if status == "downloaded":
            return "\u25cf Downloaded"
        if status == "downloading":
            return "Downloading..."
        if status == "error":
            return "\u25cf Error"
        return "\u2014"

    @staticmethod
    def _video_row_tag(tba: str, dl: str) -> str:
        """Return the row color tag based on combined video status."""
        if dl == "downloaded":
            return "video_complete"
        if tba == "found":
            return "video_ready"
        if tba == "not_found":
            return "video_unavailable"
        if tba == "error" or dl == "error":
            return "video_error"
        return ""

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

            vs = self._video_status.get(fname, {})
            tba = vs.get("tba", "unchecked")
            dl = vs.get("dl", "not_downloaded")
            tba_text = self._tba_display(tba)
            dl_text = self._dl_display(dl)
            tag = self._video_row_tag(tba, dl)

            values = (fname, log["event"], match_str, size_str, status, tba_text, dl_text)

            if fname in existing_ids:
                self.tree.item(fname, values=values, tags=(tag,) if tag else ())
            else:
                self.tree.insert(
                    "", tk.END, iid=fname, values=values,
                    tags=(tag,) if tag else (),
                )

        # Remove items no longer on the robot and not locally downloaded
        for iid in existing_ids:
            if iid not in current_filenames and iid not in self._downloaded:
                self.tree.delete(iid)

    def _refresh_video_status(self, fname: str):
        """Update video status columns and row tag for a file (main thread)."""
        vs = self._video_status.get(fname, {})
        tba = vs.get("tba", "unchecked")
        dl = vs.get("dl", "not_downloaded")
        tag = self._video_row_tag(tba, dl)
        try:
            values = list(self.tree.item(fname, "values"))
            values[5] = self._tba_display(tba)
            values[6] = self._dl_display(dl)
            self.tree.item(fname, values=values, tags=(tag,) if tag else ())
        except tk.TclError:
            pass

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
                # Initialize video status tracking
                self._video_status[fname] = {
                    "tba": "unchecked",
                    "video_url": None,
                    "dl": "not_downloaded",
                    "log": log,
                }
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
        """Worker thread that looks up and downloads match videos."""
        while self._video_queue and not self._stop_event.is_set():
            log = self._video_queue.pop(0)
            fname = log["filename"]
            dl_dir = self.dir_var.get().strip()

            if not dl_dir or not self.tba:
                break

            # Ensure video status entry exists
            if fname not in self._video_status:
                self._video_status[fname] = {
                    "tba": "unchecked", "video_url": None,
                    "dl": "not_downloaded", "log": log,
                }

            # Check if video already downloaded
            base_name = Path(fname).stem
            video_path = Path(dl_dir) / f"{base_name}.mp4"
            if video_path.exists():
                self._video_status[fname]["tba"] = "found"
                self._video_status[fname]["dl"] = "downloaded"
                self.root.after(0, self._refresh_video_status, fname)
                self.root.after(
                    0, self._set_activity, f"Video already exists: {base_name}.mp4"
                )
                continue

            # TBA lookup
            self._video_status[fname]["tba"] = "checking"
            self.root.after(0, self._refresh_video_status, fname)
            self.root.after(
                0, self._set_activity, f"Looking up match video for {fname}..."
            )

            try:
                urls = self.tba.get_video_urls_for_log(log)
            except Exception as e:
                self._video_status[fname]["tba"] = "error"
                self.root.after(0, self._refresh_video_status, fname)
                error_msg = str(e)
                self.root.after(
                    0,
                    lambda em=error_msg, fn=fname: self._show_internet_error(
                        f"TBA API error for {fn}", em
                    ),
                )
                continue

            if not urls:
                self._video_status[fname]["tba"] = "not_found"
                self.root.after(0, self._refresh_video_status, fname)
                self.root.after(
                    0,
                    self._set_activity,
                    f"No video found on TBA for {fname}",
                )
                continue

            # Video found on TBA
            self._video_status[fname]["tba"] = "found"
            self._video_status[fname]["video_url"] = urls[0]
            self.root.after(0, self._refresh_video_status, fname)

            # Download the video
            url = urls[0]
            self._video_status[fname]["dl"] = "downloading"
            self.root.after(0, self._refresh_video_status, fname)
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
                    self._video_status[fname]["dl"] = "downloaded"
                    self.root.after(0, self._refresh_video_status, fname)
                    self.root.after(
                        0,
                        self._set_activity,
                        f"Video downloaded: {result.name}",
                    )
                else:
                    self._video_status[fname]["dl"] = "error"
                    self.root.after(0, self._refresh_video_status, fname)
                    self.root.after(
                        0,
                        self._set_activity,
                        f"Video download produced no output for {base_name}",
                    )
            except Exception as e:
                self._video_status[fname]["dl"] = "error"
                self.root.after(0, self._refresh_video_status, fname)
                error_msg = str(e)
                self.root.after(
                    0,
                    lambda em=error_msg, bn=base_name: self._show_internet_error(
                        f"YouTube download error for {bn}", em
                    ),
                )

        self._downloading_video = False

    def _retry_video(self):
        """Retry TBA lookup and video download for selected items."""
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo(
                "Retry Video", "Select one or more log files to retry."
            )
            return
        if not self.tba:
            messagebox.showwarning(
                "Retry Video", "TBA API key is not configured."
            )
            return
        dl_dir = self.dir_var.get().strip()
        if not dl_dir:
            messagebox.showwarning("Retry Video", "No download directory set.")
            return

        queued = 0
        for fname in selected:
            vs = self._video_status.get(fname)
            if not vs:
                continue
            # Skip if already downloaded or currently in progress
            if vs["dl"] == "downloaded":
                continue
            if vs["dl"] == "downloading" or vs["tba"] == "checking":
                continue
            # Skip if log not downloaded yet
            if fname not in self._downloaded:
                continue
            # Reset and re-queue
            vs["tba"] = "unchecked"
            vs["video_url"] = None
            vs["dl"] = "not_downloaded"
            self.root.after(0, self._refresh_video_status, fname)
            self._video_queue.append(vs["log"])
            queued += 1

        if queued:
            self._set_activity(f"Queued {queued} video(s) for retry")
            self._maybe_start_video_download()

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
