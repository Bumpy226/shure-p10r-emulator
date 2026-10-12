"""Everything on the P10R+ main board that is not the CPU, plus the
host-facing API shared by both CPU variants."""
import os

from .devices import St7565, M95xxx, LatchSink, SpiSlave
from .fpga import Fpga, BUTTONS, VARIANT_STRAPS
from .rf import Synth, RfEnvironment

# Visible part of the 132x64 controller RAM
LCD_X0, LCD_W, LCD_H = 52, 80, 40

# band -> board variant that carries its RF front end (from `freqtable summary`)
BAND_VARIANT = {
    "G10": "A", "G10E": "A", "G10J": "A", "G11": "A", "G53": "A", "G6": "A",
    "G62": "A", "G6E": "A", "G6J": "A",
    "G14": "B", "G14J": "B", "G7": "B", "G7E": "B", "G7Z": "B", "J8": "B",
    "J8A": "B", "J8E": "B", "J8J": "B",
    "K1": "C", "K10E": "C", "K1E": "C", "K1J": "C",
    "L6": "D", "L60": "D", "L6E": "D", "L6J": "D", "L8": "D", "L8E": "D", "L8J": "D",
    "L11J": "E", "L9E": "E", "M19": "E", "P7": "E",
    "P8": "F", "Q12": "F", "Q15": "F", "Q20": "F",
    "Q21": "G", "Q22E": "G", "R20": "G", "R21": "G", "R22": "G", "R26": "G", "R27": "G",
    "X1": "J", "X2": "J", "X7": "J",
    "H8Z": "K",
}


class SerialFlash(SpiSlave):
    """SPI NOR flash holding the firmware-update staging area.  Reads as
    erased; enough for the firmware's boot-time manifest check."""

    def __init__(self, size=4 << 20):
        self.size = size
        self.data = {}
        self.wel = False

    def select(self):
        self._n = 0
        self._cmd = None
        self._addr = 0

    def xfer(self, b):
        n = self._n
        self._n += 1
        if n == 0:
            self._cmd = b
            if b == 0x06:
                self.wel = True
            elif b == 0x04:
                self.wel = False
            return 0xFF
        c = self._cmd
        if c == 0x05:
            return 0x02 if self.wel else 0x00
        if c == 0x9F:
            return (0x01, 0x40, 0x15)[(n - 1) % 3]
        if c in (0x03, 0x02, 0x20, 0xD8):
            if n <= 3:
                self._addr = (self._addr << 8) | b
                if n == 3 and c in (0x20, 0xD8) and self.wel:
                    span = 0x1000 if c == 0x20 else 0x10000
                    base = self._addr & ~(span - 1)
                    for a in [a for a in self.data if base <= a < base + span]:
                        del self.data[a]
                return 0xFF
            a = self._addr % self.size
            self._addr += 1
            if c == 0x03:
                return self.data.get(a, 0xFF)
            if c == 0x02 and self.wel:
                self.data[a] = self.data.get(a, 0xFF) & b
        return 0xFF

    def deselect(self):
        if self._cmd in (0x02, 0x20, 0xD8, 0xC7):
            self.wel = False


class Board:
    def __init__(self, state_dir=None, tag="", variant="A"):
        self.lcd = St7565()
        self.fpga = Fpga(self.lcd)
        self.fpga.strap = VARIANT_STRAPS[variant]
        self.variant = variant
        self.synth = Synth()
        self.rf = RfEnvironment(self.synth)
        self.fpga.rf = self.rf.sample
        self.fpga.ports[4] = LatchSink("synth", self.synth.latch)
        self.fpga.ports[2] = LatchSink("rfadc")
        path = None
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)
            path = os.path.join(state_dir, "eeprom_%s.bin" % tag)
        self.eeprom = M95xxx(1024, path)
        self.flash = SerialFlash()
        self.battery_mv = 2900.0            # 2 x AA

    def reset(self):
        self.fpga.reset()
        self.lcd.reset()


class Machine:
    """Host-side API common to all machines.  Subclasses provide
    `self.cpu`, `self.board`, `self.cli` (UART model) and `reset()`."""
    name = "P10R+"

    # -- running ------------------------------------------------------
    def run_ms(self, ms):
        cpu = self.cpu
        ok = cpu.run(int(cpu.cpu_hz * ms / 1000))
        if cpu.reset_requested:
            self.reset()
            return True
        return ok

    @property
    def fault(self):
        return self.cpu.fault

    @property
    def time_ms(self):
        return self.cpu.cycles * 1000.0 / self.cpu.cpu_hz

    # -- serial console ----------------------------------------------
    def type(self, text):
        self.cli.feed(text.encode("latin-1"))

    def take_console(self):
        out = bytes(self.console_out)
        del self.console_out[:]
        return out.decode("latin-1")

    def command(self, line, ms=400):
        """Run one CLI command and return what it printed."""
        self.take_console()
        self.type(line + "\r")
        self.run_ms(ms)
        return self.take_console()

    # -- front panel ----------------------------------------------------
    button_names = ("up", "down", "enter", "exit", "scan")
    has_phones = True           # earphone-detect input is modelled

    def button(self, name, down):
        self.board.fpga.set_button(BUTTONS[name], down)

    def leds(self):
        """Brightness 0..1 of (red, green, rf, backlight)."""
        return self.board.fpga.leds()

    def antenna(self):
        return self.board.fpga.antenna()

    def get_volume(self):
        return self.board.fpga.volume

    def set_volume(self, v):
        self.board.fpga.volume = max(0, min(255, int(v)))

    def get_phones(self):
        return self.board.fpga.phones

    def set_phones(self, plugged):
        self.board.fpga.set_phones(plugged)

    def save(self):
        self.board.eeprom.save()

    def click(self, name, hold_ms=120, settle_ms=150):
        self.button(name, True)
        self.run_ms(hold_ms)
        self.button(name, False)
        self.run_ms(settle_ms)

    def lcd_rows(self):
        """Visible LCD as a list of LCD_H strings of '#' and ' '."""
        lcd = self.board.lcd
        inv = lcd.reverse
        rows = []
        for y in range(LCD_H):
            page, bit = lcd.ram[y >> 3], 1 << (y & 7)
            rows.append("".join(
                "#" if (bool(page[LCD_X0 + x] & bit) != inv) and lcd.on else " "
                for x in range(LCD_W)))
        return rows

    def lcd_text(self):
        """Half-height block rendering of the LCD for terminals."""
        rows = self.lcd_rows()
        out = []
        for y in range(0, LCD_H, 2):
            out.append("".join(" ▀▄█"[(a == "#") | ((b == "#") << 1)]
                               for a, b in zip(rows[y], rows[y + 1])))
        return "\n".join(out)

    # -- first-boot provisioning ------------------------------------------
    @staticmethod
    def resolve_band(band):
        """-> (board variant, argument for provision())"""
        return BAND_VARIANT.get(band.upper(), "A"), band.upper()

    def is_blank(self):
        return all(b == 0xFF for b in self.board.eeprom.data)

    def provision(self, band):
        """Program a factory-fresh EEPROM with a frequency band, the way the
        production line does it over the CLI, then reboot."""
        self.run_ms(3000)
        out = self.command("setBand %s" % band, 1500)
        self.reset()
        return "Successful" in out
