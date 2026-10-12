"""Freescale/NXP Kinetis K-series peripheral models -- just the parts the
P10R+ firmware exercises."""
from collections import deque

from .cortexm import Peripheral, Reg32, RegBank


class Uart(Peripheral):
    """Kinetis UART (8-bit registers)."""
    BDH, BDL, C1, C2, S1, S2, C3, D = range(8)

    def __init__(self, name, irq, on_tx=None):
        self.name = name
        self.irq = irq
        self.on_tx = on_tx
        self.reset()

    def reset(self):
        self.regs = bytearray(0x20)
        self.rx = deque()

    def feed(self, data):
        self.rx.extend(data)
        self._update()

    def _update(self):
        c2 = self.regs[self.C2]
        level = bool((c2 & 0x20) and self.rx) or bool(c2 & 0xC0)
        self.cpu.set_irq(self.irq, level)

    def read(self, off, size):
        if off == self.S1:
            return 0xC0 | (0x20 if self.rx else 0)
        if off == self.D:
            v = self.rx.popleft() if self.rx else 0
            self._update()
            return v
        if off == 0x16:                         # RCFIFO
            return len(self.rx) and 1
        return self.regs[off] if off < len(self.regs) else 0

    def write(self, off, size, value):
        if off == self.D:
            if self.on_tx:
                self.on_tx(value & 0xFF)
            return
        if off < len(self.regs):
            self.regs[off] = value & 0xFF
        if off == self.C2:
            self._update()


class Mcg(Peripheral):
    """Multipurpose clock generator: report whatever mode was requested as
    locked and selected."""
    name = "mcg"

    def __init__(self, c1=0x04, c2=0x80):
        self.init = (c1, c2)
        self.reset()

    def reset(self):
        self.regs = bytearray(0x10)
        self.regs[0], self.regs[1] = self.init

    def read(self, off, size):
        if off == 6:                            # S
            c1, c6 = self.regs[0], self.regs[5]
            clks = (c1 >> 6) & 3
            s = clks << 2                       # CLKST follows CLKS
            if clks == 0 and (c6 & 0x40):
                s = 3 << 2                      # PLL selected
            if c1 & 0x04:
                s |= 0x10                       # IREFST
            if c6 & 0x40:
                s |= 0x20                       # PLLST
            s |= 0x40 | 0x02                    # LOCK0, OSCINIT0
            return s
        return self.regs[off] if off < 0x10 else 0

    def write(self, off, size, value):
        if off < 0x10:
            self.regs[off] = value & 0xFF


class Dspi(Reg32):
    """Kinetis DSPI in master mode.  Every PUSHR write clocks one frame
    through `bus.transfer(pcs, data, bits, cont)` and the reply lands in the
    RX FIFO straight away."""
    MCR, TCR, CTAR0, CTAR1, SR, RSER, PUSHR, POPR = 0, 8, 0xC, 0x10, 0x2C, 0x30, 0x34, 0x38

    def __init__(self, name, irq, bus):
        self.name = name
        self.irq = irq
        self.bus = bus
        self.reset()

    def reset(self):
        self.regs = {}
        self.sr = 0x02000000                    # TFFF
        self.rxf = deque()

    def _update(self):
        sr = self._sr()
        rser = self.regs.get(self.RSER, 0)
        self.cpu.set_irq(self.irq, bool(sr & rser & 0x9A0A0000))

    def _sr(self):
        sr = self.sr | 0x02000000
        if self.rxf:
            sr |= 0x00020000 | (min(len(self.rxf), 15) << 4)
        return sr

    def rd(self, reg):
        if reg == self.SR:
            return self._sr()
        if reg == self.POPR:
            v = self.rxf.popleft() if self.rxf else 0
            self._update()
            return v
        return self.regs.get(reg, 0x4001 if reg == self.MCR else 0)

    def peek(self, reg):
        if reg == self.POPR:
            return 0
        if reg == self.SR:
            return 0                            # w1c: partial writes only clear
        return self.rd(reg)

    def wr(self, reg, value):
        if reg == self.SR:
            self.sr &= ~(value & 0x9A0A0000)
        elif reg == self.PUSHR:
            ctar = self.regs.get(self.CTAR0 + 4 * ((value >> 28) & 1), 0x78000000)
            bits = ((ctar >> 27) & 0xF) + 1
            rx = self.bus.transfer((value >> 16) & 0x3F, value & 0xFFFF, bits,
                                   bool(value & 0x80000000))
            if len(self.rxf) < 16:
                self.rxf.append(rx & ((1 << bits) - 1))
            self.sr |= 0x80000000               # TCF
            if value & 0x08000000:
                self.sr |= 0x10000000           # EOQF
            self.regs[self.TCR] = (self.regs.get(self.TCR, 0) + 0x10000) & 0xFFFF0000
        elif reg == self.MCR:
            if value & 0x0400:
                self.rxf.clear()
            self.regs[reg] = value & ~0x0C00
        else:
            self.regs[reg] = value
        self._update()


class GpioPort:
    def __init__(self, idx):
        self.idx = idx
        self.out = 0
        self.dir = 0
        self.inp = 0xFFFFFFFF                   # external level on each pin
        self.pcr = [0] * 32


