# Devices & Controls

Manage every Gbox unit from **Devices**.

## Device states

| State | Meaning |
| --- | --- |
| `provisioning` | Waiting for Wi-Fi credentials (Bluetooth / SoftAP setup) |
| `connecting` | Credentials received; joining the network |
| `online` | Connected, healthy, ready to bridge audio |
| `degraded` | Online but poor signal/quality (watch it) |
| `offline` | No heartbeat; check power and Wi-Fi |

## Adding a device

**Devices → + Add device** creates a record and assigns a serial from your
prefix. The Bluetooth name is **`Gbox`** for every device (so a phone finds it in
one tap); it is overridable per board from NVS (`bt_name`) when several units share
an area. The provisioning SoftAP derives from the serial suffix:

- Serial `GBX-A1B2C3` → Bluetooth `Gbox`, SoftAP `GboxWifi-C3`.

## Controls (per device)

From the **Devices** table or the **Manage** panel:

| Control | Effect |
| --- | --- |
| **Reconnect** | Force the device to re-establish Wi-Fi / media links |
| **Reboot** | Restart the device; it returns to `connecting` |
| **Mute / Unmute** | Silence the device microphone |
| **Speaker volume** | 0-100; raise carefully to avoid echo |
| **Mic gain** | 0-100; match to speaker for balanced audio |
| **AEC on / off** | Acoustic echo cancellation (keep **on** for speakerphone use) |
| **Factory reset Wi-Fi** | Clears saved profiles; device returns to `provisioning` |

> **Echo tip:** if callers hear themselves, enable AEC and lower speaker volume.
> The **Connection Monitor** shows an echo-risk and balance recommendation.

## Assigning projects

Devices belong to a **project** (e.g. Reception, Warehouse). Routes and billing
are often scoped by project. Change the project when you add or edit a device.
