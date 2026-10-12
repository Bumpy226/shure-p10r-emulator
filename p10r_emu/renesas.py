"""Renesas RA6M-class peripheral models -- the subset the P10R+ firmware
(built on FSP 3.8) exercises."""
from collections import deque

from .cortexm import Peripheral, Reg32, RegBank


class System(Peripheral):
    """SYSTEM block at 0x4001E000: clock generation and low-power control.
    Oscillators start and stop instantly; OSCSF just mirrors the stop bits."""
    name = "system"
    SCKDIVCR, SCKSCR, PLLCR, MOSCCR, HOCOCR, OSCSF = 0x20, 0x26, 0x2A, 0x32, 0x36, 0x3C

    def reset(self):
        self.mem = {self.MOSCCR: 1, self.PLLCR: 1, 0x480: 1}   # MOSC, PLL, SOSC stopped
        self.words = {self.SCKDIVCR: 0x44044444}

    def read(self, off, size):
        if off == self.OSCSF:
            v = 0
            if not self.mem.get(self.HOCOCR, 0) & 1:
                v |= 0x01
            if not self.mem.get(self.MOSCCR, 0) & 1:
                v |= 0x08
            if not self.mem.get(self.PLLCR, 0) & 1:
                v |= 0x20
            return v
        base = off & ~3
        if base in self.words:
            return (self.words[base] >> (8 * (off & 3))) & ((1 << (8 * size)) - 1)
        v = 0
        for i in range(size):
            v |= self.mem.get(off + i, 0) << (8 * i)
        return v

    def write(self, off, size, value):
        base = off & ~3
        if base in self.words and size == 4:
            self.words[base] = value
            return
        for i in range(size):
            self.mem[off + i] = (value >> (8 * i)) & 0xFF


class Icu(Reg32):
    """Interrupt controller unit at 0x40006000.  Every NVIC slot n has an
    IELSRn register naming the peripheral event routed to it; bit 16 (IR) is
    the latched request the ISR clears."""
    name = "icu"
    IELSR0 = 0x300
    SLOTS = 96

    def reset(self):
        self.ielsr = [0] * self.SLOTS
        self.irqcr = bytearray(16)
        self.regs = {}
        self.unrouted = {}

    def event(self, number):
        """A peripheral raises event `number`."""
        hit = False
        for slot, v in enumerate(self.ielsr):
            if v & 0x1FF == number and number:
                self.ielsr[slot] |= 0x10000
                self.cpu.set_irq(slot, True)
                hit = True
        if not hit:
            self.unrouted[number] = self.unrouted.get(number, 0) + 1
        return hit

    def routed(self, number):
        return any(v & 0x1FF == number for v in self.ielsr)

    def read(self, off, size):
        if off < 0x10:
            return self.irqcr[off]
        return Reg32.read(self, off, size)

    def write(self, off, size, value):
        if off < 0x10 and size == 1:
            self.irqcr[off] = value & 0xFF
            return
        Reg32.write(self, off, size, value)

    def rd(self, reg):
        if self.IELSR0 <= reg < self.IELSR0 + 4 * self.SLOTS:
            return self.ielsr[(reg - self.IELSR0) >> 2]
        return self.regs.get(reg, 0)

    def wr(self, reg, value):
        if self.IELSR0 <= reg < self.IELSR0 + 4 * self.SLOTS:
            slot = (reg - self.IELSR0) >> 2
            ir = self.ielsr[slot] & value & 0x10000      # IR can only be cleared
            self.ielsr[slot] = (value & 0x010001FF & ~0x10000) | ir
            if not ir:
                self.cpu.set_irq(slot, False)
        else:
            self.regs[reg] = value

    # -- DTC hand-off ---------------------------------------------------
    DTCE = 1 << 24

    def dtc_event(self, number):
        """Like event(), but slots with DTCE set are serviced by the data
        transfer controller first; the CPU only sees the interrupt when the
        transfer count runs out."""
        hit = False
        for slot, v in enumerate(self.ielsr):
            if v & 0x1FF != number or not number:
                continue
            hit = True
            dtc = getattr(self, "dtc", None)
            if v & self.DTCE and dtc is not None and dtc.enabled:
                if not dtc.transfer(slot):
                    continue                    # more to go: CPU not bothered
                self.ielsr[slot] &= ~self.DTCE
            self.ielsr[slot] |= 0x10000
            self.cpu.set_irq(slot, True)
        if not hit:
            self.unrouted[number] = self.unrouted.get(number, 0) + 1
        return hit


