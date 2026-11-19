"""
RelayVSR Batch Upscaler - a small GUI around ComfyUI

- Splits a video into chunks (ffmpeg)
- Sends the chunks to a running ComfyUI instance one by one
- Downloads the results and merges them into a single video
- Saves progress in state.json so an interrupted job can be resumed

Requirements: Python 3.8+, requests, ffmpeg (in PATH or next to this script/exe)
Optional    : tkinterdnd2  ->  drag & drop a video onto the window
"""
import copy
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import requests

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except Exception:
    HAS_DND = False

FROZEN = getattr(sys, "frozen", False)
# Folder of the exe (when frozen) or of this script
BASE = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
BUNDLE = getattr(sys, "_MEIPASS", BASE)  # files embedded into the exe
WF_NAME = "utility_relayvsr_video_x4_upscale.json"

# Use ffmpeg.exe placed next to the program, if present
if os.path.isfile(os.path.join(BASE, "ffmpeg.exe")):
    os.environ["PATH"] = BASE + os.pathsep + os.environ.get("PATH", "")

CONFIG_FILE = os.path.join(BASE, "relayvsr_gui_config.json")
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".avi")
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEFAULTS = {
    "comfy_url": "http://127.0.0.1:8188",
    "workflow": os.path.join(BASE, WF_NAME) if os.path.isfile(os.path.join(BASE, WF_NAME))
    else os.path.join(BUNDLE, WF_NAME),
    "chunk_sec": 10,
}


class Stop(Exception):
    """Raised when the user presses Stop."""


