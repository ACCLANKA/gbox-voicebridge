# Troubleshooting

## Device never leaves `provisioning`

- Confirm you selected the correct device before pressing **Connect device**.
- If the device has no network at all, join `GboxWifi-XXXX` and open
  `http://192.168.4.1`, then pick or type the SSID.
- Make sure the SSID is **2.4 GHz** — ESP32 cannot join 5 GHz-only networks.

## Device shows `offline`

- Check power and that the AP is up.
- Verify the connected IP on your router/DHCP list.
- Look at **Connection Monitor → recent link events** for the last disconnect.

## Callers hear an echo

1. Open the device's **Manage** panel and turn **AEC on**.
2. Lower **speaker volume** (below 80 helps a lot).
3. Re-check **sound balance** in the monitor — aim for *balanced*.

## Audio is choppy / robotic

- Check **MOS / jitter / loss** in the monitor.
- Move the device closer to the AP (target RSSI > -65 dBm).
- Reduce competing traffic on the 2.4 GHz band.

## Frequent drops

- Weak signal or a roaming issue. Add a closer AP or a saved profile for a
  stronger network with higher priority.
- Inspect the **recent link events** table for the pattern (beacon timeout, etc.).

## Can't connect to a secured network from the panel

- Use **Manual setup** and type the exact SSID (case-sensitive) and password.
- For hidden networks, type the SSID manually — scan will not find them.

## WhatsApp number missing on a call

- The number is only available when the companion app can read it.
- Cellular `incoming number` depends on carrier CLI; unknown numbers show
  `unavailable`.

## Reset the demo data

Click **Reset demo data** in the sidebar footer, or `POST /api/demo/reset`.
