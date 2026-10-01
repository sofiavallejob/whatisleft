#!/usr/bin/env python3
"""
What Is Left: WebSocket -> OSC (and MIDI) bridge.

The browser page cannot send UDP, so it sends each OSC message as JSON over a
WebSocket ([address, typetags, arg1, arg2, ...]). This script receives them and
forwards real OSC over UDP, and optionally MIDI for Ableton (no Max for Live needed).

    pip install websockets python-osc            # OSC only
    pip install mido python-rtmidi               # + MIDI

    python wil_bridge.py                          # OSC -> Pd 9000 + SuperCollider 57120
    python wil_bridge.py --midi                   # + virtual MIDI port "WIL" (macOS / Linux)
    python wil_bridge.py --midi "loopMIDI Port"   # Windows: use an existing loopMIDI port
    python wil_bridge.py --to 127.0.0.1:7400      # OSC to Max for Live / anything else
    python wil_bridge.py --list-midi              # show MIDI ports and quit

Then press Connect in the page (default ws://localhost:8765).

MIDI map (when --midi is on)
  Notes = events, pitch = the voice's frequency, one channel per kind
    ch 1 killed (vel 127 if it came in a massacre/section, else 100)
    ch 2 fled   ch 3 returned   ch 4 uprooted   ch 5 resettled   ch 6 disappeared
  CCs on ch 16 = continuous data, 0..127
    20 still home  21 killed  22 disappeared  23 inside (IDPs)  24 fled  25 back
    30 kill rate   31 flee rate  32 return rate  33 uproot rate  34 disappear rate
    40 timeline progress (country start -> last year)
    41 playing (127) / paused or ended (0)
"""
import argparse
import asyncio
import json
import math

import websockets
from pythonosc.udp_client import SimpleUDPClient


def parse_target(text):
    host, port = text.rsplit(":", 1)
    return host, int(port)


def convert(types, args):
    out = []
    for t, a in zip(types, args):
        if t == "i":
            out.append(int(a))
        elif t == "f":
            out.append(float(a))
        else:
            out.append(str(a))
    return out


def ftom(f):
    return max(0, min(127, round(69 + 12 * math.log2(max(f, 1e-6) / 440))))


def to7(x):
    return max(0, min(127, round(127 * x)))


EVENT_CH = {"/wil/killed": 0, "/wil/fled": 1, "/wil/returned": 2,
            "/wil/uprooted": 3, "/wil/resettled": 4, "/wil/disappeared": 5}
CC_CH = 15


class Midi:
    """OSC messages -> MIDI notes and CCs. Only sends a CC when its value changes."""

    def __init__(self, port, note_len, rate_scale):
        self.port, self.note_len, self.rate_scale = port, note_len, rate_scale
        self.cc_last = {}
        self.span = None  # (start year, last year) of the current country

    def cc(self, num, val):
        if self.cc_last.get(num) != val:
            self.cc_last[num] = val
            self.port.send(self.mido.Message("control_change", channel=CC_CH, control=num, value=val))

    def note(self, ch, pitch, vel):
        m = self.mido.Message
        self.port.send(m("note_on", channel=ch, note=pitch, velocity=vel))
        asyncio.get_running_loop().call_later(
            self.note_len, lambda: self.port.send(m("note_off", channel=ch, note=pitch, velocity=0)))

    def handle(self, addr, v):
        if addr in EVENT_CH:
            vel = 127 if addr == "/wil/killed" and len(v) > 1 and v[1] else 100
            self.note(EVENT_CH[addr], ftom(v[0]), vel)
        elif addr == "/wil/shares":                 # left, killed, dis, inside, fled, back, ...
            for i in range(6):
                self.cc(20 + i, to7(v[i]))
        elif addr == "/wil/rates":
            for i in range(5):
                self.cc(30 + i, to7(v[i] * self.rate_scale))
        elif addr == "/wil/country":                # iso, name, start, last
            self.span = (v[2], v[3])
        elif addr == "/wil/time" and self.span:
            a, b = self.span
            self.cc(40, to7((v[0] - a) / max(1, b + 1 - a)))
        elif addr == "/wil/state":
            self.cc(41, 127 if v[0] == "play" else 0)

    def panic(self):
        for ch in range(16):
            self.port.send(self.mido.Message("control_change", channel=ch, control=123, value=0))


def open_midi(name):
    import mido
    if name == "WIL":
        try:
            return mido, mido.open_output("WIL", virtual=True), "virtual port WIL"
        except (NotImplementedError, OSError, IOError):
            raise SystemExit("Could not create a virtual MIDI port. On Windows install loopMIDI, create a port, "
                             "then run:  python wil_bridge.py --midi \"loopMIDI Port\"")
    names = mido.get_output_names()
    match = [n for n in names if name.lower() in n.lower()]
    if not match:
        raise SystemExit(f"No MIDI output matching '{name}'. Available: {names or 'none'}")
    return mido, mido.open_output(match[0]), match[0]


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765, help="WebSocket port the page connects to")
    ap.add_argument("--to", action="append", type=parse_target, help="OSC target host:port (repeatable)")
    ap.add_argument("--no-osc", action="store_true", help="MIDI only")
    ap.add_argument("--midi", nargs="?", const="WIL", help="MIDI out: virtual 'WIL' port, or part of an existing port name")
    ap.add_argument("--list-midi", action="store_true", help="list MIDI outputs and quit")
    ap.add_argument("--note-len", type=float, default=0.4, help="MIDI note length in seconds")
    ap.add_argument("--rate-scale", type=float, default=1.0, help="multiply rates before CC (if they saturate or stay tiny)")
    ap.add_argument("--verbose", action="store_true", help="print every message")
    a = ap.parse_args()

    if a.list_midi:
        import mido
        print("\n".join(mido.get_output_names()) or "no MIDI outputs")
        return

    targets = [] if a.no_osc else (a.to or [("127.0.0.1", 57120), ("127.0.0.1", 9000)])
    clients = [SimpleUDPClient(h, p) for h, p in targets]
    midi = None
    if a.midi:
        mido, port, label = open_midi(a.midi)
        midi = Midi(port, a.note_len, a.rate_scale)
        midi.mido = mido

    print(f"WebSocket ws://localhost:{a.port}")
    if targets:
        print("  -> OSC  " + ", ".join(f"{h}:{p}" for h, p in targets))
    if midi:
        print(f"  -> MIDI {label}")

    async def handler(ws):
        print("page connected")
        try:
            async for raw in ws:
                try:
                    addr, types, *args = json.loads(raw)
                    values = convert(types, args)
                except (ValueError, TypeError):
                    continue
                for c in clients:
                    c.send_message(addr, values)
                if midi:
                    try:
                        midi.handle(addr, values)
                    except (IndexError, TypeError, ValueError):
                        pass
                if a.verbose:
                    print(addr, values)
        finally:
            if midi:
                midi.panic()
            print("page disconnected")

    async with websockets.serve(handler, "localhost", a.port):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
