# FIND IEMS firmware mod

Patched copies of the three application images. Each gets one extra item,
**FIND IEMS**, at the end of the RADIO menu (second page, under ANTENNA).

| Image | Unit | Patch source |
|---|---|---|
| `App_P10RP.bin` | P10R+, Kinetis CPU | `src/find_iems.c`, `src/target_kinetis.h` |
| `App_P10RP_RA.bin` | P10R+, Renesas RA CPU | `src/find_iems.c`, `src/target_ra.h` |
| `App_P10R.bin` | original P10R | `src/find_iems_p10r.c`, `src/glue_p10r.S`, `src/target_p10r.h` |

## What it does

1. ENTER on FIND IEMS sweeps the whole band in 25 kHz steps while
   "DOING THE MAGIC!" scrolls across the display. EXIT cancels and returns to
   the channel the pack was on.
2. Carriers are picked out of the sweep: anything at least 12 dB above the
   noise floor, centred between its -6 dB points. Blocks wider than 600 kHz
   are dropped without a visit.
3. The pack tunes to every remaining carrier and waits up to 0.3 s for the
   pilot tone (what turns the RF light blue). Only those carriers are kept.
4. The hits are listed in the order found, lowest frequency first:

   ```
   RESULTS 1/3
   1.474.325 -50
   2.480.000 -62
   3.510.500 -70
   ```

   Index, frequency in MHz, level in dBm. UP/DOWN move through the list three
   rows at a time, ENTER tunes to the selected entry (stored like a frequency
   set in the menu) and stays in the list, marking the entry with `*`. EXIT
   goes back to the RADIO menu. The list does not time out.

The display is 80 pixels wide, which is 13 characters of the menu font, so
the line cannot hold `MHz |` and `dB` as well.

Two cosmetic changes mark a modded unit (`cosmetics.py`): the boot logo
reads BUMPY instead of SHURE, and the MAIN MENU title reads MOD LOADED.
The logo is drawn from the letter shapes in that file; the marquee size is
`MQ_SCALE` in the C sources (3 = 15 x 21 pixel letters).

## Building

```
python mod/build.py            # all three, or: ra | kinetis | p10r
```

reads the stock images from the repository root and rewrites the ones here.
It needs `arm-none-eabi-gcc` and `avr-gcc` (the winget packages
`Arm.ArmGnuToolchain` and `ZakKemble.avr-gcc` are found automatically). The
script refuses to patch an image whose checksum or hooked instructions are
not the stock ones.

Run a patched image in the emulator with

```
python -m p10r_emu --image mod/App_P10RP_RA.bin
```

## How the patch is attached

* **P10R+ (both CPUs)**: the code is appended to the image. Six entries of
  the RadioMenu vtable (tick, exit, enter, up, down, one-second timer) and
  two calls in its redraw are pointed at the patch; everything else goes
  through to the original functions. The scan state lives in unused RAM on
  the RA and in the stock full scan's sample buffer on the Kinetis (the saved
  spectrum is read back from the serial flash afterwards).
* **P10R**: the code goes into the unused flash behind the application; the
  image keeps its size. Three instructions are redirected: the entry of the
  menu state machine's event function, the "which screen" read in the redraw
  and the event poll in the main loop. The new screens are states 160-162,
  next to the firmware's own 0-159. The scan state uses the stock full
  scan's sample buffer, so a spectrum from an earlier FULL SCAN is lost.

Every image ends in a CRC-16/ARC of everything before it; the script
recomputes it (for the P10R, whose last two bytes are a fixed `C0 DE`, by
adjusting two spare bytes so the checksum comes out the same).

## Not verified on hardware

All of this was developed and tested against the emulator only.

* **Timing.** The emulator does not model bus or settling times. The sweep
  uses the stock full scan's 5 ms dwell per channel; how long 2881 channels
  take on a real pack, and whether 0.3 s is enough for the pilot detector,
  has to be seen on the unit. `DWELL_MS`, `SWEEP_PER_TICK` and
  `PILOT_WAIT_MS` at the top of the sources are the knobs.
* **Carrier shape.** The detection thresholds assume a carrier that falls
  off within about 150 kHz, as in the emulator's RF model.
* **Level on the P10R.** That unit has no calibrated RSSI; the dBm figure
  is derived from the detector voltage with the emulator's guess of 12 ADC
  counts per dB and is only good for comparing entries.
* **Squelch flag on the P10R+.** "Pilot found" is the FPGA's un-squelch
  flag. In NO SQUELCH mode the patch switches the pilot mute on for the
  duration of the scan.
* **Flashing.** Whether the bootloader or the update tool checks anything
  besides the trailing checksum (image length, a signature in the update
  package) is unknown. The two P10R+ images are a few kilobytes longer than
  stock. Keep a way to restore the original firmware before trying these on
  a unit.
