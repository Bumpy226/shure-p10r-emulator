"""Model of the P10R+ Spartan-6 as the CPU sees it.

The FPGA is a single SPI slave.  The first byte of every transaction picks a
target behind it:

    0x00  FPGA register file: 16-bit address (bit 15 = read), 32-bit
          big-endian data
    0x01  LCD controller (A0 line driven from register 0x238)
    0x02  RF ADC configuration port
    0x04  PLL synthesiser latch
    other pass-through ports are logged and ignored

The register file is where the radio lives: RSSI, pilot detect, audio
meters, front-panel buttons, LEDs, the battery-gauge port and the interrupt
controller.  The real thing derives those from the IF samples; here they are
fed from a mock RF environment (see rf.py).

Register map, as recovered from the firmware's FPGA driver:

    0x0000 R   bitstream version, one byte per field
    0x0004 R   [5:0] board-variant straps
    0x0010 R   0xFA signature
    0x0020 R   interrupt status (read to clear)
    0x0024 W   interrupt enable
    0x0200 R   debounced buttons   0x0210 R  raw buttons (bit7: ATE mode, W)
    0x020C RW  bit6 R: earphones plugged; bits 4,5 W: audio enables
    0x0220 W   bit0 red LED, bit1 green LED, bit2 LCD backlight
    0x0224 W   0x88 = RF ("tx") LED on
    0x0228/2C/30/34 W  PWM duty 0..255: red, green, backlight, RF LED
    0x0238 W   bit0 = LCD A0 (0 command, 1 data)
    0x0414 R   audio meters, left [30:16] right [14:0]; each a 15-segment
               thermometer code (bit n set = segment n lit)
    0x0438 R   volume pot
    0x0810 R   [23:16] RSSI slow, [15:8] RSSI fast (signed, dBm + 33)
    0x0820 R   bit16 audio squelched (no valid pilot / tone key)
    0x0A00 W   antenna: bit4 force single, bit0 select B / diversity
    0x0C00..0x0C10  HDQ master for the SB900 battery gauge
"""
from .devices import SpiSlave

IRQ_BUTTON_DOWN, IRQ_BUTTON_UP = 3, 4
IRQ_HDQ_DONE, IRQ_HDQ_ERROR = 8, 9
IRQ_POWER_SAVE = 10

BTN_UP, BTN_DOWN, BTN_ENTER, BTN_EXIT, BTN_SCAN = 0x01, 0x02, 0x04, 0x08, 0x40
BUTTONS = {"up": BTN_UP, "down": BTN_DOWN, "enter": BTN_ENTER,
           "exit": BTN_EXIT, "scan": BTN_SCAN}

RSSI_OFFSET = 33        # register value = dBm + 33, signed 8 bit

# strap value -> board variant name (decides which bands the unit accepts)
VARIANT_STRAPS = {
    "A": 0x31, "B": 0x32, "C": 0x33, "D": 0x34, "E": 0x35, "F": 0x36,
    "G": 0x37, "J": 0x38, "K": 0x39, "RA": 0x3B, "RC": 0x3C, "RD": 0x3D,
    "RE": 0x3E, "RF": 0x3F, "RG": 0x20, "RJ": 0x21, "RK": 0x22,
}


