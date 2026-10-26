# Quick Start (15 minutes)

## 1. Power the device
Plug in USB-C. The **STATUS LED** blinks slowly = ready to set up.

## 2. Join it to Wi-Fi

**Phone app (recommended):** open the Voice Bridge app, tap **Add device**, and
follow the prompt. The app sends your Wi-Fi details over Bluetooth.

**Manual:** on your phone's Wi-Fi list join **`GboxWifi-XXXX`**, open
**`http://192.168.4.1`**, pick your network and enter the password, then **Save**.

> Use a **2.4 GHz** network. The device cannot join 5 GHz-only networks.

When the LED goes **solid**, the device is online. Check it in the dashboard
(device shows **online** with an IP address).

## 3. Pair the phone (audio source)
On the phone's Bluetooth settings, pair **`Gbox-XXXX`** (shown in the dashboard).
This makes the phone's call audio flow through the device.

## 4. Make a test call
1. Call the device's phone from another phone.
2. Answer on the phone that is paired to the Gbox.
3. In the dashboard, open **Calls** — you should see the live call, its
   number, duration and quality (MOS).

## 5. Route it (optional)
By default the device routes to your plan's default destination. To send it to
your own PBX/VPS, open **Routes** and add a destination — see
[ROUTING-SIP.md](ROUTING-SIP.md).

## 6. Done
You now have a working Voice Bridge. Next: read [QUALITY.md](QUALITY.md) to
tune audio, or [ROUTING-SIP.md](ROUTING-SIP.md) to integrate with your PBX.