def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_cfg(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def safe_name(s):
    return re.sub(r"[^\w\-]+", "_", s, flags=re.UNICODE).strip("_") or "video"


class Worker:
    def __init__(self, cfg, video, ui_q, stop_evt):
        self.cfg, self.video, self.q, self.stop = cfg, video, ui_q, stop_evt
        self.stem = os.path.splitext(os.path.basename(video))[0]
        self.key = safe_name(self.stem)
        vdir = os.path.dirname(video)
        self.work = os.path.join(vdir, f"{self.stem}_work")
        self.parts_dir = os.path.join(self.work, "parts")
        self.out_dir = os.path.join(self.work, "output")
        self.state_path = os.path.join(self.work, "state.json")
        self.final = os.path.join(vdir, f"{self.stem}_upscaled.mp4")
        self.comfy = cfg["comfy_url"].rstrip("/")
        self.durations = []
        self.save_ids = set()

    # ---------- helpers ----------
    def log(self, m):
        self.q.put(("log", m))

    def progress(self, frac, text=""):
        self.q.put(("progress", frac, text))

    def check_stop(self):
        if self.stop.is_set():
            raise Stop()

    def save_state(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    def run_cmd(self, cmd):
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=NOWIN)
        if p.returncode != 0:
            raise RuntimeError(f"ffmpeg error:\n{p.stderr[-800:]}")

    # ---------- main flow ----------
    def run(self):
        try:
            self._run()
        except Stop:
            self.log("Stopped. Select the same video and press Start to resume.")
            self.q.put(("done", False))
        except Exception as e:
            self.log(f"ERROR: {e}")
            self.q.put(("done", False))

    def _run(self):
        os.makedirs(self.work, exist_ok=True)
        os.makedirs(self.out_dir, exist_ok=True)

        if os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as f:
                self.state = json.load(f)
            self.log("Previous job found, resuming.")
            any_done = any(c["status"] == "done" for c in self.state["chunks"].values())
            if self.state.get("chunk_sec") != self.cfg["chunk_sec"]:
                if any_done:
                    self.log(f"Note: saved chunk length is {self.state.get('chunk_sec')} s and some "
                             f"chunks are finished, so it is kept. Delete '{os.path.basename(self.work)}' "
                             "to start over with a new length.")
                else:
                    self.log("Chunk length changed, splitting the video again.")
                    self.state = self._new_state()
                    self.save_state()
        else:
            self.state = self._new_state()
            self.save_state()

        with open(self.cfg["workflow"], encoding="utf-8") as f:
            self.wf_template = json.load(f)
        self.validate_workflow()

        # 1) split
        if not self.state["split_done"]:
            self.split()
        names = sorted(self.state["chunks"].keys())
        total = len(names)
        self.log(f"{total} chunks in total.")

        # 2) process chunks
        for i, name in enumerate(names):
            self.check_stop()
            c = self.state["chunks"][name]
            out_file = os.path.join(self.out_dir, name)
            # v == 2 marks a verified download
            if c["status"] == "done" and c.get("v") == 2 and os.path.exists(out_file):
                continue
            self.process_chunk(name, i, total)

        # 3) merge
        self.check_stop()
        self.merge(names)
        self.progress(1.0, "Finished: 100%")
        self.log(f"DONE: {self.final}")
        self.q.put(("done", True))

    def _new_state(self):
        return {"video": self.video, "chunk_sec": self.cfg["chunk_sec"],
                "split_done": False, "chunks": {}, "merged": False}

    # ---------- splitting ----------
    def split(self):
        self.log("Splitting video...")
        self.progress(0, "Splitting...")
        if os.path.isdir(self.parts_dir):
            shutil.rmtree(self.parts_dir)
        os.makedirs(self.parts_dir)
        self.run_cmd([
            "ffmpeg", "-y", "-i", self.video, "-map", "0:v", "-map", "0:a?",
            "-c", "copy", "-f", "segment", "-segment_time", str(self.cfg["chunk_sec"]),
            "-reset_timestamps", "1", os.path.join(self.parts_dir, "part_%04d.mp4")])
        files = sorted(f for f in os.listdir(self.parts_dir) if f.endswith(".mp4"))
        if not files:
            raise RuntimeError("No chunks were created.")
        self.state["chunks"] = {f: {"status": "pending", "prompt_id": None} for f in files}
        self.state["split_done"] = True
        self.save_state()
        self.log(f"Created {len(files)} chunks: {self.parts_dir}")

    # ---------- ComfyUI ----------
    def validate_workflow(self):
        wf = self.wf_template
        if "nodes" in wf and "links" in wf:
            raise RuntimeError("This workflow is in UI format. In ComfyUI use "
                               "'Export (API)' to save it in API format.")
        self.save_ids = {k for k, v in wf.items()
                         if isinstance(v, dict) and v.get("class_type") == "SaveVideo"}
        has_load = any(isinstance(v, dict) and v.get("class_type") == "LoadVideo" for v in wf.values())
        if not has_load or not self.save_ids:
            raise RuntimeError("The workflow must contain a LoadVideo and a SaveVideo node.")

    def upload_chunk(self, name):
        """Send the chunk to ComfyUI's input folder through its API.
        Returns the value to use in the LoadVideo node."""
        src = os.path.join(self.parts_dir, name)
        sub = f"videos/{self.key}"
        with open(src, "rb") as fh:
            r = requests.post(f"{self.comfy}/upload/image",
                              files={"image": (name, fh, "video/mp4")},
                              data={"subfolder": sub, "type": "input", "overwrite": "true"},
                              timeout=300)
        if r.status_code == 200:
            j = r.json()
            s = j.get("subfolder") or ""
            return f"{s}/{j['name']}" if s else j["name"]
        raise RuntimeError(f"Uploading {name} to ComfyUI failed (HTTP {r.status_code}): "
                           f"{r.text[:300]}")

    def submit(self, name):
        file_value = self.upload_chunk(name)
        wf = copy.deepcopy(self.wf_template)
        stem = os.path.splitext(name)[0]
        for node in wf.values():
            if not isinstance(node, dict):
                continue
            if node.get("class_type") == "LoadVideo":
                node["inputs"]["file"] = file_value
            elif node.get("class_type") == "SaveVideo":
                node["inputs"]["filename_prefix"] = f"video/RelayVSR_{self.key}_{stem}"
        r = requests.post(f"{self.comfy}/prompt", json={"prompt": wf}, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"ComfyUI rejected the request ({r.status_code}): {r.text[:500]}")
        return r.json()["prompt_id"]

    def prompt_status(self, pid):
        """Returns ('done', outputs) | ('error', msg) | ('running', None) | ('missing', None)"""
        r = requests.get(f"{self.comfy}/history/{pid}", timeout=15)
        data = r.json()
        if pid in data:
            st = data[pid].get("status", {})
            if st.get("status_str") == "error":
                msg = ""
                for m in st.get("messages", []):
                    if isinstance(m, list) and len(m) > 1 and isinstance(m[1], dict) \
                            and m[1].get("exception_message"):
                        msg = m[1]["exception_message"].strip().splitlines()[0]
                return "error", msg or "unknown error (check the ComfyUI console)"
            if st.get("completed"):
                return "done", data[pid].get("outputs", {})
            return "running", None
        q = requests.get(f"{self.comfy}/queue", timeout=15).json()
        for item in q.get("queue_running", []) + q.get("queue_pending", []):
            if len(item) > 1 and item[1] == pid:
                return "running", None
        return "missing", None

    def download_output(self, outputs, dest):
        cands = []
        for node_id, node_out in outputs.items():
            if not isinstance(node_out, dict):
                continue
            for lst in node_out.values():
                if not isinstance(lst, list):
                    continue
                for f in lst:
                    if isinstance(f, dict) and str(f.get("filename", "")).lower().endswith(VIDEO_EXT):
                        cands.append((str(node_id), f))
        # only real outputs; skip input/preview files (e.g. from LoadVideo)
        cands = [(n, f) for n, f in cands if f.get("type", "output") == "output"]
        if not cands:
            raise RuntimeError("No saved video found in the ComfyUI output.")
        # prefer the SaveVideo node
        cands.sort(key=lambda x: 0 if x[0] in self.save_ids else 1)
        f = cands[0][1]
        params = {"filename": f["filename"], "subfolder": f.get("subfolder", ""),
                  "type": f.get("type", "output")}
        self.log(f"Downloading: {f.get('subfolder', '')}/{f['filename']}")
        with requests.get(f"{self.comfy}/view", params=params, stream=True, timeout=60) as r:
            r.raise_for_status()
            tmp = dest + ".part"
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
        os.replace(tmp, dest)

    def process_chunk(self, name, idx, total):
        c = self.state["chunks"][name]
        pid = c.get("prompt_id")
        t0 = time.time()
        no_conn = "Cannot connect to ComfyUI. Is it running?"

        if pid:  # may be left over from a previous session
            try:
                st, _ = self.prompt_status(pid)
            except requests.RequestException:
                raise RuntimeError(no_conn)
            if st in ("missing", "error"):
                pid = None

        if not pid:
            self.log(f"[{idx+1}/{total}] Sending {name}...")
            try:
                pid = self.submit(name)
            except requests.RequestException:
                raise RuntimeError(no_conn)
            c["status"], c["prompt_id"] = "submitted", pid
            self.save_state()
        else:
            self.log(f"[{idx+1}/{total}] {name} is already in ComfyUI, tracking it.")

        while True:
            self.check_stop()
            try:
                st, data = self.prompt_status(pid)
            except requests.RequestException:
                raise RuntimeError("Lost connection to ComfyUI. Start it again and press Start to resume.")
            if st == "error":
                raise RuntimeError(f"ComfyUI failed on {name}: {data}")
            if st == "missing":
                self.log(f"{name} was not found in the queue, sending again.")
                pid = self.submit(name)
                c["prompt_id"] = pid
                self.save_state()
            if st == "done":
                self.download_output(data, os.path.join(self.out_dir, name))
                c["status"] = "done"
                c["v"] = 2
                self.save_state()
                self.durations.append(time.time() - t0)
                break
            # estimated progress inside the current chunk
            frac_in = 0.0
            if self.durations:
                avg = sum(self.durations) / len(self.durations)
                frac_in = min(0.95, (time.time() - t0) / avg)
            frac = (idx + frac_in) / total
            self.progress(frac * 0.95, self._eta_text(idx, total, frac_in))
            time.sleep(2)

        self.progress((idx + 1) / total * 0.95, self._eta_text(idx + 1, total, 0))
        self.log(f"[{idx+1}/{total}] {name} done.")

    def _eta_text(self, done, total, frac_in):
        pct = int((done + frac_in) / total * 95)
        txt = f"{pct}%  ({done}/{total} chunks)"
        if self.durations:
            avg = sum(self.durations) / len(self.durations)
            remain = max(0, (total - done - frac_in) * avg)
            m, s = divmod(int(remain), 60)
            h, m = divmod(m, 60)
            txt += f"  -  about {h}h {m}m left" if h else f"  -  about {m}m {s}s left"
        return txt

    # ---------- merging ----------
    def merge(self, names):
        self.log("Merging chunks...")
        self.progress(0.96, "Merging...")
        lst = os.path.join(self.work, "list.txt")
        with open(lst, "w", encoding="utf-8") as f:
            for n in names:
                p = os.path.join(self.out_dir, n).replace("\\", "/").replace("'", r"'\''")
                f.write(f"file '{p}'\n")
        self.run_cmd(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                      "-c", "copy", "-movflags", "+faststart", self.final])
        os.remove(lst)
        self.state["merged"] = True
        self.save_state()


