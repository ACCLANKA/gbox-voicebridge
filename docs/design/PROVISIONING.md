# Device Provisioning — "Gbox" Bluetooth & "GboxWifi" SoftAP

How a customer gets an ESP32 board online with minimal effort, and how the board
gets Wi-Fi credentials, tenant binding, and its cloud config.

## 1. Design Goals

- **One tap from the phone.** The device appears as **`Gbox`** in the phone's
  Bluetooth list and connects easily.
- **Works with no app and no Wi-Fi.** When Wi-Fi is unavailable, the device
  opens a **`GboxWifi`** access point and a web panel at **`http://192.168.4.1`**.
- **Update Wi-Fi by search or by typing.** The portal lists nearby networks
  (scan) and also accepts a manually typed SSID + password.
- **Secure and idempotent.** Credentials are validated before switching to
  operation; no half-configured state.

## 2. Two Setup Paths

```
                    Device powered on
                           |
              valid Wi-Fi creds in NVS?
                  /                   \
               yes                     no
                |                       |
       CONNECTING -> OPERATIONAL   PROVISIONING MODE
                                        |
                 +----------------------+----------------------+
                 |                                             |
   Path B: SoftAP "GboxWifi"                    Path A: Bluetooth "Gbox"
   (works headless, no app)                     (easy from phone / app)
   captive portal @ http://192.168.4.1          BT name "Gbox" for one-tap pair
```

Both paths end with the same result: `{ssid, psk}` saved to NVS, then the device
joins Wi-Fi and continues to claim/bind + fetch config.

## 2. Bluetooth name: `Gbox`

- The device advertises the **Classic BT / BLE name `Gbox`** so it shows up as
  "Gbox" at the top of the phone's Bluetooth screen and pairs with one tap.
- **Naming with many units:** if several `Gbox` devices are in range, add a
  short suffix to disambiguate, e.g. **`Gbox-A1B2`** (last 4 of MAC/serial).
  Keep the human-friendly prefix so users still recognize it.
  - Default: `Gbox`
  - Recommended when shipping many: `Gbox-<XXXX>` (configurable).
- **BLE GATT (companion-app path):** expose a small provisioning service:
  - `device_name` (read) — e.g. `Gbox-A1B2`, serial, fw version
  - `wifi_scan` (read) — triggers scan, returns `[{ssid, rssi, secure}]`
  - `wifi_config` (write) — `{ssid, psk}` -> device tries to join
  - `status` (notify) — `connecting | connected | failed:<reason>`
  - `claim` (write) — provisioning/claim token
- Requires a pairing code shown on the device label / first-boot serial log so a
  nearby phone cannot silently provision it.

## 3. SoftAP: `GboxWifi` + captive portal

### 3.1 Access point
| Setting | Value |
| --- | --- |
| SSID | `GboxWifi` (or `GboxWifi-<XXXX>` when many units coexist) |
| Security | WPA2, password on the device label (configurable); open fallback discouraged |
| Gateway / portal | **192.168.4.1** |
| DHCP | Device hands out `192.168.4.2–254` |
| Captive portal | DNS catches all domains -> portal; OS "sign in to network" prompt |

### 3.2 What the user does
1. On the phone, connect to Wi-Fi **`GboxWifi`** (or tap the OS captive-portal prompt).
2. Browser opens **`http://192.168.4.1`**.
3. The page **scans for nearby Wi-Fi networks** and shows a list (SSID + signal).
4. The user **picks an SSID** or **types SSID + password** manually.
5. Press **Connect**. The device saves to NVS and joins the real network.

### 3.3 Captive portal (implemented with ESP-IDF `esp_http_server` + `mdns` + DNS hijack)
> Use the ESP-IDF `wifi_provisioning` SoftAP transport as a base, then add the
> scan-list + custom UI. Alternate frameworks: `WiFiManager` / `Improv Wi-Fi`.

| Route | Method | Purpose | Response |
| --- | --- | --- | --- |
| `/` | GET | Setup page (HTML/JS) | Setup UI |
| `/api/scan` | GET | List nearby SSIDs | `[{ "ssid":"Home","rssi":-52,"secure":true }]` |
| `/api/config` | POST | Save `{ssid, psk}` and start connect | `{ "status":"connecting" }` |
| `/api/status` | GET | Provisioning/connection state | `{ "state":"connected","ip":"...","rssi":-58 }` |
| `/api/device` | GET | Device identity | `{ "name":"Gbox-A1B2","serial":"...","fw":"1.0.3" }` |

