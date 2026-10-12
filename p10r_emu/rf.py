"""Mock RF world for the receiver.

The firmware tunes by writing ADF4111 latches; `Synth` decodes them back
into a frequency.  `RfEnvironment` holds a list of pretend transmitters and
answers "what would the receiver see when tuned to f?" -- which is enough to
drive the RSSI bars, pilot detect, audio meters and the firmware's own
channel / group / spectrum scans.
"""
import math
import random

IF_KHZ = 110_600            # first IF: LO sits this far above the carrier
REF_KHZ = 64_000            # PLL reference


class Synth:
    """Tracks ADF4111 R / N / function latches."""

    def __init__(self):
        self.r = 2560
        self.p = 64
        self.a = 0
        self.b = 0
        self.writes = 0

    def latch(self, data):
        if len(data) != 3:
            return
        w = int.from_bytes(data, "big")
        self.writes += 1
        c = w & 3
        if c == 0:
            self.r = (w >> 2) & 0x3FFF or 1
        elif c == 1:
            self.a = (w >> 2) & 0x3F
            self.b = (w >> 8) & 0x1FFF
        else:
            self.p = 8 << ((w >> 22) & 3)

    @property
    def lo_khz(self):
        return (self.p * self.b + self.a) * REF_KHZ / self.r

    @property
    def rf_khz(self):
        return self.lo_khz - IF_KHZ


class Transmitter:
    def __init__(self, freq_mhz, level_dbm=-45.0, left=0.5, right=0.5,
                 pilot=True, name="", follow=False):
        self.freq_mhz = float(freq_mhz)
        self.level_dbm = float(level_dbm)
        self.left = float(left)
        self.right = float(right)
        self.pilot = bool(pilot)
        self.name = name
        self.follow = follow        # always sits on whatever is tuned
        self.enabled = True

    def describe(self):
        f = "tuned freq" if self.follow else "%.3f MHz" % self.freq_mhz
        return "%-11s %4.0f dBm  L%3d%% R%3d%%  %s%s" % (
            f, self.level_dbm, self.left * 100, self.right * 100,
            "pilot" if self.pilot else "no pilot",
            "" if self.enabled else "  (off)")


class RfEnvironment:
    CHANNEL_KHZ = 150.0         # where the IF filter starts biting
    SKIRT_DB_PER_KHZ = 0.35

    def __init__(self, synth):
        self.synth = synth
        self.transmitters = []
        self.noise_dbm = -105.0
        self.squelch_dbm = -90.0     # below this the pilot is not decoded
        self.jitter = 1.0
        self._rng = random.Random(1)
        self._phase = 0.0

    def add(self, *args, **kw):
        t = Transmitter(*args, **kw)
        self.transmitters.append(t)
        return t

    def level_at(self, khz):
        """(dBm, strongest transmitter) at the given carrier frequency."""
        best, who = self.noise_dbm, None
        for t in self.transmitters:
            if not t.enabled:
                continue
            off = 0.0 if t.follow else abs(khz - t.freq_mhz * 1000.0)
            lvl = t.level_dbm
            if off > self.CHANNEL_KHZ:
                lvl -= (off - self.CHANNEL_KHZ) * self.SKIRT_DB_PER_KHZ
            elif off > 12.5:
                lvl -= (off - 12.5) * 0.04
            if lvl > best:
                best, who = lvl, (t if off <= 25.0 else None)
        return best, who

    def sample(self):
        """What the FPGA reports for the currently tuned frequency."""
        lvl, tx = self.level_at(self.synth.rf_khz)
        if self.jitter:
            lvl += self._rng.uniform(-self.jitter, self.jitter)
        locked = tx is not None and lvl >= self.squelch_dbm
        left = right = 0.0
        if locked:
            # make the meters breathe a little, like programme audio
            self._phase += 0.37
            wob = 0.85 + 0.15 * math.sin(self._phase)
            left, right = tx.left * wob, tx.right * (1.7 - wob)
        return {"rssi": lvl, "pilot": bool(locked and tx.pilot),
                "left": left, "right": right, "tx": tx}