class App:
    def __init__(self, root):
        self.root = root
        root.title("RelayVSR Batch Upscaler")
        root.geometry("720x560")
        self.cfg = load_cfg()
        self.q = queue.Queue()
        self.stop_evt = threading.Event()
        self.thread = None

        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(root)
        frm.pack(fill="both", expand=True, padx=10, pady=10)
        frm.columnconfigure(1, weight=1)

        self.v_video = tk.StringVar()
        self.v_url = tk.StringVar(value=self.cfg["comfy_url"])
        self.v_wf = tk.StringVar(value=self.cfg["workflow"])
        self.v_sec = tk.StringVar(value=str(self.cfg["chunk_sec"]))

        rows = [("Video", self.v_video, self.pick_video),
                ("ComfyUI address", self.v_url, None),
                ("Workflow (API .json)", self.v_wf, self.pick_wf),
                ("Chunk length (s)", self.v_sec, None)]
        for r, (lbl, var, cmd) in enumerate(rows):
            ttk.Label(frm, text=lbl).grid(row=r, column=0, sticky="w", **pad)
            ttk.Entry(frm, textvariable=var).grid(row=r, column=1, sticky="ew", **pad)
            if cmd:
                ttk.Button(frm, text="Browse...", command=cmd).grid(row=r, column=2, **pad)

        bf = ttk.Frame(frm)
        bf.grid(row=5, column=0, columnspan=3, pady=8)
        self.btn_start = ttk.Button(bf, text="Start / Resume", command=self.start)
        self.btn_start.pack(side="left", padx=6)
        self.btn_stop = ttk.Button(bf, text="Stop", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)

        self.pb = ttk.Progressbar(frm, maximum=100)
        self.pb.grid(row=6, column=0, columnspan=3, sticky="ew", **pad)
        self.lbl = ttk.Label(frm, text="Ready")
        self.lbl.grid(row=7, column=0, columnspan=3, sticky="w", **pad)

        self.txt = tk.Text(frm, height=14, state="disabled", wrap="word")
        self.txt.grid(row=8, column=0, columnspan=3, sticky="nsew", **pad)
        frm.rowconfigure(8, weight=1)

        if HAS_DND:
            root.drop_target_register(DND_FILES)
            root.dnd_bind("<<Drop>>", lambda e: self.v_video.set(root.tk.splitlist(e.data)[0]))
            self.write("Drag & drop is enabled: drop a video onto the window.")
        else:
            self.write("Tip: pip install tkinterdnd2 to enable drag & drop.")

        self.poll()

    def pick_video(self):
        p = filedialog.askopenfilename(filetypes=[("Video", "*.mp4 *.mov *.mkv *.webm *.avi")])
        if p:
            self.v_video.set(p)

    def pick_wf(self):
        p = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if p:
            self.v_wf.set(p)

    def write(self, m):
        self.txt.configure(state="normal")
        self.txt.insert("end", m + "\n")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def start(self):
        video = self.v_video.get().strip().strip('"')
        if not os.path.isfile(video):
            return messagebox.showerror("Error", "Select a valid video file.")
        try:
            sec = int(self.v_sec.get())
            if sec < 1:
                raise ValueError
        except ValueError:
            return messagebox.showerror("Error", "Chunk length must be a positive number.")
        if not os.path.isfile(self.v_wf.get().strip()):
            return messagebox.showerror("Error", "Workflow file not found.")
        if not shutil.which("ffmpeg"):
            return messagebox.showerror("Error", "ffmpeg was not found in PATH.")

        self.cfg.update(comfy_url=self.v_url.get().strip(),
                        workflow=self.v_wf.get().strip(), chunk_sec=sec)
        save_cfg(self.cfg)

        self.stop_evt.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        w = Worker(dict(self.cfg), video, self.q, self.stop_evt)
        self.thread = threading.Thread(target=w.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_evt.set()
        self.write("Stop requested (it will stop at the next checkpoint)...")

    def poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "log":
                    self.write(m[1])
                elif m[0] == "progress":
                    self.pb["value"] = m[1] * 100
                    if m[2]:
                        self.lbl.configure(text=m[2])
                elif m[0] == "done":
                    self.btn_start.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    if m[1]:
                        messagebox.showinfo("Finished", "The video has been merged.")
        except queue.Empty:
            pass
        self.root.after(300, self.poll)


if __name__ == "__main__":
    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
    App(root)
    root.mainloop()
