"""ATxmega256A3 on-chip peripherals, to the depth the P10R firmware uses."""

# interrupt vectors (ATxmega A3)
VEC_TCC0_OVF = 14
VEC_NVM_EE = 32
VEC_USARTE0_RXC = 58
VEC_USARTF0_RXC = 119


class Clock:
    """OSC / CLK / PMIC / SLEEP / RST: oscillators are ready as soon as they
    are enabled, a software reset reboots the machine."""

    def __init__(self, core):
        self.core = core
        core.io_ram(0x40, 5)                    # CLK
        core.io_ram(0x48)                       # SLEEP.CTRL
        core.io_ram(0x50, 7)                    # OSC
        core.io(0x51, lambda: core.D[0x50] | 0x01)      # STATUS: enabled == ready
        core.io_ram(0x70, 7)                    # PR
        core.io_ram(0x80, 3)                    # WDT
        core.io(0xA0, lambda: core.pmic_status)
        core.io_ram(0xA1)
        core.io(0xA2, lambda: core.pmic_ctrl, self._pmic_ctrl)
        core.io(0x78, lambda: 0x01, lambda v: None)     # RST.STATUS: power-on
        core.io(0x79, lambda: 0, self._rst_ctrl)

    def _pmic_ctrl(self, v):
        self.core.pmic_ctrl = v
        self.core.irq_check = True

    def _rst_ctrl(self, v):
        if v & 1:
            self.core._stop("reset")


class Port:
    """One 8-bit I/O port.  `on_out(port, old, new)` sees every change of
    the driven pins; inputs come from `inp` (pins the board drives)."""

    def __init__(self, core, base, name, on_out=None):
        self.core = core
        self.base = base
        self.name = name
        self.on_out = on_out
        self.dir = 0
        self.out = 0
        self.inp = 0xFF             # external levels (pull-ups by default)
        self.pinctrl = [0] * 8
        self.read_in = None         # optional f() -> external levels
        io = core.io
        io(base + 0, lambda: self.dir, self._set_dir)
        io(base + 1, lambda: self.dir, lambda v: self._set_dir(self.dir | v))
        io(base + 2, lambda: self.dir, lambda v: self._set_dir(self.dir & ~v))
        io(base + 3, lambda: self.dir, lambda v: self._set_dir(self.dir ^ v))
        io(base + 4, lambda: self.out, self._set_out)
        io(base + 5, lambda: self.out, lambda v: self._set_out(self.out | v))
        io(base + 6, lambda: self.out, lambda v: self._set_out(self.out & ~v))
        io(base + 7, lambda: self.out, lambda v: self._set_out(self.out ^ v))
        io(base + 8, self.pins, lambda v: None)
        core.io_ram(base + 9, 4)                # INTCTRL, INT0MASK, INT1MASK, INTFLAGS
        for i in range(8):
            io(base + 0x10 + i, (lambda i=i: self.pinctrl[i]),
               (lambda v, i=i: self.pinctrl.__setitem__(i, v)))

    def reset(self):
        self.dir = 0
        self.out = 0
        self.pinctrl = [0] * 8

    def pins(self):
        ext = self.read_in() if self.read_in else self.inp
        return (self.out & self.dir) | (ext & ~self.dir & 0xFF)

    def _set_dir(self, v):
        v &= 0xFF
        old = self.pins_driven()
        self.dir = v
        self._changed(old)

    def _set_out(self, v):
        v &= 0xFF
        old = self.pins_driven()
        self.out = v
        self._changed(old)

    def pins_driven(self):
        """Pin levels as the board sees them: undriven pins float high."""
        return (self.out & self.dir) | (~self.dir & 0xFF)

    def _changed(self, old):
        new = self.pins_driven()
        if new != old and self.on_out:
            self.on_out(self, old, new)


class VPorts:
    """VPORT0..3 at I/O 0x10..0x1F, mapped onto real ports by PORTCFG."""

    def __init__(self, core, ports):
        self.core = core
        self.ports = ports          # list indexed by port number A=0, B=1, ...
        self.map = [0, 1, 2, 3]
        core.io(0xB2, lambda: self.map[0] | (self.map[1] << 4), lambda v: self._cfg(0, v))
        core.io(0xB3, lambda: self.map[2] | (self.map[3] << 4), lambda v: self._cfg(2, v))
        for n in range(4):
            b = 0x10 + n * 4
            core.io(b + 0, (lambda n=n: self._p(n).dir), (lambda v, n=n: self._p(n)._set_dir(v)))
            core.io(b + 1, (lambda n=n: self._p(n).out), (lambda v, n=n: self._p(n)._set_out(v)))
            core.io(b + 2, (lambda n=n: self._p(n).pins()), lambda v: None)
            core.io_ram(b + 3)

    def reset(self):
        self.map = [0, 1, 2, 3]

    def _cfg(self, first, v):
        self.map[first] = v & 15
        self.map[first + 1] = v >> 4

    def _p(self, n):
        i = self.map[n]
        return self.ports[i] if i < len(self.ports) and self.ports[i] else self.ports[0]


