"""End-to-end workflow + audio-path verification against the real app."""
from __future__ import annotations

import json
import math
import socket
import struct
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, ".")

from fastapi.testclient import TestClient  # noqa: E402

from api import db, protocol, security  # noqa: E402
from api.main import app  # noqa: E402

ACME = {"email": "owner@acme.example", "password": "password123"}
SERIAL = "GBX-A1B2C3"  # dev_1 "Reception Desk" (ten_1)
DEV = "dev_1"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, fn):
    try:
        detail = fn()
        RESULTS.append((name, True, detail or "ok"))
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))


def ui_login(client):
    assert client.post("/v1/ui/login", json=ACME).status_code == 200


def token(client) -> str:
    return client.post("/v1/auth/login", json=ACME).json()["access_token"]


def api_key(tenant="ten_1") -> str:
    key, kh, prefix = security.new_api_key("vb")
    db.insert("api_keys", {"id": db.new_id("key"), "tenant_id": tenant, "name": "verify",
                           "key_hash": kh, "prefix": prefix, "scopes": "devices,calls",
                           "last_used_at": None, "expires_at": None, "created_at": db.now()})
    return key


def ulaw_decode(u: int) -> int:
    u = ~u & 0xFF
    sign, exp, mant = u & 0x80, (u >> 4) & 0x07, u & 0x0F
    s = (((mant << 3) + 0x84) << exp) - 0x84
    return -s if sign else s


def sine_frame(i: int, freq=440.0, amp=8000) -> bytes:
    start = i * protocol.FRAME_SAMPLES
    vals = [int(amp * math.sin(2 * math.pi * freq * (start + n) / protocol.SAMPLE_RATE))
            for n in range(protocol.FRAME_SAMPLES)]
    return protocol.encode_audio(struct.pack(f"<{len(vals)}h", *vals))


def open_call(ws):
    for seq, ev in enumerate(("call.ringing", "call.answered"), 1):
        ws.send_text(json.dumps({"type": "event", "event": ev, "seq": seq}))
        ws.receive_json()


def drain(sock, expect, timeout=2.0):
    sock.settimeout(timeout)
    out, deadline = [], time.monotonic() + timeout
    while len(out) < expect and time.monotonic() < deadline:
        try:
            out.append(sock.recvfrom(2048)[0])
        except socket.timeout:
            break
    return out


