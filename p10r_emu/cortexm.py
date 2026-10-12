"""Cortex-M4 system core on top of Unicorn.

Unicorn gives us the Thumb-2 instruction set and the M-profile register file
(MSP/PSP/CONTROL/BASEPRI/...), but none of the system: no NVIC, no SysTick, no
exception entry/return.  This module supplies those, plus an MMIO bus that
peripheral models plug into.
"""
import heapq
import struct

from unicorn import (Uc, UcError, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS,
                     UC_HOOK_CODE,
                     UC_ERR_WRITE_PROT, UC_PROT_READ,
                     UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UC_HOOK_INTR,
                     UC_HOOK_MEM_UNMAPPED, UC_HOOK_INSN_INVALID,
                     UC_MEM_WRITE, UC_PROT_ALL)
from unicorn.arm_const import *

EXC_RESET, EXC_NMI, EXC_HARDFAULT = 1, 2, 3
EXC_SVC, EXC_PENDSV, EXC_SYSTICK = 11, 14, 15
IRQ0 = 16

SCS_BASE = 0xE000E000
_PAGE = 0x1000

_GPR = [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
        UC_ARM_REG_R12, UC_ARM_REG_LR]
_FP_D = [UC_ARM_REG_D0 + i for i in range(8)]
_EPSR_T = 0x01000000


class Peripheral:
    """Base class for a memory-mapped peripheral.  Offsets are relative to
    the address it was attached at."""
    name = "periph"

    def read(self, off, size):
        return 0

    def write(self, off, size, value):
        pass

    def reset(self):
        pass


class RegBank(Peripheral):
    """Peripheral that just remembers what was written.  Good enough for
    clock/pin-mux style registers the firmware only configures."""

    def __init__(self, name, defaults=None):
        self.name = name
        self.defaults = dict(defaults or {})
        self.mem = {}

    def reset(self):
        self.mem = {}

    def read(self, off, size):
        v = 0
        for i in range(size):
            b = self.mem.get(off + i)
            if b is None:
                d = self.defaults.get((off + i) & ~3, 0)
                b = (d >> (8 * ((off + i) & 3))) & 0xFF
            v |= b << (8 * i)
        return v

    def write(self, off, size, value):
        for i in range(size):
            self.mem[off + i] = (value >> (8 * i)) & 0xFF


class Reg32(Peripheral):
    """Helper for peripherals made of 32-bit registers: sub-word accesses
    are folded into whole-register rd()/wr() calls."""

    def rd(self, reg):
        return 0

    def peek(self, reg):
        """Side-effect-free read used to merge partial writes."""
        return self.rd(reg)

    def wr(self, reg, value):
        pass

    def read(self, off, size):
        reg = off & ~3
        return (self.rd(reg) >> (8 * (off & 3))) & ((1 << (8 * size)) - 1)

    def write(self, off, size, value):
        reg = off & ~3
        if size != 4:
            sh = 8 * (off & 3)
            mask = ((1 << (8 * size)) - 1) << sh
            value = (self.peek(reg) & ~mask) | ((value << sh) & mask)
        self.wr(reg, value & 0xFFFFFFFF)


class BitBand(Peripheral):
    """Cortex-M bit-band alias region: one word per bit of the target."""
    name = "bitband"

    def __init__(self, target_base):
        self.target = target_base

    def read(self, off, size):
        addr, bit = self.target + (off >> 5), (off >> 2) & 7
        return (self.cpu.bus_read(addr, 1) >> bit) & 1

    def write(self, off, size, value):
        addr, bit = self.target + (off >> 5), (off >> 2) & 7
        old = self.cpu.bus_read(addr, 1)
        self.cpu.bus_write(addr, 1, (old & ~(1 << bit)) | ((value & 1) << bit))


class EmuFault(Exception):
    pass


