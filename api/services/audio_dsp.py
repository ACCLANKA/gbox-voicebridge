"""Server-side audio conditioning for the Gbox downlink — reference implementation.

The reported pattern is "quiet speech unclear, loud speech sometimes clear, distortion intermittently".
That is a level problem on top of a conversion problem, not a codec problem. See docs/AUDIO-REVIEW.md.

Everything works on float32 in [-1, 1] at the wire rates (8 kHz narrowband, 16 kHz wideband).
"""
import numpy as np

TARGET_DBFS = -20.0
CEILING_DBFS = -1.0


def db_to_lin(d):
    return 10 ** (d / 20.0)


def lin_to_db(x):
    return 20 * np.log10(max(float(x), 1e-12))


def resample(x, src, dst):
    """Band-limited polyphase resampling — never sample-repeat or drop.

    Puts a windowed-sinc low-pass at min(src,dst)/2 before decimation. Without it, everything above
    the new Nyquist folds back into the voice band: the classic harsh, lispy, distorted "telephone"
    sound, worst on loud speech.
    """
    x = np.asarray(x, dtype=np.float64)
    if src == dst:
        return x.astype(np.float32)
    from math import gcd
    g = gcd(int(src), int(dst))
    up, down = int(dst) // g, int(src) // g
    upsampled = np.zeros(len(x) * up, dtype=np.float64)
    upsampled[::up] = x
    cutoff = 0.5 * min(src, dst) / (src * up)
    n = 63
    k = np.arange(n) - (n - 1) / 2
    h = np.sinc(2 * cutoff * k) * np.hanning(n)
    h /= h.sum()
    return (np.convolve(upsampled, h, mode="same") * up)[::down].astype(np.float32)


def normalize(x, target_dbfs=TARGET_DBFS, ceiling_dbfs=CEILING_DBFS):
    """Bring RMS to the target, then soft-limit. Peaks are bent, never flat-topped.

    Quiet speech becomes intelligible without loud speech clipping — the exact symptom pair.
    """
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0:
        return x.astype(np.float32)
    rms = np.sqrt(np.mean(x ** 2))
    if rms > 0:
        x = x * db_to_lin(target_dbfs - lin_to_db(rms))
    ceil = db_to_lin(ceiling_dbfs)
    over = np.abs(x) > ceil
    x[over] = np.sign(x[over]) * ceil * np.tanh(np.abs(x[over]) / ceil)
    return x.astype(np.float32)


def highpass(x, fs=8000, fc=90.0, taps=127):
    """Remove DC and sub-90 Hz rumble — the "added bass" that is really folded energy or an offset.

    Telephony has nothing useful below ~300 Hz; anything down there reads as a thump or a boom.
    FIR: (delta - low-pass), so DC gain is exactly zero and the passband is unity.
    """
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * fc / fs * n) * np.hanning(taps)
    h /= h.sum()                  # low-pass with unity DC gain, as in resample()
    h = -h
    h[(taps - 1) // 2] += 1.0     # delta - low-pass = high-pass (DC gain exactly 0)
    return np.convolve(np.asarray(x, dtype=np.float64), h, mode="same").astype(np.float32)


def echo_gate(uplink, downlink, fs=8000, hold_ms=150, depth_db=40.0):
    """Half-duplex guard: while the downlink is playing (plus the room's echo tail), duck the uplink.

    This is the five-minute fix for a self-listening loop. It costs barge-in, which is why the AEC
    below is the real answer - but it stops the runaway today, and a runaway is what makes the sound
    "get louder and louder."
    """
    up = np.asarray(uplink, dtype=np.float64)
    down = np.abs(np.asarray(downlink, dtype=np.float64))
    n = min(len(up), len(down))
    win = max(1, int(0.01 * fs))
    env = np.convolve(down[:n], np.ones(win) / win, mode="same")
    thr = 10 ** (-45.0 / 20.0)
    hold = int(hold_ms * fs / 1000)
    active = env > thr
    if hold:
        active = np.convolve(active.astype(float), np.ones(hold) / hold, mode="same") > 0
    gain = np.where(active, 10 ** (-depth_db / 20.0), 1.0)
    out = up.copy()
    out[:n] *= gain
    return out.astype(np.float32)


def aec(near, far, mu=0.15, taps=512):
    """NLMS echo canceller: `far` is the downlink reference, `near` is the mic/uplink.

    Removes the agent's own voice from what it hears, so it does not answer itself. Returns
    (cleaned, erle_db) where ERLE is the echo-return-loss enhancement it achieved.
    """
    far = np.asarray(far, dtype=np.float64)
    near = np.asarray(near, dtype=np.float64)
    w = np.zeros(taps)
    buf = np.zeros(taps)
    out = np.empty(len(near))
    for i in range(len(near)):
        buf[1:] = buf[:-1]
        buf[0] = far[i] if i < len(far) else 0.0
        y = float(np.dot(w, buf))
        e = near[i] - y
        out[i] = e
        nrm = float(np.dot(buf, buf)) + 1e-6
        w += (mu * e / nrm) * buf
    half = len(out) // 2  # measure after convergence, not across it
    erle = lin_to_db(np.sqrt(np.mean(near[half:] ** 2)) / max(np.sqrt(np.mean(out[half:] ** 2)), 1e-12))
    return out.astype(np.float32), erle


def conceal(x, gap_samples, fade_ms=20, fs=8000):
    """Fill a lost stretch from the preceding audio with a fade, instead of silence.

    Silence in a call reads as a dropout; a faded repeat reads as a glitch at worst.
    """
    x = np.asarray(x, dtype=np.float32).copy()
    if gap_samples <= 0 or x.size == 0:
        return x
    start = max(0, x.size - gap_samples)
    seed = x[max(0, start - gap_samples):start]
    if seed.size == 0:
        return x
    rep = np.resize(seed, x.size - start).copy()
    f = int(min(rep.size, max(1, fade_ms * fs / 1000)))
    rep[:f] *= np.linspace(1.0, 0.0, f, dtype=np.float32)
    x[start:] = rep
    return x
