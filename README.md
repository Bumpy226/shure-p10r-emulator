# P10R / P10R+ firmware emulator

Runs the unmodified application images from this package on an emulated main
board, so the real menu code, CLI and settings handling can be exercised
without hardware:

| `--cpu` | Unit | Image | CPU |
|---|---|---|---|
| `kinetis` (default) | P10R+ | `App_P10RP.bin` | Kinetis K-series (Cortex-M4) |
| `ra` | P10R+ | `App_P10RP_RA.bin` | Renesas RA6M (Cortex-M4) |
| `p10r` | P10R | `App_P10R.bin` | ATxmega256A3 (AVR) |

The binaries are executed instruction-for-instruction and the hardware around
the CPU is modelled:

| Piece | How it is emulated |
|---|---|
| Cortex-M core, NVIC, SysTick, exceptions (Unicorn) | `cortexm.py` |
| Kinetis peripherals | `kinetis.py`, `machine_kinetis.py` |
| Renesas RA peripherals | `renesas.py`, `machine_ra.py` |
| AVR XMEGA core (own interpreter, Unicorn has no AVR) | `avr.py` |
| XMEGA peripherals, P10R board | `xmega.py`, `machine_p10r.py` (pin map in the file header) |
| P10R+ Spartan-6 FPGA | behavioural register model, `fpga.py` (register map in the file header) |
| LCD (ST7565-type, 80x40 visible) | `devices.py`, drawn in the window |
| Settings EEPROM | file-backed, `state/eeprom_<cpu>.bin` |
| PLL synthesiser | latches decoded back into the tuned frequency |
| RF / audio | mock transmitters, `rf.py` |

## Running

```
pip install unicorn                # only needed for the two P10R+ builds
cd emulator
python -m p10r_emu                 # P10R+, Kinetis build, window
python -m p10r_emu --cpu ra        # P10R+, Renesas build
python -m p10r_emu --cpu p10r      # original P10R
python -m p10r_emu --band L8       # another band (sets the board variant too)
python -m p10r_emu --tx 474.325,-50 --tx 480.0,-70
```

`run_emulator.bat` does the same from Explorer and passes arguments through.

On first start the EEPROM is blank and the emulator provisions it through the
firmware's own CLI, then reboots:

* P10R+: `setBand <band>`.
* P10R: the firmware writes default settings by itself; the emulator adds
  `tuned true` (otherwise the unit shows "NOT TUNED - PLEASE RETURN FOR
  SERVICE") and `band <n>`.

The board-variant straps are set to match the band and remembered in
`state/variant_<cpu>.txt`. Delete the files in `state/` to go back to a
factory-fresh unit.

P10R bands: G10 G10E G11 G10J G62 (variant A), J8 J8E J8J J8A (B), K10E (C),
L8 L8E L8J L8A (D), L9E L11J (E), P8 (F), Q21 Q23 Q22E R26 R27 A24 (G),
X1 X2 X7 (J), H8Z (K).

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
the backlight.

### Mock RF

Each entry in the list is a transmitter: frequency, level at the antenna, left
/ right audio level and whether it carries a valid pilot. The receiver sees
whatever is on the frequency it is tuned to, with a filter skirt for
neighbours, so RSSI bars, audio meters, squelch, and the firmware's own
channel / group / full scans all respond. Tick *follow tuned freq* for a
transmitter that is always on-channel.

Battery voltage and the volume pot are under *Unit*, plus the earphone-detect
line on the P10R+ (unplugging it puts the unit in power-save) and the power
switch on the P10R (off and on again restarts the firmware).

### Serial console

The pane on the right is the firmware's own CLI; `help` (P10R) or `help all`
(P10R+) lists it.

* P10R+: `systeminfo`, `td`, `bandinfo`, `freq`, `group`, `channel`, `rssi`,
  `battvolt`, `eepuke`, `fpgapeek`, `button`, `btndebug on`, `reboot`.
* P10R: `ver`, `sysver`, `board`, `band`, `freq`, `group`, `channel`,
  `adcvalues`, `gpioall`, `battery`, `squelch`, `volume`, `rfpad`,
  `btnup` / `btndown` / `btnenter` / `btnexit`, `fullscanStart`.

## Scripted / CI use

```
python -m p10r_emu --cpu p10r --headless --no-state --script test.txt
```

Lines beginning with `:` drive the emulator, everything else goes to the CLI
(see `headless.py` for the full list):

```
:wait 6000
:tx 474.325 -50
freq 474.325
:press enter
:lcd
ver
:expect CLI version
```

The exit code is 1 if an `:expect` failed and 2 if the emulation faulted.

From Python:

```python
from p10r_emu.machine_p10r import P10rMachine     # or machine_kinetis / machine_ra
m = P10rMachine("App_P10R.bin", state_dir=None)
m.provision()
m.run_ms(6000)
m.board.rf.add(470.375, -50)
m.click("enter")
print(m.lcd_text())
print(m.command("adcvalues"))
```

## What is and is not real

Real: every instruction of the application image, the menu system, settings
layout, CLI, and the bus traffic to the LCD, synthesiser, EEPROM and (P10R+)
FPGA.

Modelled or missing, all units:

* **Bootloader** - not in this package. The emulator enters the application
  directly with the clocks as the bootloader would leave them, so bootloader
  versions read as 0.0.0.0 / 65535.
* **RF** - first IF 110.6 MHz with high-side LO. On the P10R+ this was checked
  on G10 and L8 only; on the P10R the synth word is decoded as N x 25 kHz,
  which matches the firmware's `freq` on one band of each of the nine variants.
* **IR sync / second serial port** - output is captured (`machine.link_out`),
  nothing answers, so "SYNC NOW" never completes.
* **Firmware update, external SPI flash** - flash reads as erased.
* **Timing** - a fixed number of CPU cycles per instruction; peripherals
  complete instantly or after a fixed short delay. Good for functional
  testing, not for timing work.
* `Fpga_P10RP.bin` is not used.

P10R+ only:

* **FPGA** - no bitstream is executed. Registers whose meaning was recovered
  from the firmware (list in `fpga.py`) behave; the rest are plain storage.
  There is no audio path, only the meter values.
* **SB900 smart battery** - the HDQ port answers "no gauge", so the unit runs
  as if on AA cells. Set `board.fpga.hdq` to a register dict to experiment.
* **Volume pot** - passed straight through as 0..255; the real scale is a guess.

P10R only:

* **Analogue scales are guesses.** The firmware only sees ADC counts; the
  emulator maps battery volts as V / 3.3 V of full scale, RSSI as 12 counts
  per dB with -90 dBm on the first default meter segment, and audio /
  squelch-noise levels to values either side of the default thresholds
  (constants at the top of `machine_p10r.py`). The firmware's AGC step
  attenuator is fed back into the RSSI reading.
* **Pilot / tone-key detect** is taken to be the PA5 input (low = present):
  with it the firmware lights the RF LED, unmutes and shows the audio meters.
* **Earphone detection** (`hpdeton`, `hpstatus`) is not modelled; the
  headphone amplifier and digital pots only store what is written to them.
* **Charger dock** - `:charger 1` in a script (or `board.charger = True`) shows
  the charging screen; there is no smart-battery model.
* **Flash self-programming** (the SPM instruction, i.e. in-application
  firmware update) is not implemented and stops the emulation with a fault
  message if reached.

If the firmware touches a peripheral register nothing models, it reads zero
and is recorded in `machine.cpu.unknown_log` (address -> reads, writes, first
PC) - the first place to look when new firmware behaves oddly.