class Spi:
    """SPI master.  `xfer(byte) -> byte` reaches whatever the board has
    selected; the transfer completes instantly."""

    def __init__(self, core, base, name, xfer):
        self.core = core
        self.name = name
        self.xfer = xfer
        self.ctrl = 0
        self.rx = 0xFF
        self.flag = 0
        core.io(base + 0, lambda: self.ctrl, self._ctrl)
        core.io_ram(base + 1)
        core.io(base + 2, lambda: self.flag, lambda v: None)
        core.io(base + 3, self._read, self._write)

    def reset(self):
        self.ctrl = 0
        self.flag = 0

    def _ctrl(self, v):
        self.ctrl = v

    def _write(self, v):
        self.rx = self.xfer(v) & 0xFF
        self.flag = 0x80

    def _read(self):
        self.flag = 0
        return self.rx


class Usart:
    """UART with interrupt-driven receive and polled transmit."""

    def __init__(self, core, base, name, rxc_vector, on_tx):
        self.core = core
        self.name = name
        self.vec = rxc_vector
        self.on_tx = on_tx
        self.rxq = []
        self.rx = 0
        self.rxc = False
        self.ctrla = 0
        self.ctrlb = 0
        self.byte_cycles = 2400
        core.io(base + 0, self._data_r, self._data_w)
        core.io(base + 1, lambda: (0x80 if self.rxc else 0) | 0x60, lambda v: None)
        core.io(base + 3, lambda: self.ctrla, self._ctrla)
        core.io(base + 4, lambda: self.ctrlb, self._ctrlb)
        core.io_ram(base + 5, 3)

    def reset(self):
        self.rxq = []
        self.rxc = False
        self.ctrla = self.ctrlb = 0
        self._busy = False

    def feed(self, data):
        self.rxq.extend(data)
        self._pump()

    def _pump(self):
        if self.rxq and not self.rxc and not getattr(self, "_busy", False):
            self._busy = True
            self.core.schedule(self.byte_cycles, self._deliver)

    def _deliver(self):
        self._busy = False
        if not self.rxq:
            return
        if not self.ctrlb & 0x10:           # receiver off: drop
            self.rxq.pop(0)
            self._pump()
            return
        if self.rxc:
            return
        self.rx = self.rxq.pop(0)
        self.rxc = True
        self._irq()

    def _irq(self):
        lvl = (self.ctrla >> 4) & 3
        self.core.set_irq(self.vec, lvl if self.rxc else 0)

    def _ctrla(self, v):
        self.ctrla = v
        self._irq()

    def _ctrlb(self, v):
        self.ctrlb = v
        self._pump()

    def _data_r(self):
        v = self.rx
        if self.rxc:
            self.rxc = False
            self._irq()
            self._pump()
        return v

    def _data_w(self, v):
        self.on_tx(v)


class Twi:
    """TWI (I2C) master in the polled style the firmware uses.  `bus` needs
    start(addr7, read) -> ack, write(byte) -> ack, read(ack) -> byte, stop()."""

    def __init__(self, core, base, name, bus):
        self.core = core
        self.name = name
        self.bus = bus
        self.base = base
        self.reset()
        core.io(base + 1, lambda: self.ctrla, lambda v: setattr(self, "ctrla", v))
        core.io_ram(base + 2)
        core.io(base + 3, lambda: self.ctrlc, self._ctrlc)
        core.io(base + 4, lambda: self.status, self._status_w)
        core.io_ram(base + 5)
        core.io(base + 6, lambda: self.addr, self._addr)
        core.io(base + 7, self._data_r, self._data_w)

    def reset(self):
        self.ctrla = 0
        self.ctrlc = 0
        self.status = 0x01          # bus idle
        self.addr = 0
        self.data = 0
        self.reading = False
        self.owner = False

    def _status_w(self, v):
        self.status &= ~(v & 0xCC)                  # write-one-to-clear flags
        if v & 3:
            self.status = (self.status & ~3) | (v & 3)

    def _addr(self, v):
        self.addr = v
        self.reading = bool(v & 1)
        ack = self.bus.start(v >> 1, self.reading)
        self.owner = True
        self.status = 0x22 | (0 if ack else 0x10)   # CLKHOLD, owner
        if not ack:
            self.status |= 0x40                     # WIF with RXACK = NACK
        elif self.reading:
            self.data = self.bus.read(True) & 0xFF
            self.status |= 0x80                     # RIF
        else:
            self.status |= 0x40                     # WIF

    def _data_w(self, v):
        ack = self.bus.write(v)
        self.status = 0x22 | 0x40 | (0 if ack else 0x10)

    def _data_r(self):
        return self.data

    def _ctrlc(self, v):
        self.ctrlc = v & 0x04
        cmd = v & 3
        if cmd == 3:                                # stop
            self.bus.stop()
            self.owner = False
            self.status = 0x01
        elif cmd == 2 and self.reading:             # send ack/nack, fetch next byte
            if v & 0x04:                            # NACK: master is done
                self.status &= ~0x80
            else:
                self.data = self.bus.read(True) & 0xFF
                self.status = 0x22 | 0x80
        elif cmd == 1:                              # repeated start
            self._addr(self.addr)


