"""DMX512 light backend (Godox LE300Bi) over Enttec Open DMX USB or Art-Net.

Channel-map assumption (UNVERIFIED): LE300Bi 2-channel basic mode,
ch1 = intensity, ch2 = CCT (low = warm, high = cool). Confirm against the manual.
"""
from __future__ import annotations

import glob
import logging
import socket
import threading
import time

from .base import LightBackend

log = logging.getLogger("camlab.dmx")

ARTNET_PORT = 6454


class NullTransport:
    """Records frames; used for tests and transport = "none"."""

    def __init__(self) -> None:
        self.last_frame: bytes | None = None
        self.frames: list[bytes] = []
        self.is_open = True
        self.max_history = 1000

    def send(self, frame: bytes) -> None:
        self.last_frame = bytes(frame)
        self.frames.append(self.last_frame)
        if len(self.frames) > self.max_history:
            del self.frames[: len(self.frames) - self.max_history]

    def close(self) -> None:
        self.is_open = False


def find_serial_port() -> str:
    patterns = ["/dev/cu.usbserial*", "/dev/cu.usbmodem*", "/dev/ttyUSB*"]
    found: list[str] = []
    for p in patterns:
        found += sorted(glob.glob(p))
    if not found:
        raise RuntimeError("No DMX serial adapter found. Looked for: " + ", ".join(patterns))
    return found[0]


class SerialDMXTransport:
    """Enttec Open DMX USB (FTDI): 250000 baud, 8N2, software-generated BREAK."""

    BREAK_S = 0.00012
    MAB_S = 0.000012

    def __init__(self, port: str = "auto", serial_factory=None) -> None:
        self.port = find_serial_port() if port in (None, "", "auto") else port
        if serial_factory is None:
            import serial  # lazy: module imports without pyserial

            serial_factory = serial.Serial
            kwargs = dict(baudrate=250000, bytesize=serial.EIGHTBITS,
                          parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_TWO,
                          xonxoff=False, rtscts=False, dsrdtr=False)
        else:
            kwargs = dict(baudrate=250000, bytesize=8, parity="N", stopbits=2,
                          xonxoff=False, rtscts=False, dsrdtr=False)
        self._ser = serial_factory(self.port, **kwargs)
        log.info("Opened DMX serial port %s", self.port)

    @property
    def is_open(self) -> bool:
        return bool(self._ser is not None and getattr(self._ser, "is_open", True))

    def send(self, frame: bytes) -> None:
        ser = self._ser
        ser.break_condition = True
        time.sleep(self.BREAK_S)
        ser.break_condition = False
        time.sleep(self.MAB_S)
        ser.write(b"\x00" + bytes(frame))

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None


class ArtNetTransport:
    """Art-Net 4 ArtDmx packets over UDP."""

    def __init__(self, host: str, universe: int = 0, port: int = ARTNET_PORT, sock=None) -> None:
        if not host:
            raise RuntimeError("Art-Net transport requires artnet_host")
        self.host = host
        self.port = port
        self.universe = int(universe) & 0x7FFF
        self._seq = 0
        self._sock = sock or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.is_open = True

    def build_packet(self, frame: bytes) -> bytes:
        self._seq = self._seq % 255 + 1  # 1..255, never 0
        data = bytes(frame)
        return (
            b"Art-Net\x00"
            + b"\x00\x50"                       # OpCode 0x5000 (ArtDmx), little-endian
            + b"\x00\x0e"                       # ProtVer 14, high byte first
            + bytes([self._seq, 0x00])          # Sequence, Physical
            + bytes([self.universe & 0xFF,      # SubUni (low byte)
                     (self.universe >> 8) & 0x7F])  # Net (high 7 bits)
            + len(data).to_bytes(2, "big")      # Length, big-endian
            + data
        )

    def send(self, frame: bytes) -> None:
        try:
            self._sock.sendto(self.build_packet(frame), (self.host, self.port))
        except OSError as e:
            log.warning("Art-Net send failed: %s", e)

    def close(self) -> None:
        self.is_open = False
        try:
            self._sock.close()
        except OSError:
            pass


