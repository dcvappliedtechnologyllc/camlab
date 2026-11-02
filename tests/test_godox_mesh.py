"""Godox Bluetooth Mesh backend: frame building, mesh crypto against captured
wire fixtures, export parsing, and the backend driven through a fake BLE
client that behaves like a proxy node (answers filter setup, enforces the
replay floor)."""
import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from conftest import manual_lights
from server.lights import godox_mesh as gm
from server.lights.godox_mesh import (DATA_IN_UUID, GODOX_OPCODE, GodoxMeshClient, GodoxMeshLight,
                                      MeshNetwork, godox_cct, godox_crc, godox_frame, godox_power,
                                      load_mesh_export, parse_mesh_json, proxy_segments)
from server.lights.manual import ManualLight


@pytest.fixture(autouse=True)
def no_real_bluetooth(monkeypatch):
    """macOS kills a process that touches CoreBluetooth without a grant (TCC,
    signal 134). Every test here must go through FakeNode / an injected scanner."""
    import bleak

    async def boom(*a, **kw):
        raise AssertionError("test reached the real Bluetooth radio")
    monkeypatch.setattr(bleak.BleakScanner, "discover", boom)
    monkeypatch.setattr(bleak, "BleakClient", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("real BleakClient")))

# Keys and wire bytes from godox-tl's tests/fixtures (captured from the
# original Python tool's --dry-run output, which matches real hardware).
NET_KEY = "ff5a4609ace4789e81c531ee9492b2bd"
APP_KEY = "819e28a5d5fe97f3290c8e3b2e9de156"
FIXTURES = [  # (pct, kelvin, seq, payload_hex, proxy_pdu_hex)
    (0, 2800, 102, "f0001c3200000056", "00792c2ddf8a5d25f019c1e92e0824dccb9b721dbff1bd6188e8483a11e2"),
    (50, 4500, 272, "f0322d320000009c", "00796a24cb3a4fc3f20604d20d49de987f550f3eea52ef214e2c44af7b46"),
    (100, 6500, 392, "f064413200000030", "007996749c99f1fe35e86fe12581aff5010580d5aaa8d1fb495f6cdc03f8"),
]
OFF_FIXTURE = (9002, "fe01ffffffffff48", "0079a16421797a21ba2469fb6feac1e61ae9d31d9533800661e9086185a8")

EXPORT = {
    "netKeys": [{"key": NET_KEY, "index": 0}],
    "appKeys": [{"key": APP_KEY, "index": 0, "boundNetKey": 0}],
    "ivIndex": "00000000",
    "nodes": [
        {"name": "Telink iOS provisioner node", "unicastAddress": "0100", "deviceKey": "00" * 16},
        {"name": "LP800Bi", "unicastAddress": "0002", "cid": "0211", "macAddress": "AABBCCDDEE02"},
        {"name": "TL60 hall", "unicastAddress": "0115", "cid": "0211"},
    ],
}


# ---------------------------------------------------------------- frames

def test_crc_and_frames():
    assert godox_frame(0xF1, bytes([100, 0, 0, 100, 0]), end=0).hex() == "f16400006400005d"
    assert godox_power(False).hex() == OFF_FIXTURE[1]
    assert godox_power(True).hex() == "fe00ffffffffff" + f"{godox_crc(bytes.fromhex('fe00ffffffffff')):02x}"
    for pct, k, _, payload, _ in FIXTURES:
        assert godox_cct(pct, k).hex() == payload
    assert len(godox_cct(33.3, 3200)) == 8
    assert godox_cct(33.3, 3200)[6] == 3  # tenths in the end byte
    assert godox_cct(250, 99999)[1] == 100  # clamps brightness


# ---------------------------------------------------------------- crypto

def test_wire_fixtures_round_trip():
    net = MeshNetwork(NET_KEY, APP_KEY, src=1)
    for pct, k, seq, _, wire in FIXTURES:
        pdu = net.godox_pdu(2, godox_cct(pct, k), seq=seq)
        assert proxy_segments(0, pdu)[0][0] & 0x3F == 0  # network PDU type, SAR bits set (29 bytes > 19)
        assert b"".join(c[1:] for c in proxy_segments(0, pdu)).hex() == wire[2:]
    seq, payload, wire = OFF_FIXTURE
    assert net.godox_pdu(2, bytes.fromhex(payload), seq=seq).hex() == wire[2:]


