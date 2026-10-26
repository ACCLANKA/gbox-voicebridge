# Open Questions & Decisions Needed

Answers drive architecture. Please confirm or correct these.

## A. Hardware / Bluetooth
1. **"HDF support"** — confirm meaning: (a) HFP **mSBC wideband (16 kHz)** on the BT
   link, or (b) actual **BLE LE Audio / LC3**? Plain ESP32-WROOM cannot do LE Audio
   *audio*; option (b) needs a newer SoC (ESP32-C5/H-series or dedicated LE Audio).
2. Is BT **Classic HFP** the only audio use case, or must the device also act as a
   **BLE audio sink/source**?
3. Audio quality target: **wideband (16 kHz)** sufficient, or do you need higher?
4. Do devices need **speaker + mic both**, or mic-only capture for some models?
5. Volume of units (affects OTA infra, cert provisioning, secure element cost).

## B. Telephony / Routing
6. **PBX ownership:** does the customer bring their own PBX (BYO-PBX, we register
   to it), or do we host a PBX for them, or both?
7. **PSTN:** who owns the carrier relationship? Which countries/numbers?
8. **DIDs / inbound numbers:** how are numbers assigned to tenants and billed?
9. **Emergency calls (911/112):** in scope? (Big regulatory implications.)
10. **WhatsApp:** is voice *calling* mandatory, or is messaging + callback acceptable
    until a voice path is approved? Which BSP/region?

## C. Product / Business
11. Pricing model: **subscription**, **usage (per minute)**, **device fee**, or mix?
12. **Prepaid credits vs postpaid invoicing** — which is primary?
13. Who is the buyer: SOHO, call centers, enterprises, hobbyists?
14. SLA expectations per plan tier?
15. Recording: default on/off; retention; jurisdictions (consent laws)?
16. Target countries at launch (drives PoPs, carriers, compliance).

## D. Technical
17. Preferred backend language (Go/Node/Python) and cloud (AWS/GCP/Azure/self-host)?
18. Self-hosted PBX engine choice — **FreeSWITCH** vs **Asterisk** vs managed?
19. SFU choice — **LiveKit** vs **Janus** vs other, given browser listen-in needs?
20. Latency target: is **< 250 ms** mouth-to-ear acceptable, or tighter required?
21. Do customers need to **stream raw audio to their own VPS**? Push (RTP/WebSocket)
    or pull? What protocol/SDK do we promise?
22. Mobile app for provisioning (iOS/Android) — build it, or web-based BLE (limited
    browser BLE support — may force a native app)?

## E. Compliance
23. Call recording consent handling responsibility (us vs customer)?
24. KYC/toll-fraud liability model with the carrier?
25. Data residency requirements per region?

> Recommended defaults (if you have no preference): mSBC wideband; BYO-PBX + hosted
> option; prepaid + subscription mix; Go media/control + Next.js dashboard;
> FreeSWITCH; LiveKit; one launch region.