class Fpga(SpiSlave):
    def __init__(self, lcd=None, version=(1, 1, 0, 1)):
        self.lcd = lcd
        self.version = version
        self.ports = {}             # mux address -> SpiSlave
        if lcd is not None:
            self.ports[1] = lcd
        self.read_hooks = {}        # addr -> f() -> value
        self.write_hooks = {}       # addr -> f(value)
        self.access_log = None      # set to a list to record (rw, addr, val)
        self.seen = {}              # addr -> [reads, writes, last written]
        self.unknown_ports = {}
        self.on_irq_line = None     # f(level): drives the CPU interrupt pin
        # board / environment inputs
        self.strap = VARIANT_STRAPS["A"]
        self.buttons = 0
        self.phones = True          # earphones plugged in
        self.volume = 0x80          # volume pot
        self.hdq = None             # dict addr -> byte, or None = no gauge fitted
        self.rf = None              # f() -> dict(rssi=dBm, pilot=bool, left=0..1, right=0..1)
        h = self.read_hooks
        h[0x0000] = lambda: int.from_bytes(bytes(self.version), "big")
        h[0x0004] = lambda: self.strap & 0x3F
        h[0x0010] = lambda: 0xFA
        h[0x0020] = self._irq_read
        h[0x0200] = lambda: self.buttons
        h[0x0210] = lambda: (self.regs.get(0x0210, 0) & 0x80) | self.buttons
        h[0x020C] = lambda: (self.regs.get(0x020C, 0) & ~0x40) | (0x40 if self.phones else 0)
        h[0x0414] = self._audio_read
        h[0x0438] = lambda: self.volume
        h[0x0810] = self._rssi_read
        h[0x0820] = self._status_read
        self.write_hooks[0x0024] = self._irq_enable_write
        self.write_hooks[0x0238] = self._lcd_a0
        self.write_hooks[0x0C0C] = self._hdq_go
        self.reset()

    def reset(self):
        self.regs = {}
        self._bytes = []
        self._port = None
        self.irq_status = 0
        self.irq_enable = 0
        self._irq_level = 0
        if self.on_irq_line:
            self.on_irq_line(0)

    # -- outputs the front end shows ------------------------------------
    def leds(self):
        """Brightness 0..1 of (red, green, rf, backlight)."""
        r = self.regs
        en = r.get(0x0220, 0)

        def duty(a):
            return r.get(a, 0xFF) / 255.0
        return (duty(0x0228) if en & 1 else 0.0,
                duty(0x022C) if en & 2 else 0.0,
                duty(0x0234) if r.get(0x0224, 0) & 0x80 else 0.0,
                duty(0x0230) if en & 4 else 0.0)

    def antenna(self):
        v = self.regs.get(0x0A00, 0)
        if v & 0x10:
            return "B" if v & 1 else "A"
        return "DIV"

    # -- inputs -----------------------------------------------------------
    def set_button(self, mask, down):
        new = (self.buttons | mask) if down else (self.buttons & ~mask)
        if new == self.buttons:
            return
        self.buttons = new
        self.raise_irq(IRQ_BUTTON_DOWN if down else IRQ_BUTTON_UP)

    def set_phones(self, plugged):
        plugged = bool(plugged)
        if plugged != self.phones:
            self.phones = plugged
            if not plugged:
                self.raise_irq(IRQ_POWER_SAVE)

    def _rf(self):
        return self.rf() if self.rf else {"rssi": -120.0, "pilot": False,
                                          "left": 0.0, "right": 0.0}

    def _rssi_read(self):
        s = self._rf()
        slow = max(-128, min(127, int(round(s["rssi"])) + RSSI_OFFSET)) & 0xFF
        fast = max(-128, min(127, int(round(s.get("rssi_fast", s["rssi"]))) + RSSI_OFFSET)) & 0xFF
        return (slow << 16) | (fast << 8)

    def _status_read(self):
        v = self.regs.get(0x0820, 0) & ~0x10000
        return v | (0 if self._rf()["pilot"] else 0x10000)

    def _audio_read(self):
        s = self._rf()

        def lvl(x):
            return (1 << max(0, min(15, int(round(x * 15))))) - 1
        return (lvl(s["left"]) << 16) | lvl(s["right"])

    def _lcd_a0(self, value):
        if self.lcd is not None:
            self.lcd.a0 = value & 1

    # -- interrupt controller (status 0x0020 read-to-clear, enable 0x0024)
    def raise_irq(self, bit):
        self.irq_status |= 1 << bit
        self._irq_update()

    def _irq_read(self):
        v = self.irq_status
        self.irq_status = 0
        self._irq_update()
        return v

    def _irq_enable_write(self, value):
        self.irq_enable = value
        self._irq_update()

    def _irq_update(self):
        level = 1 if (self.irq_status & self.irq_enable) else 0
        if level != self._irq_level:
            self._irq_level = level
            if self.on_irq_line:
                self.on_irq_line(level)

    # -- HDQ master: 0xC08 address, 0xC00 write data, 0xC04 read data,
    #    0xC0C control/status (bit0 go, bit1 write, bit7 done, bit6 error)
    def _hdq_go(self, value):
        if not value & 1:
            return
        addr = self.regs.get(0x0C08, 0) & 0x7F
        if self.hdq is None:
            self.regs[0x0C0C] = 0xC0
            self.raise_irq(IRQ_HDQ_ERROR)
            return
        if value & 2:
            self.hdq[addr] = self.regs.get(0x0C00, 0) & 0xFF
        else:
            self.regs[0x0C04] = self.hdq.get(addr, 0)
        self.regs[0x0C0C] = 0x80
        self.raise_irq(IRQ_HDQ_DONE)

    # -- register file ------------------------------------------------
    def reg_read(self, addr):
        h = self.read_hooks.get(addr)
        v = h() if h else self.regs.get(addr, 0)
        s = self.seen.setdefault(addr, [0, 0, None])
        s[0] += 1
        if self.access_log is not None:
            self.access_log.append(("R", addr, v))
        return v & 0xFFFFFFFF

    def reg_write(self, addr, value):
        s = self.seen.setdefault(addr, [0, 0, None])
        s[1] += 1
        s[2] = value
        if self.access_log is not None:
            self.access_log.append(("W", addr, value))
        self.regs[addr] = value
        h = self.write_hooks.get(addr)
        if h:
            h(value)

    # -- SPI side -----------------------------------------------------
    def select(self):
        self._bytes = []
        self._port = None
        self._rd = 0

    def xfer(self, b):
        n = len(self._bytes)
        self._bytes.append(b)
        if n == 0:
            if b != 0:
                self._port = self.ports.get(b)
                if self._port is None:
                    self.unknown_ports.setdefault(b, [])
                else:
                    self._port.select()
            return 0
        mux = self._bytes[0]
        if mux == 0:
            if n == 2:
                self._addr = ((self._bytes[1] & 0x7F) << 8) | b
                self._is_read = bool(self._bytes[1] & 0x80)
                if self._is_read:
                    self._rd = self.reg_read(self._addr)
                return 0
            if 3 <= n <= 6:
                out = (self._rd >> (8 * (6 - n))) & 0xFF if self._is_read else 0
                if n == 6 and not self._is_read:
                    v = int.from_bytes(bytes(self._bytes[3:7]), "big")
                    self.reg_write(self._addr, v)
                return out
            return 0
        if self._port is not None:
            return self._port.xfer(b)
        return 0

    def deselect(self):
        if self._bytes and self._bytes[0] != 0:
            if self._port is not None:
                self._port.deselect()
            else:
                lst = self.unknown_ports[self._bytes[0]]
                if len(lst) < 16:
                    lst.append(bytes(self._bytes[1:]))
        self._bytes = []
        self._port = None
