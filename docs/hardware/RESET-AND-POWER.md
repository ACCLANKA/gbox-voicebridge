# Reset & Power Design

All the ways a Voice Bridge device can reset, and what each one does.

## 1. Reset sources

| Source | Trigger | Scope | Result |
| --- | --- | --- | --- |
| **POR (power-on)** | Power applied | Both boards | Full boot, `BOOT` state, load NVS |
| **EN / RST button** | User presses EN | One board | Chip reboot, links re-handshake |
| **Software reset** | `esp_restart()` | One board | Fast reboot, NVS kept |
| **Watchdog (TWDT)** | Task starvation | Offending board | Reboot + `boot_reason=WDT`, logged |
| **Brown-out (BOD)** | Vcc < ~2.5 V | Both | Safe reboot, `boot_reason=BOD` |
| **Deep sleep wake** | Timer/GPIO | Board B | Wake, re-join Wi-Fi, resume |
| **Peer reset** | `RESET_PEER` GPIO | Other board | Recover a wedged peer |
| **Factory reset** | API (`factory-reset`) | Device | Erase Wi-Fi profiles + pairing, back to `PROVISIONING` |

## 2. Coordination between boards

```
        RESET_PEER (active-low, each drives the other)
   A  o----------------------------------------------o  B
        STATUS (heartbeat square-wave / handshake)
   A  o----------------------------------------------o  B
```

- Board A senses Board B's `STATUS`; if it stops ticking, A resets B once.
- Board B does the same for A.
- **A reset of B does NOT drop the phone's BT link** — the call survives the
  Wi-Fi hiccup; Board A comfort-mutes until B re-syncs.

## 3. Watchdog strategy

- **Task WDT** on `audio_in`, `audio_out`, `bt_hfp`, `net` (subscribed tasks).
- Each task feeds the WDT only after completing real work (no dummy feeds).
- On timeout: dump task state to RTC-retained RAM, then reboot.
- Boot counter: 3 consecutive WDT boots -> boot into **safe mode** (Wi-Fi up,
  audio disabled) so the unit can be remotely recovered/OTA'ed.

## 4. Persisted state (NVS)

| Key | Survives reset? | Survives factory reset? |
| --- | --- | --- |
| Wi-Fi profiles | Yes | No (erased) |
| Device cert/key | Yes | No |
| Volume / mic gain | Yes | Yes |
| AEC enabled | Yes | Yes |
| Boot counter / last reason | Yes | Yes |
| SDP/pairing info | Yes | No |

## 5. Boot reason reporting

Every boot reports `boot_reason` + `boot_count` in telemetry so the dashboard can
show flapping devices (`BOD`, `WDT`, `SW`, `POR`). Three rapid `WDT`/`BOD`
reboots raise an alert — see MONITORING.

## 6. Manual reset UX

- **Short press EN:** restart (keeps Wi-Fi).
- **Hold EN + BOOT at boot:** enter **provisioning / recovery** (starts
  `GboxWifi` SoftAP at `192.168.4.1`).
- **Factory reset** is a dashboard/API action, not a casual button press.
