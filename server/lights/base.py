"""Light backend interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LightState:
    id: str
    model: str
    backend: str
    connected: bool
    intensity_pct: float
    cct_k: int
    intensity_min_pct: float = 0
    intensity_max_pct: float = 100
    cct_min_k: int = 2800
    cct_max_k: int = 6500
    scriptable: bool = True
    last_ack: dict | None = None     # backends that hear the fixture answer fill this in

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class LightBackend(ABC):
    def __init__(self, light_id: str, profile: dict):
        self.id = light_id
        self.profile = profile
        self.model = profile.get("model", light_id)
        self.lo = float(profile.get("intensity_min_pct", 0))
        self.hi = float(profile.get("intensity_max_pct", 100))
        self.cct_lo = int(profile.get("cct_min_k", 2800))
        self.cct_hi = int(profile.get("cct_max_k", 6500))
        self._intensity = 0.0
        self._cct = (self.cct_lo + self.cct_hi) // 2

    def clamp(self, intensity_pct: float | None, cct_k: int | None) -> tuple[float, int]:
        i = self._intensity if intensity_pct is None else max(self.lo, min(self.hi, float(intensity_pct)))
        c = self._cct if cct_k is None else max(self.cct_lo, min(self.cct_hi, int(cct_k)))
        return i, c

    @abstractmethod
    def connect(self) -> bool: ...

    @abstractmethod
    def apply(self, intensity_pct: float, cct_k: int) -> None:
        """Push values to hardware. Values are already clamped."""

    def set(self, intensity_pct: float | None = None, cct_k: int | None = None) -> LightState:
        i, c = self.clamp(intensity_pct, cct_k)
        self.apply(i, c)
        self._intensity, self._cct = i, c
        return self.state()

    @property
    def connected(self) -> bool:
        return True

    @property
    def scriptable(self) -> bool:
        return True

    def state(self) -> LightState:
        return LightState(
            id=self.id, model=self.model, backend=self.__class__.__name__,
            connected=self.connected, intensity_pct=self._intensity, cct_k=self._cct,
            intensity_min_pct=self.lo, intensity_max_pct=self.hi,
            cct_min_k=self.cct_lo, cct_max_k=self.cct_hi, scriptable=self.scriptable,
        )
