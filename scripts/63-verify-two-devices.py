#!/usr/bin/env python3
"""Data-plane verification with TWO devices connected.

RUN THIS ON THE HYPERVISOR.

62-verify-with-device.py proves a link behaves. It cannot prove the switch
FORWARDS correctly, because that needs two ports and traffic. This does,
and it does it without a traffic generator: the host's backplane NIC sits
on te2, which carries VLAN 1 untagged, so a raw Ethernet broadcast injected
there floods to every VLAN 1 front port. Counting what comes out of each
front port is then a direct measurement of the forwarding decision.

  * PoE on two ports at once, independently
  * VLAN ISOLATION - broadcast reaches a port in VLAN 1 and stops reaching
    it once it is moved to another VLAN. This is the real test that the
    membership editor does something.
  * PORT MIRRORING - copies of one port's traffic actually come out of
    another. Completely unverified until now.

Everything is restored, including on exception.

    python3 63-verify-two-devices.py --a gi0 --b gi7
"""
import argparse
import fcntl
import importlib.util
import os
import socket
import struct
import sys
import time
import traceback
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
TUI = os.path.join(HERE, "..", "payload", "opt", "encs-host", "encs-switch-tui")

GREEN, RED, YELLOW, CYAN, DIM, OFF = (
    "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[0m")
RESULTS = {"ok": 0, "fail": 0, "skip": 0}
FAILURES, FINDINGS = [], []


