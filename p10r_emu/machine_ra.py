"""P10R+ main board, Renesas RA6M CPU variant (App_P10RP_RA.bin)."""
import os

from .cortexm import CortexM, RegBank
from .renesas import System, Icu, Dtc, Ioport, Sci, Adc12, Flash, FlashWindow
from .board import Board, Machine

APP_BASE = 0x18000          # below this sits the bootloader, which we skip

# Simple-SPI (SCI0) chip selects, active low
CS_FPGA = (4, 14)
CS_EEPROM = (4, 15)
CS_FLASH = (6, 2)
FPGA_IRQ_PIN = (1, 5)       # P105 = IRQ0

# ICU event numbers (RA6M group)
EV_SCI0, EV_SCI1, EV_SCI2 = 0x174, 0x17A, 0x180
EV_ADC0_SCAN_END, EV_ADC1_SCAN_END = 0x04B, 0x051

ADC_VREF = 3.3
ADC1_CH_BATTERY = 0
BATTERY_FULL_SCALE_MV = 4200.0      # firmware: mV = 4200 * code / 4096
TEMP_CAL127 = 2058          # factory temperature-sensor code at 127 C
TEMP_25C = 1829             # sensor code the firmware converts to 25 C


class SpiBus:
    """Routes simple-SPI bytes to whichever slave has its chip select low."""

    def __init__(self):
        self.slaves = {}
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

    def xfer(self, byte):
        if self.active is None:
            return 0xFF
        return self.active.xfer(byte)


class RaMachine(Machine):
    name = "P10R+ (Renesas RA CPU)"
    image_name = "App_P10RP_RA.bin"
    tag = "ra"

    def __init__(self, image_path, state_dir=None, variant="A"):
        img = open(image_path, "rb").read()
        self.board = b = Board(state_dir, self.tag, variant)
        # ICLK: 16 MHz crystal -> PLL 192 MHz -> /8 (SysTick reload 120000 @ 200 Hz)
        self.cpu = cpu = CortexM(cpu_hz=24_000_000, cycles_per_insn=4)
        cpu.map_ram(0, 0x100000, b"\xFF" * 0x100000)
        cpu.uc.mem_write(APP_BASE, img)
        cpu.hook_unmask_sites(APP_BASE, img)
        cpu.map_ram(0x01000000, 0x10000, b"\xFF" * 0x10000)     # option-setting memory
        cpu.map_ram(0x1FFE0000, 0x60000)
        cpu.map_mmio(0x40000000, 0x100000)
        cpu.map_mmio(0x40100000, 0x10000)
        cpu.map_mmio(0x407E0000, 0x20000)

        cpu.attach(0x4001E000, 0x1000, System())
        self.icu = icu = cpu.attach(0x40006000, 0x1000, Icu())
        self.dtc = cpu.attach(0x40005400, 0x100, Dtc(icu))
        # Module-stop bits must read back.  Blocks we do not model at all
        # (stack monitor, SRAM protection, flash cache, ...) read as zero,
        # which is also what their "busy" flags want.
        cpu.attach(0x40047000, 0x1000, RegBank("mstp"))
        cpu.attach(0x407FB000, 0x1000, RegBank("flash-id", {0x17C: TEMP_CAL127}))
        self.gpio_log = None
        self.spi = SpiBus()
        self.io = cpu.attach(0x40040000, 0x1000, Ioport(icu, self._on_gpio,
                                                        {FPGA_IRQ_PIN: 0}))
        self.console_out = bytearray()
        self.link_out = bytearray()
        cpu.attach(0x40070000, 0x20, Sci("sci0", icu, EV_SCI0, spi_bus=self.spi))
        self.link = cpu.attach(0x40070020, 0x20, Sci("sci1", icu, EV_SCI1, on_tx=self._link_tx))
        self.cli = cpu.attach(0x40070040, 0x20, Sci("sci2", icu, EV_SCI2,
                                                    on_tx=self.console_out.append))
        self.adc0 = cpu.attach(0x4005C000, 0x200, Adc12("adc0", icu, EV_ADC0_SCAN_END,
                                                        lambda ch: self._adc_sample(0, ch)))
        self.adc1 = cpu.attach(0x4005C200, 0x200, Adc12("adc1", icu, EV_ADC1_SCAN_END,
                                                        lambda ch: self._adc_sample(1, ch)))
        df_path = os.path.join(state_dir, "dataflash_ra.bin") if state_dir else None
        self.iflash = cpu.attach(0x407FE000, 0x1000, Flash(icu, df_path))
        cpu.attach(0x407E0000, 0x1000, FlashWindow(self.iflash, "cmd"))
        cpu.attach(0x40100000, 0x10000, FlashWindow(self.iflash, "data"))

        self.spi.add(CS_FPGA, b.fpga)
        self.spi.add(CS_EEPROM, b.eeprom)
        self.spi.add(CS_FLASH, b.flash)
        b.fpga.on_irq_line = lambda lvl: self.io.set_input(*FPGA_IRQ_PIN, lvl)
        self.io.inp[FPGA_IRQ_PIN[0]] &= ~(1 << FPGA_IRQ_PIN[1])
        self.adc_inputs = {}
        self.reset()

    def _link_tx(self, byte):
        self.link_out.append(byte)
        del self.link_out[:-4096]

    def _adc_sample(self, unit, ch):
        v = self.adc_inputs.get((unit, ch))
        if v is not None:
            return v
        if unit == 1 and ch == ADC1_CH_BATTERY:
            return self.board.battery_mv / BATTERY_FULL_SCALE_MV
        if ch == "temp":
            return TEMP_25C / 4095.0
        if ch == "vref":
            return 1.18 / ADC_VREF
        return 0.5

    def _on_gpio(self, port, pin, level):
        handled = self.spi.on_gpio(port, pin, level)
        if not handled and self.gpio_log is not None:
            self.gpio_log.append((self.cpu.cycles, port, pin, level))

    def reset(self):
        self.spi.reset()
        self.board.reset()
        self.cpu.reset(APP_BASE)
