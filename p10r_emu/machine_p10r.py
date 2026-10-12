"""Original P10R main board: ATxmega256A3 CPU (App_P10R.bin).

Unlike the P10R+ there is no FPGA: the CPU talks to the board directly.

    SPI D   LCD (same glass and controller window as the P10R+);
            CS = PD4, A0 = PA3
    SPI E   PLL synthesiser, 24-bit words, latch enable = PE4;
            dual digital pot (MAX5487 command set), CS = expander P2.1;
            external serial flash (update staging), CS = PC3
    SPI C   RF step attenuator ("rfpad", 1 dB steps), latch = PC4
    TWI C   TCA6424 24-bit I/O expander at 0x22 and TPA6130 headphone
            amplifier at 0x60
    ADC A   PA4 volume pot, PA1 audio level (left/right multiplexed),
            PA6 squelch noise detector, PA7 RSSI
    ADC B   PB1 battery
    DAC B   centre-balance trim
    USART E0  CLI, 57600 baud      USART F0  IR link, 115200 baud
    PORT F  buttons, active low: PF7 PF1 PF4 PF5 PF6
    PE0     power switch (low = on)      PD2  charger detect (low = docked)
    PA5     pilot / tone-key detect from the stereo decoder (low = present)
    PA2     toggles every tick
    PE1 / PD0   antenna A / B RF switches

I/O expander outputs (port.bit):
    0.0 audio mix   0.2 HF boost   0.3 red LED (low = on)   0.4 green LED
    (low = on)   0.5 RF LED (low = on)   0.6 VCO on
    1.0 LCD backlight   1.1-1.4 board-variant straps (inputs)
    1.5 headphone amp enable   1.7 RF mid filter      2.0 RF low filter
    2.1 digital pot chip select

The internal EEPROM holds the settings; a blank one is filled with defaults
by the firmware itself, but stays "not tuned" until `tuned true`.
"""
import os

from .avr import AvrCore
from . import xmega as X
from .devices import St7565
from .rf import RfEnvironment, IF_KHZ
from .board import Machine, SerialFlash

CPU_HZ = 14_000_000         # 2 MHz RC x7 PLL, as the firmware programs it

PORT_A, PORT_B, PORT_C, PORT_D, PORT_E, PORT_F = range(6)
BUTTON_PINS = {"up": 7, "down": 1, "enter": 4, "exit": 5, "scan": 6}

# TCA6424 P1.1..P1.4 straps -> board variant (RF front-end group)
VARIANT_STRAPS = {"A": 0x0E, "B": 0x0D, "C": 0x0C, "D": 0x0B, "E": 0x0A,
                  "F": 0x09, "G": 0x08, "J": 0x07, "K": 0x06}

# band name -> (board variant, band index within that variant), from the
# band descriptors in the image
BANDS = {
    "G10": ("A", 0), "G10E": ("A", 1), "G11": ("A", 2), "G10J": ("A", 3), "G62": ("A", 4),
    "J8": ("B", 0), "J8E": ("B", 1), "J8J": ("B", 2), "J8A": ("B", 3),
    "K10E": ("C", 0),
    "L8": ("D", 0), "L8E": ("D", 1), "L8J": ("D", 2), "L8A": ("D", 3),
    "L9E": ("E", 0), "L11J": ("E", 1),
    "P8": ("F", 0),
    "Q21": ("G", 0), "Q23": ("G", 1), "Q22E": ("G", 2), "R26": ("G", 3), "R27": ("G", 4),
    "A24": ("G", 5),
    "X1": ("J", 0), "X2": ("J", 1), "X7": ("J", 2),
    "H8Z": ("K", 0),
}

ADC_FULL = 2047.0           # the firmware runs the ADCs in signed mode
BATTERY_VREF = 3.3          # battery sense assumed direct, 3.3 V reference
RSSI_AT_M90 = 634           # counts at -90 dBm (default first meter segment)
RSSI_PER_DB = 12.0


