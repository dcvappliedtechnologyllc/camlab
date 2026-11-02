"""Clip pulling and frame extraction. Nothing here ever deletes from the card."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .camera.base import CameraBackend


class ClipStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self.index: list[dict[str, Any]] = []
        if self.index_path.exists():
            try:
                self.index = json.loads(self.index_path.read_text() or "[]")
            except json.JSONDecodeError:
                self.index = []

    def _save(self) -> None:
        self.index_path.write_text(json.dumps(self.index, indent=2, default=str))

    def get(self, clip_id: str) -> dict[str, Any] | None:
        return next((c for c in self.index if c["id"] == clip_id or c["name"] == clip_id
                     or c.get("label") == clip_id or os.path.basename(c.get("path", "")) == clip_id), None)

    def next_seq(self, tag: str) -> int:
        return max((int(c.get("seq") or 0) for c in self.index if c.get("tag") == tag), default=0) + 1

    def pull(self, camera: CameraBackend, since: float | None = None, tag: str | None = None) -> list[dict[str, Any]]:
        """Copy new clips from the card (never deletes). With a tag, each file is saved as
        <tag>-NNN.MP4 with NNN counting up per tag; the card's own name stays in the index."""
        pulled: list[dict[str, Any]] = []
        known = {c["name"] for c in self.index}          # card names are unique per card; sizes can drift between listings
        day_dir = self.root / time.strftime("%Y-%m-%d")
        for clip in camera.list_clips():
            if clip["name"] in known:
                continue
            if since and clip.get("mtime") and clip["mtime"] < since:
                continue
            path = camera.pull_clip(clip, str(day_dir))
            entry = {"id": f"{time.strftime('%Y-%m-%d')}_{clip['name']}", "name": clip["name"],
                     "path": path, "size": os.path.getsize(path) if os.path.exists(path) else clip.get("size"),
                     "card_size": clip.get("size"), "pulled": time.time(), "duration_s": probe_duration(path)}
            if tag:
                seq = self.next_seq(tag)
                ext = os.path.splitext(path)[1] or ".MP4"
                label = f"{tag}-{seq:03d}{ext}"
                dest = os.path.join(os.path.dirname(path), label)
                if os.path.exists(path) and not os.path.exists(dest):
                    os.replace(path, dest)
                    entry["path"] = dest
                entry.update({"tag": tag, "seq": seq, "label": label})
            self.index.append(entry)
            pulled.append(entry)
        if pulled:
            self._save()
        return pulled

    def pull_reused(self, camera: CameraBackend, name: str, tag: str | None = None) -> dict[str, Any] | None:
        """The card handed out `name` again (counter reset) and its size differs from the indexed copy:
        pull it as a new file, <tag>-NNN or <stem>-N, keeping the earlier copy. None if the sizes match."""
        old = [c for c in self.index if c["name"] == name]
        card = next((c for c in camera.list_clips() if c["name"] == name), None)
        if not old or card is None or any((c.get("card_size") or c.get("size")) == card.get("size") for c in old):
            return None
        day_dir = self.root / time.strftime("%Y-%m-%d")
        path = camera.pull_clip(card, str(day_dir))
        stem, ext = os.path.splitext(os.path.basename(path)); ext = ext or ".MP4"
        entry = {"name": name, "size": os.path.getsize(path) if os.path.exists(path) else card.get("size"),
                 "card_size": card.get("size"), "pulled": time.time(), "duration_s": probe_duration(path)}
        if tag:
            seq = self.next_seq(tag); label = f"{tag}-{seq:03d}{ext}"
            entry.update({"tag": tag, "seq": seq})
        else:
            n = 2
            while (Path(path).parent / f"{stem}-{n}{ext}").exists() or any(c.get("label") == f"{stem}-{n}{ext}" for c in self.index):
                n += 1
            label = f"{stem}-{n}{ext}"
        dest = os.path.join(os.path.dirname(path), label)
        if os.path.exists(path) and not os.path.exists(dest):
            os.replace(path, dest)
        entry.update({"path": dest, "label": label, "id": f"{time.strftime('%Y-%m-%d')}_{label}"})
        self.index.append(entry)
        self._save()
        return entry

    def extract_frames(self, clip_id: str, timestamps: list[float], out_dir: str | Path) -> list[dict[str, Any]]:
        clip = self.get(clip_id)
        if not clip:
            raise FileNotFoundError(f"clip {clip_id!r} not pulled; call pull_clips first")
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg not found on PATH")
        out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
        results = []
        for t in timestamps:
            dest = out / f"{Path(clip['name']).stem}_{t:07.2f}s.jpg"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", clip["path"],
                            "-frames:v", "1", "-q:v", "2", str(dest)], check=True, timeout=60)
            results.append({"t": t, "path": str(dest)})
        return results


def probe_duration(path: str) -> float | None:
    if not shutil.which("ffprobe"):
        return None
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                            "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=20)
        return round(float(r.stdout.strip()), 2)
    except Exception:
        return None
