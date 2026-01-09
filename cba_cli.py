#!/usr/bin/env python3
"""
cba4_discharge_test.py

Battery discharge test using a West Mountain Radio CBA-IV via the wmr_cba library.

- Starts a constant-current discharge at --amps, with device cutoff voltage --cutoff.
- Waits up to 5 seconds for cba.is_running() to become True (device startup latency).
- Once per interval, prints friendly fixed-width status:
    0:15 (15.0 s) ... 12.7840V ... 0.2039A ... 2.606W ... 0.000849Ah ... 0.010859Wh
- Optional: --csv FILE writes CSV rows like:
    t(s),voltage(V),current(A),power(W),amp_hours(Ah),watt_hours(Wh)
- Stops when:
    * device stops (is_running() == False after it has started), OR
    * voltage <= cutoff (guard), OR
    * Ctrl-C / SIGTERM (immediate)
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from typing import Optional, TextIO

from wmr_cba import wmr_cba


_stop_requested = False


def _handle_signal(signum, frame):
    global _stop_requested
    _stop_requested = True


def _fmt_duration(seconds: float) -> str:
    seconds_i = int(round(seconds))
    h = seconds_i // 3600
    m = (seconds_i % 3600) // 60
    s = seconds_i % 60
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def _install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    # Not all platforms have SIGHUP, SIGQUIT
    for sig_name in ("SIGHUP", "SIGQUIT"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handle_signal)
            except Exception:
                pass


def _sleep_interruptible(total_seconds: float, step: float = 0.05) -> None:
    """
    Sleep in small chunks so Ctrl-C / signals stop the loop quickly instead of
    waiting for the full interval.
    """
    end = time.monotonic() + total_seconds
    while not _stop_requested:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(step, remaining))


def _wait_for_running(cba, timeout_s: float = 5.0) -> bool:
    """
    Wait up to timeout_s for cba.is_running() to become True.
    Returns True if running, False on timeout or stop requested.
    """
    deadline = time.monotonic() + timeout_s
    while not _stop_requested and time.monotonic() < deadline:
        try:
            if bool(cba.is_running()):
                return True
        except Exception:
            # If is_running() is temporarily unhappy, keep trying until timeout.
            pass
        _sleep_interruptible(0.05)
    return False


def _print_header() -> None:
    # Fixed-width header that matches the friendly output columns
    print(
        f"{'time':>10} ... "
        f"{'V':>9} ... "
        f"{'A':>9} ... "
        f"{'W':>9} ... "
        f"{'Ah':>12} ... "
        f"{'Wh':>12}"
    )
    sys.stdout.flush()


def _print_friendly_line(elapsed_s: float, v: float, a: float, w: float, ah: float, wh: float) -> None:
    # Example: "0:15 (15.0 s) ... 12.7840V ... 0.2039A ... 2.606W ... 0.000849Ah ... 0.010859Wh"
    dur = _fmt_duration(elapsed_s)
    print(
        f"{dur:>4} ({elapsed_s:6.1f} s) ... "
        f"{v:9.4f}V ... "
        f"{a:9.4f}A ... "
        f"{w:9.3f}W ... "
        f"{ah:12.6f}Ah ... "
        f"{wh:12.6f}Wh"
    )
    sys.stdout.flush()


def _write_csv_header(f: TextIO) -> None:
    f.write("t(s),voltage(V),current(A),power(W),amp_hours(Ah),watt_hours(Wh)\n")
    f.flush()


def _write_csv_line(f: TextIO, elapsed_s: float, v: float, a: float, w: float, ah: float, wh: float) -> None:
    f.write(f"{elapsed_s:.0f},{v:.4f},{a:.4f},{w:.3f},{ah:.6f},{wh:.6f}\n")
    f.flush()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a constant-current discharge test on a WMR CBA-IV and report Ah/Wh."
    )
    parser.add_argument("--amps", type=float, required=True, help="Discharge current in amps (e.g., 5.0).")
    parser.add_argument("--cutoff", type=float, required=True, help="Cutoff voltage in volts (e.g., 10.5).")
    parser.add_argument(
        "--interval", type=float, default=1.0, help="Sampling/print interval in seconds (default: 1.0)."
    )
    parser.add_argument(
        "--csv",
        type=str,
        default=None,
        metavar="FILE",
        help="Optional CSV log filename (writes t(s),V,A,W,Ah,Wh).",
    )
    args = parser.parse_args()

    if args.amps <= 0:
        print("ERROR: --amps must be > 0", file=sys.stderr)
        return 2
    if args.cutoff <= 0:
        print("ERROR: --cutoff must be > 0", file=sys.stderr)
        return 2
    if args.interval <= 0:
        print("ERROR: --interval must be > 0", file=sys.stderr)
        return 2

    _install_signal_handlers()

    cba = None
    csv_f: Optional[TextIO] = None

    try:
        if args.csv:
            csv_f = open(args.csv, "w", encoding="utf-8", newline="")
            _write_csv_header(csv_f)

        cba = wmr_cba.CBA4()

        # Start constant-current discharge; let the device enforce cutoff too.
        cba.do_start(float(args.amps), float(args.cutoff))

        # Wait for device to actually start running (startup latency)
        if not _wait_for_running(cba, timeout_s=5.0):
            if _stop_requested:
                print("Stopped before test started.")
                return 130  # conventional for SIGINT-ish exit
            print("ERROR: CBA-IV did not report running within 5 seconds. Aborting.", file=sys.stderr)
            return 1

        start_t = time.monotonic()
        last_t = start_t
        ah = 0.0
        wh = 0.0

        _print_header()

        while True:
            if _stop_requested:
                print("\nStop requested (signal received).")
                break

            now = time.monotonic()
            dt = now - last_t
            last_t = now

            v = float(cba.get_voltage())
            a = float(cba.get_measured_current())
            w = v * a

            if dt > 0:
                hours = dt / 3600.0
                ah += a * hours
                wh += w * hours

            elapsed = now - start_t

            _print_friendly_line(elapsed, v, a, w, ah, wh)
            if csv_f is not None:
                _write_csv_line(csv_f, elapsed, v, a, w, ah, wh)

            # Stop if device says it stopped (after having started)
            try:
                if not bool(cba.is_running()):
                    print("\nCBA-IV reports test has stopped (is_running() == False).")
                    break
            except Exception:
                # If is_running fails transiently, ignore and rely on voltage guard/signal.
                pass

            # Extra guard
            if v <= float(args.cutoff):
                print(f"\nCutoff reached (guard): {v:.4f} V <= {float(args.cutoff):.4f} V")
                break

            _sleep_interruptible(float(args.interval))

        # Stop discharge
        try:
            cba.do_stop()
        except Exception:
            pass

        duration = time.monotonic() - start_t
        print(f"Total amp-hours:  {ah:.6f} Ah")
        print(f"Total watt-hours: {wh:.6f} Wh")
        print(f"Test duration:    {_fmt_duration(duration)} ({duration:.1f} s)")
        return 0 if not _stop_requested else 130

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    finally:
        if csv_f is not None:
            try:
                csv_f.close()
            except Exception:
                pass
        if cba is not None:
            try:
                cba.do_stop()
            except Exception:
                pass
            try:
                cba.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())