class Tca6424:
    """24-bit I2C I/O expander."""

    def __init__(self):
        self.inp = [0xFF, 0xFF, 0xFF]       # external levels on input pins
        self.on_output = None
        self.reset()

    def reset(self):
        self.regs = bytearray(16)
        self.regs[4:7] = b"\xff\xff\xff"
        self.regs[0x0C:0x0F] = b"\xff\xff\xff"
        self.ptr = 0
        self.ai = False

    def pins(self, port):
        cfg = self.regs[0x0C + port]
        return (self.inp[port] & cfg) | (self.regs[4 + port] & ~cfg & 0xFF)

    def out(self, port, bit):
        """Level of an output pin (pins still configured as inputs read 1)."""
        return (self.pins(port) >> bit) & 1

    def start(self, read):
        self._first = not read

    def write(self, b):
        if self._first:
            self.ptr = b & 0x0F
            self.ai = bool(b & 0x80)
            self._first = False
            return True
        self.regs[self.ptr] = b
        self._step()
        if self.on_output:
            self.on_output()
        return True

    def read(self):
        p = self.ptr
        v = self.pins(p) if p < 3 else self.regs[p]
        self._step()
        return v

    def _step(self):
        if self.ai:
            self.ptr = (self.ptr & 0x0C) | ((self.ptr + 1) & 3)

    def stop(self):
        pass


class Tpa6130:
    """Headphone amplifier: register 1 control, register 2 mute + volume."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.regs = bytearray(8)
        self.regs[2] = 0xC0
        self.ptr = 0

    @property
    def volume(self):
        return self.regs[2] & 0x3F

    @property
    def muted(self):
        return bool(self.regs[2] & 0xC0) or not self.regs[1] & 0xC0

    def start(self, read):
        self._first = not read

    def write(self, b):
        if self._first:
            self.ptr = b & 7
            self._first = False
        else:
            self.regs[self.ptr] = b
            self.ptr = (self.ptr + 1) & 7
        return True

    def read(self):
        v = self.regs[self.ptr]
        self.ptr = (self.ptr + 1) & 7
        return v

    def stop(self):
        pass


class I2cBus:
    def __init__(self):
        self.devices = {}
        self.cur = None
        self.nacks = {}

    def start(self, addr, read):
        self.cur = self.devices.get(addr)
        if self.cur is None:
            self.nacks[addr] = self.nacks.get(addr, 0) + 1
            return False
        self.cur.start(read)
        return True

    def write(self, b):
        return self.cur.write(b) if self.cur else False

    def read(self, ack):
        return self.cur.read() if self.cur else 0xFF

    def stop(self):
        if self.cur:
            self.cur.stop()
        self.cur = None


class NSynth:
    """PLL synthesiser on SPI E.  Words with both control bits set carry
    an 18-bit N counter; the comparison frequency is the 25 kHz channel
    raster, so LO = N * 25 kHz.  The other latches are kept but not used."""
    STEP_KHZ = 25

    def __init__(self):
        self.n = 23239              # 470.375 MHz + IF
        self.latches = {}
        self.writes = 0

    def latch(self, data):
        if len(data) != 3:
            return
        w = int.from_bytes(data, "big")
        self.writes += 1
        self.latches[w & 3] = w
        if w & 3 == 3:
            n = (w >> 2) & 0x3FFFF
            if n:
                self.n = n

    @property
    def lo_khz(self):
        return self.n * self.STEP_KHZ

    @property
    def rf_khz(self):
        return self.lo_khz - IF_KHZ


class InternalEeprom:
    """The XMEGA's own 4 KiB EEPROM, optionally file-backed."""

    def __init__(self, path=None, size=4096):
        self.path = path
        self.data = bytearray(b"\xFF" * size)
        if path and os.path.exists(path):
            raw = open(path, "rb").read()[:size]
            self.data[:len(raw)] = raw
        self._dirty = False

    def touched(self):
        self._dirty = True

    def save(self):
        if self.path and self._dirty:
            with open(self.path, "wb") as f:
                f.write(self.data)
            self._dirty = False


class P10rBoard:
    def __init__(self, state_dir=None, tag="p10r", variant="A"):
        self.variant = variant
        self.lcd = St7565(two_byte=(0x81,))
        self.expander = Tca6424()
        self.expander.inp[1] = 0xE1 | (VARIANT_STRAPS[variant] << 1)
        self.hpa = Tpa6130()
        self.i2c = I2cBus()
        self.i2c.devices[0x22] = self.expander
        self.i2c.devices[0x60] = self.hpa
        self.synth = NSynth()
        self.rf = RfEnvironment(self.synth)
        path = None
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)
            path = os.path.join(state_dir, "eeprom_%s.bin" % tag)
        self.eeprom = InternalEeprom(path)
        self.battery_mv = 2900.0
        self.volume = 0x80              # volume pot, 0..255
        self.power = True               # power switch
        self.charger = False            # sitting in a charging dock
        self.flash = SerialFlash()
        self.rf_pad_db = 0              # RF step attenuator setting
        self.digipot = [0, 0]           # digital pot wipers A, B

    def reset(self):
        self.lcd.reset()
        self.expander.reset()
        self.hpa.reset()