class Dtc(Reg32):
    """Data transfer controller at 0x40005400 (normal mode only)."""
    name = "dtc"

    def __init__(self, icu):
        self.icu = icu
        icu.dtc = self
        self.reset()

    def reset(self):
        self.regs = {}
        self.transfers = 0

    @property
    def enabled(self):
        return bool(self.regs.get(0x0C, 0) & 1)

    def rd(self, reg):
        return self.regs.get(reg, 0)

    def wr(self, reg, value):
        self.regs[reg] = value

    def write(self, off, size, value):
        if off == 0x0C and size == 1:           # DTCST is a byte register
            self.regs[0x0C] = value & 1
            return
        Reg32.write(self, off, size, value)

    def transfer(self, slot):
        """One transfer for interrupt slot `slot`.  True when the CPU should
        be interrupted (count exhausted or DISEL set)."""
        cpu = self.cpu
        info = cpu.read32(self.regs.get(0x04, 0) + 4 * slot)
        cfg = cpu.read32(info)
        sar, dar, cnt = cpu.read32(info + 4), cpu.read32(info + 8), cpu.read32(info + 12)
        cra = cnt >> 16
        size = 1 << ((cfg >> 28) & 3)
        sm, dm = (cfg >> 26) & 3, (cfg >> 18) & 3
        cpu.sys_write(dar, size, cpu.sys_read(sar, size))
        self.transfers += 1
        if sm & 2:
            sar += size if sm == 2 else -size
        if dm & 2:
            dar += size if dm == 2 else -size
        cra = (cra - 1) & 0xFFFF
        cpu.write32(info + 4, sar)
        cpu.write32(info + 8, dar)
        cpu.write32(info + 12, (cra << 16) | (cnt & 0xFFFF))
        return cra == 0 or bool(cfg & (1 << 21))


def _lane_shift(off, size):
    """IOPORT/PFS registers put sub-word accesses on big-endian byte lanes:
    the byte at offset 3 is bits 7..0 of the 32-bit register."""
    return 8 * (4 - size - (off & 3))