class DMXUniverse:
    def __init__(self, transport=None) -> None:
        self.transport = transport if transport is not None else NullTransport()
        self._data = bytearray(512)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def set(self, channel: int, value: int) -> None:
        if not 1 <= channel <= 512:
            raise ValueError(f"DMX channel out of range: {channel}")
        with self._lock:
            self._data[channel - 1] = max(0, min(255, int(value)))

    def get(self, channel: int) -> int:
        with self._lock:
            return self._data[channel - 1]

    def snapshot(self) -> bytes:
        with self._lock:
            return bytes(self._data)

    def send(self) -> None:
        try:
            self.transport.send(self.snapshot())
        except Exception as e:  # keep refresh thread alive across glitches
            log.warning("DMX send failed: %s", e)

    @property
    def connected(self) -> bool:
        return bool(getattr(self.transport, "is_open", True))

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, fps: float = 30) -> None:
        if self.running:
            return
        self._stop.clear()
        period = 1.0 / fps

        def loop() -> None:
            nxt = time.monotonic()
            while not self._stop.is_set():
                self.send()
                nxt += period
                delay = nxt - time.monotonic()
                if delay < 0:
                    nxt = time.monotonic()
                    delay = 0
                self._stop.wait(delay)

        self._thread = threading.Thread(target=loop, name="dmx-refresh", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2)
        self._thread = None

    def close(self) -> None:
        self.stop()
        close = getattr(self.transport, "close", None)
        if close:
            close()


def _round_half_up(x: float) -> int:
    return int(x + 0.5)


class DMXLight(LightBackend):
    def __init__(self, light_id: str, profile: dict, universe: DMXUniverse):
        super().__init__(light_id, profile)
        self.universe = universe
        self.start = int(profile.get("dmx_start", 1))
        ch = profile.get("channels", {}) or {}
        self.ch_intensity = int(ch.get("intensity", 1))
        self.ch_cct = int(ch.get("cct", 2))
        fine = ch.get("fine_intensity")
        self.ch_fine = int(fine) if fine else None
        self.cct_inverted = bool(profile.get("cct_inverted", False))

    def _addr(self, rel: int) -> int:
        return self.start + rel - 1

    def connect(self) -> bool:
        ok = self.universe.connected
        if not ok:
            log.warning("DMX transport not open for light %s", self.id)
        return ok

    @property
    def connected(self) -> bool:
        return self.universe.connected

    def apply(self, intensity_pct: float, cct_k: int) -> None:
        pct = max(0.0, min(100.0, float(intensity_pct)))
        if self.ch_fine is not None:
            v16 = _round_half_up(pct * 65535 / 100)
            coarse, fine = v16 >> 8, v16 & 0xFF
        else:
            coarse, fine = _round_half_up(pct * 255 / 100), None
        span = self.cct_hi - self.cct_lo
        frac = 0.0 if span <= 0 else (cct_k - self.cct_lo) / span
        frac = max(0.0, min(1.0, frac))
        if self.cct_inverted:
            frac = 1.0 - frac
        cct_val = _round_half_up(frac * 255)
        self.universe.set(self._addr(self.ch_intensity), coarse)
        if fine is not None:
            self.universe.set(self._addr(self.ch_fine), fine)
        self.universe.set(self._addr(self.ch_cct), cct_val)
        if not self.universe.running:
            self.universe.send()  # no refresh thread: push immediately


def build_universe(dmx_cfg: dict) -> DMXUniverse:
    kind = str(dmx_cfg.get("transport", "none")).lower()
    transport = None
    try:
        if kind == "serial":
            transport = SerialDMXTransport(dmx_cfg.get("port", "auto"))
        elif kind == "artnet":
            transport = ArtNetTransport(dmx_cfg.get("artnet_host", ""), dmx_cfg.get("universe", 0))
        elif kind != "none":
            log.warning("Unknown DMX transport %r; using null transport", kind)
    except Exception as e:
        log.warning("DMX transport %r unavailable (%s); falling back to null transport", kind, e)
        transport = None
    return DMXUniverse(transport or NullTransport())