class CortexM:
    def __init__(self, cpu_hz=48_000_000, cycles_per_insn=4):
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        self.cpu_hz = cpu_hz
        self.cycles_per_insn = cycles_per_insn
        self.cycles = 0                 # virtual CPU cycles since reset
        self.periphs = []               # (base, end, periph)
        self._page_map = {}             # page -> periph list
        self.unknown_log = {}           # addr -> [reads, writes, first pc]
        self.trace_unknown = False
        self.vtor = 0
        # NVIC state
        self.pending = set()            # latched exception numbers
        self.lines = set()              # exception numbers whose IRQ line is high
        self.active = []                # stack of active exception numbers
        self.enabled = set()            # enabled external IRQ exception numbers
        self.prio = {}                  # exception number -> priority byte
        self.systick_ctrl = 0
        self.systick_load = 0
        self.systick_next = None        # cycle count of next wrap
        self.shcsr = 0
        self.scb_regs = {}
        self.timers = []                # objects with .next_event / .fire(now)
        self._events = []               # heap of (cycle, seq, fn): one-shot callbacks
        self._event_seq = 0
        self.wfi = False
        self.reset_requested = False
        self._stop_reason = None
        self._stop_pc = None
        self._replaying = False
        self._in_emu = False
        self.fault = None
        uc = self.uc
        uc.hook_add(UC_HOOK_INTR, self._on_intr)
        uc.hook_add(UC_HOOK_MEM_UNMAPPED, self._on_unmapped)
        uc.hook_add(UC_HOOK_INSN_INVALID, self._on_invalid)
        # System control space.  The SCS page itself is write-protected:
        # a store there (PendSV request, NVIC enable, ...) must take effect
        # before the next instruction, and a protection fault is the only
        # way to get Unicorn to stop that precisely (see run()).
        self.map_mmio(0xE0000000, 0x100000)
        uc.mem_protect(SCS_BASE, _PAGE, UC_PROT_READ)
        self._scs_wr_pc = None
        # EXC_RETURN landing page: never executed, a fetch here is trapped
        # by the unmapped hook.

    # ---- memory -----------------------------------------------------
    def map_ram(self, base, size, data=None):
        self.uc.mem_map(base, size, UC_PROT_ALL)
        if data:
            self.uc.mem_write(base, bytes(data))

    def map_mmio(self, base, size):
        self.uc.mem_map(base, size, UC_PROT_ALL)
        self.uc.hook_add(UC_HOOK_MEM_READ, self._mmio_read, begin=base,
                         end=base + size - 1)
        self.uc.hook_add(UC_HOOK_MEM_WRITE, self._mmio_write, begin=base,
                         end=base + size - 1)

    def attach(self, base, size, periph):
        periph.cpu = self
        periph.base = base
        self.periphs.append((base, base + size, periph))
        for page in range(base & ~(_PAGE - 1), base + size, _PAGE):
            self._page_map.setdefault(page, []).append((base, base + size, periph))
        return periph

    def _find(self, addr):
        for base, end, p in self._page_map.get(addr & ~(_PAGE - 1), ()):
            if base <= addr < end:
                return p, addr - base
        return None, 0

    # Unicorn quirk: a memory hook that fires inside a Thumb IT block leaves
    # stale IT bits in the CPU state, which then make the first instruction
    # of the *next* translation block conditional.  Clearing EPSR.IT before
    # returning from every hook avoids that (the rest of the IT block is
    # already translated, so it still executes correctly).
    def _mmio_read(self, uc, access, addr, size, value, ud):
        if self._replaying:
            uc.reg_write(UC_ARM_REG_EPSR, _EPSR_T)
            return                      # value already latched on first pass
        try:
            if 0xE000E000 <= addr < 0xE000F000:
                v = self._scs_read(addr, size)
            else:
                p, off = self._find(addr)
                if p is None:
                    v = self._unknown(addr, size, None)
                else:
                    v = p.read(off, size)
            uc.mem_write(addr, (v & ((1 << (8 * size)) - 1)).to_bytes(size, "little"))
        except Exception as e:  # never let an exception cross the C boundary
            self._py_error(e)
        uc.reg_write(UC_ARM_REG_EPSR, _EPSR_T)

    def _mmio_write(self, uc, access, addr, size, value, ud):
        if self._replaying:
            uc.reg_write(UC_ARM_REG_EPSR, _EPSR_T)
            return                      # second pass of an interrupted store
        try:
            value &= (1 << (8 * size)) - 1
            if 0xE000E000 <= addr < 0xE000F000:
                self._scs_wr_pc = uc.reg_read(UC_ARM_REG_PC)
                self._scs_write(addr, size, value)
            else:
                p, off = self._find(addr)
                if p is None:
                    self._unknown(addr, size, value)
                else:
                    p.write(off, size, value)
        except Exception as e:
            self._py_error(e)
        uc.reg_write(UC_ARM_REG_EPSR, _EPSR_T)

    def bus_read(self, addr, size):
        """Device-level read, bypassing the CPU (used by bit-band etc)."""
        p, off = self._find(addr)
        if p is None:
            return self._unknown(addr, size, None)
        return p.read(off, size) & ((1 << (8 * size)) - 1)

    def bus_write(self, addr, size, value):
        p, off = self._find(addr)
        if p is None:
            self._unknown(addr, size, value)
        else:
            p.write(off, size, value)

    def sys_read(self, addr, size):
        """Read as a bus master other than the CPU would (DMA/DTC)."""
        if self._find(addr)[0] is not None:
            return self.bus_read(addr, size)
        return int.from_bytes(self.uc.mem_read(addr, size), "little")

    def sys_write(self, addr, size, value):
        if self._find(addr)[0] is not None:
            self.bus_write(addr, size, value)
        else:
            self.uc.mem_write(addr, (value & ((1 << (8 * size)) - 1)).to_bytes(size, "little"))

    def _py_error(self, e):
        import traceback
        self.fault = "python error in device model: %r\n%s" % (e, traceback.format_exc())
        self._stop("fault")

    def _unknown(self, addr, size, value):
        ent = self.unknown_log.get(addr)
        if ent is None:
            ent = self.unknown_log[addr] = [0, 0, self.pc]
            if self.trace_unknown:
                print("  [mmio?] %s %08x size %d%s pc=%08x" % (
                    "R" if value is None else "W", addr, size,
                    "" if value is None else " = %x" % value, self.pc))
        ent[0 if value is None else 1] += 1
        return 0

    # ---- registers --------------------------------------------------
    @property
    def pc(self):
        return self.uc.reg_read(UC_ARM_REG_PC)

    @property
    def sp(self):
        return self.uc.reg_read(UC_ARM_REG_SP)

    def r(self, n):
        return self.uc.reg_read(UC_ARM_REG_R0 + n)

    def read32(self, addr):
        return struct.unpack("<I", self.uc.mem_read(addr, 4))[0]

    def write32(self, addr, v):
        self.uc.mem_write(addr, struct.pack("<I", v & 0xFFFFFFFF))

    def read_cstr(self, addr, maxlen=256):
        out = bytearray()
        while len(out) < maxlen:
            b = self.uc.mem_read(addr + len(out), 1)[0]
            if not b:
                break
            out.append(b)
        return out.decode("latin-1")

    # ---- system control space ---------------------------------------
    def _systick_cur(self):
        if not (self.systick_ctrl & 1) or self.systick_next is None:
            return 0
        rem = self.systick_next - self.cycles
        return max(0, min(rem, self.systick_load))

    def _scs_read(self, addr, size):
        a = addr & ~3
        sh = (addr & 3) * 8
        if a == 0xE000E010:
            v = self.systick_ctrl
            self.systick_ctrl &= ~0x10000
        elif a == 0xE000E014:
            v = self.systick_load
        elif a == 0xE000E018:
            v = self._systick_cur()
        elif a == 0xE000E01C:
            v = 0
        elif 0xE000E100 <= a < 0xE000E120 or 0xE000E180 <= a < 0xE000E1A0:
            n = (a & 0x7F) // 4
            v = sum(1 << i for i in range(32) if (IRQ0 + n * 32 + i) in self.enabled)
        elif 0xE000E200 <= a < 0xE000E220 or 0xE000E280 <= a < 0xE000E2A0:
            n = (a & 0x7F) // 4
            v = sum(1 << i for i in range(32) if (IRQ0 + n * 32 + i) in self.pending)
        elif 0xE000E300 <= a < 0xE000E320:
            n = (a & 0x7F) // 4
            v = sum(1 << i for i in range(32) if (IRQ0 + n * 32 + i) in self.active)
        elif 0xE000E400 <= a < 0xE000E500:
            base = IRQ0 + (a - 0xE000E400)
            v = sum(self.prio.get(base + i, 0) << (8 * i) for i in range(4))
        elif a == 0xE000ED00:
            v = 0x410FC241                       # Cortex-M4 r0p1
        elif a == 0xE000ED04:                    # ICSR
            v = self.active[-1] if self.active else 0
            pend = self._highest_pending(ignore_mask=True)
            if pend:
                v |= pend << 12
                if pend >= IRQ0:
                    v |= 1 << 22
            if EXC_PENDSV in self.pending:
                v |= 1 << 28
            if EXC_SYSTICK in self.pending:
                v |= 1 << 26
            if len(self.active) <= 1:
                v |= 1 << 11
        elif a == 0xE000ED08:
            v = self.vtor
        elif 0xE000ED18 <= a < 0xE000ED24:
            base = 4 + (a - 0xE000ED18)
            v = sum(self.prio.get(base + i, 0) << (8 * i) for i in range(4))
        elif a == 0xE000ED24:
            v = self.shcsr
        else:
            v = self.scb_regs.get(a, 0)
        return (v >> sh) & ((1 << (8 * size)) - 1)

    def _scs_write(self, addr, size, value):
        a = addr & ~3
        sh = (addr & 3) * 8
        if size != 4:
            # byte/halfword access: merge for plain storage registers
            if 0xE000E400 <= addr < 0xE000E500:
                for i in range(size):
                    self.prio[IRQ0 + (addr - 0xE000E400) + i] = (value >> (8 * i)) & 0xFF
                return
            if 0xE000ED18 <= addr < 0xE000ED24:
                for i in range(size):
                    self.prio[4 + (addr - 0xE000ED18) + i] = (value >> (8 * i)) & 0xFF
                return
            old = self._scs_read(a, 4)
            mask = ((1 << (8 * size)) - 1) << sh
            value = (old & ~mask) | (value << sh)
        if a == 0xE000E010:
            was = self.systick_ctrl & 1
            self.systick_ctrl = value & 7
            if (value & 1) and not was:
                self.systick_next = self.cycles + self.systick_load + 1
            elif not (value & 1):
                self.systick_next = None
        elif a == 0xE000E014:
            self.systick_load = value & 0xFFFFFF
        elif a == 0xE000E018:
            if self.systick_ctrl & 1:
                self.systick_next = self.cycles + self.systick_load + 1
        elif 0xE000E100 <= a < 0xE000E120:
            n = (a & 0x7F) // 4
            for i in range(32):
                if value >> i & 1:
                    self.enabled.add(IRQ0 + n * 32 + i)
            self._stop("irq")
        elif 0xE000E180 <= a < 0xE000E1A0:
            n = (a & 0x7F) // 4
            for i in range(32):
                if value >> i & 1:
                    self.enabled.discard(IRQ0 + n * 32 + i)
        elif 0xE000E200 <= a < 0xE000E220:
            n = (a & 0x7F) // 4
            for i in range(32):
                if value >> i & 1:
                    self.pending.add(IRQ0 + n * 32 + i)
            self._stop("irq")
        elif 0xE000E280 <= a < 0xE000E2A0:
            n = (a & 0x7F) // 4
            for i in range(32):
                if value >> i & 1:
                    self.pending.discard(IRQ0 + n * 32 + i)
        elif 0xE000E400 <= a < 0xE000E500:
            for i in range(4):
                self.prio[IRQ0 + (a - 0xE000E400) + i] = (value >> (8 * i)) & 0xFF
        elif a == 0xE000ED04:                    # ICSR
            if value & (1 << 28):
                self.pending.add(EXC_PENDSV)
                self._stop("irq")
            if value & (1 << 27):
                self.pending.discard(EXC_PENDSV)
            if value & (1 << 26):
                self.pending.add(EXC_SYSTICK)
                self._stop("irq")
            if value & (1 << 25):
                self.pending.discard(EXC_SYSTICK)
        elif a == 0xE000ED08:
            self.vtor = value & 0xFFFFFF80
        elif a == 0xE000ED0C:                    # AIRCR
            if (value >> 16) == 0x05FA and value & 4:
                self._stop("reset")
            self.scb_regs[a] = value & 0x700
        elif 0xE000ED18 <= a < 0xE000ED24:
            for i in range(4):
                self.prio[4 + (a - 0xE000ED18) + i] = (value >> (8 * i)) & 0xFF
        elif a == 0xE000ED24:
            self.shcsr = value
        else:
            self.scb_regs[a] = value

    # ---- NVIC -------------------------------------------------------
    def set_irq(self, irq, level=True):
        """Drive external interrupt line `irq` (0-based).  Lines are level
        sensitive: a handler that returns with the line still high is
        re-entered, as on the real NVIC."""
        exc = IRQ0 + irq
        if level:
            if exc not in self.lines:
                self.lines.add(exc)
                self.pending.add(exc)
                if exc in self.enabled:
                    self._stop("irq")
        else:
            self.lines.discard(exc)
            self.pending.discard(exc)

    def pulse_irq(self, irq):
        """Edge-style request: latch pending once."""
        self.pending.add(IRQ0 + irq)
        self._stop("irq")

    def _prio_of(self, exc):
        if exc == EXC_NMI:
            return -2
        if exc == EXC_HARDFAULT:
            return -1
        return self.prio.get(exc, 0)

    def _exec_prio(self, ignore_mask=False):
        p = 256
        if self.active:
            p = min(self._prio_of(e) for e in self.active)
        if not ignore_mask:
            if self.uc.reg_read(UC_ARM_REG_PRIMASK) & 1:
                p = min(p, 0)
            bp = self.uc.reg_read(UC_ARM_REG_BASEPRI) & 0xFF
            if bp:
                p = min(p, bp)
        return p

    def _highest_pending(self, ignore_mask=False):
        best = None
        bestp = self._exec_prio(ignore_mask)
        cand = self.pending
        if self.lines:
            cand = cand | self.lines
        for e in cand:
            if e >= IRQ0 and (e not in self.enabled or e in self.active):
                continue
            pr = self._prio_of(e)
            if pr < bestp or (pr == bestp and best is not None and e < best):
                best, bestp = e, pr
        return best

    def _push_frame(self, uc, ret_pc):
        control = uc.reg_read(UC_ARM_REG_CONTROL)
        fpca = bool(control & 4)
        sp = uc.reg_read(UC_ARM_REG_SP)
        xpsr = uc.reg_read(UC_ARM_REG_XPSR)
        frame_size = 0x68 if fpca else 0x20
        new_sp = sp - frame_size
        align = 0
        if new_sp & 4:
            new_sp -= 4
            align = 1
        regs = [uc.reg_read(r) for r in _GPR]
        frame = struct.pack("<8I", regs[0], regs[1], regs[2], regs[3], regs[4],
                            regs[5], ret_pc & ~1,
                            (xpsr & ~(1 << 9)) | (align << 9) | (1 << 24))
        if fpca:
            for d in _FP_D:
                frame += struct.pack("<Q", uc.reg_read(d))
            frame += struct.pack("<II", uc.reg_read(UC_ARM_REG_FPSCR), 0)
        uc.mem_write(new_sp, frame)
        uc.reg_write(UC_ARM_REG_SP, new_sp)
        return control, fpca

    def _enter_exception(self, exc):
        uc = self.uc
        ret_pc = uc.reg_read(UC_ARM_REG_PC)
        control, fpca = self._push_frame(uc, ret_pc)
        thread = not self.active
        if thread and (control & 2):
            exc_return = 0xFFFFFFFD
        elif thread:
            exc_return = 0xFFFFFFF9
        else:
            exc_return = 0xFFFFFFF1
        if fpca:
            exc_return &= ~0x10
        self.active.append(exc)
        self.pending.discard(exc)
        # Entering handler mode: IPSR != 0 makes Unicorn bank to MSP.
        uc.reg_write(UC_ARM_REG_IPSR, exc)
        uc.reg_write(UC_ARM_REG_EPSR, _EPSR_T)
        uc.reg_write(UC_ARM_REG_CONTROL, control & ~6)
        uc.reg_write(UC_ARM_REG_LR, exc_return)
        handler = self.read32(self.vtor + 4 * exc)
        uc.reg_write(UC_ARM_REG_PC, handler | 1)
        self.wfi = False

    def _exit_exception(self, exc_return):
        uc = self.uc
        if not self.active:
            raise EmuFault("exception return 0x%08x with no active exception" % exc_return)
        self.active.pop()
        to_thread = bool(exc_return & 8)
        use_psp = to_thread and bool(exc_return & 4)
        has_fp = not (exc_return & 0x10)
        control = uc.reg_read(UC_ARM_REG_CONTROL) & 1
        # Leave handler mode first so the SPSEL write takes effect.
        uc.reg_write(UC_ARM_REG_IPSR, self.active[-1] if self.active else 0)
        if to_thread:
            uc.reg_write(UC_ARM_REG_CONTROL, control | (2 if use_psp else 0))
        sp = uc.reg_read(UC_ARM_REG_SP)
        r0, r1, r2, r3, r12, lr, pc, xpsr = struct.unpack("<8I", uc.mem_read(sp, 32))
        sp += 0x20
        if has_fp:
            for i, d in enumerate(_FP_D):
                uc.reg_write(d, struct.unpack("<Q", uc.mem_read(sp + 8 * i, 8))[0])
            uc.reg_write(UC_ARM_REG_FPSCR, self.read32(sp + 0x40))
            sp += 0x48
        if xpsr & (1 << 9):
            sp += 4
        for reg, val in zip(_GPR, (r0, r1, r2, r3, r12, lr)):
            uc.reg_write(reg, val)
        uc.reg_write(UC_ARM_REG_SP, sp)
        uc.reg_write(UC_ARM_REG_APSR, xpsr & 0xF80F0000)
        uc.reg_write(UC_ARM_REG_EPSR, xpsr & 0x0700FC00)
        c = uc.reg_read(UC_ARM_REG_CONTROL)
        uc.reg_write(UC_ARM_REG_CONTROL, (c | 4) if has_fp else (c & ~4))
        uc.reg_write(UC_ARM_REG_PC, pc | 1)

    # ---- hooks ------------------------------------------------------
    def _stop(self, reason, in_store=True):
        """Ask the run loop to break out of Unicorn.  Called from device
        models (inside a memory hook) and from outside emulation alike."""
        if self._stop_reason is None or reason in ("fault", "reset"):
            self._stop_reason = reason
        if not in_store:
            self.uc.emu_stop()
        elif self._scs_wr_pc is not None:
            pass                        # SCS store: the fault stops us anyway
        elif self._in_emu and not self._replaying:
            # emu_stop() inside a memory hook abandons the current
            # instruction; remember it so run() can finish it off.
            if self._stop_pc is None:
                self._stop_pc = self.uc.reg_read(UC_ARM_REG_PC)
            self.uc.emu_stop()

    def _finish_scs_store(self):
        """A store to the write-protected SCS page has faulted.  The device
        write itself already happened in the write hook.  Outside an IT
        block Unicorn stops *on* the store, so step over it; inside an IT
        block it stops after the block and there is nothing left to do."""
        uc = self.uc
        pc = uc.reg_read(UC_ARM_REG_PC)
        if pc != self._scs_wr_pc:
            if self._stop_reason is None:
                self._stop_reason = "irq"
            return
        hw1, hw2 = struct.unpack("<HH", uc.mem_read(pc & ~1, 4))
        size = 0
        if hw1 < 0xE800:
            if (hw1 & 0xF800) in (0x6000, 0x7000, 0x8000) or (hw1 & 0xFA00) == 0x5000                     or (hw1 & 0xFE00) == 0x5400:
                size = 2                # STR/STRB/STRH, immediate or register offset
        elif (hw1 & 0xFF90) == 0xF880 or                 ((hw1 & 0xFF90) == 0xF800 and (hw2 & 0x0FC0) == 0):
            size = 4                    # 32-bit forms without writeback
        if size:
            uc.reg_write(UC_ARM_REG_PC, (pc + size) | 1)
        else:
            # Unusual addressing mode: let the CPU do it for real.
            uc.mem_protect(SCS_BASE, _PAGE, UC_PROT_ALL)
            self._replaying = True
            try:
                uc.emu_start(pc | 1, 0xFFFFFFF0, count=1)
            finally:
                self._replaying = False
                uc.mem_protect(SCS_BASE, _PAGE, UC_PROT_READ)
        if self._stop_reason is None:
            self._stop_reason = "irq"

    def hook_unmask_sites(self, base, image):
        """Find every instruction that can lower the interrupt mask
        (MSR BASEPRI/PRIMASK, CPSIE) and re-evaluate pending exceptions
        right after it.  An RTOS depends on a pended PendSV firing the
        instant a critical section ends; waiting for the end of the
        current time slice lets the task run on and corrupt kernel lists."""
        sites = set()
        for off in range(0, len(image) - 3, 2):
            hw1 = image[off] | (image[off + 1] << 8)
            if hw1 == 0xB662:
                sites.add(base + off + 2)
            elif (hw1 & 0xFFF0) == 0xF380:
                hw2 = image[off + 2] | (image[off + 3] << 8)
                if hw2 in (0x8810, 0x8811):
                    sites.add(base + off + 4)
        for addr in sites:
            self.uc.hook_add(UC_HOOK_CODE, self._on_unmask, begin=addr, end=addr)
        return len(sites)

    def _on_unmask(self, uc, addr, size, ud):
        if (self.pending or self.lines) and self._highest_pending() is not None:
            self._stop("irq", False)

    def _on_intr(self, uc, intno, ud):
        try:
            if intno == 2:                       # SVC
                self.pending.add(EXC_SVC)
                self._stop("irq", False)
            elif intno == 8:                     # EXCP_EXCEPTION_EXIT
                self._pending_exit = uc.reg_read(UC_ARM_REG_PC) | 1
                self._stop("excret", False)
            elif intno == 7:                     # BKPT
                self.fault = "BKPT at %08x" % self.pc
                self._stop("fault", False)
            else:
                self.fault = "CPU exception %d at pc=%08x" % (intno, self.pc)
                self._stop("fault", False)
        except Exception as e:
            self._py_error(e)

    def _on_unmapped(self, uc, access, addr, size, value, ud):
        if addr >= 0xFFFFFF00 and access not in (UC_MEM_WRITE,):
            self._pending_exit = addr | 1
            self._stop_reason = "excret"
            return False
        self.fault = "unmapped %s at %08x (size %d) pc=%08x lr=%08x" % (
            "write" if access in (UC_MEM_WRITE, 20) else "access/%d" % access,
            addr, size, self.pc, uc.reg_read(UC_ARM_REG_LR))
        self._stop_reason = "fault"
        return False

    def _on_invalid(self, uc, ud):
        self.fault = "invalid instruction at %08x" % self.pc
        self._stop_reason = "fault"
        return False

    # ---- run loop ---------------------------------------------------
    def reset(self, vtor):
        uc = self.uc
        self.vtor = vtor
        self.pending.clear()
        self.lines.clear()
        self.active.clear()
        self.enabled.clear()
        self.prio.clear()
        self.systick_ctrl = 0
        self.systick_next = None
        self.cycles = 0
        self.fault = None
        self.wfi = False
        self.reset_requested = False
        self._events = []
        self.shcsr = 0
        self.scb_regs = {}
        self._stop_reason = None
        for _, _, p in self.periphs:
            p.reset()
        for reg in (UC_ARM_REG_PRIMASK, UC_ARM_REG_BASEPRI, UC_ARM_REG_FAULTMASK,
                    UC_ARM_REG_IPSR):
            uc.reg_write(reg, 0)
        uc.reg_write(UC_ARM_REG_CONTROL, 0)
        uc.reg_write(UC_ARM_REG_MSP, self.read32(vtor))
        uc.reg_write(UC_ARM_REG_SP, self.read32(vtor))
        uc.reg_write(UC_ARM_REG_LR, 0xFFFFFFFF)
        uc.reg_write(UC_ARM_REG_PC, self.read32(vtor + 4) | 1)

    def add_timer(self, t):
        self.timers.append(t)

    def schedule(self, delay_cycles, fn):
        """Call fn() once, `delay_cycles` CPU cycles from now."""
        self._event_seq += 1
        heapq.heappush(self._events, (self.cycles + max(1, int(delay_cycles)),
                                      self._event_seq, fn))
        if self._in_emu:
            self._stop("irq")           # shorten the slice in progress

    def _next_event(self):
        nxt = self.systick_next
        if self._events and (nxt is None or self._events[0][0] < nxt):
            nxt = self._events[0][0]
        for t in self.timers:
            n = t.next_event
            if n is not None and (nxt is None or n < nxt):
                nxt = n
        return nxt

    def _service_time(self):
        now = self.cycles
        if self.systick_next is not None and now >= self.systick_next:
            period = self.systick_load + 1
            missed = (now - self.systick_next) // period
            self.systick_next += (missed + 1) * period
            self.systick_ctrl |= 0x10000
            if self.systick_ctrl & 2:
                self.pending.add(EXC_SYSTICK)
        ev = self._events
        while ev and ev[0][0] <= now:
            heapq.heappop(ev)[2]()
        for t in self.timers:
            n = t.next_event
            if n is not None and now >= n:
                t.fire(now)

    def run(self, cycles, slice_insns=2000):
        """Run for `cycles` virtual CPU cycles.  Returns False on a fault."""
        uc = self.uc
        end = self.cycles + cycles
        cpi = self.cycles_per_insn
        while self.cycles < end:
            if self.fault:
                return False
            self._service_time()
            exc = self._highest_pending()
            if exc is not None:
                self._enter_exception(exc)
            nxt = self._next_event()
            if self.wfi and self._highest_pending(ignore_mask=True) is not None:
                self.wfi = False        # WFI wakes even if the IRQ is masked
            if self.wfi:
                # Sleeping: jump straight to the next timed event.
                tgt = end if nxt is None else min(nxt, end)
                self.cycles = max(self.cycles + 1, tgt)
                continue
            budget = end - self.cycles
            if nxt is not None:
                budget = min(budget, nxt - self.cycles)
            n = max(1, min(slice_insns, budget // cpi))
            self._stop_reason = None
            self._stop_pc = None
            self._scs_wr_pc = None
            pc = uc.reg_read(UC_ARM_REG_PC)
            self._in_emu = True
            try:
                try:
                    uc.emu_start(pc | 1, 0xFFFFFFF0, count=n)
                except UcError as e:
                    if e.errno != UC_ERR_WRITE_PROT or self._scs_wr_pc is None:
                        raise
                    self._finish_scs_store()
                if (self._stop_pc is not None and self._stop_reason != "fault"
                        and uc.reg_read(UC_ARM_REG_PC) == self._stop_pc):
                    self._replaying = True
                    uc.emu_start(self._stop_pc | 1, 0xFFFFFFF0, count=1)
            except UcError as e:
                if self._stop_reason is None:
                    self.fault = "unicorn: %s at pc=%08x" % (e, self.pc)
                    self._stop_reason = "fault"
            finally:
                self._replaying = False
                self._in_emu = False
            reason = self._stop_reason
            self.cycles += (n if reason is None else max(1, n // 4)) * cpi
            if reason == "excret":
                try:
                    self._exit_exception(self._pending_exit)
                except (EmuFault, UcError) as e:
                    self.fault = "bad exception return: %s" % e
                    return False
            elif reason == "fault":
                return False
            elif reason == "reset":
                self.reset_requested = True
                return False
            elif reason is None:
                # Did we stop on a WFI?  Unicorn halts the vCPU there.
                npc = uc.reg_read(UC_ARM_REG_PC)
                try:
                    if bytes(uc.mem_read((npc & ~1) - 2, 2)) == b"\x30\xbf":
                        self.wfi = self._highest_pending(ignore_mask=True) is None
                except UcError:
                    pass
        return True