Example:
```
GET  http://192.168.4.1/api/scan
POST http://192.168.4.1/api/config   {"ssid":"Office","psk":"secret123"}
GET  http://192.168.4.1/api/status   -> {"state":"connected","ip":"192.168.1.42"}
```

### 3.4 Behavior rules
- Portal is **setup-only**: it does not expose call/control APIs.
- SoftAP shuts down after a **successful** Wi-Fi join (or after a timeout,
  default 10 min, then re-opens if still unconfigured).
- Wrong password -> `/api/status` returns `failed:auth`; portal stays open to retry.
- If the saved network later disappears, the device retries with backoff; after
  **N** failed attempts it re-opens `GboxWifi` for re-setup.

## 4. End-to-End Provisioning Sequence

```
1. Power on. Read Wi-Fi creds + claim token from NVS.
2a. Creds present  -> CONNECTING -> OPERATIONAL.
2b. No creds       -> enter PROVISIONING MODE:
        BT name "Gbox" advertised  +  SoftAP "GboxWifi" @ 192.168.4.1
3. User supplies {ssid, psk} via BT (app) or the web panel.
4. Device saves to NVS, connects, verifies gateway + DNS + cloud reachability.
5. Device claims itself (or uses pre-provisioned claim token):
       POST /v1/provision/claim {serial, claim_token} -> tenant_id, cert, config
6. Device stores device certificate + config, reboots into OPERATIONAL.
7. OPTIONAL: user pairs BT "Gbox" to the phone as a headset (audio path).
8. Operate: BT HFP audio -> Wi-Fi/SRTP -> cloud -> route.
```

## 5. Security

| Concern | Control |
| --- | --- |
| Over-the-air credential exposure | SoftAP is WPA2 and/or **setup-use only**, time-boxed |
| Silent provisioning by a stranger nearby | BT pairing code + SoftAP password on device label |
| Replay / cloning of claim tokens | Single-use, short-TTL claim tokens; per-device certs |
| Rogue portal | Portal serves static UI + local config only; no tenant data |
| Persisted secrets | Credentials + private key in **encrypted NVS** / secure element |
| Fleet integrity | Secure Boot + Flash Encryption + signed OTA |
| Tenant correctness | Server **binds serial -> tenant** at claim; rejects mismatches |

## 6. Edge Cases

| Case | Handling |
| --- | --- |
| Several `Gbox` units in range | Disambiguate with `Gbox-XXXX` / `GboxWifi-XXXX` suffix |
| SSID hidden | Allow **manual entry** of SSID + password |
| 5 GHz-only network | Not supported (ESP32 is 2.4 GHz) -> portal shows a hint |
| Enterprise/802.1X Wi-Fi | Out of scope for v1 (documented limitation) |
| Phone can't reach 192.168.4.1 | DNS-capture + captive-portal prompt; also print URL in serial log |
| User forgets device | Show serial/label; factory reset (button/`AT+RESET`) re-opens `GboxWifi` |
| Interrupted setup | NVS writes are atomic; device retries cleanly on next boot |

## 7. Device Identity

- **Serial:** unique per unit; on the label and via BT `/api/device`.
- **BT/AP names:** derived from serial suffix -> `Gbox-XXXX`, `GboxWifi-XXXX`.
- **Claim token:** printed on a card/label; entered in the portal or sent via BT
  to bind the device to the right tenant/account.

## 8. API Additions (server side)

See [../api/API.md](../api/API.md#provisioning). Summary:

| Method | Path | Description |
| --- | --- | --- |
| POST | `/v1/provision/claim` | Device redeems a claim token -> tenant, cert, config |
| GET | `/v1/provision/tokens` | Tenant lists/creates provisioning tokens |
| POST | `/v1/provision/tokens` | Create a token (optionally bound to a project) |
| DELETE | `/v1/provision/tokens/{id}` | Revoke a token |
| GET | `/v1/provision/manifest` | Device fetches config/OTA manifest after claiming |
