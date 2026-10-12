"""Command line entry point:  python -m p10r_emu  [options]"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FW = os.path.dirname(os.path.dirname(HERE))
DEFAULT_STATE = os.path.join(os.path.dirname(HERE), "state")
DEFAULT_BAND = "G10"


def machine_class(kind):
    if kind == "kinetis":
        from .machine_kinetis import KinetisMachine as cls
    elif kind == "ra":
        from .machine_ra import RaMachine as cls
    else:
        from .machine_p10r import P10rMachine as cls
    return cls


def build_machine(args):
    cls = machine_class(args.cpu)
    image = args.image or os.path.join(args.firmware_dir, cls.image_name)
    if not os.path.exists(image):
        sys.exit("firmware image not found: %s" % image)
    state = None if args.no_state else args.state_dir
    # The board-variant straps have to match the band in the EEPROM, so the
    # variant is remembered next to the EEPROM image between runs.
    note = os.path.join(state, "variant_%s.txt" % cls.tag) if state else None
    variant = args.variant
    if not variant and args.band:
        variant = cls.resolve_band(args.band)[0]
    if not variant and note and os.path.exists(note):
        variant = open(note).read().strip()
    variant = variant or cls.resolve_band(DEFAULT_BAND)[0]
    m = cls(image, state_dir=state, variant=variant)
    if note:
        with open(note, "w") as f:
            f.write(variant + "\n")
    return m


def main(argv=None):
    ap = argparse.ArgumentParser(prog="p10r_emu",
                                 description="Shure P10R / P10R+ firmware emulator")
    ap.add_argument("--cpu", "--model", choices=("kinetis", "ra", "p10r"), default="kinetis",
                    help="which unit to run: P10R+ App_P10RP.bin (kinetis), P10R+ "
                         "App_P10RP_RA.bin (ra) or the original P10R App_P10R.bin (p10r)")
    ap.add_argument("--firmware-dir", default=DEFAULT_FW, help="folder holding the .bin files")
    ap.add_argument("--image", help="explicit path to an application image")
    ap.add_argument("--band",
                    help="frequency band: sets the board variant and is what a blank "
                         "EEPROM is provisioned with (default G10)")
    ap.add_argument("--variant", help="board variant straps (A..K; P10R+ also RA..RK); "
                    "default: whatever the band needs")
    ap.add_argument("--state-dir", default=DEFAULT_STATE, help="where the EEPROM image lives")
    ap.add_argument("--no-state", action="store_true", help="keep the EEPROM in memory only")
    ap.add_argument("--tx", action="append", default=[], metavar="MHZ[,DBM]",
                    help="add a mock transmitter (repeatable)")
    ap.add_argument("--headless", action="store_true", help="no window; drive it from stdin")
    ap.add_argument("--script", help="headless: read commands from this file instead of stdin")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):     # the LCD dump uses block characters
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    m = build_machine(args)
    for spec in args.tx:
        parts = spec.split(",")
        m.board.rf.add(float(parts[0]), float(parts[1]) if len(parts) > 1 else -45.0)
    if m.is_blank():
        band = (args.band or DEFAULT_BAND).upper()
        print("Blank EEPROM: provisioning band %s (variant %s)..." % (band, m.board.variant))
        if not m.provision(m.resolve_band(band)[1]):
            print("warning: the firmware rejected band %s for board variant %s" % (
                band, m.board.variant))
    if args.headless:
        from .headless import repl
        return repl(m, open(args.script) if args.script else sys.stdin)
    from .gui import App
    App(m).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
