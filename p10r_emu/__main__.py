"""Command line entry point:  python -m p10r_emu  [options]"""
import argparse
import os
import sys

from .board import BAND_VARIANT

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FW = os.path.dirname(os.path.dirname(HERE))
DEFAULT_STATE = os.path.join(os.path.dirname(HERE), "state")


def build_machine(args):
    variant = args.variant or BAND_VARIANT.get(args.band.upper(), "A")
    if args.cpu == "kinetis":
        from .machine_kinetis import KinetisMachine as cls
    else:
        from .machine_ra import RaMachine as cls
    image = args.image or os.path.join(args.firmware_dir, cls.image_name)
    if not os.path.exists(image):
        sys.exit("firmware image not found: %s" % image)
    state = None if args.no_state else args.state_dir
    return cls(image, state_dir=state, variant=variant)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="p10r_emu", description="Shure P10R+ firmware emulator")
    ap.add_argument("--cpu", choices=("kinetis", "ra"), default="kinetis",
                    help="which build to run: App_P10RP.bin (kinetis) or App_P10RP_RA.bin (ra)")
    ap.add_argument("--firmware-dir", default=DEFAULT_FW, help="folder holding the .bin files")
    ap.add_argument("--image", help="explicit path to an application image")
    ap.add_argument("--band", default="G10",
                    help="frequency band to provision a blank EEPROM with (default G10)")
    ap.add_argument("--variant", help="board variant straps (A..K, RA..RK); "
                    "default: whatever the band needs")
    ap.add_argument("--state-dir", default=DEFAULT_STATE, help="where the EEPROM image lives")
    ap.add_argument("--no-state", action="store_true", help="keep the EEPROM in memory only")
    ap.add_argument("--tx", action="append", default=[], metavar="MHZ[,DBM]",
                    help="add a mock transmitter (repeatable)")
    ap.add_argument("--headless", action="store_true", help="no window; drive it from stdin")
    ap.add_argument("--script", help="headless: read commands from this file instead of stdin")
    args = ap.parse_args(argv)

    m = build_machine(args)
    for spec in args.tx:
        parts = spec.split(",")
        m.board.rf.add(float(parts[0]), float(parts[1]) if len(parts) > 1 else -45.0)
    if m.is_blank():
        print("Blank EEPROM: provisioning band %s (variant %s)..." % (
            args.band.upper(), m.board.variant))
        if not m.provision(args.band.upper()):
            print("warning: the firmware rejected band %s for board variant %s" % (
                args.band.upper(), m.board.variant))
    if args.headless:
        from .headless import repl
        return repl(m, open(args.script) if args.script else sys.stdin)
    from .gui import App
    App(m).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
