"""Godox Bluetooth Mesh light backend (Godox LP800Bi fill panel).

The Godox Light phone app (com.godox.ble.mesh) drives its lights over
standard Bluetooth SIG Mesh on a Telink radio. The light output is NOT
changed by the standard Generic OnOff / Light CTL models; it only answers to
Godox's own vendor opcode (0x0211F0, company id 0x0211) carrying an 8-byte
"V2" frame. Everything here is a port of the protocol work in:

  - github.com/SegulaLabs/ha-godox-ble-mesh  (MIT; verified on TL60; the mesh
    crypto below is their meshcrypto.py, kept line-for-line)
  - github.com/TheAlexLichter/godox-tl        (MIT; TL30; its captured wire
    fixtures are what tests/test_godox_mesh.py checks this file against)
  - github.com/mattharrison/godox-ul60bi-bt   (MIT; the original Python tool)
  - github.com/binary-person/ha-godox-mesh    (MIT; its docs/firmware/mcu/c8.md
    traces the LP800Bi's own MCU firmware, LP800Bi_V102.bin: the 0xF0 frame is
    [brightness <=100, CCT/100 in 18..100, tint, ignored, ignored], output
    clamped to 2800-6500 K, no green/magenta support (tint byte stays 50), and
    0xF0 clears the off flag, so a CCT command turns the panel on. 0xFE 01 is
    off. That is exactly what godox_cct()/godox_power() emit. Read from code,
    not yet tested on a real LP800Bi.)

What we need from the user: the Godox app's mesh configuration export (the
Bluetooth Mesh "CDB" JSON the app keeps in its own database, see README).
From it we take netKeys[0], appKeys[0] and the unicast address of the panel.
We never provision and never touch device keys, so the panel's pairing with
the phone app is left alone.

Layers in this file, lowest first:
  godox_frame / godox_cct / godox_power   8-byte vendor payload (+ CRC-8/MAXIM)
  NetKey / AppKey / encrypt_*             Mesh Profile 1.0 s3.8 crypto
  MeshNetwork                             sequence counter + PDU builders
  load_mesh_export                        parse the app export (.json or .db)
  GodoxMeshClient                         one GATT proxy connection (bleak)
  GodoxMeshLight                          the camlab LightBackend

bleak and cryptography are imported lazily so the server imports this module
(and starts) without them; the controller falls back to ManualLight when
construction fails.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .base import LightBackend

log = logging.getLogger("camlab.godox_mesh")

PROXY_SVC_UUID = "00001828-0000-1000-8000-00805f9b34fb"
DATA_IN_UUID = "00002add-0000-1000-8000-00805f9b34fb"
DATA_OUT_UUID = "00002ade-0000-1000-8000-00805f9b34fb"

GODOX_OPCODE = bytes([0xF0, 0x11, 0x02])  # vendor opcode 0x0211F0, little-endian on the wire
GODOX_STATUS_OPCODE = bytes([0xF1, 0x11, 0x02])  # 0x0211F1: the light's echo / status report
GODOX_COMPANY_ID = "0211"
ALL_NODES = 0xFFFF
APP_SRC = 0x0100              # the phone app's provisioner address; lights may report to it
ECHO_CCT = 0xA0               # `A0 bri K/100 tint FF FF FF` after an F0 command (~0.85 s later on the LP800Bi)
ECHO_POWER = 0xAE             # after FE; binary-person's traces say the radio chip may drop this one
ACK_TIMEOUT = 3.0

PROXY_PDU_NETWORK = 0
PROXY_PDU_BEACON = 1
PROXY_PDU_CONFIG = 2

DEFAULT_SRC = 0x0500          # our provisioner address; app = 0x0100, HA = 0x0400, script = 0x0300
DEFAULT_TTL = 10              # what the original Python tool sends (captured fixtures)
SEQ_PROBE_START = 0x10
SEQ_PROBE_STEPS = (0x40000, 0x50000, 0x60000, 0x80000, 0xA0000, 0xC0000,
                   0x100000, 0x180000, 0x200000, 0x300000, 0x500000)
SEQ_SAFETY_MARGIN = 500_000   # on top of what probing finds
SEQ_RESTART_MARGIN = 64       # on top of the persisted counter (crash headroom)
SEQ_SAVE_EVERY = 16


# --------------------------------------------------------------------------
# Godox vendor frame
# --------------------------------------------------------------------------

def godox_crc(data: bytes) -> int:
    """CRC-8/MAXIM (poly 0x31 reflected = 0x8C), as on every Godox V2 frame."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc


def godox_frame(cmd: int, data: bytes, end: int = 0xFF) -> bytes:
    """8 bytes: cmd, 5 data bytes (0xFF padded), end byte, CRC over the first 7."""
    if len(data) > 5:
        raise ValueError("Godox V2 frame carries at most 5 data bytes")
    body = bytes([cmd & 0xFF]) + bytes(data) + b"\xFF" * (5 - len(data)) + bytes([end & 0xFF])
    return body + bytes([godox_crc(body)])


def godox_power(on: bool) -> bytes:
    """Polarity is reversed on the wire: 0x00 = ON, 0x01 = OFF."""
    return godox_frame(0xFE, bytes([0x00 if on else 0x01]))


