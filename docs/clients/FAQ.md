# Client FAQ

**Do I need a WhatsApp API / Business account?**
No. The phone connects to the Gbox as a **Bluetooth headset**; audio bridges
without any provider API. See [BLUETOOTH-HEADSET-MODE.md](BLUETOOTH-HEADSET-MODE.md).

**Can I use 5 GHz Wi-Fi?**
No. The device is **2.4 GHz only**. Publish a 2.4 GHz SSID (or enable 2.4 GHz on
your dual-band router).

**How many devices can I have?**
Any number. Devices are grouped into **projects**, each independently routed.

**Can each device go to a different PBX/VPS?**
Yes. Routes are per device/project, so every device can point anywhere.

**How many devices fit on one server?**
For our hosted pilot, one small VPS handles **~300-500 concurrent calls**
(relay) or a registry of **10,000+ mostly-idle devices**. See the operator
doc `VPS-CAPACITY.md` for the math.

**Will DTMF (IVR "press 1") work?**
On **cellular/PSTN/SIP** calls: yes, reliably. On **WhatsApp** calls: best-effort
only — not guaranteed. See the DTMF guidance in [ROUTING-SIP.md](ROUTING-SIP.md).

**Is the audio good enough for transcription (STT)?**
Yes — default is 16 kHz wideband, Opus with FEC, AEC on, mild NS. See
[QUALITY.md](QUALITY.md).

**What ports must I open?**
Only **outbound** UDP/TCP 443, UDP 3478 and UDP 16384-32767. No inbound needed
unless you run your own PBX.

**How do I reset a device?**
Short press = restart. Hold EN+BOOT at power-on = re-provision. Dashboard =
**Factory reset** (erases Wi-Fi + pairing). See
[DEVICE-PROVISIONING.md](DEVICE-PROVISIONING.md).

**What if the Wi-Fi drops mid-call?**
The device keeps the Bluetooth link alive, comfort-mutes, and reconnects
automatically. The drop is logged in **Connection Monitor**.

**Which codecs are supported?**
Opus (preferred), G.722, PCMU/G.711, PCMA. The bridge negotiates per leg.

**Can I keep the audio on my own server?**
Yes — route SIP to your VPS, or use a **Webhook** route for events/metadata.

**How is billing counted?**
Per bridged call-minute, by project. Plans set included devices; see the
in-app **Billing** help.