class Ioport(Peripheral):
    """PORT0..PORTB data registers (0x40040000) and the PmnPFS pin function
    block (0x40040800)."""
    name = "ioport"
    NPORTS = 12

    def __init__(self, icu, on_output=None, irq_pins=None):
        self.icu = icu
        self.on_output = on_output              # f(port, pin, level)
        self.irq_pins = irq_pins or {}          # (port, pin) -> IRQ number
        self.inp = [0xFFFF] * self.NPORTS       # external pin levels
        self.reset()

    def reset(self):
        self.out = [0] * self.NPORTS
        self.dir = [0] * self.NPORTS
        self.pfs = {}
        self.pwpr = 0x80

    # -- board side --
    def level(self, port, pin):
        if self.dir[port] >> pin & 1:
            return self.out[port] >> pin & 1
        return self.inp[port] >> pin & 1

    def set_input(self, port, pin, level):
        level = 1 if level else 0
        old = self.inp[port] >> pin & 1
        if old == level:
            return
        self.inp[port] = (self.inp[port] & ~(1 << pin)) | (level << pin)
        if self.pfs.get((port, pin), 0) & (1 << 14):        # ISEL
            n = self.irq_pins.get((port, pin), 0)
            mode = self.icu.irqcr[n] & 3
            if (mode == 0 and not level) or (mode == 1 and level) or mode == 2 \
                    or (mode == 3 and not level):
                self.icu.event(1 + n)

    def _set(self, port, out, dirn):
        changed = ((self.out[port] ^ out) & dirn) | ((self.dir[port] ^ dirn) & dirn)
        self.out[port], self.dir[port] = out, dirn
        if changed and self.on_output:
            for pin in range(16):
                if changed >> pin & 1:
                    self.on_output(port, pin, out >> pin & 1)

    def _pins(self, port):
        d = self.dir[port]
        return (self.out[port] & d) | (self.inp[port] & ~d)

    # -- registers --
    def _rd32(self, off):
        if off >= 0x800:
            if off >= 0xD00:
                return self.pwpr if off == 0xD00 else 0
            idx = (off - 0x800) >> 2
            port, pin = idx >> 4, idx & 15
            if port >= self.NPORTS:
                return 0
            v = self.pfs.get((port, pin), 0) & ~7
            return v | (self.out[port] >> pin & 1) | ((self._pins(port) >> pin & 1) << 1) \
                | ((self.dir[port] >> pin & 1) << 2)
        port, reg = off >> 5, off & 0x1C
        if port >= self.NPORTS:
            return 0
        if reg == 0:
            return (self.out[port] << 16) | self.dir[port]
        if reg == 4:
            return self._pins(port)
        return 0

    def _wr32(self, off, value, mask):
        if off >= 0x800:
            if off >= 0xD00:
                if off == 0xD00 and mask & 0xFF:
                    self.pwpr = value & 0xC0
                return
            idx = (off - 0x800) >> 2
            port, pin = idx >> 4, idx & 15
            if port >= self.NPORTS:
                return
            old = self._rd32(off)
            v = (old & ~mask) | (value & mask)
            self.pfs[(port, pin)] = v
            bit = 1 << pin
            self._set(port, (self.out[port] & ~bit) | (bit if v & 1 else 0),
                      (self.dir[port] & ~bit) | (bit if v & 4 else 0))
            return
        port, reg = off >> 5, off & 0x1C
        if port >= self.NPORTS:
            return
        if reg == 0:
            cur = (self.out[port] << 16) | self.dir[port]
            v = (cur & ~mask) | (value & mask)
            self._set(port, v >> 16, v & 0xFFFF)
        elif reg == 8:
            v = value & mask
            self._set(port, (self.out[port] | (v & 0xFFFF)) & ~(v >> 16), self.dir[port])

    def read(self, off, size):
        if size == 4:
            return self._rd32(off)
        return (self._rd32(off & ~3) >> _lane_shift(off, size)) & ((1 << (8 * size)) - 1)

    def write(self, off, size, value):
        if size == 4:
            self._wr32(off, value, 0xFFFFFFFF)
        else:
            sh = _lane_shift(off, size)
            self._wr32(off & ~3, value << sh, ((1 << (8 * size)) - 1) << sh)