def godox_cct(brightness_pct: float, kelvin: int) -> bytes:
    """Brightness 0-100 (tenths go in the end byte) and CCT in 100 K steps.
    gm = 50 means no green/magenta tint."""
    pct = int(max(0, min(100, round(brightness_pct))))
    tenths = int(max(0, min(9, round((float(brightness_pct) - pct) * 10))))
    return godox_frame(0xF0, bytes([pct, (int(kelvin) // 100) & 0xFF, 50, 0, 0]), end=tenths)


STATUS_RECORD_CCT = 0xA0      # last colour-temperature state: [1] brightness, [2] K/100
STATUS_RECORD_LAST = 0x03     # the MCU's most recent frame (current mode)
STATUS_REPLY_TIMEOUT = 2.0    # the radio chip answers from RAM in ~60 ms, once
STATUS_SETTLE_S = 0.6         # a command within 0.5 s before a request makes the reply unreliable


def godox_status_request(record: int = STATUS_RECORD_CCT) -> bytes:
    """`FD 01 FF FF FF FF <record> crc`: ask the light's Telink radio chip for
    the last record of that kind. It answers once on 0x0211F1 (binary-person
    docs/consolidated/protocol.md). Lights do not echo commands unprompted on
    every MCU family, so bring-up and state readback use this."""
    return godox_frame(0xFD, b"\x01", end=record)


def split_opcode(access: bytes) -> tuple[bytes, bytes]:
    """Mesh access opcode (1, 2 or 3 bytes) and parameters."""
    if not access:
        return b"", b""
    if access[0] & 0x80 == 0:
        return access[:1], access[1:]
    if access[0] & 0x40 == 0:
        return access[:2], access[2:]
    return access[:3], access[3:]


def decode_echo(payload: bytes) -> dict[str, Any]:
    """Interpret a 0x0211F1 status payload. Two A0 layouts are known from the
    MCU firmware traces (binary-person/ha-godox-mesh docs/firmware/mcu):
      C8 (LP800Bi):  A0 <bri> <K/100> <tint> FF FF FF
      C1 (LE300Bi):  A0 <bri/10> <K/100> FF <?> <tint> <bri%10>
    Both are reported; `brightness_candidates` holds every reading that could
    be meant so a caller can match either."""
    out: dict[str, Any] = {"hex": payload.hex(), "kind": "unknown"}
    if not payload:
        return out
    cmd = payload[0]
    if cmd == ECHO_CCT and len(payload) >= 3:
        b1, k = payload[1], payload[2]
        cands = {b1}
        if len(payload) >= 7 and payload[6] != 0xFF and b1 <= 10:
            cands.add(b1 * 10 + payload[6])
        out.update(kind="cct", kelvin=k * 100, brightness_candidates=sorted(cands),
                   tint=payload[3] if len(payload) > 3 else None)
    elif cmd == ECHO_POWER:
        out.update(kind="power", raw=payload[1:].hex())
    else:
        out.update(kind=f"0x{cmd:02X}")
    return out


def echo_matches(frame: bytes, payload: bytes) -> bool:
    """Does the status payload acknowledge this command frame?"""
    d = decode_echo(payload)
    if frame[0] == 0xF0:
        return d["kind"] == "cct" and frame[1] in d["brightness_candidates"] and d["kelvin"] == frame[2] * 100
    if frame[0] == 0xFE:
        return d["kind"] == "power"
    return False


# --------------------------------------------------------------------------
# Bluetooth Mesh crypto (Mesh Profile 1.0 s3.8) - from ha-godox-ble-mesh
# --------------------------------------------------------------------------

def cmac(key: bytes, msg: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import algorithms
    from cryptography.hazmat.primitives.cmac import CMAC
    c = CMAC(algorithms.AES(key))
    c.update(msg)
    return c.finalize()


def aes_ecb(key: bytes, block: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    e = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return e.update(block) + e.finalize()


def _ccm(key: bytes, tag_length: int):
    from cryptography.hazmat.primitives.ciphers.aead import AESCCM
    return AESCCM(key, tag_length=tag_length)


def s1(m: bytes) -> bytes:
    return cmac(bytes(16), m)


def k1(n: bytes, salt: bytes, p: bytes) -> bytes:
    return cmac(cmac(salt, n), p)


def k2(n: bytes, p: bytes = b"\x00") -> tuple[int, bytes, bytes]:
    t = cmac(s1(b"smk2"), n)
    t1 = cmac(t, p + b"\x01")
    t2 = cmac(t, t1 + p + b"\x02")
    t3 = cmac(t, t2 + p + b"\x03")
    return t1[15] & 0x7F, t2, t3  # NID, EncryptionKey, PrivacyKey


def k3(n: bytes) -> bytes:
    return cmac(cmac(s1(b"smk3"), n), b"id64\x01")[-8:]


def k4(n: bytes) -> int:
    return cmac(cmac(s1(b"smk4"), n), b"id6\x01")[-1] & 0x3F


class NetKey:
    def __init__(self, key_hex: str):
        self.key = bytes.fromhex(key_hex)
        if len(self.key) != 16:
            raise ValueError("network key must be 16 bytes")
        self.nid, self.enc, self.priv = k2(self.key)
        self.network_id = k3(self.key)
        self.beacon_key = k1(self.key, s1(b"nkbk"), b"id128\x01")


class AppKey:
    def __init__(self, key_hex: str):
        self.key = bytes.fromhex(key_hex)
        if len(self.key) != 16:
            raise ValueError("app key must be 16 bytes")
        self.aid = k4(self.key)


def _pecb(nk: NetKey, iv: int, privacy_random: bytes) -> bytes:
    return aes_ecb(nk.priv, bytes(5) + iv.to_bytes(4, "big") + privacy_random[:7])


def encrypt_network_pdu(nk: NetKey, iv: int, ctl: int, ttl: int, seq: int, src: int,
                        dst: int, transport_pdu: bytes, proxy: bool = False) -> bytes:
    ctl_ttl = (ctl << 7) | (ttl & 0x7F)
    nonce_type = 0x03 if proxy else 0x00
    nonce = bytes([nonce_type, 0 if proxy else ctl_ttl]) + seq.to_bytes(3, "big") + \
        src.to_bytes(2, "big") + bytes(2) + iv.to_bytes(4, "big")
    mic_len = 8 if ctl else 4
    enc = _ccm(nk.enc, mic_len).encrypt(nonce, dst.to_bytes(2, "big") + transport_pdu, None)
    header = bytes([ctl_ttl]) + seq.to_bytes(3, "big") + src.to_bytes(2, "big")
    pecb = _pecb(nk, iv, enc)
    obf = bytes(a ^ b for a, b in zip(header, pecb))
    return bytes([((iv & 1) << 7) | nk.nid]) + obf + enc


def decrypt_network_pdu(nk: NetKey, iv_candidates: Iterable[int], pdu: bytes,
                        proxy: bool = False) -> dict[str, Any] | None:
    """Return a dict or None. Tries each IV index whose LSB matches IVI."""
    from cryptography.exceptions import InvalidTag
    if len(pdu) < 14 or (pdu[0] & 0x7F) != nk.nid:
        return None
    ivi = pdu[0] >> 7
    for iv in iv_candidates:
        if (iv & 1) != ivi:
            continue
        pecb = _pecb(nk, iv, pdu[7:])
        hdr = bytes(a ^ b for a, b in zip(pdu[1:7], pecb))
        ctl, ttl = hdr[0] >> 7, hdr[0] & 0x7F
        seq = int.from_bytes(hdr[1:4], "big")
        src = int.from_bytes(hdr[4:6], "big")
        mic_len = 8 if ctl else 4
        nonce = bytes([0x03 if proxy else 0x00, 0 if proxy else hdr[0]]) + hdr[1:6] + bytes(2) + iv.to_bytes(4, "big")
        try:
            plain = _ccm(nk.enc, mic_len).decrypt(nonce, pdu[7:], None)
        except InvalidTag:
            continue
        return dict(iv=iv, ctl=ctl, ttl=ttl, seq=seq, src=src,
                    dst=int.from_bytes(plain[:2], "big"), transport=plain[2:])
    return None


def app_nonce(device: bool, seq: int, src: int, dst: int, iv: int, szmic: int = 0) -> bytes:
    return bytes([0x02 if device else 0x01, szmic << 7]) + seq.to_bytes(3, "big") + \
        src.to_bytes(2, "big") + dst.to_bytes(2, "big") + iv.to_bytes(4, "big")


def encrypt_access(key: bytes, device: bool, seq: int, src: int, dst: int, iv: int, access: bytes) -> bytes:
    return _ccm(key, 4).encrypt(app_nonce(device, seq, src, dst, iv), access, None)


def decrypt_access(key: bytes, device: bool, seq: int, src: int, dst: int, iv: int,
                   upper: bytes, mic_len: int = 4) -> bytes | None:
    from cryptography.exceptions import InvalidTag
    try:
        return _ccm(key, mic_len).decrypt(
            app_nonce(device, seq, src, dst, iv, 1 if mic_len == 8 else 0), upper, None)
    except InvalidTag:
        return None


def parse_secure_beacon(nk: NetKey, data: bytes) -> tuple[int, int] | None:
    """data starts with beacon type byte 0x01. Returns (iv_index, flags) if authenticated."""
    if len(data) != 22 or data[0] != 0x01:
        return None
    flags, net_id, iv, auth = data[1], data[2:10], data[10:14], data[14:22]
    if net_id != nk.network_id:
        return None
    if cmac(nk.beacon_key, data[1:14])[:8] != auth:
        return None
    return int.from_bytes(iv, "big"), flags


def proxy_segments(pdu_type: int, pdu: bytes, mtu: int = 20) -> list[bytes]:
    """Split a proxy PDU into GATT writes with SAR bits (Mesh Profile s6.3)."""
    room = mtu - 1
    if len(pdu) <= room:
        return [bytes([pdu_type & 0x3F]) + pdu]
    parts = [pdu[i:i + room] for i in range(0, len(pdu), room)]
    out = []
    for i, part in enumerate(parts):
        sar = 1 if i == 0 else (3 if i == len(parts) - 1 else 2)
        out.append(bytes([(sar << 6) | (pdu_type & 0x3F)]) + part)
    return out


# --------------------------------------------------------------------------
# Mesh export (the Godox app's CDB JSON)
# --------------------------------------------------------------------------

@dataclass
class MeshNode:
    name: str
    address: int
    mac: str = ""
    mesh_name: str = ""     # the mesh JSON's own name when `name` came from the app's Node table


@dataclass
class MeshExport:
    network_key: str
    app_key: str
    nodes: list[MeshNode] = field(default_factory=list)
    iv_index: int = 0

    def find(self, want: int | str | None) -> MeshNode:
        if not self.nodes:
            raise ValueError("mesh export lists no Godox lights (cid 0211)")
        if want is None or want == "":
            return self.nodes[0]
        if isinstance(want, int):
            for n in self.nodes:
                if n.address == want:
                    return n
            raise ValueError(f"no node with unicast address 0x{want:04X} in export")
        s = str(want).strip()
        for n in self.nodes:
            if s.lower() in (n.name.lower(), n.mesh_name.lower()) or n.mac.lower() == s.replace(":", "").lower():
                return n
        prefix = [n for n in self.nodes if n.name.lower().startswith(s.lower() + "-")]
        if len(prefix) == 1:  # "LE300Bi" for the app's "LE300Bi-F9D7"
            return prefix[0]
        try:
            addr = int(s, 16) if s.lower().startswith("0x") else int(s)
        except ValueError:
            raise ValueError(f"mesh_address {want!r} matches no node name, MAC or address") from None
        return self.find(addr)


def parse_mesh_json(data: Any) -> MeshExport:
    """Accept the CDB document itself, or a wrapper holding it in `meshJson`."""
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, list):           # several projects: take the one with lights
        for item in data:
            try:
                return parse_mesh_json(item)
            except ValueError:
                continue
        raise ValueError("no project in export contains Godox lights")
    if not isinstance(data, dict):
        raise ValueError("mesh export is not a JSON object")
    if "meshJson" in data and "netKeys" not in data:
        return parse_mesh_json(data["meshJson"])
    try:
        network_key = str(data["netKeys"][0]["key"]).strip().lower()
        app_key = str(data["appKeys"][0]["key"]).strip().lower()
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError("export has no netKeys/appKeys") from e
    if len(network_key) != 32 or len(app_key) != 32:
        raise ValueError("network/app key must be 32 hex chars")
    nodes: list[MeshNode] = []
    provisioner_uuids = {str(p.get("UUID", "")).upper() for p in data.get("provisioners", []) or [] if isinstance(p, dict)}
    for node in data.get("nodes", []) or []:
        if str(node.get("cid", "")).upper() != GODOX_COMPANY_ID:
            continue  # another vendor
        if str(node.get("UUID", "")).upper() in provisioner_uuids or "provisioner" in str(node.get("name", "")).lower():
            continue  # the phone's own provisioner entry (the real app gives it cid 0211 too)
        try:
            address = int(str(node["unicastAddress"]), 16)
        except (KeyError, TypeError, ValueError):
            continue
        nodes.append(MeshNode(name=node.get("name") or f"Godox 0x{address:04X}", address=address,
                              mac=str(node.get("macAddress", "")).replace(":", "").upper()))
    if not nodes:
        raise ValueError("mesh export lists no Godox lights (cid 0211)")
    iv = 0
    try:
        iv = int(str(data.get("ivIndex", 0)), 16) if isinstance(data.get("ivIndex"), str) else int(data.get("ivIndex", 0) or 0)
    except ValueError:
        iv = 0
    return MeshExport(network_key=network_key, app_key=app_key, nodes=nodes, iv_index=iv)


def load_mesh_export(path: str | Path) -> MeshExport:
    """`path` is either the JSON export or the app's GodoxLight.db (SQLite,
    Project table, meshJson column)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"mesh export not found: {p}")
    head = p.read_bytes()[:16]
    if head.startswith(b"SQLite format 3"):
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        names: dict[str, str] = {}
        try:
            rows = [r[0] for r in con.execute("SELECT meshJson FROM Project") if r[0]]
            try:  # the app's display names ("LE300Bi-F9D7") live in Node, not in the mesh JSON
                names = {str(m).replace(":", "").upper(): str(n) for n, m in
                         con.execute("SELECT name, macAddress FROM Node WHERE macAddress IS NOT NULL") if n and m}
            except sqlite3.Error:
                pass
        finally:
            con.close()
        ex = parse_mesh_json([json.loads(r) for r in rows])
        for node in ex.nodes:
            if node.mac in names and names[node.mac] != node.name:
                node.mesh_name, node.name = node.name, names[node.mac]
        return ex
    return parse_mesh_json(json.loads(p.read_text(encoding="utf-8-sig")))


# --------------------------------------------------------------------------
# Mesh network state: keys, sequence counter, PDU builders (no I/O)
# --------------------------------------------------------------------------

class MeshNetwork:
    def __init__(self, network_key: str, app_key: str, src: int = DEFAULT_SRC,
                 seq: int = SEQ_PROBE_START, iv: int = 0, state_path: str | Path | None = None):
        self.nk = NetKey(network_key)
        self.ak = AppKey(app_key)
        self.src = int(src)
        self.iv = int(iv)
        self.seq = int(seq)
        self.state_path = Path(state_path) if state_path else None
        self._sends_since_save = 0
        self.load_state()

    # persistence: the sequence floor only ever rises, so never go backwards
    def load_state(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            d = json.loads(self.state_path.read_text())
            stored = d.get(f"seq_{self.src:04x}", d.get("sequence_number"))
            if stored is not None:
                self.seq = max(self.seq, int(stored) + SEQ_RESTART_MARGIN)
            self.iv = int(d.get("iv_index", self.iv))
        except (ValueError, OSError, json.JSONDecodeError) as e:
            log.warning("mesh state %s unreadable (%s); starting from seq %d", self.state_path, e, self.seq)

    def save_state(self) -> None:
        if not self.state_path:
            return
        try:
            d = {}
            if self.state_path.exists():
                try:
                    d = json.loads(self.state_path.read_text())
                except (ValueError, json.JSONDecodeError):
                    d = {}
            d[f"seq_{self.src:04x}"] = self.seq
            d["sequence_number"] = self.seq
            d["iv_index"] = self.iv
            self.state_path.write_text(json.dumps(d, indent=2))
        except OSError as e:
            log.warning("could not save mesh state to %s: %s", self.state_path, e)

    def next_seq(self) -> int:
        seq = self.seq
        self.seq = (self.seq + 1) & 0xFFFFFF
        self._sends_since_save += 1
        if self._sends_since_save >= SEQ_SAVE_EVERY:
            self._sends_since_save = 0
            self.save_state()
        return seq

    # builders ---------------------------------------------------------
    def beacon_pdu(self) -> bytes:
        body = bytes([0x00]) + self.nk.network_id + self.iv.to_bytes(4, "big")
        return b"\x01" + body + cmac(self.nk.beacon_key, body)[:8]

    def proxy_config_pdu(self, payload: bytes) -> bytes:
        return encrypt_network_pdu(self.nk, self.iv, 1, 0, self.next_seq(), self.src, 0x0000, payload, proxy=True)

    def access_pdu(self, dst: int, access: bytes, ttl: int = DEFAULT_TTL, seq: int | None = None) -> bytes:
        seq = self.next_seq() if seq is None else seq
        upper = encrypt_access(self.ak.key, False, seq, self.src, dst, self.iv, access)
        return encrypt_network_pdu(self.nk, self.iv, 0, ttl, seq, self.src, dst,
                                   bytes([0x40 | self.ak.aid]) + upper)

    def godox_pdu(self, dst: int, frame: bytes, ttl: int = DEFAULT_TTL, seq: int | None = None) -> bytes:
        return self.access_pdu(dst, GODOX_OPCODE + frame, ttl=ttl, seq=seq)

    # decoding (replies / beacons) -------------------------------------
    def decode(self, pdu_type: int, pdu: bytes) -> dict[str, Any] | None:
        if pdu_type == PROXY_PDU_BEACON:
            r = parse_secure_beacon(self.nk, pdu)
            return None if r is None else {"kind": "beacon", "iv": r[0], "flags": r[1]}
        proxy = pdu_type == PROXY_PDU_CONFIG
        d = decrypt_network_pdu(self.nk, (self.iv, self.iv - 1, self.iv + 1), pdu, proxy=proxy)
        if d is None:
            return None
        d["kind"] = "config" if proxy else "network"
        return d


# --------------------------------------------------------------------------
# BLE client (bleak) - one proxy connection
# --------------------------------------------------------------------------

class GodoxMeshError(Exception):
    pass


class GodoxMeshClient:
    """Talks to one proxy node over GATT. `client_factory(address)` must return
    an object with bleak's BleakClient surface (connect, disconnect,
    is_connected, start_notify, write_gatt_char); tests inject a fake."""

    def __init__(self, net: MeshNetwork, ble_address: str | None = None,
                 client_factory: Callable[[str], Any] | None = None,
                 scanner: Callable[[float], Any] | None = None,
                 scan_timeout: float = 6.0, reply_timeout: float = 2.5) -> None:
        self.net = net
        self.ble_address = ble_address
        self._factory = client_factory
        self._scanner = scanner
        self.scan_timeout = scan_timeout
        self.reply_timeout = reply_timeout
        self.client: Any = None
        self.gateway: str | None = None
        self._rx = bytearray()
        self._beacon_event: asyncio.Event | None = None
        self._filter_event: asyncio.Event | None = None
        self._lock: asyncio.Lock | None = None
        self.seq_probed = False
        # echo listener: lights report every command back on opcode 0x0211F1
        self.last_ack: dict[int, tuple[float, bytes]] = {}       # node address -> (time.time(), payload)
        self._ack_waiters: dict[int, list[asyncio.Future]] = {}
        self.on_ack: Callable[[int, bytes], None] | None = None  # optional hook (CLI / tests)

    @property
    def is_connected(self) -> bool:
        c = self.client
        return bool(c is not None and getattr(c, "is_connected", False))

    # discovery --------------------------------------------------------
    async def find_gateways(self) -> list[tuple[str, int, str]]:
        """[(address, rssi, name)] of nodes advertising this mesh's Network ID."""
        if self._scanner is not None:
            found = await self._scanner(self.scan_timeout)
        else:
            from bleak import BleakScanner
            found = await BleakScanner.discover(timeout=self.scan_timeout, return_adv=True)
        out: list[tuple[str, int, str]] = []
        for dev, adv in found.values():
            data = (adv.service_data or {}).get(PROXY_SVC_UUID)
            if not data or len(data) < 9 or data[0] != 0x00:
                continue  # not a Network-ID proxy advert
            if bytes(data[1:9]) != self.net.nk.network_id:
                continue
            out.append((dev.address, int(adv.rssi or -127), dev.name or ""))
        out.sort(key=lambda t: -t[1])
        return out

    # connection -------------------------------------------------------
    async def connect(self) -> None:
        if self.is_connected:
            return
        self._beacon_event = asyncio.Event()
        self._filter_event = asyncio.Event()
        self._lock = self._lock or asyncio.Lock()
        candidates: list[str] = [self.ble_address] if self.ble_address else []
        if not candidates:
            gws = await self.find_gateways()
            if not gws:
                raise GodoxMeshError("no Godox mesh node advertising this network (is the panel on, "
                                     "and is the phone app disconnected?)")
            log.info("godox mesh gateways: %s", ", ".join(f"{a} ({r} dBm)" for a, r, _ in gws))
            candidates = [a for a, _, _ in gws]
        last: Exception | None = None
        for addr in candidates:
            try:
                client = self._factory(addr) if self._factory else self._make_bleak(addr)
                await client.connect()
                await client.start_notify(DATA_OUT_UUID, self._on_notify)
            except Exception as e:  # try the next advertising node
                log.debug("connect to %s failed: %s", addr, e)
                last = e
                continue
            self.client, self.gateway = client, addr
            self._rx = bytearray()
            try:
                await asyncio.wait_for(self._beacon_event.wait(), 3.0)
            except asyncio.TimeoutError:
                log.debug("no secure beacon from %s; assuming IV index %d", addr, self.net.iv)
            await self._write_pdu(PROXY_PDU_BEACON, self.net.beacon_pdu())
            await asyncio.sleep(0.2)
            if not self.seq_probed:
                await self.probe_sequence()
            else:
                await self.setup_filter()
            log.info("godox mesh: connected through %s", addr)
            return
        raise GodoxMeshError(f"could not connect to any mesh node: {last}")

    def _make_bleak(self, addr: str):
        from bleak import BleakClient
        return BleakClient(addr, disconnected_callback=self._on_disconnect, timeout=20.0)

    def _on_disconnect(self, _client) -> None:
        log.warning("godox mesh: gateway %s disconnected", self.gateway)
        self.client = None

    async def disconnect(self) -> None:
        c, self.client = self.client, None
        if c is not None:
            try:
                await c.disconnect()
            except Exception:
                pass

    # proxy filter / sequence probe -------------------------------------
    async def _filter_set_type(self) -> bool:
        assert self._filter_event is not None
        self._filter_event.clear()
        await self._write_pdu(PROXY_PDU_CONFIG, self.net.proxy_config_pdu(bytes([0x00, 0x00])))
        try:
            await asyncio.wait_for(self._filter_event.wait(), self.reply_timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def setup_filter(self) -> bool:
        ok = await self._filter_set_type()
        # accept list: us, broadcast, and the phone app's address (a light that
        # publishes its status report to the app would otherwise be filtered out)
        accept = [self.net.src, ALL_NODES] + ([APP_SRC] if self.net.src != APP_SRC else [])
        await self._write_pdu(PROXY_PDU_CONFIG, self.net.proxy_config_pdu(
            bytes([0x01]) + b"".join(a.to_bytes(2, "big") for a in accept)))
        if not ok:
            log.warning("godox mesh: proxy did not answer the filter setup; sequence number may be stale")
        return ok

    async def probe_sequence(self) -> int:
        """Lights drop anything at or below the last sequence number they saw
        from our address (replay protection). Find a number that is accepted."""
        start = self.net.seq
        for seq in (start, *[s for s in SEQ_PROBE_STEPS if s > start]):
            self.net.seq = seq
            if await self._filter_set_type():
                if seq != start:  # the floor had moved: leave generous headroom above it
                    self.net.seq = seq + SEQ_SAFETY_MARGIN
                self.seq_probed = True
                self.net.save_state()
                log.info("godox mesh: sequence accepted at %d, continuing from %d", seq, self.net.seq)
                await self.setup_filter()
                return seq
        raise GodoxMeshError("no mesh node answered at any sequence number; check the keys in the export")

    # send ---------------------------------------------------------------
    async def _write_pdu(self, pdu_type: int, pdu: bytes) -> None:
        if self.client is None:
            raise GodoxMeshError("not connected")
        for chunk in proxy_segments(pdu_type, pdu):
            await self.client.write_gatt_char(DATA_IN_UUID, chunk, response=False)

    async def send_frame(self, dst: int, frame: bytes, ack_timeout: float | None = None) -> bytes | None:
        """Send one Godox frame. With `ack_timeout`, wait for the node's echo on
        0x0211F1 and return its payload (None on timeout). The waiter is armed
        before the write so an echo that arrives during the write is not lost."""
        assert self._lock is not None
        async with self._lock:
            fut = self._arm_ack(dst) if ack_timeout is not None and dst != ALL_NODES else None
            try:
                await self._write_pdu(PROXY_PDU_NETWORK, self.net.godox_pdu(dst, frame))
            except BaseException:
                if fut is not None:
                    self._disarm_ack(dst, fut)
                raise
        if fut is None:
            return None
        return await self._await_ack(dst, fut, ack_timeout)

    # acks -----------------------------------------------------------------
    def _arm_ack(self, node_addr: int) -> asyncio.Future:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._ack_waiters.setdefault(node_addr, []).append(fut)
        return fut

    def _disarm_ack(self, node_addr: int, fut: asyncio.Future) -> None:
        lst = self._ack_waiters.get(node_addr, [])
        if fut in lst:
            lst.remove(fut)
        if not lst:
            self._ack_waiters.pop(node_addr, None)

    async def _await_ack(self, node_addr: int, fut: asyncio.Future, timeout: float) -> bytes | None:
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._disarm_ack(node_addr, fut)

    async def query_status(self, dst: int, record: int = STATUS_RECORD_CCT,
                           timeout: float = STATUS_REPLY_TIMEOUT) -> bytes | None:
        """Send a status request and return the 0x0211F1 reply payload (None on timeout)."""
        return await self.send_frame(dst, godox_status_request(record), ack_timeout=timeout)

    async def wait_ack(self, node_addr: int, timeout: float = ACK_TIMEOUT) -> bytes | None:
        """Wait for the next 0x0211F1 status payload from `node_addr`; None on timeout."""
        fut = self._arm_ack(node_addr)
        return await self._await_ack(node_addr, fut, timeout)

    def _record_ack(self, src: int, dst: int, payload: bytes) -> None:
        self.last_ack[src] = (time.time(), payload)
        d = decode_echo(payload)
        log.info("godox mesh: echo from 0x%04X (to 0x%04X): %s %s", src, dst, payload.hex(),
                 {k: v for k, v in d.items() if k != "hex"})
        for fut in self._ack_waiters.pop(src, []):
            if not fut.done():
                fut.set_result(payload)
        if self.on_ack:
            try:
                self.on_ack(src, payload)
            except Exception as e:  # a hook must never break the receive path
                log.debug("on_ack hook failed: %s", e)

    # receive --------------------------------------------------------------
    def _on_notify(self, _sender, data: bytearray) -> None:
        if not data:
            return
        sar, typ = data[0] >> 6, data[0] & 0x3F
        if sar == 0:
            self._handle(typ, bytes(data[1:]))
        elif sar == 1:
            self._rx = bytearray(data[1:])
        elif sar == 2:
            self._rx += data[1:]
        else:
            self._rx += data[1:]
            self._handle(typ, bytes(self._rx))
            self._rx = bytearray()

    def _handle(self, typ: int, pdu: bytes) -> None:
        d = self.net.decode(typ, pdu)
        if d is None:
            return
        if d["kind"] == "beacon":
            if d["iv"] != self.net.iv:
                log.info("godox mesh: IV index %d from beacon", d["iv"])
                self.net.iv = d["iv"]
            if self._beacon_event:
                self._beacon_event.set()
        elif d["kind"] == "config":
            if d["transport"][:1] == b"\x03" and self._filter_event:  # Filter Status
                self._filter_event.set()
        elif d["kind"] == "network" and not d["ctl"]:
            self._handle_access(d)

    def _handle_access(self, d: dict[str, Any]) -> None:
        """Upper transport of an access message: unsegmented (SEG=0), app-key
        encrypted (AKF=1) with our AID. Matches the Godox status opcode."""
        t = d["transport"]
        if not t or t[0] & 0x80:
            log.debug("godox mesh: segmented access message from 0x%04X ignored", d["src"])
            return
        if not (t[0] & 0x40) or (t[0] & 0x3F) != self.net.ak.aid:
            return  # device-key encrypted, or another app key
        access = decrypt_access(self.net.ak.key, False, d["seq"], d["src"], d["dst"], d["iv"], t[1:])
        if access is None:
            return
        opcode, params = split_opcode(access)
        if opcode == GODOX_STATUS_OPCODE:
            self._record_ack(d["src"], d["dst"], params)
        else:
            log.debug("godox mesh: access message from 0x%04X opcode %s params %s", d["src"], opcode.hex(), params.hex())


# --------------------------------------------------------------------------
# shared event loop and shared connections
# --------------------------------------------------------------------------

_loop_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_clients: dict[tuple[str, int], GodoxMeshClient] = {}
_client_refs: dict[tuple[str, int], int] = {}


def mesh_loop() -> asyncio.AbstractEventLoop:
    """One event-loop thread for every mesh light in the process: asyncio
    locks/events inside a shared client must all live on the same loop."""
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is None or _loop_thread is None or not _loop_thread.is_alive():
            loop = asyncio.new_event_loop()
            t = threading.Thread(target=loop.run_forever, name="godox-mesh", daemon=True)
            t.start()
            _loop, _loop_thread = loop, t
        return _loop


def bluetooth_usage_described() -> str | None:
    """macOS kills (SIGABRT, TCC) any process that touches CoreBluetooth without an
    NSBluetoothAlwaysUsageDescription in its bundle's Info.plist -- that is every
    "python keeps crashing" report on this Mac. The signed camlab.app has the key;
    a bare `python -m server.main` does not. Return None when Bluetooth is safe to
    touch, else a one-line reason. CAMLAB_BLUETOOTH_OK=1 skips the check (bringup
    from Terminal.app, where macOS shows the permission prompt instead)."""
    if sys.platform != "darwin" or os.environ.get("CAMLAB_BLUETOOTH_OK"):
        return None
    try:
        from Foundation import NSBundle
        info = NSBundle.mainBundle().infoDictionary() or {}
    except Exception:
        return None  # no pyobjc: bleak cannot load either, handled downstream
    if info.get("NSBluetoothAlwaysUsageDescription"):
        return None
    return ("this process has no NSBluetoothAlwaysUsageDescription (not the signed camlab.app); "
            "macOS would kill it on first Bluetooth use. Run dist/camlab.app, or set CAMLAB_BLUETOOTH_OK=1")


def shared_client(export_path: Path, src: int, ble_address: str | None,
                  state_path: str | Path | None) -> GodoxMeshClient:
    """Lights on the same export (same mesh) share one GATT connection and one
    sequence counter -- Bluetooth Mesh reaches every node through any proxy,
    and two controllers on one provisioner address would otherwise collide."""
    key = (str(export_path.resolve()), int(src))
    with _loop_lock:
        c = _clients.get(key)
        if c is None:
            export = load_mesh_export(export_path)
            net = MeshNetwork(export.network_key, export.app_key, src=src, iv=export.iv_index,
                              state_path=state_path or (str(export_path) + ".state.json"))
            reason = bluetooth_usage_described()
            if reason:
                raise GodoxMeshError(reason)
            import bleak  # noqa: F401  lazy: fail here (-> ManualLight) if not installed
            c = GodoxMeshClient(net, ble_address=ble_address or None)
            c.export = export
            _clients[key] = c
            _client_refs[key] = 0
        _client_refs[key] += 1
        return c


def release_client(client: GodoxMeshClient) -> bool:
    """Drop one reference; True when this was the last light using it."""
    with _loop_lock:
        for key, c in list(_clients.items()):
            if c is client:
                _client_refs[key] -= 1
                if _client_refs[key] <= 0:
                    del _clients[key], _client_refs[key]
                    return True
                return False
    return True


# --------------------------------------------------------------------------
# camlab backend
# --------------------------------------------------------------------------

class GodoxMeshLight(LightBackend):
    """Profile keys (config.toml [lights.fill]):
        mesh_export        path to the Godox app export (.json or GodoxLight.db), relative to camlab/
        mesh_address       which light in the export: name, MAC, or unicast address (default: first)
        ble_address        optional gateway BLE address/UUID to skip scanning
        mesh_src           our provisioner address (default 0x0500); lights with the same
                           export and mesh_src share one BLE connection and sequence counter
        mesh_state         where the sequence counter is persisted (default <export>.state.json)
        connect_timeout_s  how long connect() blocks at startup (default 20)
    """

    def __init__(self, light_id: str, profile: dict, client: GodoxMeshClient | None = None,
                 root: Path | None = None):
        super().__init__(light_id, profile)
        root = root or Path(__file__).resolve().parents[2]
        export_path = profile.get("mesh_export")
        if client is None:
            if not export_path:
                raise ValueError("godox_mesh backend needs mesh_export = \"<path>\" in its profile")
            p = Path(export_path)
            p = p if p.is_absolute() else root / p
            client = shared_client(p, int(profile.get("mesh_src", DEFAULT_SRC)),
                                   profile.get("ble_address") or None, profile.get("mesh_state"))
            self._shared = True
            self.node = client.export.find(profile.get("mesh_address"))
        else:
            self._shared = False
            self.node = MeshNode(name=self.model, address=int(profile.get("mesh_address", 2)))
        self.client = client
        self.dst = self.node.address
        self.connect_timeout = float(profile.get("connect_timeout_s", 20))
        self.apply_timeout = float(profile.get("apply_timeout_s", 3))
        self._power_on: bool | None = None
        self._last_sent: tuple[float, int] | None = None
        self._target: tuple[float, int] | None = None
        self._retry_after = 0.0

    # event loop thread ----------------------------------------------------
    def _run(self, coro, timeout: float):
        fut = asyncio.run_coroutine_threadsafe(coro, mesh_loop())
        try:
            return fut.result(timeout=timeout)
        except TimeoutError:
            # leave the coroutine running (it will finish the send); the caller just stops waiting
            raise

    # LightBackend ----------------------------------------------------------
    def connect(self) -> bool:
        try:
            if not self.client.is_connected:
                self._run(self.client.connect(), self.connect_timeout)
            log.info("light %s: Godox mesh node %r (0x%04X) via %s", self.id, self.node.name, self.dst, self.client.gateway)
            return True
        except Exception as e:
            log.warning("light %s: Godox mesh connect failed (%s); will retry on next change", self.id, e)
            self._retry_after = time.monotonic() + 10
            return False

    @property
    def connected(self) -> bool:
        return self.client.is_connected

    def state(self):
        s = super().state()
        ack = self.client.last_ack.get(self.dst)
        if ack is not None:
            ts, payload = ack
            s.last_ack = {"t": round(ts, 3), "age_s": round(max(0.0, time.time() - ts), 2), **decode_echo(payload)}
        return s

    def apply(self, intensity_pct: float, cct_k: int) -> None:
        self._target = (float(intensity_pct), int(cct_k))
        if not self.connected and time.monotonic() < self._retry_after:
            return  # don't block every slider tick on a dead radio
        try:
            self._run(self._push(), self.apply_timeout)
        except TimeoutError:
            log.debug("light %s: mesh send still in flight", self.id)
        except Exception as e:
            log.warning("light %s: mesh send failed: %s", self.id, e)
            self._retry_after = time.monotonic() + 10

    async def _push(self) -> None:
        if not self.client.is_connected:
            await self.client.connect()
            self._power_on = None
        target = self._target
        if target is None or target == self._last_sent:
            return
        pct, k = target
        # Verified 2026-10-21 on the LE300Bi/LP800Bi: the FE 01 "off" frame is
        # accepted (the MCU keeps reporting the old brightness) but the output
        # does not change, while F0 with brightness 0 goes dark, which is also
        # what the Godox app sends when its slider hits 0. So "off" is
        # brightness 0; FE 01 follows for models that do honour it. Tenths in
        # the end byte were not applied either (the app never sends them), so
        # brightness is sent as a whole percent.
        if pct <= 0:
            if self._power_on is not False:
                await self.client.send_frame(self.dst, godox_cct(0, k))
                await asyncio.sleep(0.05)
                await self.client.send_frame(self.dst, godox_power(False))
                self._power_on = False
        else:
            if self._power_on is not True:
                await self.client.send_frame(self.dst, godox_power(True))
                self._power_on = True
                await asyncio.sleep(0.05)
            await self.client.send_frame(self.dst, godox_cct(round(pct), k))
        self._last_sent = target
        self._schedule_readback()

    def _schedule_readback(self) -> None:
        """Ask the light for its A0 record shortly after a change so state()
        shows what the MCU actually applied; off the caller's path so slider
        ticks are not slowed down. One pending query per light at a time."""
        task = getattr(self, "_readback_task", None)
        if task is not None and not task.done():
            return

        async def go():
            await asyncio.sleep(STATUS_SETTLE_S)
            try:
                if self.client.is_connected:
                    await self.client.query_status(self.dst, timeout=STATUS_REPLY_TIMEOUT)
            except Exception as e:
                log.debug("light %s: status readback failed: %s", self.id, e)
        self._readback_task = asyncio.ensure_future(go())

    def close(self) -> None:
        self.client.net.save_state()
        last = release_client(self.client) if self._shared else True
        if last:
            try:
                self._run(self.client.disconnect(), 5)
            except Exception:
                pass


# --------------------------------------------------------------------------
# bring-up: pass/fail from the echoes, no eyes on the light needed
# --------------------------------------------------------------------------

BRINGUP_STEPS: list[tuple[str, bytes, bool]] = [  # (label, frame, status must match)
    ("power on", godox_power(True), False),       # FE frames: reply optional, the A0 record does not carry on/off
    ("30% 5600K", godox_cct(30, 5600), True),
    ("100% 2800K", godox_cct(100, 2800), True),
    ("100% 6500K", godox_cct(100, 6500), True),
    ("0% 5600K", godox_cct(0, 5600), True),       # how these lights actually go dark
    ("power off frame", godox_power(False), False),  # FE 01: accepted but no effect on LE300Bi/LP800Bi
]
BRINGUP_SETTLE_S = 0.6        # between steps; also the wait for an unprompted echo before asking
LUMA_RISE_MIN = 0.02          # median luma gain (0..1) that counts as "the light came on"


@dataclass
class StepResult:
    node: str
    step: str
    ok: bool
    detail: str
    ms: float | None = None


def _fmt_table(rows: list[StepResult]) -> str:
    w_node = max([4] + [len(r.node) for r in rows])
    w_step = max([4] + [len(r.step) for r in rows])
    lines = [f"{'node':<{w_node}}  {'step':<{w_step}}  result  ms     detail",
             "-" * (w_node + w_step + 32)]
    for r in rows:
        ms = f"{r.ms:5.0f}" if r.ms is not None else "    -"
        lines.append(f"{r.node:<{w_node}}  {r.step:<{w_step}}  {'PASS' if r.ok else 'FAIL':<6}  {ms}  {r.detail}")
    return "\n".join(lines)


def open_camera():
    """A real camera for the --camera check, or None. The mock camera does
    not see the room, so only hardware backends count."""
    import importlib.util
    if importlib.util.find_spec("gphoto2") is None:     # python-gphoto2 bindings
        return None
    try:
        from ..camera.gphoto2_backend import Gphoto2Backend
        cam = Gphoto2Backend()
        cam.connect()
        return cam
    except Exception as e:
        log.warning("bringup: no camera (%s)", e)
        return None


def _luma_median(camera) -> float:
    from .. import analysis
    fr = camera.capture_frame()
    return float(analysis.analyze(fr.jpeg)["luma"]["median"])


async def _bringup_async(export: MeshExport, nodes: list[MeshNode], client: GodoxMeshClient,
                         camera, out: Callable[[str], None], ack_timeout: float,
                         settle: float, dump_on_fail: bool) -> list[StepResult]:
    results: list[StepResult] = []
    out(f"export: netKey {export.network_key}  appKey {export.app_key}  ivIndex {export.iv_index}  "
        f"network id {client.net.nk.network_id.hex()}")
    for n in export.nodes:
        mark = "  <- testing" if n in nodes else ""
        out(f"  node 0x{n.address:04X}  {n.name!r}  mac={n.mac or '-'}{mark}")

    # 1. scan (skipped when the gateway address was given)
    gws: list[tuple[str, int, str]] = []
    if client.ble_address:
        out(f"scan skipped: gateway {client.ble_address} given")
    else:
        try:
            gws = await client.find_gateways()
        except Exception as e:
            out(f"scan failed: {e}")
    if client.ble_address and not gws:
        pass
    elif gws:
        out("mesh proxies advertising our Network ID:")
        for addr, rssi, name in gws:
            out(f"  {rssi:5} dBm  {addr}  {name}")
        if not client.ble_address:
            client.ble_address = gws[0][0]
    else:
        out("scan: no node advertises our Network ID" + ("" if client.ble_address else
            " (is a light on and in Bluetooth mode? is the phone app closed?)"))
    results.append(StepResult("mesh", "scan", bool(gws) or bool(client.ble_address),
                              f"{len(gws)} proxy node(s)" if gws else ("using --ble address" if client.ble_address else "none")))

    # 2. connect + sequence probe
    t0 = time.monotonic()
    try:
        await client.connect()
        results.append(StepResult("mesh", "connect+probe", True,
                                  f"gateway {client.gateway}, seq {client.net.seq}", (time.monotonic() - t0) * 1000))
    except Exception as e:
        results.append(StepResult("mesh", "connect+probe", False, str(e), (time.monotonic() - t0) * 1000))
        if dump_on_fail:
            await _dump_diagnostics(client, out)
        return results

    # 3. command / echo per node
    for node in nodes:
        for label, frame, required in BRINGUP_STEPS:
            t0 = time.monotonic()
            how = "echo"
            try:
                # some MCU families echo unprompted; wait briefly for that, then ask the radio chip
                ack = await client.send_frame(node.address, frame, ack_timeout=max(settle, STATUS_SETTLE_S))
                if ack is None or not echo_matches(frame, ack):
                    how = "status"
                    ack = await client.query_status(node.address, STATUS_RECORD_CCT, timeout=ack_timeout)
            except Exception as e:
                results.append(StepResult(node.name, label, False, f"send failed: {e}"))
                continue
            ms = (time.monotonic() - t0) * 1000
            if ack is None:
                ok = not required
                results.append(StepResult(node.name, label, ok,
                                          "no reply (optional for power frames)" if ok else f"no echo and no status reply within {ack_timeout:.1f}s", ms))
            else:
                d = decode_echo(ack)
                desc = f"{how} {ack.hex()}"
                if d["kind"] == "cct":
                    desc += f" = bri {'/'.join(map(str, d['brightness_candidates']))} {d['kelvin']}K"
                if frame[0] == 0xF0:
                    match = echo_matches(frame, ack)
                else:  # power frames: the A0 record does not carry on/off; a reply proves the node is alive
                    match = True
                results.append(StepResult(node.name, label, match, desc if match else desc + "  (does not match command)", ms))
            await asyncio.sleep(settle)

    # 4. optional camera check
    if camera is not None:
        cam_settle = settle * 3   # light output + camera live view need a moment
        for node in nodes:
            try:
                await client.send_frame(node.address, godox_power(False))
                await asyncio.sleep(cam_settle)
                dark = _luma_median(camera)
                await client.send_frame(node.address, godox_power(True))
                await asyncio.sleep(settle / 3)
                await client.send_frame(node.address, godox_cct(100, 5600))
                await asyncio.sleep(cam_settle)
                bright = _luma_median(camera)
                await client.send_frame(node.address, godox_power(False))
                rise = bright - dark
                results.append(StepResult(node.name, "camera luma", rise >= LUMA_RISE_MIN,
                                          f"median {dark:.3f} -> {bright:.3f} ({rise:+.3f})"))
            except Exception as e:
                results.append(StepResult(node.name, "camera luma", False, f"capture failed: {e}"))

    if dump_on_fail and not all(r.ok for r in results):
        await _dump_diagnostics(client, out)
    return results


async def _dump_diagnostics(client: GodoxMeshClient, out: Callable[[str], None]) -> None:
    out("---- diagnostics (a step failed) ----")
    try:
        if client._scanner is not None:
            found = await client._scanner(client.scan_timeout)
        else:
            from bleak import BleakScanner
            found = await BleakScanner.discover(timeout=client.scan_timeout, return_adv=True)
        out(f"full scan: {len(found)} BLE devices")
        for dev, adv in sorted(found.values(), key=lambda t: -(getattr(t[1], 'rssi', None) or -127)):
            sd = {k: bytes(v).hex() for k, v in (getattr(adv, "service_data", None) or {}).items()}
            out(f"  {getattr(adv, 'rssi', '?'):>4} dBm  {dev.address}  {getattr(dev, 'name', '') or '':<20} sd={sd}")
    except Exception as e:
        out(f"full scan failed: {e}")
    c = client.client
    services = getattr(c, "services", None)
    if c is None or services is None:
        out(f"gateway GATT services: n/a (connected={client.is_connected})")
        return
    out(f"gateway {client.gateway} GATT services:")
    try:
        for svc in services:
            out(f"  service {svc.uuid}  {getattr(svc, 'description', '')}")
            for ch in getattr(svc, "characteristics", []):
                out(f"     char {ch.uuid}  {getattr(ch, 'description', '')}  props={','.join(getattr(ch, 'properties', []))}")
    except Exception as e:
        out(f"  (could not enumerate: {e})")


def run_bringup(export_path: str | Path, lights: list[str] | None = None, camera: bool | Any = False,
                client: GodoxMeshClient | None = None, ble_address: str | None = None,
                src: int = DEFAULT_SRC, ack_timeout: float = ACK_TIMEOUT, settle: float = BRINGUP_SETTLE_S,
                out: Callable[[str], None] = print, dump_on_fail: bool = True) -> int:
    """The Phase 0 bring-up: scan, connect, probe, drive every node through
    the step list and wait for each echo. Prints a PASS/FAIL table; returns 0
    only when every step passed. `client` lets tests inject a prebuilt
    GodoxMeshClient (fake BLE node); `camera` is False, True (open a real
    one) or a CameraBackend instance."""
    export = load_mesh_export(export_path)
    nodes = [export.find(l) for l in lights] if lights else list(export.nodes)
    if client is None:
        net = MeshNetwork(export.network_key, export.app_key, src=src, iv=export.iv_index,
                          state_path=str(export_path) + ".state.json")
        import bleak  # noqa: F401
        client = GodoxMeshClient(net, ble_address=ble_address)
    elif ble_address:
        client.ble_address = ble_address
    cam = None
    if camera is True:
        cam = open_camera()
        if cam is None:
            out("camera check requested but no camera backend is available (gphoto2 + tethered body)")
    elif camera:
        cam = camera

    async def go():
        try:
            return await _bringup_async(export, nodes, client, cam, out, ack_timeout, settle, dump_on_fail)
        finally:
            client.net.save_state()
            await client.disconnect()

    results = asyncio.run(go())
    if camera is True and cam is None:
        results.append(StepResult("camera", "open", False, "no camera backend available"))
    if cam is not None and camera is True:
        try:
            cam.disconnect()
        except Exception:
            pass
    out("")
    out(_fmt_table(results))
    failed = [r for r in results if not r.ok]
    out("")
    out("BRINGUP PASS" if not failed else f"BRINGUP FAIL ({len(failed)} step(s))")
    return 0 if not failed else 1


# --------------------------------------------------------------------------
# CLI: python -m server.lights.godox_mesh scan | probe <addr> | export <file> | set | bringup
# --------------------------------------------------------------------------

async def _cli_scan(timeout: float, net: MeshNetwork | None) -> None:
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    print(f"{len(found)} BLE devices seen in {timeout:.0f}s")
    for dev, adv in sorted(found.values(), key=lambda t: -(t[1].rssi or -127)):
        sd = {k[4:8] if k.startswith("0000") else k: bytes(v).hex() for k, v in (adv.service_data or {}).items()}
        md = {f"{k:04x}": bytes(v).hex() for k, v in (adv.manufacturer_data or {}).items()}
        tag = ""
        p = (adv.service_data or {}).get(PROXY_SVC_UUID)
        if p:
            tag = "  [mesh proxy: " + ("Network ID " + bytes(p[1:9]).hex() if p[0] == 0 else "Node Identity") + "]"
            if net and p[0] == 0 and bytes(p[1:9]) == net.nk.network_id:
                tag += "  <-- OUR MESH"
        print(f"{adv.rssi:5} dBm  {dev.address}  {dev.name or '':<20} svc={[u[4:8] if u.startswith('0000') else u for u in adv.service_uuids]} sd={sd} md={md}{tag}")


async def _cli_probe(address: str) -> None:
    from bleak import BleakClient
    async with BleakClient(address, timeout=20) as c:
        print("connected:", c.is_connected)
        for svc in c.services:
            print(f"service {svc.uuid}  {svc.description}")
            for ch in svc.characteristics:
                print(f"   char {ch.uuid}  {ch.description}  props={','.join(ch.properties)}")


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m server.lights.godox_mesh")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="list BLE adverts; marks mesh proxies, and our mesh if --export given")
    s.add_argument("--export"); s.add_argument("--timeout", type=float, default=8)
    p = sub.add_parser("probe", help="connect to a BLE address and dump services/characteristics")
    p.add_argument("address")
    e = sub.add_parser("export", help="parse a Godox app export and list its lights")
    e.add_argument("path")
    t = sub.add_parser("set", help="send brightness/CCT to a light in the export")
    t.add_argument("--export", required=True); t.add_argument("--light")
    t.add_argument("--ble"); t.add_argument("pct", type=float); t.add_argument("kelvin", type=int)
    b = sub.add_parser("bringup", help="scan, connect, drive every light through a step list and check each echo; "
                                       "prints PASS/FAIL per node and step, exit 0 only when all pass")
    b.add_argument("--export", required=True)
    b.add_argument("--light", action="append", help="node name/MAC/address to test (repeatable; default: all)")
    b.add_argument("--ble", help="gateway BLE address, skips the scan")
    b.add_argument("--camera", action="store_true", help="also capture frames at 0%% and 100%% and require the luma median to rise")
    b.add_argument("--ack-timeout", type=float, default=ACK_TIMEOUT)
    b.add_argument("--src", type=lambda s: int(s, 0), default=DEFAULT_SRC)
    b.add_argument("-q", "--quiet", action="store_true", help="INFO logging instead of DEBUG")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if getattr(a, "quiet", False) else logging.DEBUG,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("bleak").setLevel(logging.WARNING)   # its DEBUG prints every advert several times a second
    if a.cmd == "bringup":
        try:
            return run_bringup(a.export, a.light, camera=a.camera, ble_address=a.ble, src=a.src,
                               ack_timeout=a.ack_timeout)
        except (FileNotFoundError, ValueError) as e:
            print(f"BRINGUP FAIL: {e}")
            return 2
    if a.cmd == "export":
        ex = load_mesh_export(a.path)
        print(f"netKey {ex.network_key}  appKey {ex.app_key}  ivIndex {ex.iv_index}")
        print("network id", NetKey(ex.network_key).network_id.hex())
        for n in ex.nodes:
            print(f"  0x{n.address:04X}  {n.name!r}  mac={n.mac}")
        return 0
    if a.cmd == "scan":
        net = None
        if a.export:
            ex = load_mesh_export(a.export)
            net = MeshNetwork(ex.network_key, ex.app_key)
        asyncio.run(_cli_scan(a.timeout, net)); return 0
    if a.cmd == "probe":
        asyncio.run(_cli_probe(a.address)); return 0
    if a.cmd == "set":
        prof = {"model": "cli", "mesh_export": a.export, "mesh_address": a.light, "ble_address": a.ble,
                "cct_min_k": 2000, "cct_max_k": 10000}
        light = GodoxMeshLight("cli", prof, root=Path.cwd())
        ok = light.connect()
        print("connected:", ok, "gateway:", light.client.gateway)
        if ok:
            light.set(a.pct, a.kelvin)
            print("sent", light.state())
        light.close()
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
