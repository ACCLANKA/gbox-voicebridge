# Getting Started

Welcome to **Voice Bridge** — the control plane for ESP32-to-cloud voice routing.

## 1. Open the dashboard

Open the web dashboard (default `http://127.0.0.1:8080/`). The left sidebar is
your main navigation:

| Section | What it does |
| --- | --- |
| **Overview** | Fleet KPIs and any live calls at a glance |
| **Devices** | Add, configure and control each Gbox device |
| **Wi-Fi & Provisioning** | Scan networks, save profiles, set up a device |
| **Connection Monitor** | Live drops, quality, sound balance, connected IP |
| **Calls** | Active call detail + call history (CDR) |
| **Device ID Settings** | Serial prefix used for new devices |
| **Help & Docs** | These guides |

## 2. Add your first device

1. Go to **Devices → + Add device**.
2. Give it a name (e.g. "Reception Desk"). A serial is generated automatically
   using your Device ID prefix (see **Device ID Settings**).
3. The device starts in `provisioning` state.

## 3. Connect it to Wi-Fi

1. Open **Wi-Fi & Provisioning**.
2. Pick the device, then either:
   - press **Scan networks** and click **Connect device** next to a network, or
   - type the **SSID + password** under **Manual setup** and press **Save & connect**.
3. The device's status turns `online` with a connected IP.

See [Wi-Fi Setup](wifi-setup) for the on-device `GboxWifi` / `192.168.4.1` flow.

## 4. Watch it live

Open **Connection Monitor** to see, per device:

- connection status and drops,
- quality (MOS, jitter, loss, latency),
- sound balance (speaker vs mic),
- connected IP and SSID.

## 5. Make a call

Pair a phone to the device's Bluetooth name (e.g. `Gbox-A1B2`). Incoming calls
appear under **Calls** with the source app and, when available, the WhatsApp
number and caller name. See [Calls & Detection](calls).

> The demo data loaded on first run lets you explore everything without hardware.
> Press **Reset demo data** in the sidebar footer to restore it.
