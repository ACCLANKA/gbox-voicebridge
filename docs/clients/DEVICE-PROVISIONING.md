# Device Provisioning & Factory Reset

## Bluetooth

| Name | Purpose |
| --- | --- |
| `Gbox-XXXX` | Classic BT **HFP** link — carries call audio |
| `Gbox` | BLE/Classic name for **setup & control** |

Pair **`Gbox-XXXX`** for audio. Use the app/`Gbox` for setup.

## Wi-Fi setup

### Option A — App (BLE)
Open the app -> **Add device** -> enter SSID + password -> **Send**. Credentials
go to the device over the encrypted BLE control channel.

### Option B — SoftAP captive portal
1. Join `GboxWifi-XXXX` (or `GboxWifi`).
2. Open `http://192.168.4.1`.
3. **Scan** or **type** the SSID and password.
4. **Save**; the device joins and reboots into OPERATIONAL.

### Multiple networks
Save up to **4 SSID profiles**. The device tries them in priority order and
reconnects with backoff. Add a backup network for reliability.

## Static IP (optional)
In the portal, enable **Advanced -> Static IP** and enter IP/gateway/DNS.
Otherwise DHCP is used (recommended).

## Reset

| Action | How | Effect |
| --- | --- | --- |
| **Restart** | Short press EN/RST | Keeps Wi-Fi |
| **Re-provision** | Hold EN+BOOT at power-on | Starts `GboxWifi` SoftAP |
| **Factory reset** | Dashboard -> Device -> **Factory reset**, or `POST /api/devices/{id}/factory-reset` | Erases Wi-Fi + pairing + cert; back to provisioning |
| **Remote reboot** | Dashboard -> **Reboot** | Device restarts in place |

> A **factory reset** unlinks the device from your tenant; you must re-add it.

## LED reference

| Pattern | Meaning |
| --- | --- |
| Slow blink | Provisioning / no Wi-Fi |
| Solid | Online, idle |
| Fast blink | Connecting / reconnecting |
| Double-flash | Active call |
| Long off | Powered down / deep sleep |

## Troubleshooting
See the in-app **Troubleshooting** help, or [FAQ.md](FAQ.md).