class P10rMachine(Machine):
    name = "P10R (XMEGA CPU)"
    image_name = "App_P10R.bin"
    tag = "p10r"
    button_names = ("up", "down", "enter", "exit", "scan")
    has_phones = False

    def __init__(self, image_path, state_dir=None, variant="A"):
        img = open(image_path, "rb").read()
        self.board = b = P10rBoard(state_dir, self.tag, variant)
        self.cpu = cpu = AvrCore(img, cpu_hz=CPU_HZ, cycles_per_insn=4)
        self.console_out = bytearray()
        self.link_out = bytearray()
        self.buttons = 0                # PORTF pins currently held low
        self._spi_e = bytearray()
        self._spi_c = bytearray()
        self._pot = bytearray()
        self._pot_cs = 1
        self._audio_right = False
        self._sample = None
        self._sample_at = -1

        X.Clock(cpu)
        self.ports = [X.Port(cpu, 0x600 + 0x20 * i, n, self._on_port)
                      for i, n in enumerate("ABCDEF")]
        self.port_r = X.Port(cpu, 0x7E0, "R")
        self.vports = X.VPorts(cpu, self.ports)
        self.ports[PORT_A].read_in = lambda: 0xDF if self._rf()["pilot"] else 0xFF
        self.ports[PORT_D].read_in = lambda: 0xFB if b.charger else 0xFF
        self.ports[PORT_E].read_in = lambda: 0xFE if b.power else 0xFF
        self.ports[PORT_F].read_in = lambda: 0xFF & ~self.buttons
        self.spi_c = X.Spi(cpu, 0x8C0, "spic", self._spi_c_xfer)
        self.spi_d = X.Spi(cpu, 0x9C0, "spid", self._lcd_xfer)
        self.spi_e = X.Spi(cpu, 0xAC0, "spie", self._spi_e_xfer)
        self.cli = X.Usart(cpu, 0xAA0, "usarte0", X.VEC_USARTE0_RXC, self.console_out.append)
        self.cli.byte_cycles = CPU_HZ * 10 // 57600
        self.link = X.Usart(cpu, 0xBA0, "usartf0", X.VEC_USARTF0_RXC, self._link_tx)
        self.link.byte_cycles = CPU_HZ * 10 // 115200
        self.twi = X.Twi(cpu, 0x480, "twic", b.i2c)
        self.tick = X.Tc0(cpu, 0x800, "tcc0", X.VEC_TCC0_OVF)
        self.tcd0 = X.Tc0(cpu, 0x900, "tcd0")
        self.adc_a = X.Adc(cpu, 0x200, "adca", self._adc_a)
        self.adc_b = X.Adc(cpu, 0x240, "adcb", self._adc_b)
        self.dac = X.Dac(cpu, 0x320, "dacb")
        self.nvm = X.Nvm(cpu, b.eeprom.data, b.eeprom.touched)
        b.expander.on_output = self._on_expander
        self._periphs = [self.vports, self.spi_c, self.spi_d, self.spi_e, self.cli,
                         self.link, self.twi, self.tick, self.tcd0, self.adc_a,
                         self.adc_b, self.nvm, self.port_r] + self.ports
        self.reset()

    # -- board wiring ----------------------------------------------------
    def _link_tx(self, byte):
        self.link_out.append(byte)
        del self.link_out[:-4096]

    def _on_port(self, port, old, new):
        changed = old ^ new
        if port.name == "E" and changed & 0x10 and new & 0x10:     # synth latch enable
            if self._spi_e:
                self.board.synth.latch(bytes(self._spi_e))
            self._spi_e = bytearray()
        elif port.name == "C":
            if changed & 0x10 and new & 0x10:           # attenuator latch
                if self._spi_c:
                    self.board.rf_pad_db = (self._spi_c[-1] >> 1) & 31
                self._spi_c = bytearray()
            if changed & 0x08:                          # serial flash chip select
                if new & 0x08:
                    self.board.flash.deselect()
                else:
                    self.board.flash.select()

    def _on_expander(self):
        cs = self.board.expander.out(2, 1)
        if cs and not self._pot_cs and len(self._pot) >= 2:
            cmd, val = self._pot[0], self._pot[1]
            if cmd in (0x01, 0x02):                     # write wiper A / B
                self.board.digipot[cmd - 1] = val
        if cs != self._pot_cs:
            self._pot = bytearray()
        self._pot_cs = cs

    def _lcd_xfer(self, byte):
        if self.ports[PORT_D].pins_driven() & 0x10:
            return 0xFF
        lcd = self.board.lcd
        lcd.a0 = (self.ports[PORT_A].pins_driven() >> 3) & 1
        return lcd.xfer(byte)

    def _spi_e_xfer(self, byte):
        if not self.ports[PORT_E].pins_driven() & 0x10:     # synth latch enable low
            self._spi_e.append(byte)
            del self._spi_e[:-8]
            return 0
        if not self._pot_cs:
            self._pot.append(byte)
            return 0
        if not self.ports[PORT_C].pins_driven() & 0x08:
            return self.board.flash.xfer(byte)
        return 0xFF

    def _spi_c_xfer(self, byte):
        self._spi_c.append(byte)
        del self._spi_c[:-8]
        return 0

    # -- analogue inputs ---------------------------------------------------
    def _rf(self):
        # one RF sample per millisecond is plenty and keeps the channels coherent
        now = self.cpu.cycles // (CPU_HZ // 1000)
        if now != self._sample_at:
            self._sample_at = now
            self._sample = self.board.rf.sample()
        return self._sample

    def _adc_a(self, adc, ch, mux, ctrl):
        pin = (mux >> 3) & 7
        b = self.board
        if pin == 4:                                    # volume pot
            return b.volume / 255.0
        s = self._rf()
        if pin == 7:                                    # RSSI, after the step attenuator
            dbm = s["rssi"] - b.rf_pad_db
            return (RSSI_AT_M90 + (dbm + 90.0) * RSSI_PER_DB) / ADC_FULL
        carrier = s["tx"] is not None and s["rssi"] >= b.rf.squelch_dbm
        if pin == 6:                                    # noise above the audio band
            return (60 if carrier else 900) / ADC_FULL
        if pin == 1:                                    # audio level detector
            lvl = s["right"] if self._audio_right else s["left"]
            self._audio_right = not self._audio_right
            return (900 + 900 * lvl) / ADC_FULL if lvl > 0 else 0.05
        return 0.5

    def _adc_b(self, adc, ch, mux, ctrl):
        if (mux >> 3) & 7 == 1:
            return self.board.battery_mv / 1000.0 / BATTERY_VREF
        return 0.5

    # -- host API ----------------------------------------------------------
    def button(self, name, down):
        bit = 1 << BUTTON_PINS[name]
        self.buttons = (self.buttons | bit) if down else (self.buttons & ~bit)

    def leds(self):
        e = self.board.expander
        return (0.0 if e.out(0, 3) else 1.0, 0.0 if e.out(0, 4) else 1.0,
                0.0 if e.out(0, 5) else 1.0,
                1.0 if e.out(1, 0) and not e.regs[0x0D] & 1 else 0.0)

    def antenna(self):
        a = (self.ports[PORT_E].pins_driven() >> 1) & 1
        b = self.ports[PORT_D].pins_driven() & 1
        return "A" if a and not b else "B" if b and not a else "A+B" if a else "off"

    def get_volume(self):
        return self.board.volume

    def set_volume(self, v):
        self.board.volume = max(0, min(255, int(v)))

    def get_phones(self):
        return True

    def set_phones(self, plugged):
        pass                            # earphone detection is not modelled

    def set_power(self, on):
        self.board.power = bool(on)

    def run_ms(self, ms):
        ok = super().run_ms(ms)
        self.board.eeprom.save()        # no-op unless the firmware wrote to it
        return ok

    @staticmethod
    def resolve_band(band):
        """-> (board variant, argument for provision())"""
        variant, index = BANDS.get(band.upper(), ("A", 0))
        return variant, index

    def is_blank(self):
        return all(x == 0xFF for x in self.board.eeprom.data[0x100:0x200])

    def provision(self, band=None):
        """A blank EEPROM is filled with defaults by the firmware; all that
        is left of factory setup is the "tuned" flag (and the band index)."""
        self.run_ms(6000)
        out = self.command("tuned true", 600)
        if band not in (None, "", 0):
            out += self.command("band %s" % band, 600)
        self.board.eeprom.save()
        self.reset()
        return "true" in out

    def reset(self):
        for p in self._periphs:
            p.reset()
        self.board.reset()
        self._spi_e = bytearray()
        self._spi_c = bytearray()
        self._pot = bytearray()
        self._pot_cs = 1
        self.cpu.reset()
