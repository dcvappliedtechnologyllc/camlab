"""Replay captured Godox-app frames to one light and log every reply.
Usage: .venv/bin/python scripts/mesh_replay.py <export> <light> <hexframe> [<hexframe> ...]
Frames are the 8/7-byte vendor payloads exactly as the app log shows them
(after the F01102 opcode), e.g. FC0703020005A6 F0003832000000B1 FE01FFFFFFFFFF48.
A frame may be prefixed with 'sleep:<s>' to pause. Status (FD 01 .. A0) is
queried after each frame. Run from the terminal panel (Bluetooth grant)."""
import asyncio, logging, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.lights import godox_mesh as gm

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
logging.getLogger("bleak").setLevel(logging.WARNING)


async def main(export_path, light, items):
    ex = gm.load_mesh_export(export_path)
    node = ex.find(light)
    net = gm.MeshNetwork(ex.network_key, ex.app_key, src=gm.DEFAULT_SRC, iv=ex.iv_index,
                         state_path=str(export_path) + ".state.json")
    client = gm.GodoxMeshClient(net)
    client.on_ack = lambda src, p: print(f"   <- reply from 0x{src:04X}: {p.hex()} {gm.decode_echo(p)}")
    await client.connect()
    print(f"connected via {client.gateway}; target {node.name} 0x{node.address:04X}")
    try:
        for it in items:
            if it.startswith("sleep:"):
                await asyncio.sleep(float(it[6:])); continue
            frame = bytes.fromhex(it)
            print(f"{time.strftime('%H:%M:%S')} -> 0x{node.address:04X}: {frame.hex()}")
            ack = await client.send_frame(node.address, frame, ack_timeout=0.7)
            if ack is None:
                st = await client.query_status(node.address, timeout=1.5)
                print(f"   status A0: {st.hex() if st else 'none'}")
            await asyncio.sleep(1.0)
    finally:
        net.save_state()
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3:]))