class Tc0:
    """16-bit timer: free-running counter, overflow interrupt."""
    DIV = {0: 0, 1: 1, 2: 2, 3: 4, 4: 8, 5: 64, 6: 256, 7: 1024}

    def __init__(self, core, base, name, ovf_vector=None):
        self.core = core
        self.name = name
        self.vec = ovf_vector
        self.base = base
        self.reset()
        core.io(base + 0, lambda: self.ctrla, self._ctrla)
        core.io_ram(base + 1, 5)
        core.io(base + 6, lambda: self.intctrla, self._intctrla)
        core.io_ram(base + 7, 5)
        core.io(base + 0x0C, lambda: 1 if self.ovf else 0, self._flags_w)
        core.io_ram(base + 0x0D, 0x13)
        core.io(base + 0x20, self._cnt_lo, self._cnt_w_lo)
        core.io(base + 0x21, lambda: self._tmp, self._cnt_w_hi)
        core.io_ram(base + 0x22, 4)
        core.io(base + 0x26, lambda: self.per & 0xFF, self._per_lo)
        core.io(base + 0x27, lambda: self.per >> 8, self._per_hi)
        core.io_ram(base + 0x28, 8)
        if ovf_vector is not None:
            core.irq_ack[ovf_vector] = self._ack

    def reset(self):
        self.ctrla = 0
        self.intctrla = 0
        self.per = 0xFFFF
        self.ovf = False
        self._tmp = 0
        self._t0 = 0                # cycle at which CNT was 0
        self._gen = 0

    def _div(self):
        return self.DIV[self.ctrla & 7]

    def count(self):
        d = self._div()
        if not d:
            return 0
        return ((self.core.cycles - self._t0) // d) % (self.per + 1)

    def _cnt_lo(self):
        c = self.count()
        self._tmp = c >> 8
        return c & 0xFF

    def _cnt_w_lo(self, v):
        self._wlo = v

    def _cnt_w_hi(self, v):
        c = (v << 8) | getattr(self, "_wlo", 0)
        d = self._div() or 1
        self._t0 = self.core.cycles - c * d
        self._arm()

    def _per_lo(self, v):
        self._plo = v

    def _per_hi(self, v):
        self.per = (v << 8) | getattr(self, "_plo", 0)
        self._arm()

    def _ctrla(self, v):
        self.ctrla = v
        self._t0 = self.core.cycles
        self._arm()

    def _intctrla(self, v):
        self.intctrla = v
        self._irq()

    def _flags_w(self, v):
        if v & 1:
            self.ovf = False
            self._irq()

    def _ack(self):
        self.ovf = False
        self.core.set_irq(self.vec, 0)

    def _irq(self):
        if self.vec is not None:
            self.core.set_irq(self.vec, (self.intctrla & 3) if self.ovf else 0)

    def _arm(self):
        self._gen += 1
        d = self._div()
        if not d:
            return
        period = (self.per + 1) * d
        gen = self._gen
        elapsed = (self.core.cycles - self._t0) % period

        def fire():
            if gen != self._gen:
                return
            self.ovf = True
            self._irq()
            self.core.schedule(period, fire)
        self.core.schedule(period - elapsed, fire)


class Adc:
    """12-bit ADC, four channels, polled.  `sample(mux) -> 0..1` of full
    scale for the channel's MUXCTRL value."""

    def __init__(self, core, base, name, sample):
        self.core = core
        self.name = name
        self.sample = sample
        self.base = base
        self.flags = 0
        self.res = [0] * 4
        core.io(base + 0, lambda: core.D[base] & 0x03, self._ctrla)
        core.io_ram(base + 1, 5)
        core.io(base + 6, lambda: self.flags, self._flags_w)
        core.io_ram(base + 7, 9)
        for ch in range(4):
            cb = base + 0x20 + ch * 8
            core.io(cb + 0, (lambda cb=cb: core.D[cb] & 0x7F),
                    (lambda v, ch=ch, cb=cb: self._chctrl(ch, cb, v)))
            core.io_ram(cb + 1, 2)
            core.io(cb + 3, (lambda ch=ch: (self.flags >> ch) & 1),
                    (lambda v, ch=ch: self._flags_w((v & 1) << ch)))
            core.io(cb + 4, (lambda ch=ch: self._res_lo(ch)), lambda v: None)
            core.io(cb + 5, (lambda ch=ch: self.res[ch] >> 8), lambda v: None)
            core.io(base + 0x10 + ch * 2, (lambda ch=ch: self._res_lo(ch)), lambda v: None)
            core.io(base + 0x11 + ch * 2, (lambda ch=ch: self.res[ch] >> 8), lambda v: None)

    def reset(self):
        self.flags = 0

    def _flags_w(self, v):
        self.flags &= ~v

    def _res_lo(self, ch):
        # free-running: every read sees a fresh conversion
        if self.core.D[self.base + 1] & 0x08 and self.core.D[self.base] & 1:
            self._convert(ch)
        return self.res[ch] & 0xFF

    def _convert(self, ch):
        cb = self.base + 0x20 + ch * 8
        mux = self.core.D[cb + 1]
        signed = bool(self.core.D[self.base + 1] & 0x10)
        x = self.sample(self, ch, mux, self.core.D[cb])
        top = 2047 if signed else 4095
        self.res[ch] = max(0, min(top, int(round(x * top)))) & 0xFFFF
        self.flags |= 1 << ch

    def _ctrla(self, v):
        self.core.D[self.base] = v
        for ch in range(4):
            if v & (4 << ch):
                self._convert(ch)

    def _chctrl(self, ch, cb, v):
        self.core.D[cb] = v
        if v & 0x80:
            self._convert(ch)


class Dac:
    def __init__(self, core, base, name):
        self.core = core
        self.base = base
        core.io_ram(base, 0x1C)
        core.io(base + 5, lambda: 0x03)             # STATUS: both channels ready
        core.io_ram(base + 0x18, 4)

    def value(self, ch):
        b = self.base + 0x18 + ch * 2
        return self.core.D[b] | (self.core.D[b + 1] << 8)


class Nvm:
    """NVM controller: the memory-mapped internal EEPROM and its page
    buffer.  `eeprom` is a bytearray; `on_change()` is called after writes."""
    PAGE = 32

    def __init__(self, core, eeprom, on_change=None, base=0x1C0):
        self.core = core
        self.ee = eeprom
        self.on_change = on_change
        self.base = base
        self.buf = {}
        self.intctrl = 0
        core.io_ram(base, 0x0B)
        core.io(base + 4, lambda: core.D[base + 4], self._data0)
        core.io(base + 0x0B, lambda: 0, self._ctrla)
        core.io_ram(base + 0x0C)
        core.io(base + 0x0D, lambda: self.intctrl, self._intctrl)
        core.io(base + 0x0F, lambda: 0x02 if self.buf else 0x00, lambda v: None)
        core.on_ee_write = self._buf_write
        self.sync()

    def reset(self):
        self.buf = {}
        self.intctrl = 0
        self.sync()

    def sync(self):
        self.core.D[0x1000:0x1000 + len(self.ee)] = self.ee

    def _buf_write(self, addr, v):
        self.buf[addr % self.PAGE] = v

    def _intctrl(self, v):
        self.intctrl = v
        # "EEPROM ready" is a level interrupt and we are never busy
        self.core.set_irq(VEC_NVM_EE, v & 3)

    def _addr(self):
        D, b = self.core.D, self.base
        return D[b] | (D[b + 1] << 8) | (D[b + 2] << 16)

    def _data0(self, v):
        # with "load EEPROM page buffer" selected, writing DATA0 loads the byte
        D, b = self.core.D, self.base
        D[b + 4] = v
        if D[b + 0x0A] == 0x33:
            self.buf[self._addr() % self.PAGE] = v

    def _ctrla(self, v):
        if not v & 1:
            return
        D, b = self.core.D, self.base
        cmd = D[b + 0x0A]
        addr = self._addr() % len(self.ee)
        page = addr & ~(self.PAGE - 1)
        if cmd == 0x06:                             # read EEPROM
            D[b + 4] = self.ee[addr]
        elif cmd == 0x36:                           # erase page buffer
            self.buf = {}
        elif cmd in (0x32, 0x34, 0x35):             # erase / write / erase+write page
            for off, val in (self.buf.items() if self.buf else ()):
                a = page + off
                if cmd == 0x32:
                    self.ee[a] = 0xFF
                elif cmd == 0x34:
                    self.ee[a] &= val
                else:
                    self.ee[a] = val
            self.buf = {}
            self._changed()
        elif cmd == 0x30:                           # erase whole EEPROM
            self.ee[:] = b"\xFF" * len(self.ee)
            self._changed()
        else:
            e = self.core.unknown_log.setdefault(0x10000 | cmd, [0, 0, self.core.pc])
            e[1] += 1

    def _changed(self):
        self.sync()
        if self.on_change:
            self.on_change()