def test_decrypt_our_own_pdu():
    net = MeshNetwork(NET_KEY, APP_KEY, src=0x0500)
    pdu = net.godox_pdu(0x0002, godox_cct(40, 5600))
    d = gm.decrypt_network_pdu(net.nk, (0,), pdu)
    assert d and d["src"] == 0x0500 and d["dst"] == 2 and d["ttl"] == gm.DEFAULT_TTL and d["ctl"] == 0
    assert d["transport"][0] == 0x40 | net.ak.aid
    access = gm.decrypt_access(net.ak.key, False, d["seq"], d["src"], d["dst"], 0, d["transport"][1:])
    assert access == GODOX_OPCODE + godox_cct(40, 5600)


def test_proxy_segmentation():
    pdu = bytes(range(45))
    segs = proxy_segments(0, pdu, mtu=20)
    assert [s[0] >> 6 for s in segs] == [1, 2, 3]
    assert all(len(s) <= 20 for s in segs)
    assert b"".join(s[1:] for s in segs) == pdu
    assert proxy_segments(2, bytes(19)) == [b"\x02" + bytes(19)]


# ---------------------------------------------------------------- export

def test_parse_export_filters_provisioner_and_finds_lights():
    ex = parse_mesh_json(EXPORT)
    assert ex.network_key == NET_KEY and [n.address for n in ex.nodes] == [2, 0x115]
    assert ex.find(None).name == "LP800Bi"
    assert ex.find("lp800bi").address == 2
    assert ex.find("AA:BB:CC:DD:EE:02").address == 2
    assert ex.find("0x0115").name == "TL60 hall"
    assert ex.find(277).name == "TL60 hall"
    with pytest.raises(ValueError):
        ex.find("nope")
    # wrapper form (the raw DB row)
    ex2 = parse_mesh_json({"meshJson": json.dumps(EXPORT), "deviceNum": 2})
    assert ex2.app_key == APP_KEY


