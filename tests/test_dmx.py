import time

from server.lights.dmx import (ArtNetTransport, DMXLight, DMXUniverse,
                               NullTransport, build_universe)


def profile(**kw):
    p = {"dmx_start": 1, "channels": {"intensity": 1, "cct": 2},
         "cct_min_k": 2800, "cct_max_k": 6500}
    p.update(kw)
    return p


def make(**kw):
    u = DMXUniverse(NullTransport())
    return DMXLight("key", profile(**kw), u), u


def test_intensity_mapping():
    l, u = make()
    l.apply(50, 2800)
    assert u.get(1) == 128
    l.apply(0, 2800)
    assert u.get(1) == 0
    l.apply(100, 2800)
    assert u.get(1) == 255


def test_cct_mapping_and_inversion():
    l, u = make()
    l.apply(10, 2800)
    assert u.get(2) == 0
    l.apply(10, 6500)
    assert u.get(2) == 255
    l, u = make(cct_inverted=True)
    l.apply(10, 2800)
    assert u.get(2) == 255
    l.apply(10, 6500)
    assert u.get(2) == 0


def test_dmx_start_addressing():
    l, u = make(dmx_start=10)
    l.apply(100, 6500)
    d = u.snapshot()
    assert d[9] == 255 and d[10] == 255
    assert d[0] == 0 and d[11] == 0


def test_fine_intensity():
    l, u = make(channels={"intensity": 1, "cct": 2, "fine_intensity": 3})
    l.apply(100, 2800)
    assert (u.get(1), u.get(3)) == (255, 255)
    l.apply(50, 2800)
    assert u.get(1) * 256 + u.get(3) == 32768


def test_connected_and_set_pushes_frame():
    l, u = make()
    assert l.connect() and l.connected
    l.set(50, 6500)
    assert u.transport.last_frame[0] == 128


def test_artnet_header():
    for uni, subuni, net in [(0, 0, 0), (0x0105, 0x05, 0x01)]:
        t = ArtNetTransport("127.0.0.1", uni)
        pkt = t.build_packet(bytes(512))
        assert pkt[:8] == b"Art-Net\x00"
        assert pkt[8:10] == b"\x00\x50"
        assert pkt[10:12] == b"\x00\x0e"
        assert pkt[12] == 1 and pkt[13] == 0
        assert pkt[14] == subuni and pkt[15] == net
        assert pkt[16:18] == b"\x02\x00"
        assert len(pkt) == 18 + 512
        t.close()


def test_artnet_sequence_wraps():
    t = ArtNetTransport("127.0.0.1", 0)
    seqs = [t.build_packet(bytes(512))[12] for _ in range(300)]
    assert seqs[0] == 1 and seqs[254] == 255 and seqs[255] == 1 and 0 not in seqs
    t.close()


def test_refresh_thread():
    u = DMXUniverse(NullTransport())
    u.start(fps=50)
    time.sleep(0.2)
    u.stop()
    n = len(u.transport.frames)
    assert n >= 2
    assert not u.running
    time.sleep(0.1)
    assert len(u.transport.frames) == n


def test_build_universe_fallbacks():
    assert isinstance(build_universe({"transport": "none"}).transport, NullTransport)
    u = build_universe({"transport": "serial", "port": "/dev/does-not-exist"})
    assert isinstance(u.transport, NullTransport)