class Sci(Peripheral):
    """Serial communications interface, either as an asynchronous UART or
    as a simple-SPI master.  Events are raised through the ICU: base+0 RXI,
    +1 TXI, +2 TEI, +3 ERI."""
    SMR, BRR, SCR, TDR, SSR, RDR = range(6)
    TIE, RIE, TE, RE, TEIE = 0x80, 0x40, 0x20, 0x10, 0x04
    BYTE_CYCLES = 400               # time on the wire for one character

    def __init__(self, name, icu, event_base, on_tx=None, spi_bus=None):
        self.name = name
        self.icu = icu
        self.ev = event_base
        self.on_tx = on_tx          # UART mode: f(byte)
        self.spi_bus = spi_bus      # SPI mode: object with xfer(byte) -> byte
        self.reset()

    def reset(self):
        self.regs = bytearray(0x20)
        self.rxq = deque()
        self.rdr = 0
        self.rdrf = False
        self.busy = False

    # -- host side (UART) --
    def feed(self, data):
        self.rxq.extend(data)
        self._rx_pump()

    def _rx_pump(self):
        if self.rxq and not self.rdrf and self.regs[self.SCR] & self.RE:
            self.rdr = self.rxq.popleft()
            self.rdrf = True
            if self.regs[self.SCR] & self.RIE:
                self.icu.dtc_event(self.ev)

    # -- wire --
    def _shift_done(self, byte):
        self.busy = False
        scr = self.regs[self.SCR]
        if self.spi_bus is not None:
            rx = self.spi_bus.xfer(byte)
            if scr & self.RE:
                self.rdr = rx
                self.rdrf = True
        elif self.on_tx:
            self.on_tx(byte)
        if scr & self.TIE and scr & self.TE:
            self.icu.dtc_event(self.ev + 1)     # TDR empty again
        if self.rdrf and scr & self.RIE and self.spi_bus is not None:
            self.icu.dtc_event(self.ev)
        if not self.busy and scr & self.TEIE:
            self.icu.event(self.ev + 2)

    def read(self, off, size):
        if off == self.SSR:
            v = 0x00 if self.busy else 0x84     # TDRE | TEND
            if self.rdrf:
                v |= 0x40
            return v
        if off == self.RDR:
            v = self.rdr
            self.rdrf = False
            if self.spi_bus is None:
                self.cpu.schedule(self.BYTE_CYCLES, self._rx_pump)
            return v
        return self.regs[off] if off < len(self.regs) else 0

    def write(self, off, size, value):
        value &= 0xFF
        if off == self.TDR:
            if self.regs[self.SCR] & self.TE:
                self.busy = True
                self.cpu.schedule(self.BYTE_CYCLES, lambda b=value: self._shift_done(b))
            return
        if off == self.SSR or off >= len(self.regs):
            return
        old = self.regs[off]
        self.regs[off] = value
        if off == self.SCR:
            rose = value & ~old
            if value & self.TE and value & self.TIE and rose & (self.TE | self.TIE) \
                    and not self.busy:
                self.cpu.schedule(8, self._txi)
            if value & self.TEIE and rose & self.TEIE and not self.busy:
                self.cpu.schedule(8, self._tei)
            if rose & (self.RE | self.RIE):
                self.cpu.schedule(8, self._rx_pump)

    def _txi(self):
        scr = self.regs[self.SCR]
        if scr & self.TIE and scr & self.TE and not self.busy:
            self.icu.dtc_event(self.ev + 1)

    def _tei(self):
        if self.regs[self.SCR] & self.TEIE and not self.busy:
            self.icu.event(self.ev + 2)


class Adc12(Peripheral):
    """12-bit A/D converter unit.  A scan completes the moment it is
    started; `sample(channel)` returns 0.0..1.0 of full scale.  Channel
    numbers 0..27 are analog inputs, "temp" and "vref" the internal ones."""
    ADCSR, ADANSA0, ADANSA1, ADEXICR, ADTSDR, ADOCDR, ADDR0 = 0x00, 0x04, 0x06, 0x12, 0x1A, 0x1C, 0x20

    def __init__(self, name, icu, scan_end_event, sample):
        self.name = name
        self.icu = icu
        self.ev = scan_end_event
        self.sample = sample
        self.reset()

    def reset(self):
        self.mem = bytearray(0x100)
        self.seen = {}

    def _r16(self, off):
        return self.mem[off] | (self.mem[off + 1] << 8)

    def _w16(self, off, v):
        self.mem[off], self.mem[off + 1] = v & 0xFF, (v >> 8) & 0xFF

    def _code(self, ch):
        self.seen[ch] = self.seen.get(ch, 0) + 1
        return max(0, min(4095, int(self.sample(ch) * 4095)))

    def _scan(self):
        mask = self._r16(self.ADANSA0) | (self._r16(self.ADANSA1) << 16)
        for ch in range(28):
            if mask >> ch & 1:
                self._w16(self.ADDR0 + 2 * ch, self._code(ch))
        ex = self._r16(self.ADEXICR)
        if ex & 0x100:
            self._w16(self.ADTSDR, self._code("temp"))
        if ex & 0x200:
            self._w16(self.ADOCDR, self._code("vref"))
        self.icu.event(self.ev)

    def read(self, off, size):
        v = 0
        for i in range(size):
            if off + i < len(self.mem):
                v |= self.mem[off + i] << (8 * i)
        return v

    def write(self, off, size, value):
        for i in range(size):
            if off + i < len(self.mem):
                self.mem[off + i] = (value >> (8 * i)) & 0xFF
        if off <= self.ADCSR + 1 < off + size and self.mem[1] & 0x80:
            self.mem[1] &= ~0x80                # ADST clears: scan finished
            self._scan()
        if off <= 0x9E < off + size:            # ADCALEXE: calibration is instant
            self.mem[0x9E] &= ~0xC0


