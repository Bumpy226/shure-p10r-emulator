"""Board-level chips shared by both P10R+ CPU variants.

Everything here is a byte-oriented SPI slave: `select()` at chip-select
assert, `xfer(byte) -> byte` per clocked byte, `deselect()` at release.
"""
import os


class SpiSlave:
    def select(self):
        pass

    def xfer(self, byte):
        return 0xFF

    def deselect(self):
        pass


class St7565(SpiSlave):
    """132x64 ST7565-family LCD controller.  The data/command (A0) line is
    not part of the SPI stream; whoever owns it sets `.a0`."""
    COLS, PAGES = 132, 8

    def __init__(self):
        self.reset()

    def reset(self):
        self.ram = [bytearray(self.COLS) for _ in range(self.PAGES)]
        self.page = 0
        self.col = 0
        self.a0 = 0
        self.on = False
        self.reverse = False
        self.all_on = False
        self.seg_reverse = False
        self.com_reverse = False
        self.start_line = 0
        self.contrast = 0x20
        self._arg = None
        self.dirty = True
        self.cmd_log = []

    def xfer(self, byte):
        if self.a0:
            if self.col < self.COLS:
                self.ram[self.page][self.col] = byte
                self.col += 1
            self.dirty = True
            return 0
        if self._arg is not None:
            if self._arg == 0x81:
                self.contrast = byte & 0x3F
            self._arg = None
            self.dirty = True
            return 0
        c = byte
        if len(self.cmd_log) < 64:
            self.cmd_log.append(c)
        if c & 0xF0 == 0xB0:
            self.page = c & 0x0F
            if self.page >= self.PAGES:
                self.page = self.PAGES - 1
        elif c & 0xF0 == 0x10:
            self.col = (self.col & 0x0F) | ((c & 0x0F) << 4)
        elif c & 0xF0 == 0x00:
            self.col = (self.col & 0xF0) | (c & 0x0F)
        elif c & 0xC0 == 0x40:
            self.start_line = c & 0x3F
        elif c in (0xAE, 0xAF):
            self.on = bool(c & 1)
        elif c in (0xA6, 0xA7):
            self.reverse = bool(c & 1)
        elif c in (0xA4, 0xA5):
            self.all_on = bool(c & 1)
        elif c in (0xA0, 0xA1):
            self.seg_reverse = bool(c & 1)
        elif c & 0xF0 == 0xC0:
            self.com_reverse = bool(c & 8)
        elif c == 0xE2:
            page, col = self.page, self.col
            self.start_line = 0
            self.page = self.col = 0
        elif c in (0x81, 0xF8, 0xAC, 0xAD):
            self._arg = c
        self.dirty = True
        return 0

    def pixel(self, x, y):
        """Pixel as the viewer sees it, honouring scan-direction flags."""
        row = (y + self.start_line) & 63
        return bool(self.ram[row >> 3][x] >> (row & 7) & 1)


class M95xxx(SpiSlave):
    """ST M95-series SPI EEPROM (M95080 = 1 KiB), optionally file-backed."""

    def __init__(self, size=1024, path=None, addr_bytes=2):
        self.size = size
        self.path = path
        self.addr_bytes = addr_bytes
        self.data = bytearray(b"\xFF" * size)
        if path and os.path.exists(path):
            raw = open(path, "rb").read()[:size]
            self.data[:len(raw)] = raw
        self.status = 0
        self.wel = False
        self._bytes = []
        self._dirty = False

    def select(self):
        self._bytes = []
        self._addr = 0
        self._cmd = None
        self._wrote = False

    def xfer(self, b):
        n = len(self._bytes)
        self._bytes.append(b)
        if n == 0:
            self._cmd = b
            if b == 0x06:
                self.wel = True
            elif b == 0x04:
                self.wel = False
            return 0xFF
        cmd = self._cmd
        if cmd == 0x05:                              # RDSR
            return (self.status & 0x8C) | (2 if self.wel else 0)
        if cmd == 0x01:                              # WRSR
            if self.wel:
                self.status = b & 0x8C
            return 0xFF
        if cmd in (0x03, 0x02):
            if n <= self.addr_bytes:
                self._addr = ((self._addr << 8) | b) % self.size
                return 0xFF
            if cmd == 0x03:
                v = self.data[self._addr]
                self._addr = (self._addr + 1) % self.size
                return v
            if self.wel:
                self.data[self._addr] = b
                # writes wrap inside the 32-byte page
                self._addr = (self._addr & ~31) | ((self._addr + 1) & 31)
                self._wrote = True
            return 0xFF
        return 0xFF

    def deselect(self):
        if self._cmd in (0x02, 0x01):
            self.wel = False
        if getattr(self, "_wrote", False):
            self._dirty = True
            self.save()

    def save(self):
        if self.path and self._dirty:
            with open(self.path, "wb") as f:
                f.write(self.data)
            self._dirty = False


class LatchSink(SpiSlave):
    """Write-only shift-register device (PLL synth, DTC, ...): collects the
    bytes of each transaction and hands them to `on_word`."""

    def __init__(self, name, on_word=None):
        self.name = name
        self.on_word = on_word
        self.last = b""
        self.history = []

    def select(self):
        self._buf = bytearray()

    def xfer(self, b):
        self._buf.append(b)
        return 0

    def deselect(self):
        if self._buf:
            self.last = bytes(self._buf)
            self.history.append(self.last)
            del self.history[:-32]
            if self.on_word:
                self.on_word(self.last)
