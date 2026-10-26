# Monitoring & Alerts

The **Connection Monitor** is the live health view of your fleet. It refreshes
automatically over a server-sent event (SSE) stream.

## Connected IP

Each device reports the IP address it obtained from the network. Use it to
correlate with your router, firewall or DHCP logs.

## Connection drops

A drop is recorded whenever the device's Wi-Fi or media link is lost and later
restored. The monitor shows:

- total **drops** per device,
- **stability %** (share of recent samples that were online),
- a **recent link events** table (connect / disconnect / reconnect).

Frequent drops usually mean weak signal (low RSSI) or an overloaded access point.

## Quality metrics

| Metric | Good | Watch | Bad |
| --- | --- | --- | --- |
| **MOS** | ≥ 4.0 | 3.5-4.0 | < 3.5 |
| **Jitter** | < 15 ms | 15-30 ms | > 30 ms |
| **Packet loss** | < 0.5% | 0.5-1% | > 1% |
| **RSSI** | > -65 dBm | -65 to -75 dBm | < -75 dBm |

## Sound balance

The monitor compares **speaker volume** and **mic gain** and flags imbalance:

- **mic hot** (imbalance negative): callers hear you too loud / background noise.
- **speaker hot** (imbalance positive): risk of echo and feedback.
- A **balanced** device shows ✔ and needs no change.

When echo risk is high the monitor recommends enabling **AEC** or lowering the
speaker volume.

## Alerts (typical rules)

- Device **offline** for more than 2 minutes.
- **MOS < 3.5** sustained over a call.
- More than **3 drops** in an hour.
- **Echo risk high** (AEC off + high volume).

Wire these to email/Slack/webhook in your deployment; the API exposes the
underlying data at `/api/monitor/overview` and `/api/stream`.
