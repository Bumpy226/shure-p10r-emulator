"""P10R+ main board, Kinetis K-series CPU variant (App_P10RP.bin)."""
from .cortexm import CortexM, BitBand, RegBank
from .kinetis import Uart, Mcg, Dspi, Gpio, PortCtl, Adc16
from .board import Board, Machine

APP_BASE = 0x18000          # below this sits the bootloader, which we skip
PORT_C, PORT_D, PORT_E = 2, 3, 4

# SPI0 chip selects (active-low GPIOs) and the FPGA interrupt pin
CS_FPGA = (PORT_C, 4)
CS_EEPROM = (PORT_D, 5)
CS_FLASH = (PORT_D, 4)
FPGA_IRQ_PIN = (PORT_E, 18)
RESET_OUT_PIN = (PORT_E, 3)     # firmware "reboot": pulses the board reset circuit

ADC_CH_BATTERY, ADC_CH_TEMP, ADC_CH_BANDGAP = 5, 26, 27
ADC_VREF = 3.3
BANDGAP_V = 1.0
BATTERY_DIVIDER = 2.333       # firmware: mV = 2333 * adc(batt) / adc(bandgap)


class SpiBus:
    """Routes DSPI frames to whichever slave has its chip select low."""

    def __init__(self):
        self.slaves = {}            # (port, pin) -> SpiSlave
        self.active = None

    def add(self, cs, slave):
        self.slaves[cs] = slave

    def reset(self):
        self.active = None

    def on_gpio(self, port, pin, level):
        s = self.slaves.get((port, pin))
        if s is None:
            return False
        if level:
            if self.active is s:
                s.deselect()
                self.active = None
        else:
            if self.active is not None and self.active is not s:
                self.active.deselect()
            self.active = s
            s.select()
        return True

    def transfer(self, pcs, data, bits, cont):
        if self.active is None:
            return 0
        if bits <= 8:
            return self.active.xfer(data & 0xFF)
        hi = self.active.xfer((data >> 8) & 0xFF)
        return (hi << 8) | self.active.xfer(data & 0xFF)


class KinetisMachine(Machine):
    name = "P10R+ (Kinetis CPU)"
    image_name = "App_P10RP.bin"
    tag = "kinetis"

    def __init__(self, image_path, state_dir=None, variant="A"):
        img = open(image_path, "rb").read()
        self.board = Board(state_dir, self.tag, variant)
        self.cpu = cpu = CortexM(cpu_hz=32_000_000, cycles_per_insn=4)
        cpu.map_ram(0, 0x80000, b"\xFF" * 0x80000)
        cpu.uc.mem_write(APP_BASE, img)
        cpu.hook_unmask_sites(APP_BASE, img)
        cpu.map_ram(0x1FFF0000, 0x20000)
        cpu.map_mmio(0x40000000, 0x100000)
        cpu.map_mmio(0x42000000, 0x2000000)
        cpu.attach(0x42000000, 0x2000000, BitBand(0x40000000))

        self.console_out = bytearray()      # CLI UART, firmware -> host
        self.link_out = bytearray()         # framed serial link, firmware -> host
        self.cli = cpu.attach(0x4006B000, 0x1000, Uart("uart1", 33, self.console_out.append))
        self.link = cpu.attach(0x4006A000, 0x1000, Uart("uart0", 31, self._link_tx))
        # The (absent) bootloader leaves the clock tree on the 32 MHz
        # external reference with all dividers at /1.
        cpu.attach(0x40064000, 0x1000, Mcg(c1=0x80, c2=0x26))
        cpu.attach(0x40047000, 0x2000, RegBank("sim"))
        cpu.attach(0x4007D000, 0x1000, RegBank("pmc"))

        self.gpio = cpu.attach(0x400FF000, 0x1000, Gpio(59, self._on_gpio))
        for i in range(5):
            cpu.attach(0x40049000 + 0x1000 * i, 0x1000, PortCtl(self.gpio, i))
        self.gpio_log = None

        self.spi = SpiBus()
        cpu.attach(0x4002C000, 0x1000, Dspi("spi0", 26, self.spi))
        self.adc_seen = {}
        cpu.attach(0x4003B000, 0x1000, Adc16("adc0", 39, self._adc_sample))

        b = self.board
        self.spi.add(CS_FPGA, b.fpga)
        self.spi.add(CS_EEPROM, b.eeprom)
        self.spi.add(CS_FLASH, b.flash)
        b.fpga.on_irq_line = lambda lvl: self.gpio.set_input(*FPGA_IRQ_PIN, lvl)
        self.gpio.ports[FPGA_IRQ_PIN[0]].inp &= ~(1 << FPGA_IRQ_PIN[1])
        self.reset()

    def _link_tx(self, byte):
        self.link_out.append(byte)
        del self.link_out[:-4096]

    def _adc_sample(self, ch, diff):
        self.adc_seen[ch] = self.adc_seen.get(ch, 0) + 1
        if ch == ADC_CH_BATTERY:
            return self.board.battery_mv / 1000.0 / BATTERY_DIVIDER / ADC_VREF
        if ch == ADC_CH_BANDGAP:
            return BANDGAP_V / ADC_VREF
        if ch == ADC_CH_TEMP:
            return 0.716 / ADC_VREF     # 25 C on the Kinetis temperature sensor
        return 0.5

    def _on_gpio(self, port, pin, level):
        handled = self.spi.on_gpio(port, pin, level)
        if (port, pin) == RESET_OUT_PIN and level:
            self.cpu._stop("reset")
        if not handled and self.gpio_log is not None:
            self.gpio_log.append((self.cpu.cycles, port, pin, level))

    def reset(self):
        self.spi.reset()
        self.board.reset()
        self.cpu.reset(APP_BASE)