def load_tui():
    loader = SourceFileLoader("encs_switch_tui", os.path.abspath(TUI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


t = load_tui()


def ok(w, d=""):
    RESULTS["ok"] += 1
    print(f"  {GREEN}ok{OFF}   {w} {DIM}{d}{OFF}")


def fail(w, d=""):
    RESULTS["fail"] += 1
    FAILURES.append(f"{w} {d}".strip())
    print(f"  {RED}FAIL{OFF} {w} {DIM}{d}{OFF}")


def skip(w, d=""):
    RESULTS["skip"] += 1
    print(f"  {YELLOW}skip{OFF} {w} {DIM}({d}){OFF}")


def finding(w):
    FINDINGS.append(w)
    print(f"  {CYAN}>>{OFF}   {w}")


# ============================================================ host plumbing
def backplane_nic():
    """The host NIC on te2 - the parent of the sw2363 VLAN interface."""
    base = "/sys/class/net/sw2363"
    try:
        for n in os.listdir(base):
            if n.startswith("lower_"):
                return n[len("lower_"):]
    except OSError:
        pass
    return None


def inject_broadcast(nic, count=40):
    """Send raw Ethernet broadcasts, untagged, so they land in VLAN 1.

    AF_PACKET with a made-up EtherType rather than ping or arping: the
    interface has no IP of its own (only the sw2363 VLAN child does), and
    this needs frames on the NATIVE vlan, not the tagged management one.
    EtherType 0x88b5 is the IEEE local-experimental range, so nothing else
    will react to it.
    """
    src = open(f"/sys/class/net/{nic}/address").read().strip()
    src_bytes = bytes(int(b, 16) for b in src.split(":"))
    frame = (b"\xff" * 6 + src_bytes + b"\x88\xb5"
             + b"encs-verify-probe" + b"\x00" * 30)
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    try:
        s.bind((nic, 0))
        for _ in range(count):
            s.send(frame)
            time.sleep(0.01)
    finally:
        s.close()
    return len(frame) * count


def tx_bytes(sw, port):
    s = next((x for x in sw.stats() if x["interfaceName"] == port), {})
    return int(s.get("transmitPacketByteCount") or 0)


def measure(sw, ports, nic, count=40, settle=3):
    """Inject broadcast and return per-port tx byte delta."""
    before = {p: tx_bytes(sw, p) for p in ports}
    inject_broadcast(nic, count)
    time.sleep(settle)
    return {p: tx_bytes(sw, p) - before[p] for p in ports}


class Restorer:
    def __init__(self):
        self.undo = []

    def add(self, what, fn):
        self.undo.append((what, fn))

    def run(self):
        print(f"\n{'-'*70}\nRestoring\n{'-'*70}")
        for what, fn in reversed(self.undo):
            try:
                if t.ok(fn()):
                    ok(f"restored {what}")
                else:
                    fail(f"restore of {what} REJECTED", "check by hand")
            except Exception as e:
                fail(f"restore of {what} raised", str(e)[:60])


# =================================================================== tests
def test_poe_both(sw, a, b):
    print(f"\n{'='*70}\nPoE ON BOTH PORTS\n{'='*70}")
    total = 0.0
    for port in (a, b):
        pe = next((x for x in sw.poe() if x["interfaceName"] == port), {})
        det = pe.get("detectionStatus", "")
        try:
            watts = int(pe.get("outputPower", "0")) / 1000
        except ValueError:
            watts = 0.0
        total += watts
        if det == "3":
            ok(f"{port} is DELIVERING",
               f"class {pe.get('powerClassification','?')}, {watts:.1f} W")
        else:
            skip(f"{port} PoE", f"status {t.POE_DETECT.get(det, det)}")
    finding(f"two PDs powered simultaneously, {total:.1f} W total")


def test_flood_baseline(sw, a, b, nic):
    """Both ports in VLAN 1: an injected broadcast must reach both."""
    print(f"\n{'='*70}\nBASELINE FLOODING (both in VLAN 1)\n{'='*70}")
    d = measure(sw, [a, b], nic)
    finding(f"injected broadcast -> tx delta {a}={d[a]}B {b}={d[b]}B")
    if d[a] > 0 and d[b] > 0:
        ok("broadcast floods to both front ports",
           "the switch is forwarding as expected")
        return True
    fail("injected broadcast did not reach both ports",
         f"{a}={d[a]}B {b}={d[b]}B - cannot measure isolation without this")
    return False


def test_vlan_isolation(sw, a, b, nic, r):
    """Move B to its own VLAN; VLAN 1 broadcast must stop reaching it."""
    print(f"\n{'='*70}\nVLAN ISOLATION\n{'='*70}")
    VID = "3997"
    if any(v["VLANID"] == VID for v in sw.vlans()):
        skip("VLAN isolation", f"VLAN {VID} already exists")
        return
    cur = next((row for row in sw.vlan_interfaces()
                if row.get("interfaceName") == b), {})
    orig_mode = cur.get("switchportModeAdmin")
    orig_pvid = cur.get("accessPVID")
    r.add(f"VLAN {VID}", lambda: sw.vlan(VID, False))
    r.add(f"{b} VLAN membership",
          lambda: sw.set_vlan_interface(b, mode=orig_mode,
                                        access_pvid=orig_pvid))
    if not t.ok(sw.vlan(VID, True, "isolation")):
        fail(f"creating VLAN {VID} was rejected")
        return
    if not t.ok(sw.set_vlan_interface(b, mode="11", access_pvid=VID)):
        fail(f"moving {b} into VLAN {VID} was rejected")
        return
    ok(f"moved {b} into VLAN {VID}", "a is still in VLAN 1")
    time.sleep(3)

    d = measure(sw, [a, b], nic)
    finding(f"after isolating {b}: tx delta {a}={d[a]}B {b}={d[b]}B")
    if d[a] > 0 and d[b] == 0:
        ok("VLAN isolation WORKS",
           f"VLAN 1 broadcast still reaches {a} and no longer reaches {b}")
    elif d[b] > 0:
        fail("the isolated port still received VLAN 1 broadcast",
             f"{b} tx grew by {d[b]}B - isolation is NOT working")
    else:
        skip("VLAN isolation", f"{a} saw nothing either; test inconclusive")


def test_mirroring(sw, a, b, nic, r):
    """Mirror A to B and prove copies actually come out of B.

    B is put in its own VLAN first so ordinary flooding cannot reach it -
    otherwise a byte count on B proves nothing, since VLAN 1 broadcast
    would arrive there anyway.
    """
    print(f"\n{'='*70}\nPORT MIRRORING ({a} -> {b})\n{'='*70}")
    VID = "3996"
    if any(v["VLANID"] == VID for v in sw.vlans()):
        skip("mirroring", f"VLAN {VID} already exists")
        return
    cur = next((row for row in sw.vlan_interfaces()
                if row.get("interfaceName") == b), {})
    orig_mode = cur.get("switchportModeAdmin")
    orig_pvid = cur.get("accessPVID")
    r.add(f"VLAN {VID}", lambda: sw.vlan(VID, False))
    r.add(f"{b} VLAN membership (mirror)",
          lambda: sw.set_vlan_interface(b, mode=orig_mode,
                                        access_pvid=orig_pvid))
    sw.vlan(VID, True, "mirror")
    sw.set_vlan_interface(b, mode="11", access_pvid=VID)
    time.sleep(3)

    quiet = measure(sw, [b], nic)
    if quiet[b] != 0:
        skip("mirroring", f"{b} is not quiet ({quiet[b]}B) - cannot attribute")
        return
    ok(f"{b} is quiet with no mirror configured", "0 bytes from a flood")

    r.add("mirror session 1", lambda: sw.del_span_destination(1))
    if not t.ok(sw.set_span_destination(1, b)):
        fail("setting the mirror destination was rejected")
        return
    ok(f"mirror destination set to {b}", f"ifIndex {t.span_index(b)}")
    if not t.ok(sw.add_span_source(1, ifname=a, direction=t.SPAN_BOTH)):
        fail("adding the mirror source was rejected")
        return
    ok(f"mirroring {a} both directions", f"ifIndex {t.span_index(a)}")
    time.sleep(3)

    d = measure(sw, [a, b], nic)
    finding(f"with mirroring on: tx delta {a}={d[a]}B {b}={d[b]}B")
    if d[b] > 0:
        ok("PORT MIRRORING WORKS",
           f"{b} emitted {d[b]}B of copies while in a different VLAN")
        if d[a] and abs(d[b] - d[a]) <= max(200, d[a] * 0.5):
            ok("the copied volume tracks the source port", f"{d[a]}B vs {d[b]}B")
        else:
            finding(f"copied volume {d[b]}B vs source {d[a]}B - mirroring "
                    f"tx as well as rx will not match exactly")
    else:
        fail("no copies came out of the mirror destination",
             "the SpanSourceTable/SpanDestinationTable writes were accepted "
             "but nothing was mirrored - check span_index()")


# ==================================================================== main
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--a", default="gi0", help="source / VLAN 1 port")
    p.add_argument("--b", default="gi7", help="port to isolate and mirror to")
    args = p.parse_args()
    a, b = args.a, args.b
    for n in (a, b):
        if not n.startswith("gi"):
            sys.exit(f"{RED}{n} is not a front port{OFF}")

    nic = backplane_nic()
    if not nic:
        sys.exit(f"{RED}cannot find the backplane NIC behind sw2363{OFF}")
    print(f"injecting on {nic} (te2, VLAN 1 untagged)")

    sw = t.Switch()
    try:
        sw.login()
    except t.SwitchError as e:
        sys.exit(f"{RED}cannot reach the switch:{OFF} {e}")
    print(f"connected to {sw.ip}  (tui {t.VERSION})")

    r = Restorer()
    try:
        for n in (a, b):
            row = next((x for x in sw.ports() if x["interfaceName"] == n), {})
            if row.get("adminState") != "1":
                r.add(f"{n} admin", lambda n=n: sw.set_port_admin(n, False))
                sw.set_port_admin(n, True)
            pe = next((x for x in sw.poe() if x["interfaceName"] == n), {})
            if pe.get("adminEnable") != "1":
                r.add(f"{n} PoE", lambda n=n: sw.set_poe(n, False))
                sw.set_poe(n, True)
        print(f"  {DIM}waiting for links ...{OFF}")
        for _ in range(30):
            up = [x["interfaceName"] for x in sw.ports()
                  if x["interfaceName"] in (a, b) and x["linkState"] == "1"]
            if len(up) == 2:
                break
            time.sleep(1)
        if len(up) != 2:
            fail("both links did not come up", f"up: {up}")
            return 1
        ok(f"links up on {a} ({t.panel_label(a)}) and {b} ({t.panel_label(b)})")

        test_poe_both(sw, a, b)
        if test_flood_baseline(sw, a, b, nic):
            test_vlan_isolation(sw, a, b, nic, r)
            test_mirroring(sw, a, b, nic, r)
    except KeyboardInterrupt:
        print(f"\n{YELLOW}interrupted{OFF}")
    except Exception:
        traceback.print_exc()
    finally:
        r.run()
        sw.logout()

    print(f"\n{'='*70}")
    print(f"{RESULTS['ok']} ok, {RESULTS['fail']} failed, "
          f"{RESULTS['skip']} skipped")
    for f in FAILURES:
        print(f"  {RED}FAILED{OFF} {f}")
    if FINDINGS:
        print(f"\n{CYAN}Learned from the hardware:{OFF}")
        for f in FINDINGS:
            print(f"  * {f}")
    return 1 if RESULTS["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