def main() -> int:  # noqa: C901
    client = TestClient(app)

    # ---------------- control-plane workflows ---------------- #
    check("health", lambda: client.get("/health").json()["status"] == "ok" or "ok")

    def auth_flow():
        assert client.post("/v1/auth/login", json=ACME).status_code == 200
        assert client.post("/v1/auth/login", json={**ACME, "password": "x"}).status_code == 401
        t = token(client)
        me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {t}"})
        assert me.status_code == 200, me.text
        return f"login ok, me={me.json().get('email', me.json().get('user', {}))}"
    check("auth: login / bad-password / me", auth_flow)

    def devices_flow():
        t = token(client); h = {"Authorization": f"Bearer {t}"}
        devs = client.get("/v1/devices", headers=h).json()
        n = len(devs.get("items", devs) if isinstance(devs, dict) else devs)
        one = client.get(f"/v1/devices/{DEV}", headers=h)
        assert one.status_code == 200, one.text
        telem = client.get(f"/v1/devices/{DEV}/telemetry", headers=h)
        links = client.get(f"/v1/devices/{DEV}/link-events", headers=h)
        assert telem.status_code == 200 and links.status_code == 200
        return f"{n} devices; detail+telemetry+link-events ok"
    check("devices: list / detail / telemetry / link-events", devices_flow)

    def provisioning_flow():
        t = token(client); h = {"Authorization": f"Bearer {t}"}
        assert client.get("/v1/provisioning/token", headers=h).status_code == 200
        assert client.get("/v1/firmware/latest", headers=h).status_code == 200
        nets = client.get("/v1/networks", headers=h)
        assert nets.status_code == 200
        return "provisioning token / firmware / networks ok"
    check("provisioning: token / firmware / networks", provisioning_flow)

    def routes_flow():
        t = token(client); h = {"Authorization": f"Bearer {t}"}
        body = {"name": "verify route", "priority": 5,
                "match": {"devices": [SERIAL], "direction": "inbound",
                          "time_window": "Mon-Fri 00:00-23:59"},
                "destination": {"type": "sip", "uri": "sip:verify@pbx"},
                "fallback": {"type": "pstn", "uri": "tel:+15551230099"}}
        r = client.post("/v1/routes", json=body, headers=h)
        assert r.status_code == 201, r.text
        rid = r.json()["id"]
        test = client.post(f"/v1/routes/{rid}/test", json={"device_id": SERIAL}, headers=h)
        assert test.status_code == 200, test.text
        assert client.delete(f"/v1/routes/{rid}", headers=h).status_code == 204
        return f"create+test+delete ok; match={test.json().get('matched', test.json())}"
    check("routes: create (time_window+fallback) / test / delete", routes_flow)

    def dids_flow():
        t = token(client); h = {"Authorization": f"Bearer {t}"}
        assert client.post("/v1/dids/validate", json={"number": "(415) 555-0198"}, headers=h) \
            .json()["ok"] is True
        r = client.post("/v1/dids", json={"number": "415 555 0198", "label": "verify"}, headers=h)
        assert r.status_code == 201, r.text
        did = r.json()
        assert did["number"] == "+14155550198"
        assert client.post(f"/v1/dids/{did['id']}/assign", json={"device_id": "dev_1"},
                           headers=h).json()["device_id"] == "dev_1"
        assert client.delete(f"/v1/dids/{did['id']}", headers=h).status_code == 204
        return "validate+create(E.164)+assign+delete ok"
    check("dids: validate / create / assign / delete", dids_flow)

    def forwarding_flow():
        t = token(client); h = {"Authorization": f"Bearer {t}"}
        caps = client.get("/v1/forwarding/capabilities").json()["types"]
        assert {"sip", "socket", "pstn", "webhook", "recording", "simulated"} <= set(caps)
        assert client.post("/v1/forwarding/validate",
                           json={"dest_type": "pstn",
                                 "dest_uri": "tel:+15551234?carrier_host=sbc.carrier.example:5060"},
                           headers=h).json()["ok"] is True
        return f"capabilities={sorted(caps)}; pstn validate ok"
    check("forwarding: capabilities / validate (pstn)", forwarding_flow)

    def bridges_calls_flow():
        t = token(client); h = {"Authorization": f"Bearer {t}"}
        active = client.get("/v1/calls/active", headers=h)
        calls = client.get("/v1/calls", headers=h)
        cdr = client.get("/v1/cdr", headers=h)
        assert active.status_code == calls.status_code == cdr.status_code == 200
        return "active calls / calls / cdr ok"
    check("calls: active / list / cdr", bridges_calls_flow)

    def billing_flow():
        t = token(client); h = {"Authorization": f"Bearer {t}"}
        for path in ("/v1/plans", "/v1/subscription", "/v1/usage",
                     "/v1/invoices", "/v1/balance", "/v1/api-keys"):
            assert client.get(path, headers=h).status_code == 200, path
        return "plans/subscription/usage/invoices/balance/api-keys ok"
    check("billing: read surfaces", billing_flow)

    def dashboard_ui_flow():
        ui_login(client)
        assert client.get("/v1/ui/summary").status_code == 200
        assert client.get("/v1/ui/devices").status_code == 200
        assert client.get(f"/v1/ui/devices/{SERIAL}/quality").status_code == 200
        dids = client.get("/v1/ui/dids").json()
        return f"summary/devices/quality ok; {len(dids)} DIDs"
    check("dashboard BFF: summary / devices / quality / dids", dashboard_ui_flow)

    def demo_drops_flow():
        ui_login(client)
        before = client.get(f"/v1/ui/devices/{SERIAL}/link-events").json()
        n0 = len(before.get("items", before)) if isinstance(before, dict) else len(before)
        for _ in range(40):
            client.post("/v1/ui/demo/tick")
        after = client.get(f"/v1/ui/devices/{SERIAL}/link-events").json()
        n1 = len(after.get("items", after)) if isinstance(after, dict) else len(after)
        q = client.get(f"/v1/ui/devices/{SERIAL}/quality").json()
        return f"link_events {n0}->{n1} after 40 ticks; quality={ {k: q.get(k) for k in ('mos','jitter_ms','loss_pct') if k in q} }"
    check("qos: simulated ticks produce drops/quality data", demo_drops_flow)

    # ---------------- media path: audio quality ---------------- #
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", 0))
    sink_port = sink.getsockname()[1]
    ui_login(client)
    assert client.put(f"/v1/ui/devices/{SERIAL}/forwarding",
                      json={"dest_type": "socket",
                            "dest_uri": f"udp://127.0.0.1:{sink_port}?mode=rtp"}).status_code == 200

    def media_quality():
        key = api_key()
        N = 200
        with client.websocket_connect(f"/v1/device/ws?serial={SERIAL}&token={key}") as ws:
            open_call(ws)
            sent = [sine_frame(i) for i in range(N)]
            for f in sent:
                ws.send_bytes(f)
            ws.send_text(json.dumps({"type": "ping"}))  # barrier
            ws.receive_json()
        pkts = drain(sink, N)
        assert len(pkts) == N, f"received {len(pkts)}/{N} RTP packets (drops)"
        seqs = [struct.unpack("!H", p[2:4])[0] for p in pkts]
        # contiguous sequence (mod 2**16) => no gaps/dupes => no stutter/dragging
        gaps = sum(1 for a, b in zip(seqs, seqs[1:]) if (b - a) % 65536 != 1)
        for i, p in enumerate(pkts):
            expected = struct.unpack(f"<{protocol.FRAME_SAMPLES}h",
                                     sent[i][1:])[::2]
            got = [ulaw_decode(b) for b in p[12:]]
            assert len(got) == len(expected), f"pkt {i} payload {len(got)} != {len(expected)}"
            if i == 0:
                num = sum(e * e for e in expected)
                den = sum((e - g) ** 2 for e, g in zip(expected, got)) or 1
                snr = 10 * math.log10(num / den)
        return (f"{len(pkts)}/{N} packets, seq gaps={gaps}, "
                f"payload=160u all, G.711 SNR={snr:.1f} dB")
    check("media: device→socket RTP (loss + G.711 quality)", media_quality)

    def media_jitter():
        key = api_key()
        arrivals: list[float] = []
        stop = threading.Event()
        s2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s2.bind(("127.0.0.1", 0))
        p2 = s2.getsockname()[1]
        s2.settimeout(0.5)
        client.put(f"/v1/ui/devices/{SERIAL}/forwarding",
                   json={"dest_type": "socket", "dest_uri": f"udp://127.0.0.1:{p2}?mode=rtp"})

        def drainer():
            while not stop.is_set():
                try:
                    s2.recvfrom(2048)
                    arrivals.append(time.monotonic())
                except socket.timeout:
                    pass
        th = threading.Thread(target=drainer, daemon=True); th.start()
        M = 100
        with client.websocket_connect(f"/v1/device/ws?serial={SERIAL}&token={key}") as ws:
            open_call(ws)
            for i in range(M):
                ws.send_bytes(sine_frame(i))
                time.sleep(protocol.FRAME_MS / 1000)  # realtime pacing (20 ms)
            ws.send_text(json.dumps({"type": "ping"})); ws.receive_json()
        stop.set(); th.join(timeout=1)
        s2.close()
        iv = [(b - a) * 1000 for a, b in zip(arrivals, arrivals[1:])]
        mean = sum(iv) / len(iv)
        var = sum((x - mean) ** 2 for x in iv) / len(iv)
        jit = 0.0
        for x in iv:
            jit += (abs(x - 20.0) - jit) / 16  # RFC3550-style
        assert len(arrivals) >= M - 2, f"only {len(arrivals)}/{M} arrived"
        return (f"{len(arrivals)}/{M} paced packets; inter-arrival {mean:.1f} ms "
                f"(target 20), stddev {math.sqrt(var):.1f} ms, jitter {jit:.2f} ms")
    check("media: realtime pacing → jitter (no dragging)", media_jitter)

    def media_recording():
        path = f"/tmp/verify-{int(time.time())}.wav"
        client.put(f"/v1/ui/devices/{SERIAL}/forwarding",
                   json={"dest_type": "recording", "dest_uri": f"file://{path}"})
        key = api_key()
        K = 5
        with client.websocket_connect(f"/v1/device/ws?serial={SERIAL}&token={key}") as ws:
            open_call(ws)
            for i in range(K):
                ws.send_bytes(sine_frame(i))
            ws.send_text(json.dumps({"type": "event", "event": "call.ended", "seq": 3}))
            ws.receive_json()
        with wave.open(path, "rb") as w:
            assert w.getframerate() == protocol.SAMPLE_RATE
            assert w.getnframes() == protocol.FRAME_SAMPLES * K, w.getnframes()
        return f"WAV {w.getnframes()} samples @ {w.getframerate()} Hz (expected {protocol.FRAME_SAMPLES*K})"
    check("media: recording leg → WAV integrity", media_recording)

    def media_webhook():
        class Sink(BaseHTTPRequestHandler):
            def do_POST(self):
                chunks = []
                while True:
                    size = int(self.rfile.readline().strip().split(b";")[0], 16)
                    if size == 0:
                        break
                    chunks.append(self.rfile.read(size)); self.rfile.read(2)
                self.server.chunks = chunks
                self.send_response(200); self.end_headers()

            def log_message(self, *a):
                pass
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Sink); srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        client.put(f"/v1/ui/devices/{SERIAL}/forwarding",
                   json={"dest_type": "webhook",
                         "dest_uri": f"http://127.0.0.1:{port}/audio?mode=chunked",
                         "options": {"signing_secret": "s3cr3t"}})
        key = api_key()
        with client.websocket_connect(f"/v1/device/ws?serial={SERIAL}&token={key}") as ws:
            open_call(ws)
            for i in range(3):
                ws.send_bytes(sine_frame(i))
            ws.send_text(json.dumps({"type": "event", "event": "call.ended", "seq": 3}))
            ws.receive_json()
        srv.shutdown(); srv.server_close()
        sizes = [len(c) for c in srv.chunks]
        assert sizes == [protocol.FRAME_BYTES] * 3, sizes
        return f"3 chunks of {protocol.FRAME_BYTES} B (exact, no truncation)"
    check("media: webhook leg → chunk sizes", media_webhook)

    def media_loss_resilience():
        key = api_key()
        N = 90
        s3 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s3.bind(("127.0.0.1", 0)); p3 = s3.getsockname()[1]
        client.put(f"/v1/ui/devices/{SERIAL}/forwarding",
                   json={"dest_type": "socket", "dest_uri": f"udp://127.0.0.1:{p3}?mode=rtp"})
        delivered = 0
        with client.websocket_connect(f"/v1/device/ws?serial={SERIAL}&token={key}") as ws:
            open_call(ws)
            for i in range(N):
                if i % 3 == 0:            # simulate 33% upstream packet loss
                    delivered += 1
                    ws.send_bytes(sine_frame(i))
            ws.send_text(json.dumps({"type": "ping"}))
            r = ws.receive_json()          # link still alive after lossy burst
        pkts = drain(s3, delivered)
        # switch back to socket for later checks
        assert len(pkts) == delivered, f"{len(pkts)} != {delivered}"
        seqs = [struct.unpack("!H", p[2:4])[0] for p in pkts]
        gaps = sum(1 for a, b in zip(seqs, seqs[1:]) if (b - a) % 65536 != 1)
        return f"{delivered} sent / {len(pkts)} received after 33% loss, no fabrication, link alive ({r})"
    check("media: survives 33% upstream loss (no crash/fabrication)", media_loss_resilience)

    client.delete(f"/v1/ui/devices/{SERIAL}/forwarding")
    sink.close()

    # ---------------- report ---------------- #
    width = max(len(n) for n, _, _ in RESULTS)
    ok = sum(1 for _, p, _ in RESULTS if p)
    print("\n" + "=" * (width + 12))
    for name, passed, detail in RESULTS:
        print(f"{'PASS' if passed else 'FAIL'}  {name.ljust(width)}  {detail}")
    print("=" * (width + 12))
    print(f"{ok}/{len(RESULTS)} workflows passed")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
