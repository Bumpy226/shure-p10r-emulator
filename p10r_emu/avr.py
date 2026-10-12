"""AVR XMEGA CPU core (22-bit PC) for the original P10R.

Unicorn has no AVR back end, so this is a small interpreter of its own: the
whole flash image is pre-decoded into one Python closure per instruction and
the run loop just chains them.  Time is counted in instructions
(`cycles_per_insn` CPU clocks each), which is all the firmware's tick-driven
code needs.

Data space, as on the XMEGA A3:
    0x0000..0x0FFF  I/O registers  (handlers installed with `io()`)
    0x1000..0x1FFF  memory-mapped EEPROM
    0x2000..        SRAM

Interrupts follow the XMEGA PMIC model: a peripheral asserts a vector at a
level 1..3 with `set_irq()`; the core takes it when global interrupts and
that level are enabled and no interrupt of the same or a higher level is
executing.  RETI drops the executing level.
"""
import heapq
import struct

C_, Z_, N_, V_, S_, H_, T_, I_, SP_, RAMPZ_, EIND_ = range(11)

IO_END = 0x1000
RAM_START = 0x2000


class _Break(Exception):
    """Leaves the instruction batch early; core.pc is already up to date."""


class AvrCore:
    BATCH = 48

    def __init__(self, flash, cpu_hz=32_000_000, cycles_per_insn=2, ram_end=0x5FFF,
                 vector_words=2):
        if len(flash) & 1:
            flash += b"\xFF"
        self.cpu_hz = cpu_hz
        self.cpi = cycles_per_insn
        self.ram_end = ram_end
        self.vector_words = vector_words
        self.flash = bytearray(flash)
        self.words = list(struct.unpack("<%dH" % (len(flash) // 2), bytes(flash)))
        self.R = [0] * 32
        self.S = [0] * 11
        self.D = bytearray(0x10000)
        self.pc = 0
        self.cycles = 0
        self.insns = 0
        self.fault = None
        self.reset_requested = False
        self.sleeping = False
        self.irq_check = False
        self.pending = {}           # vector -> level
        self.irq_ack = {}           # vector -> f() called when the vector is taken
        self.pmic_ctrl = 0
        self.pmic_status = 0
        self.unknown_log = {}       # addr -> [reads, writes, first pc]
        self.on_ee_write = None     # f(addr, value): store into the mapped EEPROM page
        self._events = []
        self._seq = 0
        self.trace = None           # optional f(pc) called per batch start
        self.ior = [None] * IO_END
        self.iow = [None] * IO_END
        for a in range(IO_END):
            self.ior[a] = self._default_read(a)
            self.iow[a] = self._default_write(a)
        self._build()

    # ------------------------------------------------------------------
    # I/O space
    # ------------------------------------------------------------------
    def _default_read(self, a):
        D, log = self.D, self.unknown_log

        def f():
            e = log.get(a)
            if e is None:
                e = log[a] = [0, 0, self.pc]
            e[0] += 1
            return D[a]
        return f

    def _default_write(self, a):
        D, log = self.D, self.unknown_log

        def f(v):
            e = log.get(a)
            if e is None:
                e = log[a] = [0, 0, self.pc]
            e[1] += 1
            D[a] = v
        return f

    def io(self, addr, read=None, write=None):
        """Install handlers for one I/O address: read() -> byte, write(byte)."""
        if read is not None:
            self.ior[addr] = read
        if write is not None:
            self.iow[addr] = write

    def io_ram(self, addr, count=1):
        """Plain storage, no logging."""
        D = self.D
        for a in range(addr, addr + count):
            self.ior[a] = (lambda a=a: D[a])
            self.iow[a] = (lambda v, a=a: D.__setitem__(a, v))

    def read_mem(self, a):
        return self.D[a] if a >= IO_END else self.ior[a]()

    def write_mem(self, a, v):
        if a >= RAM_START:
            self.D[a] = v
        elif a >= IO_END:
            if self.on_ee_write:
                self.on_ee_write(a - IO_END, v)
        else:
            self.iow[a](v)

    # ------------------------------------------------------------------
    # interrupts, time
    # ------------------------------------------------------------------
    def set_irq(self, vector, level):
        """Assert (level 1..3) or drop (level 0) an interrupt request."""
        if level:
            if self.pending.get(vector) != level:
                self.pending[vector] = level
                self.irq_check = True
        else:
            self.pending.pop(vector, None)

    def schedule(self, delay_cycles, fn):
        self._seq += 1
        heapq.heappush(self._events, (self.cycles + max(0, int(delay_cycles)), self._seq, fn))

    def _stop(self, reason):
        if reason == "reset":
            self.reset_requested = True
        else:
            self.fault = reason
        raise _Break()

    def _exec_level(self):
        st = self.pmic_status & 7
        return 3 if st & 4 else 2 if st & 2 else 1 if st & 1 else 0

    def _wake_wanted(self):
        en = self.pmic_ctrl & 7
        for lvl in self.pending.values():
            if en & (1 << (lvl - 1)):
                return True
        return False

    def _dispatch(self):
        self.irq_check = False
        if not self.pending or not self.S[I_]:
            return
        cur = self._exec_level()
        en = self.pmic_ctrl & 7
        best = None
        for vec, lvl in self.pending.items():
            if lvl > cur and en & (1 << (lvl - 1)):
                if best is None or (lvl, -vec) > (best[1], -best[0]):
                    best = (vec, lvl)
        if best is None:
            return
        vec, lvl = best
        S, D = self.S, self.D
        sp, pc = S[SP_], self.pc
        D[sp] = pc & 0xFF
        D[sp - 1] = (pc >> 8) & 0xFF
        D[sp - 2] = (pc >> 16) & 0xFF
        S[SP_] = sp - 3
        self.pmic_status |= 1 << (lvl - 1)
        self.pc = vec * self.vector_words
        self.sleeping = False
        ack = self.irq_ack.get(vec)
        if ack:
            ack()
        if self.pending:
            self.irq_check = True

    def reset(self):
        self.R[:] = [0] * 32
        self.S[:] = [0] * 11
        self.S[SP_] = self.ram_end
        self.D[RAM_START:] = bytes(len(self.D) - RAM_START)
        self.D[:IO_END] = bytes(IO_END)
        self.pc = 0
        self.fault = None
        self.reset_requested = False
        self.sleeping = False
        self.pending.clear()
        self.pmic_ctrl = 0
        self.pmic_status = 0
        self._events = []
        self.irq_check = False

    def run(self, cycles):
        """Run for a number of CPU clock cycles.  Returns False on a fault."""
        target = self.cycles + cycles
        ops, cpi, events = self._ops, self.cpi, self._events
        BATCH = self.BATCH
        while self.cycles < target and not self.fault and not self.reset_requested:
            now = self.cycles
            while events and events[0][0] <= now:
                fn = heapq.heappop(events)[2]
                try:
                    fn()
                except _Break:
                    pass
            if self.irq_check:
                self._dispatch()
            limit = target
            if events and events[0][0] < limit:
                limit = events[0][0]
            if self.sleeping:
                if self.pending and self._wake_wanted():
                    self.sleeping = False
                    self.irq_check = True
                    continue
                self.cycles = max(now + cpi, limit)
                continue
            n = (limit - now) // cpi
            if n > BATCH:
                n = BATCH
            elif n < 1:
                n = 1
            pc = self.pc
            if self.trace:
                self.trace(pc)
            try:
                for _ in range(n):
                    pc = ops[pc]()
                self.pc = pc
            except _Break:
                pass
            except IndexError:
                self.fault = "PC ran off the end of flash near 0x%05x" % (pc * 2)
            self.cycles = now + n * cpi
            self.insns += n
        return not self.fault

    # -- debugging helpers --------------------------------------------------
    @property
    def sreg(self):
        S = self.S
        return sum((1 if S[i] else 0) << i for i in range(8))

    @property
    def sp(self):
        return self.S[SP_]

    def read_cstr(self, addr, maxlen=128):
        out = bytearray()
        while len(out) < maxlen and self.D[addr]:
            out.append(self.D[addr])
            addr += 1
        return out.decode("latin-1")

    # ------------------------------------------------------------------
    # instruction decoding
    # ------------------------------------------------------------------
    def _build(self):
        core = self
        R, S, D = self.R, self.S, self.D
        W, FL = self.words, self.flash
        ior, iow = self.ior, self.iow
        nwords = len(W)
        PCMASK = 0x3FFFFF

        def get_sreg():
            return ((1 if S[0] else 0) | (2 if S[1] else 0) | (4 if S[2] else 0) |
                    (8 if S[3] else 0) | (16 if S[4] else 0) | (32 if S[5] else 0) |
                    (64 if S[6] else 0) | (128 if S[7] else 0))

        def set_sreg(v):
            for i in range(8):
                S[i] = (v >> i) & 1
            if v & 0x80:
                core.irq_check = True

        def set_spl(v):
            S[8] = (S[8] & 0xFF00) | v

        def set_sph(v):
            S[8] = (S[8] & 0x00FF) | (v << 8)

        self.io(0x3F, get_sreg, set_sreg)
        self.io(0x3D, lambda: S[8] & 0xFF, set_spl)
        self.io(0x3E, lambda: S[8] >> 8, set_sph)
        self.io(0x3B, lambda: S[9], lambda v: S.__setitem__(9, v))
        self.io(0x3C, lambda: S[10], lambda v: S.__setitem__(10, v))
        self.io_ram(0x00, 16)           # GPIOR0..F
        self.io_ram(0x34)               # CCP
        self.io_ram(0x38, 3)            # RAMPD / RAMPX / RAMPY

        def wr_low(a, v):
            if a >= IO_END:
                if core.on_ee_write:
                    core.on_ee_write(a - IO_END, v)
            else:
                iow[a](v)

        def is2(op):
            return (op & 0xFE0E) == 0x940C or (op & 0xFE0E) == 0x940E or (op & 0xFC0F) == 0x9000

        def bad(pc, op):
            def f():
                core.pc = pc
                core._stop("illegal opcode 0x%04x at 0x%05x" % (op, pc * 2))
            return f

        # ---- generators ------------------------------------------------
        def g_add(d, r, n, carry):
            if carry:
                def f():
                    a = R[d]; b = R[r]; c = 1 if S[0] else 0
                    res = a + b + c
                    S[5] = ((a & 15) + (b & 15) + c) >> 4
                    S[0] = res >> 8
                    res &= 255
                    v = ((a ^ res) & (b ^ res)) >> 7
                    nn = res >> 7
                    S[3] = v; S[2] = nn; S[4] = nn ^ v; S[1] = res == 0
                    R[d] = res
                    return n
            else:
                def f():
                    a = R[d]; b = R[r]
                    res = a + b
                    S[5] = ((a & 15) + (b & 15)) >> 4
                    S[0] = res >> 8
                    res &= 255
                    v = ((a ^ res) & (b ^ res)) >> 7
                    nn = res >> 7
                    S[3] = v; S[2] = nn; S[4] = nn ^ v; S[1] = res == 0
                    R[d] = res
                    return n
            return f

        def g_sub(d, r, n, store, imm=None):
            # sub / cp / subi / cpi
            if imm is None:
                def f():
                    a = R[d]; b = R[r]
                    res = (a - b) & 255
                    S[5] = (a & 15) < (b & 15)
                    S[0] = a < b
                    v = ((a ^ b) & (a ^ res)) >> 7
                    nn = res >> 7
                    S[3] = v; S[2] = nn; S[4] = nn ^ v; S[1] = res == 0
                    if store:
                        R[d] = res
                    return n
            else:
                b = imm

                def f():
                    a = R[d]
                    res = (a - b) & 255
                    S[5] = (a & 15) < (b & 15)
                    S[0] = a < b
                    v = ((a ^ b) & (a ^ res)) >> 7
                    nn = res >> 7
                    S[3] = v; S[2] = nn; S[4] = nn ^ v; S[1] = res == 0
                    if store:
                        R[d] = res
                    return n
            return f

        def g_sbc(d, r, n, store, imm=None):
            # sbc / cpc / sbci
            def f():
                a = R[d]
                b = R[r] if imm is None else imm
                c = 1 if S[0] else 0
                res = a - b - c
                S[5] = (a & 15) - (b & 15) - c < 0
                S[0] = res < 0
                res &= 255
                v = ((a ^ b) & (a ^ res)) >> 7
                nn = res >> 7
                S[3] = v; S[2] = nn; S[4] = nn ^ v
                if res:
                    S[1] = 0
                if store:
                    R[d] = res
                return n
            return f

        def g_logic(d, r, n, kind, imm=None):
            if kind == "and":
                def f():
                    res = R[d] & (R[r] if imm is None else imm)
                    R[d] = res
                    nn = res >> 7
                    S[3] = 0; S[2] = nn; S[4] = nn; S[1] = res == 0
                    return n
            elif kind == "or":
                def f():
                    res = R[d] | (R[r] if imm is None else imm)
                    R[d] = res
                    nn = res >> 7
                    S[3] = 0; S[2] = nn; S[4] = nn; S[1] = res == 0
                    return n
            else:
                def f():
                    res = R[d] ^ R[r]
                    R[d] = res
                    nn = res >> 7
                    S[3] = 0; S[2] = nn; S[4] = nn; S[1] = res == 0
                    return n
            return f

        def g_mov(d, r, n):
            def f():
                R[d] = R[r]
                return n
            return f

        def g_ldi(d, k, n):
            def f():
                R[d] = k
                return n
            return f

        def g_movw(d, r, n):
            def f():
                R[d] = R[r]; R[d + 1] = R[r + 1]
                return n
            return f

        def g_skip_cpse(d, r, n, sk):
            def f():
                return sk if R[d] == R[r] else n
            return f

        def g_sbr(r, b, n, sk, want):
            m = 1 << b
            if want:
                def f():
                    return sk if R[r] & m else n
            else:
                def f():
                    return n if R[r] & m else sk
            return f

        def g_sbi_skip(a, b, n, sk, want):
            m = 1 << b
            def f():
                core.pc = n
                v = ior[a]() & m
                return sk if bool(v) == want else n
            return f

        def g_sbi(a, b, n, setit):
            m = 1 << b

            def f():
                core.pc = n
                v = ior[a]()
                iow[a]((v | m) if setit else (v & ~m & 255))
                return n
            return f

        def g_in(d, a, n):
            def f():
                core.pc = n
                R[d] = ior[a]()
                return n
            return f

        def g_out(r, a, n):
            def f():
                core.pc = n
                iow[a](R[r])
                return n
            return f

        def g_branch(flag, want, n, tgt):
            if want:
                def f():
                    return tgt if S[flag] else n
            else:
                def f():
                    return n if S[flag] else tgt
            return f

        def g_jmp(tgt):
            def f():
                return tgt
            return f

        def g_call(tgt, ret):
            lo, mid, hi = ret & 255, (ret >> 8) & 255, (ret >> 16) & 255

            def f():
                sp = S[8]
                D[sp] = lo; D[sp - 1] = mid; D[sp - 2] = hi
                S[8] = sp - 3
                return tgt
            return f

        def g_ret(reti):
            if reti:
                def f():
                    sp = S[8]
                    S[8] = sp + 3
                    st = core.pmic_status
                    if st & 4:
                        st &= ~4
                    elif st & 2:
                        st &= ~2
                    else:
                        st &= ~1
                    core.pmic_status = st
                    core.irq_check = True
                    core.pc = ((D[sp + 1] << 16) | (D[sp + 2] << 8) | D[sp + 3]) & PCMASK
                    raise _Break()
            else:
                def f():
                    sp = S[8]
                    S[8] = sp + 3
                    return ((D[sp + 1] << 16) | (D[sp + 2] << 8) | D[sp + 3]) & PCMASK
            return f

        def g_ijmp(ext, call, ret):
            lo, mid, hi = ret & 255, (ret >> 8) & 255, (ret >> 16) & 255

            def f():
                if call:
                    sp = S[8]
                    D[sp] = lo; D[sp - 1] = mid; D[sp - 2] = hi
                    S[8] = sp - 3
                t = R[30] | (R[31] << 8)
                if ext:
                    t |= S[10] << 16
                return t & PCMASK
            return f

        def g_push(r, n):
            def f():
                sp = S[8]
                D[sp] = R[r]
                S[8] = sp - 1
                return n
            return f

        def g_pop(d, n):
            def f():
                sp = S[8] + 1
                S[8] = sp
                R[d] = D[sp]
                return n
            return f

        def g_lds(d, k, n):
            if k >= IO_END:
                def f():
                    R[d] = D[k]
                    return n
            else:
                def f():
                    core.pc = n
                    R[d] = ior[k]()
                    return n
            return f

        def g_sts(r, k, n):
            if k >= RAM_START:
                def f():
                    D[k] = R[r]
                    return n
            else:
                def f():
                    core.pc = n
                    wr_low(k, R[r])
                    return n
            return f

        def g_ld(d, lo, mode, q, n):
            # mode: 0 plain / displacement, 1 post-increment, 2 pre-decrement
            hi = lo + 1
            if mode == 0:
                def f():
                    a = ((R[lo] | (R[hi] << 8)) + q) & 0xFFFF
                    if a >= IO_END:
                        R[d] = D[a]
                    else:
                        core.pc = n
                        R[d] = ior[a]()
                    return n
            elif mode == 1:
                def f():
                    a = R[lo] | (R[hi] << 8)
                    b = (a + 1) & 0xFFFF
                    R[lo] = b & 255; R[hi] = b >> 8
                    if a >= IO_END:
                        R[d] = D[a]
                    else:
                        core.pc = n
                        R[d] = ior[a]()
                    return n
            else:
                def f():
                    a = ((R[lo] | (R[hi] << 8)) - 1) & 0xFFFF
                    R[lo] = a & 255; R[hi] = a >> 8
                    if a >= IO_END:
                        R[d] = D[a]
                    else:
                        core.pc = n
                        R[d] = ior[a]()
                    return n
            return f

        def g_st(r, lo, mode, q, n):
            hi = lo + 1
            if mode == 0:
                def f():
                    a = ((R[lo] | (R[hi] << 8)) + q) & 0xFFFF
                    if a >= RAM_START:
                        D[a] = R[r]
                    else:
                        core.pc = n
                        wr_low(a, R[r])
                    return n
            elif mode == 1:
                def f():
                    a = R[lo] | (R[hi] << 8)
                    v = R[r]
                    b = (a + 1) & 0xFFFF
                    R[lo] = b & 255; R[hi] = b >> 8
                    if a >= RAM_START:
                        D[a] = v
                    else:
                        core.pc = n
                        wr_low(a, v)
                    return n
            else:
                def f():
                    v = R[r]
                    a = ((R[lo] | (R[hi] << 8)) - 1) & 0xFFFF
                    R[lo] = a & 255; R[hi] = a >> 8
                    if a >= RAM_START:
                        D[a] = v
                    else:
                        core.pc = n
                        wr_low(a, v)
                    return n
            return f

        flen = len(FL)

        def g_lpm(d, ext, inc, n):
            def f():
                a = R[30] | (R[31] << 8)
                if ext:
                    a |= S[9] << 16
                R[d] = FL[a] if a < flen else 0xFF
                if inc:
                    a += 1
                    R[30] = a & 255; R[31] = (a >> 8) & 255
                    if ext:
                        S[9] = (a >> 16) & 255
                return n
            return f

        def g_unary(d, m, n):
            if m == 0:      # com
                def f():
                    res = 255 - R[d]
                    R[d] = res
                    nn = res >> 7
                    S[0] = 1; S[3] = 0; S[2] = nn; S[4] = nn; S[1] = res == 0
                    return n
            elif m == 1:    # neg
                def f():
                    a = R[d]
                    res = (-a) & 255
                    R[d] = res
                    v = res == 0x80
                    nn = res >> 7
                    S[0] = res != 0; S[3] = v; S[2] = nn; S[4] = nn ^ v; S[1] = res == 0
                    S[5] = ((res | a) >> 3) & 1
                    return n
            elif m == 2:    # swap
                def f():
                    a = R[d]
                    R[d] = ((a << 4) | (a >> 4)) & 255
                    return n
            elif m == 3:    # inc
                def f():
                    a = R[d]
                    res = (a + 1) & 255
                    R[d] = res
                    v = a == 0x7F
                    nn = res >> 7
                    S[3] = v; S[2] = nn; S[4] = nn ^ v; S[1] = res == 0
                    return n
            elif m == 5:    # asr
                def f():
                    a = R[d]
                    res = (a >> 1) | (a & 0x80)
                    R[d] = res
                    c = a & 1
                    nn = res >> 7
                    v = nn ^ c
                    S[0] = c; S[2] = nn; S[3] = v; S[4] = nn ^ v; S[1] = res == 0
                    return n
            elif m == 6:    # lsr
                def f():
                    a = R[d]
                    res = a >> 1
                    R[d] = res
                    c = a & 1
                    S[0] = c; S[2] = 0; S[3] = c; S[4] = c; S[1] = res == 0
                    return n
            elif m == 7:    # ror
                def f():
                    a = R[d]
                    res = (a >> 1) | (0x80 if S[0] else 0)
                    R[d] = res
                    c = a & 1
                    nn = res >> 7
                    v = nn ^ c
                    S[0] = c; S[2] = nn; S[3] = v; S[4] = nn ^ v; S[1] = res == 0
                    return n
            else:           # dec (m == 10)
                def f():
                    a = R[d]
                    res = (a - 1) & 255
                    R[d] = res
                    v = a == 0x80
                    nn = res >> 7
                    S[3] = v; S[2] = nn; S[4] = nn ^ v; S[1] = res == 0
                    return n
            return f

        def g_adiw(d, k, n, sub):
            if sub:
                def f():
                    a = R[d] | (R[d + 1] << 8)
                    res = a - k
                    S[0] = res < 0
                    res &= 0xFFFF
                    v = ((a & ~res) >> 15) & 1
                    nn = res >> 15
                    S[3] = v; S[2] = nn; S[4] = nn ^ v; S[1] = res == 0
                    R[d] = res & 255; R[d + 1] = res >> 8
                    return n
            else:
                def f():
                    a = R[d] | (R[d + 1] << 8)
                    res = a + k
                    S[0] = res >> 16
                    res &= 0xFFFF
                    v = ((~a & res) >> 15) & 1
                    nn = res >> 15
                    S[3] = v; S[2] = nn; S[4] = nn ^ v; S[1] = res == 0
                    R[d] = res & 255; R[d + 1] = res >> 8
                    return n
            return f

        def g_mul(d, r, n, sd, sr, frac):
            def f():
                a = R[d]; b = R[r]
                if sd and a & 0x80:
                    a -= 256
                if sr and b & 0x80:
                    b -= 256
                res = (a * b) & 0xFFFF
                S[0] = res >> 15
                if frac:
                    res = (res << 1) & 0xFFFF
                S[1] = res == 0
                R[0] = res & 255; R[1] = res >> 8
                return n
            return f

        def g_flag(bit, val, n):
            if bit == 7 and val:
                def f():
                    S[7] = 1
                    core.irq_check = True
                    return n
            else:
                def f():
                    S[bit] = val
                    return n
            return f

        def g_bst(r, b, n):
            def f():
                S[6] = (R[r] >> b) & 1
                return n
            return f

        def g_bld(d, b, n):
            m = 1 << b

            def f():
                if S[6]:
                    R[d] |= m
                else:
                    R[d] &= ~m & 255
                return n
            return f

        def g_sleep(n):
            def f():
                core.pc = n
                core.sleeping = True
                raise _Break()
            return f

        def g_nop(n):
            def f():
                return n
            return f

        def g_xch(r, kind, n):
            def f():
                a = R[30] | (R[31] << 8)
                old = D[a] if a >= IO_END else ior[a]()
                v = R[r]
                new = (v, old | v, old & ~v & 255, old ^ v)[kind]
                R[r] = old
                if a >= RAM_START:
                    D[a] = new
                else:
                    core.pc = n
                    wr_low(a, new)
                return n
            return f

        def g_spm(n):
            def f():
                core.pc = n
                core._stop("SPM (flash self-programming) is not emulated")
            return f

        # ---- decoder -----------------------------------------------------
        def decode(pc):
            op = W[pc]
            n = pc + 1
            nxt = W[n] if n < nwords else 0
            sk = n + (2 if is2(nxt) else 1)
            d5 = (op >> 4) & 31
            r5 = (op & 15) | ((op >> 5) & 16)
            d4 = 16 + ((op >> 4) & 15)
            k8 = ((op >> 4) & 0xF0) | (op & 15)
            hi = op >> 12
            if hi == 0:
                s = (op >> 8) & 15
                if op == 0:
                    return g_nop(n)
                if s == 1:
                    return g_movw(((op >> 4) & 15) * 2, (op & 15) * 2, n)
                if s == 2:
                    return g_mul(d4, 16 + (op & 15), n, True, True, False)
                if s == 3:
                    d = 16 + ((op >> 4) & 7); r = 16 + (op & 7)
                    sel = ((op >> 6) & 2) | ((op >> 3) & 1)
                    return g_mul(d, r, n, sel != 1, sel == 2, sel != 0)
                t = (op >> 10) & 3
                if t == 1:
                    return g_sbc(d5, r5, n, False)
                if t == 2:
                    return g_sbc(d5, r5, n, True)
                if t == 3:
                    return g_add(d5, r5, n, False)
                return bad(pc, op)
            if hi == 1:
                t = (op >> 10) & 3
                if t == 0:
                    return g_skip_cpse(d5, r5, n, sk)
                if t == 1:
                    return g_sub(d5, r5, n, False)
                if t == 2:
                    return g_sub(d5, r5, n, True)
                return g_add(d5, r5, n, True)
            if hi == 2:
                t = (op >> 10) & 3
                if t == 3:
                    return g_mov(d5, r5, n)
                return g_logic(d5, r5, n, ("and", "eor", "or")[t])
            if hi == 3:
                return g_sub(d4, 0, n, False, k8)
            if hi == 4:
                return g_sbc(d4, 0, n, True, k8)
            if hi == 5:
                return g_sub(d4, 0, n, True, k8)
            if hi == 6:
                return g_logic(d4, 0, n, "or", k8)
            if hi == 7:
                return g_logic(d4, 0, n, "and", k8)
            if hi == 14:
                return g_ldi(d4, k8, n)
            if hi in (8, 10):
                q = ((op >> 8) & 0x20) | ((op >> 7) & 0x18) | (op & 7)
                lo = 28 if op & 8 else 30
                if op & 0x200:
                    return g_st(d5, lo, 0, q, n)
                return g_ld(d5, lo, 0, q, n)
            if hi == 9:
                if (op & 0xFC00) == 0x9000:
                    st = bool(op & 0x200)
                    m = op & 15
                    if m == 0:
                        return g_sts(d5, nxt, n + 1) if st else g_lds(d5, nxt, n + 1)
                    ptr = {1: (30, 1), 2: (30, 2), 9: (28, 1), 10: (28, 2),
                           12: (26, 0), 13: (26, 1), 14: (26, 2)}.get(m)
                    if ptr:
                        if st:
                            return g_st(d5, ptr[0], ptr[1], 0, n)
                        return g_ld(d5, ptr[0], ptr[1], 0, n)
                    if m == 15:
                        return g_push(d5, n) if st else g_pop(d5, n)
                    if m in (4, 5, 6, 7):
                        if st:
                            return g_xch(d5, m - 4, n)
                        return g_lpm(d5, m >= 6, bool(m & 1), n)
                    return bad(pc, op)
                if (op & 0xFE00) == 0x9400:
                    m = op & 15
                    if m in (0, 1, 2, 3, 5, 6, 7, 10):
                        return g_unary(d5, m, n)
                    if m == 8:
                        if op & 0x100:
                            special = {0x9508: g_ret(False), 0x9518: g_ret(True),
                                       0x9588: g_sleep(n), 0x95A8: g_nop(n),
                                       0x95C8: g_lpm(0, False, False, n),
                                       0x95D8: g_lpm(0, True, False, n),
                                       0x95E8: g_spm(n), 0x95F8: g_spm(n)}
                            return special.get(op) or bad(pc, op)
                        return g_flag((op >> 4) & 7, 0 if op & 0x80 else 1, n)
                    if m == 9:
                        t = {0x9409: (False, False), 0x9419: (True, False),
                             0x9509: (False, True), 0x9519: (True, True)}.get(op)
                        if t:
                            return g_ijmp(t[0], t[1], n)
                        return bad(pc, op)
                    if m in (12, 13, 14, 15):
                        k = ((op & 0x1F0) << 13) | ((op & 1) << 16) | nxt
                        if m >= 14:
                            return g_call(k, n + 1)
                        return g_jmp(k)
                    return bad(pc, op)
                if (op & 0xFE00) == 0x9600:
                    d = 24 + ((op >> 4) & 3) * 2
                    return g_adiw(d, ((op >> 2) & 0x30) | (op & 15), n, bool(op & 0x100))
                if (op & 0xFC00) == 0x9800:
                    a = (op >> 3) & 31
                    b = op & 7
                    t = (op >> 8) & 3
                    if t == 0:
                        return g_sbi(a, b, n, False)
                    if t == 2:
                        return g_sbi(a, b, n, True)
                    return g_sbi_skip(a, b, n, sk, t == 3)
                if (op & 0xFC00) == 0x9C00:
                    return g_mul(d5, r5, n, False, False, False)
                return bad(pc, op)
            if hi == 11:
                a = ((op >> 5) & 0x30) | (op & 15)
                if op & 0x800:
                    return g_out(d5, a, n)
                return g_in(d5, a, n)
            if hi == 12 or hi == 13:
                k = op & 0xFFF
                if k & 0x800:
                    k -= 0x1000
                tgt = (n + k) % nwords
                return g_call(tgt, n) if hi == 13 else g_jmp(tgt)
            # hi == 15
            b = op & 7
            if (op & 0x800) == 0:
                k = (op >> 3) & 0x7F
                if k & 0x40:
                    k -= 0x80
                return g_branch(b, not (op & 0x400), n, n + k)
            if op & 8:
                return bad(pc, op)
            t = (op >> 9) & 3
            if t == 0:
                return g_bld(d5, b, n)
            if t == 1:
                return g_bst(d5, b, n)
            return g_sbr(d5, b, n, sk, t == 3)

        self._ops = [decode(pc) for pc in range(nwords)]