class Flash(Peripheral):
    """Flash control (FACI, 0x407FE000) with its command window at
    0x407E0000 and the data-flash read window at 0x40100000."""
    name = "flash"
    FASTAT, FRDYIE, FSADDR, FEADDR, FSTATR, FENTRYR, FBCSTAT = 0x10, 0x18, 0x30, 0x34, 0x80, 0x84, 0xD4
    EV_FRDYI = 0x31
    DF_BASE, DF_SIZE, DF_BLOCK = 0x40100000, 0x10000, 64

    def __init__(self, icu, path=None):
        import os
        self.icu = icu
        self.path = path
        self.data = bytearray(b"\xFF" * self.DF_SIZE)
        self.written = bytearray(self.DF_SIZE)      # 1 = programmed since erase
        if path and os.path.exists(path):
            raw = open(path, "rb").read()
            n = min(len(raw) // 2, self.DF_SIZE)
            self.data[:n] = raw[:n]
            self.written[:n] = raw[self.DF_SIZE:self.DF_SIZE + n]
        self.reset()

    def reset(self):
        self.regs = {}
        self.entry = 0
        self.cmd = []
        self.bcstat = 0

    def save(self):
        if self.path:
            with open(self.path, "wb") as f:
                f.write(self.data + self.written)

    def _done(self):
        if self.regs.get(self.FRDYIE, 0) & 1:
            self.cpu.schedule(200, lambda: self.icu.event(self.EV_FRDYI))

    def _df(self, addr):
        off = addr - self.DF_BASE
        return off if 0 <= off < self.DF_SIZE else None

    # command window (attached separately, see Window below)
    def command(self, value, size):
        c = self.cmd
        c.append(value)
        op = c[0]
        start = self.regs.get(self.FSADDR, 0)
        if op == 0xE8:                              # program
            if len(c) >= 2 and len(c) == c[1] + 3:
                off = self._df(start)
                if c[-1] == 0xD0 and off is not None:
                    for i, w in enumerate(c[2:-1]):
                        for j in (0, 1):
                            a = off + 2 * i + j
                            if a < self.DF_SIZE:
                                self.data[a] &= (w >> (8 * j)) & 0xFF
                                self.written[a] = 1
                    self.save()
                self.cmd = []
                self._done()
        elif op == 0x20:                            # block erase
            if len(c) == 2:
                off = self._df(start)
                if c[1] == 0xD0 and off is not None:
                    b = off - off % self.DF_BLOCK
                    self.data[b:b + self.DF_BLOCK] = b"\xFF" * self.DF_BLOCK
                    self.written[b:b + self.DF_BLOCK] = bytes(self.DF_BLOCK)
                    self.save()
                self.cmd = []
                self._done()
        elif op == 0x71:                            # blank check
            if len(c) == 2:
                a, e = self._df(start), self._df(self.regs.get(self.FEADDR, 0))
                self.bcstat = 0
                if a is not None and e is not None and any(self.written[a:e + 1]):
                    self.bcstat = 1
                self.cmd = []
                self._done()
        else:                                       # status clear, stop, ...
            self.cmd = []
            self._done()

    def read(self, off, size):
        if off == self.FSTATR:
            return 0x8000                           # FRDY, no errors
        if off == self.FENTRYR:
            return self.entry
        if off == self.FBCSTAT:
            return self.bcstat
        if off == self.FASTAT:
            return 0x10 if self.entry else 0        # CMDLK clear
        return self.regs.get(off, 0)

    def write(self, off, size, value):
        if off == self.FENTRYR:
            if (value >> 8) == 0xAA:
                self.entry = value & 0x81
                self.cmd = []
            return
        self.regs[off] = value


class FlashWindow(Peripheral):
    """Either the FACI command area (writes are commands) or the data-flash
    read area."""

    def __init__(self, flash, kind):
        self.flash = flash
        self.kind = kind
        self.name = "flash-" + kind

    def read(self, off, size):
        if self.kind == "data":
            return int.from_bytes(self.flash.data[off:off + size], "little")
        return 0

    def write(self, off, size, value):
        if self.kind == "cmd":
            self.flash.command(value, size)