def test_load_export_json_and_sqlite(tmp_path):
    j = tmp_path / "godox_mesh.json"
    j.write_text(json.dumps(EXPORT))
    assert load_mesh_export(j).nodes[0].address == 2
    db = tmp_path / "GodoxLight.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE Project (id INTEGER, name TEXT, meshJson TEXT)")
    con.execute("INSERT INTO Project VALUES (1, 'empty', NULL)")
    con.execute("INSERT INTO Project VALUES (2, 'studio', ?)", (json.dumps(EXPORT),))
    con.commit(); con.close()
    assert [n.name for n in load_mesh_export(db).nodes] == ["LP800Bi", "TL60 hall"]
    with pytest.raises(FileNotFoundError):
        load_mesh_export(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"; bad.write_text('{"netKeys": []}')
    with pytest.raises(ValueError):
        load_mesh_export(bad)


def test_sequence_state_persists_and_never_goes_backwards(tmp_path):
    st = tmp_path / "state.json"
    net = MeshNetwork(NET_KEY, APP_KEY, src=0x0500, seq=1000, state_path=st)
    for _ in range(gm.SEQ_SAVE_EVERY):
        net.next_seq()
    assert json.loads(st.read_text())["seq_0500"] == 1000 + gm.SEQ_SAVE_EVERY
    net2 = MeshNetwork(NET_KEY, APP_KEY, src=0x0500, seq=5, state_path=st)
    assert net2.seq == 1000 + gm.SEQ_SAVE_EVERY + gm.SEQ_RESTART_MARGIN


# ---------------------------------------------------------------- fake node

class FakeNode:
    """Stands in for bleak's BleakClient AND the proxy node behind it."""

    def __init__(self, address: str, net_keys: tuple[str, str], node_addr: int = 2,
                 seq_floor: int = 0, fail_connect: bool = False, echo: bool = True,
                 echo_power: bool = True, echo_delay: float = 0.0, family: str = "c8",
                 nodes: tuple[int, ...] | None = None, answer_status: bool = True):
        self.address = address
        self.nk = gm.NetKey(net_keys[0]); self.ak = gm.AppKey(net_keys[1])
        self.node_addr = node_addr
        self.nodes = nodes or (node_addr,)      # every light this proxy relays for (all echo)
        self.seq_floor = seq_floor
        self.fail_connect = fail_connect
        self.echo = echo                        # unprompted A0 after F0 (C2/C8 families do; C1 does not)
        self.answer_status = answer_status      # FD 01 .. A0 request answered on 0x0211F1 (the radio chip does)
        self.echo_power = echo_power            # AE after FE (the radio chip may drop it on real units)
        self.echo_delay = echo_delay            # seconds; the LP800Bi reports ~0.85 s after a command
        self.family = family                    # "c8" (LP800Bi A0 layout) or "c1" (LE300Bi layout)
        self.is_connected = False
        self.writes: list[bytes] = []
        self.frames: list[bytes] = []          # decrypted Godox frames addressed to us
        self.all_frames: list[tuple[int, bytes]] = []   # (dst, frame) for every node on the mesh
        self.config: list[bytes] = []
        self.echoes: list[tuple[int, bytes]] = []       # (src, payload) we sent back
        self.state: dict[int, dict] = {}       # per node: on, bri, k
        self._notify = None
        self._rx = bytearray()
        self._seq = 1

    # what the light would do with the frame, and the echo it reports back
    def _apply(self, node: int, frame: bytes) -> bytes | None:
        st = self.state.setdefault(node, {"on": True, "bri": 0, "k": 5600, "tint": 50})
        if frame[0] == 0xF0:
            st.update(on=True, bri=frame[1], k=frame[2] * 100, tint=frame[3])
            if not self.echo:
                return None
            if self.family == "c1":
                return bytes([0xA0, st["bri"] // 10, st["k"] // 100, 0xFF, 0x00, st["tint"], st["bri"] % 10])
            return godox_frame(0xA0, bytes([st["bri"], st["k"] // 100, st["tint"]]))
        if frame[0] == 0xFE:
            st["on"] = frame[1] == 0x00
            return godox_frame(0xAE, bytes([frame[1]])) if self.echo_power else None
        if frame[0] == 0xFD and frame[1] == 0x01 and self.answer_status:
            # the Telink chip answers from RAM with the last record of the requested kind
            if frame[6] == gm.STATUS_RECORD_CCT:
                if self.family == "c1":
                    return bytes([0xA0, st["bri"] // 10, st["k"] // 100, 0xFF, 0x00, st["tint"], st["bri"] % 10])
                return godox_frame(0xA0, bytes([st["bri"], st["k"] // 100, st["tint"]]))
            return godox_frame(0xAF, b"\x30\x01\x00\x00")   # some other record
        return None

    def output_level(self, node: int | None = None) -> float:
        """0..1 light output as a camera would see it."""
        st = self.state.get(node or self.node_addr)
        if not st or not st["on"]:
            return 0.0
        return st["bri"] / 100.0

    def _send_echo(self, src: int, dst: int, payload: bytes) -> None:
        access = gm.GODOX_STATUS_OPCODE + payload
        seq = self._seq; self._seq += 1
        upper = gm.encrypt_access(self.ak.key, False, seq, src, dst, 0, access)
        pdu = gm.encrypt_network_pdu(self.nk, 0, 0, 5, seq, src, dst, bytes([0x40 | self.ak.aid]) + upper)
        self.echoes.append((src, payload))
        for chunk in gm.proxy_segments(gm.PROXY_PDU_NETWORK, pdu):
            self._notify(None, bytearray(chunk))

    async def connect(self):
        if self.fail_connect:
            raise OSError("device unreachable")
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_notify(self, uuid, cb):
        self._notify = cb

    async def write_gatt_char(self, uuid, data, response=False):
        assert uuid == DATA_IN_UUID and len(data) <= 20
        self.writes.append(bytes(data))
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

    def _handle(self, typ, pdu):
        if typ == gm.PROXY_PDU_BEACON:
            return
        d = gm.decrypt_network_pdu(self.nk, (0,), pdu, proxy=(typ == gm.PROXY_PDU_CONFIG))
        assert d is not None, "node could not decrypt what the backend sent"
        if d["seq"] <= self.seq_floor:
            return  # replay protection: silently dropped
        self.seq_floor = d["seq"]
        if typ == gm.PROXY_PDU_CONFIG:
            self.config.append(d["transport"])
            if d["transport"][0] == 0x00:  # Set Filter Type -> Filter Status
                reply = gm.encrypt_network_pdu(self.nk, 0, 1, 0, self._seq, self.node_addr, 0,
                                               bytes([0x03, d["transport"][1], 0, 0]), proxy=True)
                self._seq += 1
                self._notify(None, bytearray(b"\x02" + reply))
            return
        t = d["transport"]
        access = gm.decrypt_access(self.ak.key, False, d["seq"], d["src"], d["dst"], 0, t[1:])
        assert access is not None and access[:3] == GODOX_OPCODE
        self.all_frames.append((d["dst"], access[3:]))
        if d["dst"] == self.node_addr:
            self.frames.append(access[3:])
        targets = list(self.nodes) if d["dst"] == gm.ALL_NODES else ([d["dst"]] if d["dst"] in self.nodes else [])
        for node in targets:
            payload = self._apply(node, access[3:])
            if payload is None:
                continue
            if self.echo_delay > 0:
                asyncio.get_running_loop().call_later(self.echo_delay, self._send_echo, node, d["src"], payload)
            else:
                self._send_echo(node, d["src"], payload)


def make_light(tmp_path, node: FakeNode, **profile):
    prof = {"model": "Godox LP800Bi", "cct_min_k": 2800, "cct_max_k": 6500, "mesh_address": node.node_addr,
            "connect_timeout_s": 5, "apply_timeout_s": 5}
    prof.update(profile)
    net = MeshNetwork(NET_KEY, APP_KEY, src=0x0500, state_path=tmp_path / "state.json")
    client = GodoxMeshClient(net, ble_address=node.address, client_factory=lambda a: node, reply_timeout=0.2)
    return GodoxMeshLight("fill", prof, client=client)


def test_backend_connects_probes_and_sends(tmp_path):
    node = FakeNode("AA:BB", (NET_KEY, APP_KEY), seq_floor=0x45000)
    light = make_light(tmp_path, node)
    try:
        assert light.connect() and light.connected
        assert light.client.net.seq > 0x45000  # found a working sequence number above the floor
        assert any(c[0] == 0x01 for c in node.config)  # filter accept list was set
        s = light.state()
        assert s.backend == "GodoxMeshLight" and s.scriptable and s.connected

        light.set(50, 4500)
        assert node.frames[-2:] == [godox_power(True), godox_cct(50, 4500)]
        light.set(50, 4500)   # unchanged -> nothing new
        assert len(node.frames) == 2
        light.set(0, 4500)    # zero intensity -> brightness 0 (what goes dark), then the FE 01 off frame
        assert node.frames[-2:] == [godox_cct(0, 4500), godox_power(False)] and len(node.frames) == 4
        light.set(20, 3200)   # back on
        assert node.frames[-2:] == [godox_power(True), godox_cct(20, 3200)]
        light.set(33.3, 3200)  # whole percents only: the lights ignore tenths in the end byte
        assert node.frames[-1] == godox_cct(33, 3200)

        light.set(150, 9000)  # guardrails clamp before apply()
        assert node.frames[-1] == godox_cct(100, 6500)
        assert light.state().intensity_pct == 100 and light.state().cct_k == 6500
        assert json.loads((tmp_path / "state.json").read_text())["seq_0500"] > 0x45000
    finally:
        light.close()
    assert not node.is_connected


def test_backend_survives_connect_failure_and_retries(tmp_path):
    node = FakeNode("AA:BB", (NET_KEY, APP_KEY), fail_connect=True)
    light = make_light(tmp_path, node)
    try:
        assert light.connect() is False and not light.connected
        light.set(40, 5000)                 # must not raise or block
        assert node.frames == []
        node.fail_connect = False
        light._retry_after = 0              # cool-down over
        light.set(40, 5000)
        assert light.connected and node.frames[-1] == godox_cct(40, 5000)
    finally:
        light.close()


def test_gateway_discovery_filters_on_network_id(tmp_path):
    net = MeshNetwork(NET_KEY, APP_KEY)

    class Dev:
        def __init__(self, address, name): self.address, self.name = address, name

    class Adv:
        def __init__(self, sd, rssi): self.service_data, self.rssi = sd, rssi

    other = gm.NetKey("00" * 16).network_id
    found = {
        "A": (Dev("A", "GD_LED"), Adv({gm.PROXY_SVC_UUID: b"\x00" + net.nk.network_id}, -70)),
        "B": (Dev("B", "GD_LED"), Adv({gm.PROXY_SVC_UUID: b"\x00" + other}, -40)),
        "C": (Dev("C", "GD_LED"), Adv({gm.PROXY_SVC_UUID: b"\x00" + net.nk.network_id}, -50)),
        "D": (Dev("D", "watch"), Adv({}, -30)),
        "E": (Dev("E", "GD_LED"), Adv({gm.PROXY_SVC_UUID: b"\x01" + bytes(16)}, -45)),  # node identity
    }

    async def scanner(timeout):
        return found

    client = GodoxMeshClient(net, scanner=scanner)
    gws = asyncio.run(client.find_gateways())
    assert [a for a, _, _ in gws] == ["C", "A"]


def test_controller_falls_back_to_manual_when_export_missing(tmp_path):
    from server import config
    from server.controller import Controller
    cfg = config.load()
    cfg["camera"]["backend"] = "mock"
    cfg["dmx"]["transport"] = "none"
    cfg["paths"] = {"frames": str(tmp_path / "frames"), "clips": str(tmp_path / "clips"),
                    "recipes": str(tmp_path / "recipes.json")}
    manual_lights(cfg)
    cfg["lights"]["fill"]["backend"] = "godox_mesh"
    cfg["lights"]["fill"]["mesh_export"] = str(tmp_path / "does-not-exist.json")
    ctl = Controller(cfg)
    ctl.start()
    try:
        assert isinstance(ctl.lights["fill"], ManualLight)
        assert ctl.state.lights["fill"]["scriptable"] is False
    finally:
        ctl.stop()


def test_controller_uses_mesh_backend_with_valid_export(tmp_path, monkeypatch):
    """With a readable export the controller builds GodoxMeshLight; bleak's
    BleakClient is replaced by the fake node so no radio is touched."""
    import bleak
    from server import config
    from server.controller import Controller
    monkeypatch.setenv("CAMLAB_BLUETOOTH_OK", "1")   # the radio is faked below; skip the TCC guard
    node = FakeNode("gateway", (NET_KEY, APP_KEY))
    monkeypatch.setattr(bleak, "BleakClient", lambda addr, **kw: node)
    export = tmp_path / "godox_mesh.json"; export.write_text(json.dumps(EXPORT))
    cfg = config.load()
    cfg["camera"]["backend"] = "mock"
    cfg["dmx"]["transport"] = "none"
    cfg["paths"] = {"frames": str(tmp_path / "frames"), "clips": str(tmp_path / "clips"),
                    "recipes": str(tmp_path / "recipes.json")}
    manual_lights(cfg)
    cfg["lights"]["fill"].update({"backend": "godox_mesh", "mesh_export": str(export),
                                  "mesh_address": "LP800Bi", "ble_address": "gateway"})
    cfg["lights"]["key"].update({"backend": "godox_mesh", "mesh_export": str(export),
                                 "mesh_address": "0x0115", "ble_address": "gateway"})
    connects = []
    orig_connect = node.connect

    async def counting_connect():
        connects.append(1); await orig_connect()
    node.connect = counting_connect
    ctl = Controller(cfg)
    ctl.start()
    try:
        fill, key = ctl.lights["fill"], ctl.lights["key"]
        assert isinstance(fill, GodoxMeshLight) and fill.dst == 2 and fill.connected
        assert isinstance(key, GodoxMeshLight) and key.dst == 0x115 and key.connected
        assert key.client is fill.client and len(connects) == 1  # one shared connection
        assert ctl.state.lights["fill"]["backend"] == "GodoxMeshLight"
        assert node.frames[-1] == godox_cct(30, 5600)                 # startup default for fill
        assert (0x115, godox_cct(80, 5600)) in node.all_frames        # startup default for key
        ctl.set_light("key", intensity_pct=55, cct_k=4000)
        assert node.all_frames[-1] == (0x115, godox_cct(55, 4000))
        assert (tmp_path / "godox_mesh.json.state.json").exists()
    finally:
        ctl.stop()
    assert not node.is_connected
    assert gm._clients == {}  # both lights released the shared client


# ---------------------------------------------------------------- echo / ack

def test_echo_decoding_and_matching():
    c8 = godox_frame(0xA0, bytes([30, 56, 50]))                       # LP800Bi layout
    assert c8[:7].hex() == "a01e3832ffffff"
    d = gm.decode_echo(c8)
    assert d["kind"] == "cct" and d["kelvin"] == 5600 and d["brightness_candidates"] == [30]
    assert gm.echo_matches(godox_cct(30, 5600), c8) and not gm.echo_matches(godox_cct(31, 5600), c8)
    c1 = bytes([0xA0, 10, 28, 0xFF, 0x00, 50, 0])                     # LE300Bi layout: 100 % as 10 + 0
    assert gm.echo_matches(godox_cct(100, 2800), c1)
    assert gm.echo_matches(godox_cct(10, 2800), c1)                   # ambiguous by design: both readings kept
    assert gm.decode_echo(godox_frame(0xAE, b"\x00"))["kind"] == "power"
    assert gm.echo_matches(godox_power(True), godox_frame(0xAE, b"\x00"))
    assert not gm.echo_matches(godox_power(True), c8)
    assert gm.decode_echo(b"")["kind"] == "unknown"
    assert gm.split_opcode(gm.GODOX_STATUS_OPCODE + b"\xa0") == (gm.GODOX_STATUS_OPCODE, b"\xa0")
    assert gm.split_opcode(b"\x82\x04\x01") == (b"\x82\x04", b"\x01")
    assert gm.split_opcode(b"\x03\xaa") == (b"\x03", b"\xaa")


def test_client_hears_echo_and_wait_ack(tmp_path):
    node = FakeNode("AA:BB", (NET_KEY, APP_KEY), echo_delay=0.05)
    net = MeshNetwork(NET_KEY, APP_KEY, src=0x0500, state_path=tmp_path / "state.json")
    client = GodoxMeshClient(net, ble_address="AA:BB", client_factory=lambda a: node, reply_timeout=0.2)
    heard = []
    client.on_ack = lambda src, payload: heard.append((src, payload))

    async def go():
        await client.connect()
        # the accept list carries us, broadcast and the phone app's address
        add = [c for c in node.config if c[0] == 0x01][-1]
        assert add[1:] == bytes.fromhex("0500ffff0100")
        ack = await client.send_frame(2, godox_cct(30, 5600), ack_timeout=1.0)
        assert ack is not None and gm.echo_matches(godox_cct(30, 5600), ack)
        assert client.last_ack[2][1] == ack and heard == [(2, ack)]
        # standalone waiter: arm, then send without waiting, the echo lands in the waiter
        waiter = asyncio.ensure_future(client.wait_ack(2, timeout=1.0))
        await asyncio.sleep(0)
        await client.send_frame(2, godox_power(False))
        assert gm.decode_echo(await waiter)["kind"] == "power"
        # timeout path: nobody at that address
        assert await client.wait_ack(0x0999, timeout=0.05) is None
        assert client._ack_waiters == {}
        # a silent light: send_frame returns None after the timeout
        node.echo = False
        assert await client.send_frame(2, godox_cct(50, 5000), ack_timeout=0.1) is None
        reply = await client.query_status(2, timeout=0.5)
        assert reply is not None and gm.echo_matches(godox_cct(50, 5000), reply)
        assert node.frames[-1] == gm.godox_status_request() and node.frames[-1].hex() == "fd01ffffffffa0" + f"{godox_crc(bytes.fromhex('fd01ffffffffa0')):02x}"
        await client.disconnect()

    asyncio.run(go())


def test_light_state_exposes_last_ack(tmp_path):
    node = FakeNode("AA:BB", (NET_KEY, APP_KEY))
    light = make_light(tmp_path, node)
    try:
        assert light.connect()
        assert light.state().last_ack is None or light.state().last_ack["kind"] in ("cct", "power")
        light.set(40, 5000)
        ack = light.state().to_dict()["last_ack"]
        assert ack["kind"] == "cct" and ack["kelvin"] == 5000 and 40 in ack["brightness_candidates"]
        assert ack["age_s"] >= 0 and "hex" in ack
        # a light that never echoes unprompted is read back with a status request shortly after
        node.echo = False
        light.set(55, 4000)
        assert light.state().last_ack["brightness_candidates"] == [40]      # nothing yet
        import time as _t
        deadline = _t.time() + 3
        while _t.time() < deadline and light.state().last_ack["brightness_candidates"] != [55]:
            _t.sleep(0.05)
        assert light.state().last_ack["brightness_candidates"] == [55] and node.frames[-1] == gm.godox_status_request()
    finally:
        light.close()


# ---------------------------------------------------------------- bringup

class FakeCamera:
    """Frame brightness follows the fake node's light output."""

    def __init__(self, node: FakeNode):
        self.node = node

    def capture_frame(self):
        import io
        from PIL import Image
        from server.camera.base import Frame
        level = sum(self.node.output_level(n) for n in self.node.nodes) / len(self.node.nodes)
        v = int(20 + 200 * level)
        buf = io.BytesIO()
        Image.new("RGB", (64, 48), (v, v, v)).save(buf, format="JPEG")
        return Frame(jpeg=buf.getvalue(), width=64, height=48)


def _bringup_client(node: FakeNode, tmp_path, scanner=None, ble=False):
    """`ble=True` mimics --ble: gateway known, scan skipped."""
    net = MeshNetwork(NET_KEY, APP_KEY, src=0x0500, state_path=tmp_path / "state.json")
    scanner = scanner or _scanner_for(node, gm.NetKey(NET_KEY).network_id)
    return GodoxMeshClient(net, ble_address=node.address if ble else None,
                           client_factory=lambda a: node, scanner=scanner, reply_timeout=0.2)


def _scanner_for(node: FakeNode, net_id: bytes):
    class Dev:
        def __init__(self, address, name): self.address, self.name = address, name

    class Adv:
        def __init__(self, sd, rssi): self.service_data, self.rssi = sd, rssi

    async def scanner(timeout):
        return {node.address: (Dev(node.address, "GD_LED"), Adv({gm.PROXY_SVC_UUID: b"\x00" + net_id}, -55)),
                "X": (Dev("X", "watch"), Adv({}, -30))}
    return scanner


def test_bringup_passes_with_echoing_nodes(tmp_path, capsys):
    export = tmp_path / "godox_mesh.json"; export.write_text(json.dumps(EXPORT))
    node = FakeNode("GW", (NET_KEY, APP_KEY), nodes=(2, 0x115), echo_delay=0.02)
    client = _bringup_client(node, tmp_path, scanner=_scanner_for(node, gm.NetKey(NET_KEY).network_id))
    rc = gm.run_bringup(export, client=client, camera=FakeCamera(node), ack_timeout=0.5, settle=0)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "BRINGUP PASS" in out and "-55 dBm  GW" in out and "FAIL" not in out
    # every node got the full step list, in order
    sent = [f for d, f in node.all_frames if d == 2][:len(gm.BRINGUP_STEPS)]
    assert sent == [f for _, f, _ in gm.BRINGUP_STEPS]
    assert any(d == 0x115 and f == godox_cct(100, 6500) for d, f in node.all_frames)
    assert "camera luma" in out and "PASS" in out
    assert not node.is_connected and (tmp_path / "state.json").exists()


def test_bringup_tolerates_missing_power_echo_and_c1_layout(tmp_path, capsys):
    export = tmp_path / "godox_mesh.json"; export.write_text(json.dumps(EXPORT))
    node = FakeNode("GW", (NET_KEY, APP_KEY), node_addr=0x115, echo=False, echo_power=False, family="c1")
    client = _bringup_client(node, tmp_path, ble=True)
    rc = gm.run_bringup(export, lights=["TL60 hall"], client=client, ack_timeout=0.1, settle=0)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "scan skipped" in out and "status a00a1cff0032" in out   # answered via FD 01 .. A0, C1 layout
    assert "no reply (optional for power frames)" not in out       # the A0 record answers power steps too
    table = out[out.index("result  ms"):]
    assert "TL60 hall" in table and "LP800Bi" not in table   # only the requested light was driven


def test_bringup_fails_on_silent_light_and_dumps_diagnostics(tmp_path, capsys):
    export = tmp_path / "godox_mesh.json"; export.write_text(json.dumps(EXPORT))
    node = FakeNode("GW", (NET_KEY, APP_KEY), echo=False, echo_power=False, answer_status=False)
    client = _bringup_client(node, tmp_path, scanner=_scanner_for(node, gm.NetKey(NET_KEY).network_id))
    rc = gm.run_bringup(export, lights=["LP800Bi"], client=client, ack_timeout=0.05, settle=0)
    out = capsys.readouterr().out
    assert rc == 1
    assert "BRINGUP FAIL (4 step(s))" in out            # the four CCT steps; power frames are optional
    assert "no echo and no status reply within" in out
    assert "---- diagnostics" in out and "full scan: 2 BLE devices" in out and "GATT services: n/a" in out


def test_bringup_fails_when_no_gateway_and_cannot_connect(tmp_path, capsys):
    export = tmp_path / "godox_mesh.json"; export.write_text(json.dumps(EXPORT))
    node = FakeNode("GW", (NET_KEY, APP_KEY), fail_connect=True)

    async def empty_scanner(timeout):
        return {}
    client = _bringup_client(node, tmp_path, scanner=empty_scanner)
    rc = gm.run_bringup(export, client=client, ack_timeout=0.05, settle=0)
    out = capsys.readouterr().out
    assert rc == 1 and "no node advertises our Network ID" in out
    assert "connect+probe  FAIL" in out and "---- diagnostics" in out


def test_bringup_camera_check_catches_light_that_acks_but_stays_dark(tmp_path, capsys):
    export = tmp_path / "godox_mesh.json"; export.write_text(json.dumps(EXPORT))
    node = FakeNode("GW", (NET_KEY, APP_KEY))

    class DarkCamera(FakeCamera):
        def capture_frame(self):
            for st in self.node.state.values():
                st["bri"] = 0                        # acks fine, but nothing comes out
            return super().capture_frame()
    client = _bringup_client(node, tmp_path)
    rc = gm.run_bringup(export, lights=["LP800Bi"], client=client, camera=DarkCamera(node), ack_timeout=0.2, settle=0)
    out = capsys.readouterr().out
    assert rc == 1 and re.search(r"camera luma\s+FAIL", out)


def test_bringup_cli_entry(tmp_path, monkeypatch, capsys):
    """`python -m server.lights.godox_mesh bringup ...` reaches run_bringup with the parsed flags."""
    export = tmp_path / "godox_mesh.json"; export.write_text(json.dumps(EXPORT))
    seen = {}

    def fake_run(path, lights, camera, ble_address, src, ack_timeout):
        seen.update(path=path, lights=lights, camera=camera, ble=ble_address, src=src, t=ack_timeout)
        return 0
    monkeypatch.setattr(gm, "run_bringup", fake_run)
    assert gm.main(["bringup", "--export", str(export), "--light", "LP800Bi", "--light", "0x0115",
                    "--camera", "--ble", "GW", "--src", "0x0600", "--ack-timeout", "1.5"]) == 0
    assert seen == {"path": str(export), "lights": ["LP800Bi", "0x0115"], "camera": True, "ble": "GW",
                    "src": 0x600, "t": 1.5}


def test_real_app_export_shape(tmp_path):
    """The Godox Light 4.2.0 export: the provisioner node carries cid 0211 and
    a UUID listed under provisioners; display names live in the DB's Node table."""
    doc = json.loads(json.dumps(EXPORT))
    doc["provisioners"] = [{"provisionerName": "Telink iOS provisioner", "UUID": "C9F8D55D-1BAB-B94C-AB1D-3F51BAE06839"}]
    doc["nodes"][0].update(cid="0211", UUID="C9F8D55D-1BAB-B94C-AB1D-3F51BAE06839", name="Telink iOS provisioner node")
    doc["nodes"][1].update(name="GD_LED_EE02")
    ex = parse_mesh_json(doc)
    assert [n.address for n in ex.nodes] == [2, 0x115]
    db = tmp_path / "GodoxLight.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE Project (id INTEGER, title TEXT, meshJson TEXT)")
    con.execute("INSERT INTO Project VALUES (0, 'Demo', '')")
    con.execute("INSERT INTO Project VALUES (1, 'Studio 01', ?)", (json.dumps(doc),))
    con.execute("CREATE TABLE Node (id INTEGER, name TEXT, macAddress TEXT, deviceType TEXT)")
    con.execute("INSERT INTO Node VALUES (36, 'LP800Bi-EE02', 'AABBCCDDEE02', '00A8')")
    con.commit(); con.close()
    ex = load_mesh_export(db)
    n = ex.find("LP800Bi")                 # model prefix of the app name
    assert n.address == 2 and n.name == "LP800Bi-EE02" and n.mesh_name == "GD_LED_EE02"
    assert ex.find("GD_LED_EE02") is n and ex.find("LP800Bi-EE02") is n
    assert ex.find("TL60 hall").mesh_name == ""


def test_bare_python_without_bluetooth_description_falls_back_to_manual(tmp_path, monkeypatch):
    """macOS kills a process that touches CoreBluetooth with no usage description
    (every 'python keeps crashing' report on the bench). From bare Python the
    controller must log and use manual lights, never touch bleak."""
    import bleak
    from server import config
    from server.controller import Controller
    monkeypatch.delenv("CAMLAB_BLUETOOTH_OK", raising=False)
    monkeypatch.setattr(gm, "bluetooth_usage_described", lambda: "no usage description (test)")
    monkeypatch.setattr(bleak, "BleakClient", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("touched bleak")))
    export = tmp_path / "godox_mesh.json"; export.write_text(json.dumps(EXPORT))
    cfg = config.load()
    cfg["camera"]["backend"] = "mock"
    cfg["dmx"]["transport"] = "none"
    cfg["paths"] = {"frames": str(tmp_path / "frames"), "clips": str(tmp_path / "clips"),
                    "recipes": str(tmp_path / "recipes.json")}
    manual_lights(cfg)
    cfg["lights"]["fill"].update({"backend": "godox_mesh", "mesh_export": str(export),
                                  "mesh_address": "LP800Bi", "ble_address": "gateway"})
    ctl = Controller(cfg)
    try:
        ctl.start()
        assert isinstance(ctl.lights["fill"], ManualLight)
    finally:
        ctl.stop()