class Gpio(Peripheral):
    """GPIO + PORT (pin control / pin interrupts) for ports A..E."""
    name = "gpio"

    def __init__(self, port_irq0, on_output=None):
        self.port_irq0 = port_irq0
        self.on_output = on_output              # f(port, pin, level)
        self.ports = [GpioPort(i) for i in range(5)]

    def reset(self):
        for p in self.ports:
            p.out = p.dir = 0
            p.pcr = [0] * 32

    # -- board side --
    def level(self, port, pin):
        p = self.ports[port]
        if p.dir >> pin & 1:
            return p.out >> pin & 1
        return p.inp >> pin & 1

    def set_input(self, port, pin, level):
        p = self.ports[port]
        old = p.inp >> pin & 1
        level = 1 if level else 0
        if old == level:
            return
        p.inp = (p.inp & ~(1 << pin)) | (level << pin)
        irqc = (p.pcr[pin] >> 16) & 0xF
        if (irqc == 9 and level) or (irqc == 10 and not level) or irqc == 11 \
                or (irqc == 8 and not level) or (irqc == 12 and level):
            p.pcr[pin] |= 1 << 24
            self.cpu.set_irq(self.port_irq0 + port)

    def _set_out(self, p, new):
        changed = (p.out ^ new) & p.dir
        p.out = new
        if changed and self.on_output:
            for pin in range(32):
                if changed >> pin & 1:
                    self.on_output(p.idx, pin, new >> pin & 1)

    # -- GPIO registers (0x400FF000) --
    def read(self, off, size):
        p = self.ports[(off >> 6) % 5]
        reg = off & 0x3C
        if reg == 0x00:
            v = p.out
        elif reg == 0x10:
            v = (p.out & p.dir) | (p.inp & ~p.dir)
        elif reg == 0x14:
            v = p.dir
        else:
            v = 0
        return (v >> (8 * (off & 3))) & ((1 << (8 * size)) - 1)

    def write(self, off, size, value):
        p = self.ports[(off >> 6) % 5]
        reg = off & 0x3C
        sh = 8 * (off & 3)
        mask = ((1 << (8 * size)) - 1) << sh
        value = (value << sh) & mask
        if reg == 0x00:
            self._set_out(p, (p.out & ~mask) | value)
        elif reg == 0x04:
            self._set_out(p, p.out | value)
        elif reg == 0x08:
            self._set_out(p, p.out & ~value)
        elif reg == 0x0C:
            self._set_out(p, p.out ^ value)
        elif reg == 0x14:
            old = p.dir
            p.dir = (p.dir & ~mask) | value
            newly = p.dir & ~old
            if newly and self.on_output:
                for pin in range(32):
                    if newly >> pin & 1:
                        self.on_output(p.idx, pin, p.out >> pin & 1)


class PortCtl(Reg32):
    """PORTx_PCRn / ISFR block for one port; shares state with Gpio."""

    def __init__(self, gpio, idx):
        self.gpio = gpio
        self.idx = idx
        self.name = "port" + "abcde"[idx]

    def rd(self, reg):
        p = self.gpio.ports[self.idx]
        if reg < 0x80:
            return p.pcr[reg >> 2]
        if reg == 0xA0:
            return sum(1 << i for i in range(32) if p.pcr[i] & (1 << 24))
        return 0

    def peek(self, reg):
        if reg == 0xA0:
            return 0
        return self.rd(reg) & ~(1 << 24) if reg < 0x80 else 0

    def wr(self, reg, value):
        p = self.gpio.ports[self.idx]
        if reg < 0x80:
            n = reg >> 2
            isf = p.pcr[n] & (1 << 24)
            if value & (1 << 24):
                isf = 0
            p.pcr[n] = (value & ~(1 << 24)) | isf
        elif reg == 0x80 or reg == 0x84:
            base = 0 if reg == 0x80 else 16
            for i in range(16):
                if value >> (16 + i) & 1:
                    p.pcr[base + i] = (p.pcr[base + i] & ~0xFFFF) | (value & 0xFFFF)
        elif reg == 0xA0:
            for i in range(32):
                if value >> i & 1:
                    p.pcr[i] &= ~(1 << 24)
        if not any(x & (1 << 24) for x in p.pcr):
            self.cpu.set_irq(self.gpio.port_irq0 + self.idx, False)


class Adc16(Reg32):
    """16-bit SAR ADC.  `sample(channel)` supplies the conversion result."""
    SC1A, SC1B, CFG1, CFG2, RA, RB, CV1, CV2, SC2, SC3 = range(0, 0x28, 4)

    def __init__(self, name, irq, sample):
        self.name = name
        self.irq = irq
        self.sample = sample
        self.reset()

    def reset(self):
        self.regs = {}
        self.coco = False
        self.result = 0

    def rd(self, reg):
        if reg == self.SC1A:
            return (self.regs.get(reg, 0x1F) & 0x7F) | (0x80 if self.coco else 0)
        if reg == self.RA:
            self.coco = False
            self.cpu.set_irq(self.irq, False)
            return self.result
        if reg == self.SC3:
            return self.regs.get(reg, 0) & ~0xC0    # calibration done, no fail
        return self.regs.get(reg, 0)

    def peek(self, reg):
        return self.regs.get(reg, 0)

    def wr(self, reg, value):
        self.regs[reg] = value
        if reg == self.SC3 and value & 0x80:
            self.coco = True                    # self-calibration finishes at once
        if reg == self.SC1A:
            ch = value & 0x1F
            self.coco = False
            if ch != 0x1F:
                mode = (self.regs.get(self.CFG1, 0) >> 2) & 3
                bits = (8, 12, 10, 16)[mode]
                v = self.sample(ch, bool(value & 0x20))     # 0.0 .. 1.0
                self.result = max(0, min((1 << bits) - 1, int(v * ((1 << bits) - 1))))
                self.coco = True
                if value & 0x40:
                    self.cpu.set_irq(self.irq, True)
