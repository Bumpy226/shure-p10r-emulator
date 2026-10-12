# P10R+ firmware emulator

Runs the unmodified P10R+ application images from this package
(`App_P10RP.bin` and `App_P10RP_RA.bin`) on an emulated main board, so the
real menu code, CLI and NV-data handling can be exercised without hardware.

It executes the binaries instruction-for-instruction (Unicorn Cortex-M4 core)
and models the hardware around the CPU:

| Piece | How it is emulated |
|---|---|
| CPU core, NVIC, SysTick, exceptions | `cortexm.py` |
| Kinetis K-series peripherals (`App_P10RP.bin`) | `kinetis.py`, `machine_kinetis.py` |
| Renesas RA6M peripherals (`App_P10RP_RA.bin`) | `renesas.py`, `machine_ra.py` |
| Spartan-6 FPGA | behavioural register model, `fpga.py` (register map in the file header) |
| LCD (ST7565-type, 80x40 visible) | `devices.py`, drawn in the window |
| M95080 EEPROM | file-backed, `state/eeprom_<cpu>.bin` |
| ADF4111 synthesiser | latches decoded back into the tuned frequency, `rf.py` |
| RF / audio | mock transmitters, `rf.py` |

## Running

```
pip install unicorn
cd emulator
python -m p10r_emu                 # Kinetis build, window
python -m p10r_emu --cpu ra        # Renesas build
python -m p10r_emu --band L8       # provision a blank EEPROM with another band
python -m p10r_emu --tx 474.325,-50 --tx 480.0,-70
```

`run_emulator.bat` does the same from Explorer.

On first start the EEPROM is blank, so the emulator provisions it the way
production does: it types `setBand <band>` into the firmware CLI and reboots.
The board-variant straps are chosen to match the band. Delete the files in
`state/` to go back to a factory-fresh unit.

### Front panel

Click the display, then:

| Key | Button |
|---|---|
| Up / Down | up / down |
| Enter | enter |
| Esc (or Backspace) | exit |
| S | scan |

Holding a key holds the button. The on-screen buttons do the same with the
mouse. The three dots are the red, green and RF LEDs; the display tint follows
the backlight PWM.

### Mock RF

Each entry in the list is a transmitter: frequency, level at the antenna, left
/ right audio level and whether it carries a valid pilot. The receiver sees
whatever is on the frequency it is tuned to, with a filter skirt for
neighbours, so RSSI bars, audio meters, squelch, and the firmware's own
channel / group / full scans all respond. Tick *follow tuned freq* for a
transmitter that is always on-channel.

Battery voltage, the volume pot and the earphone-detect line (unplugging it
puts the unit in power-save) are under *Unit*.

### Serial console

The pane on the right is the firmware's own CLI. `help all` lists it; useful
ones: `systeminfo`, `td`, `bandinfo`, `freq`, `group`, `channel`, `rssi`,
`battvolt`, `eepuke`, `fpgapeek`, `button`, `btndebug on`, `reboot`.

## Scripted / CI use

```
python -m p10r_emu --headless --no-state --script test.txt
```

Lines beginning with `:` drive the emulator, everything else goes to the CLI
(see `headless.py` for the full list):

```
:wait 5000
:tx 474.325 -50
:press scan
:press enter 1500
:lcd
battvolt
:expect Battery Voltage is
```

The exit code is 1 if an `:expect` failed and 2 if the emulation faulted.

From Python:

```python
from p10r_emu.machine_kinetis import KinetisMachine
m = KinetisMachine("App_P10RP.bin", state_dir=None)
m.provision("G10")
m.run_ms(5000)
m.board.rf.add(470.125, -50)
m.click("enter")
print(m.lcd_text())
print(m.command("rssi"))
```

## What is and is not real

Real: every instruction of the application image, the RTOS, the menu system,
NV data layout, CLI, the SPI traffic to the FPGA / EEPROM / LCD / synthesiser.

Modelled or missing:

* **Bootloader** - not in this package. The emulator enters the application
  at its vector table (0x18000) with the clock tree as the bootloader would
  leave it. `verboot` reports 0.0.0.0.
* **FPGA** - no bitstream is executed. Registers whose meaning was recovered
  from the firmware (list in `fpga.py`) behave; the rest are plain storage.
  There is no audio path, only the meter values.
* **RF** - first IF assumed 110.6 MHz with high-side LO on every band (this
  is what the firmware does on G10 and L8, the two bands checked).
* **SB900 smart battery** - the HDQ port answers "no gauge", so the unit runs
  as if on AA cells. Set `board.fpga.hdq` to a register dict to experiment.
* **IR sync / second serial port** - output is captured (`machine.link_out`),
  nothing answers.
* **Firmware update, external SPI flash** - flash reads as erased.
* **Volume pot** - passed straight through as 0..255; the real scale is a guess.
* **Timing** - 4 CPU cycles per instruction, peripherals complete instantly or
  after a fixed short delay. Good for functional testing, not for timing work.
* **`App_P10R.bin`** (the AVR-based non-plus P10R) and `Fpga_P10RP.bin` are
  not used.

If the firmware touches a peripheral register nothing models, it reads zero
and is recorded in `machine.cpu.unknown_log` (address -> reads, writes, first
PC) - the first place to look when new firmware behaves oddly.
