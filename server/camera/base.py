"""Camera backend interface. All backends are synchronous; the server wraps
them in a thread executor. Setting values are strings exactly as the camera
names them ("1/125", "f/5.6", "400", "5600"), so recipes are portable."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Frame:
    jpeg: bytes
    width: int
    height: int
    orientation: str = "landscape"   # landscape | portrait


@dataclass
class CameraInfo:
    model: str
    backend: str
    connected: bool
    lens: str | None = None
    focal_mm: float | None = None
    mode: str | None = None          # e.g. "movie", "M", "Av"
    capabilities: dict[str, Any] = field(default_factory=dict)
    # capabilities keys used by the server:
    #   iso: [..], shutter: [..], aperture: [..], picture_style: [..],
    #   wb_mode: [..], kelvin_range: [lo, hi], movie_modes: [{resolution,fps}],
    #   focus_drive: bool, af_point: bool, record: bool, liveview: bool, raw_jpeg: bool


class CameraError(RuntimeError):
    pass


class CameraBackend(ABC):
    name: str = "base"

    @abstractmethod
    def connect(self) -> CameraInfo: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def info(self) -> CameraInfo: ...

    @abstractmethod
    def get_settings(self) -> dict[str, Any]:
        """Return current values: iso, shutter, aperture, ev_comp, wb_mode,
        kelvin, picture_style, focus_mode, movie_resolution, movie_fps,
        orientation. Missing keys are allowed; the server fills None."""

    @abstractmethod
    def set_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Apply any subset of the keys above. Return the values actually
        applied (camera may snap to nearest legal step)."""

    @abstractmethod
    def capture_frame(self) -> Frame:
        """Grab one live-view JPEG."""

    def set_exposure_mode(self, mode: str) -> bool:
        """Try to put the body in this exposure mode ("manual"). Returns True when the
        write was accepted; the caller re-reads info().mode to see whether it held.
        Default: not possible from here (the top dial decides)."""
        return False

    def focus_drive(self, step: str) -> None:
        """step in near3,near2,near1,far1,far2,far3. Optional."""
        raise CameraError(f"{self.name}: focus drive unsupported")

    def set_af_point(self, x: float, y: float) -> None:
        raise CameraError(f"{self.name}: af point unsupported")

    def record(self, action: str) -> dict[str, Any]:
        """action start|stop. Return {"recording": bool, "clip": str|None}."""
        raise CameraError(f"{self.name}: record unsupported")

    def list_clips(self) -> list[dict[str, Any]]:
        """Return [{name, folder, size, mtime}] for movie files on the card."""
        return []

    def pull_clip(self, clip: dict[str, Any], dest_dir: str) -> str:
        """Copy a clip from card to dest_dir; return local path. Never delete."""
        raise CameraError(f"{self.name}: pull unsupported")

    # ---- stills ----
    def still_mode(self) -> bool | None:
        """Is the body set up for still photos? True = photo, False = movie, None = cannot tell.
        The photo/movie choice is a physical control on Canon bodies; backends only report it."""
        return None

    def set_image_quality(self, raw: bool = False) -> dict[str, Any]:
        """raw=True asks for RAW+JPEG, False for JPEG only. Optional (capabilities["raw_jpeg"])."""
        raise CameraError(f"{self.name}: image quality unsupported")

    def take_picture(self, dest_dir: str) -> dict[str, Any]:
        """Take one still to the card and copy it into dest_dir. Return {name, path, raw_path|None}.
        The card copy stays where it is: nothing here deletes."""
        raise CameraError(f"{self.name}: still capture unsupported")
