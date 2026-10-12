"""Line-oriented driver for scripted / CI use.

Lines starting with ':' are emulator commands; anything else is typed into
the firmware's serial CLI.

    :wait MS                 run for MS milliseconds
    :press NAME [MS]         click a button (up down enter exit scan), optional hold time
    :down NAME / :up NAME    press / release without releasing / pressing
    :lcd                     print the display
    :expect TEXT             fail unless TEXT appeared on the console since the last :expect
    :tx MHZ [DBM]            add a mock transmitter      :tx clear   remove them all
    :txfollow [DBM]          transmitter that tracks the tuned frequency
    :noise DBM               noise floor
    :battery VOLTS           :volume 0-255          :phones 0|1
    :state                   tuned frequency, LEDs, antenna
    :reset                   reset the CPU           :quit
"""
import sys


def repl(m, stream, out=sys.stdout):
    seen = ""

    def flush():
        nonlocal seen
        s = m.take_console()
        if s:
            seen += s
            out.write(s.replace("\r", ""))
            out.flush()

    rc = 0
    for raw in stream:
        line = raw.rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(":"):
            m.type(line + "\r")
            m.run_ms(400)
            flush()
            continue
        cmd, *a = line[1:].split()
        rf = m.board.rf
        if cmd == "quit":
            break
        elif cmd == "wait":
            m.run_ms(int(a[0]))
        elif cmd == "press":
            m.click(a[0], hold_ms=int(a[1]) if len(a) > 1 else 120)
        elif cmd in ("down", "up"):
            m.button(a[0], cmd == "down")
            m.run_ms(50)
        elif cmd == "lcd":
            flush()
            out.write("\n".join("|" + r + "|" for r in m.lcd_text().split("\n")) + "\n")
        elif cmd == "expect":
            flush()
            want = line.split(None, 1)[1]
            if want not in seen:
                out.write("EXPECT FAILED: %r\n" % want)
                rc = 1
            seen = ""
        elif cmd == "tx":
            if a[0] == "clear":
                del rf.transmitters[:]
            else:
                rf.add(float(a[0]), float(a[1]) if len(a) > 1 else -45.0)
        elif cmd == "txfollow":
            rf.add(0, float(a[0]) if a else -45.0, follow=True)
        elif cmd == "noise":
            rf.noise_dbm = float(a[0])
        elif cmd == "battery":
            m.board.battery_mv = float(a[0]) * 1000.0
        elif cmd == "volume":
            m.board.fpga.volume = int(a[0])
        elif cmd == "phones":
            m.board.fpga.set_phones(a[0] not in ("0", "off"))
        elif cmd == "state":
            b = m.board
            out.write("t=%.0f ms  tuned %.3f MHz  LO %.3f MHz  antenna %s  "
                      "leds(red,green,rf,backlight)=%s\n" % (
                          m.time_ms, b.synth.rf_khz / 1000, b.synth.lo_khz / 1000,
                          b.fpga.antenna(), ",".join("%.2f" % x for x in b.fpga.leds())))
        elif cmd == "reset":
            m.reset()
        else:
            out.write("unknown emulator command: %s\n" % cmd)
        flush()
        if m.fault:
            out.write("EMULATION FAULT: %s\n" % m.fault)
            return 2
    m.board.eeprom.save()
    return rc
