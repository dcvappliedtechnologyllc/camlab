"""Manual light: not scriptable (e.g. LP800Bi, Bluetooth app only). Records
what the user set by hand so recipes stay complete."""
from __future__ import annotations

from .base import LightBackend


class ManualLight(LightBackend):
    def connect(self) -> bool:
        return True

    def apply(self, intensity_pct: float, cct_k: int) -> None:
        # Nothing to push; the user adjusts the fixture. We just remember it.
        return None

    @property
    def scriptable(self) -> bool:
        return False
