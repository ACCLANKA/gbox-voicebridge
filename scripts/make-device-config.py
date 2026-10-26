#!/usr/bin/env python3
"""Build a per-device NVS image so Wi-Fi/server credentials never leave this
machine (they are NOT compiled into the published firmware image).

The firmware reads namespace ``vb`` from NVS at boot and falls back to the
menuconfig defaults when a key is absent (see firmware/main/vb_config.c).

Run this on the developer/field machine that has ESP-IDF sourced:

    . $IDF_PATH/export.sh
    python scripts/make-device-config.py \
        --ssid "MyNetwork" --password "s3cret" \
        --host 192.168.1.50 --port 8080 \
        --serial GBX-A1B2C3 --key vb_XXXXXXXX \
        --out build/nvs-config.bin

Then flash the app + this NVS image (NVS partition starts at 0x9000 by default):

    esptool.py --chip esp32 --port COMx write_flash \
        0x9000 build/nvs-config.bin 0x0 voicebridge-esp32-merged.bin

Generate *separately* per board; keep the file off version control.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

NVS_OFFSET = "0x9000"
NVS_SIZE = "0x6000"  # 24K, matches the default single-app partition table


def _idf_python() -> str:
    """The interpreter that can import the bundled NVS generator.

    ``sys.executable`` may be the system python (e.g. when this script is called
    from a tool that is not inside the ESP-IDF venv), which does not have
    ``esp_idf_nvs_partition_gen`` on its path. Prefer the IDF venv's python.
    """
    env = os.environ.get("IDF_PYTHON_ENV_PATH")
    if env:
        for rel in ("bin/python", "bin/python3", "Scripts/python.exe"):
            cand = Path(env) / rel
            if cand.exists():
                return str(cand)
    return sys.executable


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="make-device-config", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ssid", required=True)
    p.add_argument("--password", default="")
    p.add_argument("--host", required=True, help="ingest host / LAN IP")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--tls", action="store_true", help="use wss:// (default ws://)")
    p.add_argument("--serial", required=True, help="device serial, must exist in the tenant")
    p.add_argument("--key", required=True, help="tenant API key (vb_...)")
    p.add_argument("--sntp", default="pool.ntp.org", help="SNTP server (for wss://)")
    p.add_argument("--bt-name", default=None,
                   help="Bluetooth name advertised to phones (default: firmware's Gbox)")
    p.add_argument("--audio", choices=["on", "off"], default=None,
                   help="mic capture override (default: firmware compile-time value)")
    p.add_argument("--bt", choices=["on", "off"], default=None,
                   help="Bluetooth HFP call ingress override (default: firmware value)")
    p.add_argument("--silence", choices=["on", "off"], default=None,
                   help="stream zero frames when idle (link diagnostic; default off)")
    p.add_argument("--auto-answer", dest="auto_answer", choices=["on", "off"], default=None,
                   help="answer incoming HFP calls without the cloud (default off)")
    p.add_argument("--i2s-bclk", type=int, default=None, help="override I2S BCLK GPIO")
    p.add_argument("--i2s-ws", type=int, default=None, help="override I2S WS GPIO")
    p.add_argument("--i2s-din", type=int, default=None, help="override I2S DIN GPIO")
    p.add_argument("--i2s-slot", choices=["left", "right"], default=None,
                   help="mic L/R slot (default left: INMP441 L/R->GND)")
    p.add_argument("--out", default="nvs-config.bin")
    p.add_argument("--idf-path", default=os.environ.get("IDF_PATH", ""))
    args = p.parse_args(argv)

    gen = os.path.join(args.idf_path, "components", "nvs_flash",
                       "nvs_partition_generator", "nvs_partition_gen.py")
    if not os.path.isfile(gen):
        print("error: nvs_partition_gen.py not found; source ESP-IDF first "
              "(. $IDF_PATH/export.sh) or pass --idf-path", file=sys.stderr)
        return 2

    rows = [
        "key,type,encoding,value",
        "vb,namespace,,",
        f"ssid,data,string,{args.ssid}",
        f"pass,data,string,{args.password}",
        f"host,data,string,{args.host}",
        f"port,data,i32,{args.port}",
        f"tls,data,i32,{1 if args.tls else 0}",
        f"serial,data,string,{args.serial}",
        f"key,data,string,{args.key}",
        f"sntp,data,string,{args.sntp}",
    ]
    # Mic overrides are only written when asked, so the firmware's compile-time
    # defaults apply otherwise. Lets the bench sweep pinouts with no rebuild.
    if args.bt_name is not None:
        rows.append(f"bt_name,data,string,{args.bt_name}")
    if args.bt is not None:
        rows.append(f"bt_enable,data,i32,{1 if args.bt == 'on' else 0}")
    if args.silence is not None:
        rows.append(f"silence_enable,data,i32,{1 if args.silence == 'on' else 0}")
    if args.auto_answer is not None:
        rows.append(f"auto_answer,data,i32,{1 if args.auto_answer == 'on' else 0}")
    if args.audio is not None:
        rows.append(f"audio_enable,data,i32,{1 if args.audio == 'on' else 0}")
    if args.i2s_bclk is not None:
        rows.append(f"i2s_bclk,data,i32,{args.i2s_bclk}")
    if args.i2s_ws is not None:
        rows.append(f"i2s_ws,data,i32,{args.i2s_ws}")
    if args.i2s_din is not None:
        rows.append(f"i2s_din,data,i32,{args.i2s_din}")
    if args.i2s_slot is not None:
        rows.append(f"i2s_slot,data,i32,{1 if args.i2s_slot == 'right' else 0}")
    csv = "\n".join(rows) + "\n"
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
        f.write(csv)
        csv_path = f.name

    try:
        subprocess.run([_idf_python(), gen, "generate", csv_path, args.out,
                        NVS_SIZE], check=True)
    finally:
        os.unlink(csv_path)

    print(f"\nwrote {args.out} ({os.path.getsize(args.out)} bytes)")
    print("flash with (esptool v5 — module form, no esptool.py, no -z):")
    print(f"  python -m esptool --chip esp32 --port COMx write-flash {NVS_OFFSET} {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
