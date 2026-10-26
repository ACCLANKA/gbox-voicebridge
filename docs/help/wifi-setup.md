# Wi-Fi Setup & Profiles

Voice Bridge connects each device to the best available network automatically,
using a **Wi-Fi profile** model.

## How a device picks a network

On boot the device tries, in order:

1. **Saved profiles**, highest **priority** first (these store SSID + key).
2. **Open networks** that match an allowed profile (no password required).
3. If nothing works, it opens **setup mode**: Bluetooth `Gbox-XXXX` and/or
   SoftAP `GboxWifi-XXXX` with a web panel at **http://192.168.4.1**.

```
Boot -> saved profile? --yes-> connect (priority order)
                       --no --> open network allowed? --yes-> connect
                                                     --no --> SETUP MODE
                                                             (GboxWifi / 192.168.4.1)
```

## On-device setup (the phone flow)

1. Power the device. If it has no working network it appears as:
   - **Bluetooth:** `Gbox-A1B2` (pair with one tap), and
   - **Wi-Fi AP:** `GboxWifi-A1B2`.
2. Connect your phone to `GboxWifi-A1B2`.
3. Open **http://192.168.4.1** in the phone browser.
4. Either **scan and pick** a nearby SSID, or **type the SSID + password**.
5. Press **Connect**. The device joins the network and leaves setup mode.

## From the dashboard

**Wi-Fi & Provisioning** mirrors the same flow server-side:

| Action | What it does |
| --- | --- |
| **Scan networks** | Refreshes the discovered network list (open + requested) |
| **Connect device** | Sends that SSID (+ password) to the selected device |
| **Save password** | Stores a *requested* profile for a secured network |
| **Manual setup** | Type SSID + password directly |
| **Show setup (GboxWifi / 192.168.4.1)** | Shows the device's BT/AP names and portal URL |

## Network types

| Type | Behaviour |
| --- | --- |
| **Open** | Device may join automatically (no password). Marked `open`. |
| **Requested** | Admin saved the SSID + password; device can join automatically. |
| **Unknown / hidden** | Use **Manual setup** to type the SSID + password. |

## Saved profiles

Each device keeps a list of profiles (SSID, security, priority, auto-connect).
The dashboard shows and edits them under **Saved Wi-Fi profiles**. Remove a
profile to stop the device joining that network.

## Limitations

- ESP32 is **2.4 GHz only** — 5 GHz-only networks cannot be joined.
- Enterprise / 802.1X networks are out of scope for now.
