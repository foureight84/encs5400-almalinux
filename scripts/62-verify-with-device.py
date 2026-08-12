#!/usr/bin/env python3
"""Verify the things that need a cable, using ONE connected device.

RUN THIS ON THE HYPERVISOR.

61-verify-on-switch.py proves the API tables behave. It cannot prove
anything about a link, because the switch ships with every front port shut
and nothing plugged in. This covers what one device can settle:

  * PoE detection, class and draw
  * DUPLEX ENUM RESOLUTION - the open question. Cisco's admin constants say
    HALF=2 FULL=3; duplexOperMode reads back 2=full 3=half. A real link
    partner is the only way to find out which is right.
  * forcing speed, and autonegotiation coming back
  * MAC learning, and whether "UP idle" detection actually works
  * a static MAC entry pinned to a real address

It finds the port itself: every gi port is enabled, and whichever one comes
up with a link is the one under test. Ports it enabled are shut again on
the way out, PoE is put back as it was, and every setting is restored.

    python3 62-verify-with-device.py              # find and test
    python3 62-verify-with-device.py --port gi3   # skip discovery
    python3 62-verify-with-device.py --poe-cycle  # also power-cycle the PD

--poe-cycle REBOOTS the attached device by cutting its power. It proves PoE
control works end to end; it is off by default because it is rude.
"""
import argparse
import importlib.util
import os
import sys
import time
import traceback
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
TUI = os.path.join(HERE, "..", "payload", "opt", "encs-host", "encs-switch-tui")

GREEN, RED, YELLOW, CYAN, DIM, OFF = (
    "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[0m")
RESULTS = {"ok": 0, "fail": 0, "skip": 0}
FAILURES = []
FINDINGS = []


def load_tui():
    loader = SourceFileLoader("encs_switch_tui", os.path.abspath(TUI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


t = load_tui()


def ok(what, detail=""):
    RESULTS["ok"] += 1
    print(f"  {GREEN}ok{OFF}   {what} {DIM}{detail}{OFF}")


def fail(what, detail=""):
    RESULTS["fail"] += 1
    FAILURES.append(f"{what} {detail}".strip())
    print(f"  {RED}FAIL{OFF} {what} {DIM}{detail}{OFF}")


def skip(what, why):
    RESULTS["skip"] += 1
    print(f"  {YELLOW}skip{OFF} {what} {DIM}({why}){OFF}")


def finding(what):
    """A fact learned from the hardware, not a pass/fail."""
    FINDINGS.append(what)
    print(f"  {CYAN}>>{OFF}   {what}")


def port_row(sw, ifname):
    return next((p for p in sw.ports() if p["interfaceName"] == ifname), {})


def poe_row(sw, ifname):
    return next((p for p in sw.poe() if p["interfaceName"] == ifname), {})


def wait_link(sw, ifname, want_up=True, timeout=25):
    """Autonegotiation is not instant; give the PHY time to settle."""
    for _ in range(timeout):
        st = port_row(sw, ifname).get("linkState")
        if (st == "1") == want_up:
            return True
        time.sleep(1)
    return False


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


# ============================================================== discovery
def discover(sw, r):
    """Enable every front port and see which one has something on it."""
    print(f"\n{'='*70}\nFINDING THE DEVICE\n{'='*70}")
    gi = [p["interfaceName"] for p in sw.ports()
          if p["interfaceName"].startswith("gi")]
    for n in gi:
        before = port_row(sw, n).get("adminState")
        if before != "1":
            r.add(f"{n} admin", lambda n=n: sw.set_port_admin(n, False))
            sw.set_port_admin(n, True)
        pe = poe_row(sw, n).get("adminEnable")
        if pe != "1":
            r.add(f"{n} PoE", lambda n=n: sw.set_poe(n, False))
            sw.set_poe(n, True)
    ok("enabled all gi ports and PoE", "will be shut again afterwards")

    print(f"  {DIM}waiting for a link ...{OFF}")
    found = []
    for _ in range(30):
        for p in sw.ports():
            n = p["interfaceName"]
            if n.startswith("gi") and p["linkState"] == "1" and n not in found:
                found.append(n)
        if found:
            break
        time.sleep(1)
    return found


# ================================================================== tests
def test_poe(sw, port, do_cycle, r):
    print(f"\n{'='*70}\nPoE on {port}\n{'='*70}")
    pe = poe_row(sw, port)
    det = pe.get("detectionStatus", "")
    label = t.POE_DETECT.get(det, det)
    if det == "3":
        ok("PoE is DELIVERING", f"status {det}")
    else:
        skip("PoE delivery", f"status is {label} - is the device PoE powered?")
    try:
        watts = int(pe.get("outputPower", "0")) / 1000
        limit = int(pe.get("powerLimit", "0")) / 1000
    except ValueError:
        watts = limit = 0.0
    finding(f"PoE class {pe.get('powerClassification', '?')}, "
            f"drawing {watts:.1f} W of a {limit:.1f} W limit")
    if watts > 0:
        ok("the switch reports a real power draw", f"{watts:.1f} W")
    else:
        skip("power draw", "reads 0 W")

    if not do_cycle:
        skip("PoE power cycle", "not requested; --poe-cycle reboots the device")
        return
    print(f"  {YELLOW}cutting PoE - the device will reboot{OFF}")
    if not t.ok(sw.set_poe(port, False)):
        fail("PoE off was rejected")
        return
    time.sleep(4)
    after = poe_row(sw, port).get("detectionStatus", "")
    if after != "3":
        ok("PoE off stopped delivery", f"status now {t.POE_DETECT.get(after, after)}")
    else:
        fail("PoE off did not stop delivery", "still reporting DELIVERING")
    sw.set_poe(port, True)
    for _ in range(30):
        if poe_row(sw, port).get("detectionStatus") == "3":
            ok("PoE back on, device powered again")
            break
        time.sleep(2)
    else:
        fail("device did not come back after PoE was restored",
             "check it by hand")
    wait_link(sw, port, True, 40)


def test_link_and_learning(sw, port):
    print(f"\n{'='*70}\nLINK AND MAC LEARNING on {port}\n{'='*70}")
    p = port_row(sw, port)
    finding(f"link {t.LINK.get(p.get('linkState'), '?')}, "
            f"speedOper {p.get('speedOper')} Mb/s, "
            f"duplexOperMode raw value {p.get('duplexOperMode')}")

    macs = [m for m in sw.macs() if m["interfaceName"] == port]
    if macs:
        ok(f"the switch learned {len(macs)} MAC(s) on {port}",
           ", ".join(m["MACAddress"] for m in macs[:3]))
    else:
        skip("MAC learning", "nothing learned yet - has the device sent a frame?")

    stats = next((s for s in sw.stats() if s["interfaceName"] == port), {})
    rx = stats.get("receiveUnicastPacketCount", "0")
    rxb = stats.get("receivePacketByteCount", "0")
    finding(f"rx {rx} unicast / {rxb} bytes, "
            f"tx {stats.get('transmitUnicastPacketCount','0')} unicast / "
            f"{stats.get('transmitPacketByteCount','0')} bytes, "
            f"errors {stats.get('packetErrorCount','?')}")
    # Bytes without unicast packets means the device is talking, but only
    # broadcast/multicast - which is exactly the case "UP idle" is meant to
    # flag, since unicast is what proves a useful conversation.
    if rxb not in ("", "0") and rx in ("", "0"):
        finding("the device IS sending frames, but no unicast - broadcast "
                "or multicast only (DHCP discovery, LLDP, mDNS and so on)")
    # The "UP idle" colouring in the Ports view keys off exactly this.
    if rx not in ("", "0"):
        ok("rx counter is moving", "so the port renders UP, not 'UP idle'")
    else:
        ok("rx counter is zero", "the port should render 'UP idle' in yellow")
    return macs[0]["MACAddress"] if macs else None


def test_duplex(sw, port, r):
    """Settle which duplexOperMode value means full.

    Cisco's admin constants (switch_interfaces.py:463) say HALF=2 FULL=3.
    docs/CONFIG.md, from hardware, says duplexOperMode 2=full 3=half. Both
    cannot be right for the same enum. A link partner decides it: any
    modern PD negotiates FULL duplex, so whatever value appears on an
    autonegotiated link is what "full" reads as.
    """
    print(f"\n{'='*70}\nDUPLEX ENUM on {port}\n{'='*70}")
    p = port_row(sw, port)
    if p.get("linkState") != "1":
        skip("duplex", "no link")
        return
    if (p.get("autoNegotiationAdminEnabled") or "1") != "1":
        skip("duplex", "autonegotiation is off; cannot infer from the link")
        return
    raw = p.get("duplexOperMode")
    speed = p.get("speedOper")
    ours = t.DUPLEX.get(raw, "?")
    finding(f"autonegotiated link reports duplexOperMode={raw} "
            f"at {speed} Mb/s, which this tool labels '{ours}'")
    # A gigabit link is full duplex by definition - 1000BASE-T has no
    # half-duplex mode in practice, so this is a safe inference.
    if speed == "1000":
        if ours == "full":
            ok("duplexOperMode 2=full is CORRECT",
               "a 1000BASE-T link is necessarily full duplex")
            finding("CONFIRMED: docs/CONFIG.md is right, duplexOperMode "
                    "2=full 3=half. Cisco's SWITCH_DUPLEX_* constants "
                    "describe duplexAdminMode, a DIFFERENT enum.")
        else:
            fail(f"duplexOperMode {raw} is labelled '{ours}'",
                 "but a 1000BASE-T link must be full duplex - the enum "
                 "in this tool is INVERTED")
            finding("ACTION: flip DUPLEX in encs-switch-tui and the enum "
                    "table in docs/CONFIG.md")
    else:
        skip("duplex confirmation",
             f"link is {speed} Mb/s; only 1000 lets us infer full safely")


def test_duplex_admin(sw, port, r):
    """Now settle duplexAdminMode, the enum we refused to guess.

    test_duplex confirmed the OPER side: duplexOperMode 2=full. That makes
    the two enums genuinely different rather than contradictory, so
    Cisco's SWITCH_DUPLEX_HALF=2 / SWITCH_DUPLEX_FULL=3 for the ADMIN side
    is now plausible - and testable. Force each admin value at 100 Mb/s
    with autonegotiation off and read back what the link actually came up
    as, using the oper enum we just proved.
    """
    print(f"\n{'='*70}\nDUPLEX ADMIN ENUM on {port}\n{'='*70}")
    if port_row(sw, port).get("linkState") != "1":
        skip("duplex admin", "no link")
        return
    r.add(f"{port} autonegotiation",
          lambda: sw.set_port(port, {"autoNegotiationAdminEnabled": "1"}))

    results = {}
    for admin_val, cisco_says in (("3", "full"), ("2", "half")):
        if not t.ok(sw.set_port(port, {
                "autoNegotiationAdminEnabled": "2", "speedAdmin": "100",
                "duplexAdminMode": admin_val})):
            fail(f"duplexAdminMode={admin_val} was rejected")
            continue
        if not wait_link(sw, port, True, 30):
            skip(f"duplexAdminMode={admin_val}",
                 "link did not come up; the far end may not support it")
            continue
        time.sleep(2)
        raw = port_row(sw, port).get("duplexOperMode")
        got = t.DUPLEX.get(raw, "?")
        results[admin_val] = got
        finding(f"duplexAdminMode={admin_val} (Cisco calls it {cisco_says}) "
                f"-> link came up duplexOperMode={raw} = '{got}'")

    sw.set_port(port, {"autoNegotiationAdminEnabled": "1"})
    wait_link(sw, port, True, 30)

    # The verdict hinges on admin=3. If forcing it yields a full-duplex
    # link, Cisco's FULL=3 is proven and forcing full is safe to offer.
    #
    # admin=2 is a weaker signal on purpose. Most modern PDs do not support
    # half duplex at all, so a partner that refuses to come up half - or
    # comes up reporting n/a - is evidence about the PARTNER, not about the
    # enum. Treating that as "the constants are wrong" was the first
    # version of this check and it was simply incorrect.
    if results.get("3") == "full":
        ok("duplexAdminMode 3 = full CONFIRMED",
           "forced full duplex and the link came up full")
        if results.get("2") == "half":
            ok("duplexAdminMode 2 = half CONFIRMED", "both values verified")
            finding("RESOLVED: admin 2=half 3=full; oper 2=full 3=half. "
                    "The two enums are inverted relative to each other, "
                    "which is why Cisco's constants looked wrong.")
        else:
            finding(f"duplexAdminMode 2 gave '{results.get('2', 'no link')}' "
                    f"rather than half - almost certainly because this "
                    f"device does not do half duplex. Cisco's HALF=2 stands "
                    f"unrefuted but UNVERIFIED; only 'full' is proven.")
    elif results:
        fail("forcing duplexAdminMode=3 did not produce a full-duplex link",
             f"observed {results}")
        finding(f"duplexAdminMode mapping observed: {results} - do NOT "
                f"implement forcing until this is understood")
    else:
        skip("duplex admin enum", "neither value produced a usable link")


def test_speed(sw, port, r):
    print(f"\n{'='*70}\nSPEED FORCING on {port}\n{'='*70}")
    p = port_row(sw, port)
    if p.get("linkState") != "1":
        skip("speed forcing", "no link")
        return
    orig_speed = p.get("speedOper")
    r.add(f"{port} autonegotiation",
          lambda: sw.set_port(port, {"autoNegotiationAdminEnabled": "1"}))
    if not t.ok(sw.set_port(port, {"autoNegotiationAdminEnabled": "2",
                                   "speedAdmin": "100"})):
        fail("forcing 100 Mb/s was rejected")
        return
    if not wait_link(sw, port, True, 30):
        fail("the link did not come back at a forced 100 Mb/s",
             "restoring autonegotiation")
        return
    now = port_row(sw, port).get("speedOper")
    if now == "100":
        ok("forced speed took effect", f"{orig_speed} -> {now} Mb/s")
    else:
        fail(f"forced 100 Mb/s but the port reports {now}",
             "speedAdmin may not be the right element")
    # Put autonegotiation back and confirm it recovers.
    sw.set_port(port, {"autoNegotiationAdminEnabled": "1"})
    if wait_link(sw, port, True, 30):
        back = port_row(sw, port).get("speedOper")
        if back == orig_speed:
            ok("autonegotiation restored the original speed", f"{back} Mb/s")
        else:
            fail(f"after restoring autoneg the port is {back} Mb/s",
                 f"was {orig_speed}")
    else:
        fail("the link did not return after restoring autonegotiation",
             "CHECK THE PORT BY HAND")


def test_poe_limit(sw, port, r):
    """Set the per-port power budget BELOW the device's draw.

    The only field in PoEPSEInterfaceList this firmware reports that
    set_poe_port can meaningfully write. If the limit is enforced, the
    switch should stop delivering to a device asking for more than it -
    which also proves the limit is a real policy rather than a display
    value. The device drops and comes back when the limit is restored.
    """
    print(f"\n{'='*70}\nPoE POWER LIMIT ENFORCEMENT on {port}\n{'='*70}")
    pe = poe_row(sw, port)
    if pe.get("detectionStatus") != "3":
        skip("power limit", "the device is not currently being powered")
        return
    try:
        draw = int(pe.get("outputPower", "0"))
        limit = int(pe.get("powerLimit", "0"))
    except ValueError:
        skip("power limit", "cannot read the current draw")
        return
    if draw <= 0:
        skip("power limit", "no measurable draw to undercut")
        return
    finding(f"before: drawing {draw/1000:.1f} W against a "
            f"{limit/1000:.1f} W limit")

    # Well under the measured draw, so there is no ambiguity about whether
    # it should trip.
    tight = max(1000, draw // 2)
    r.add("PoE power limit",
          lambda: sw.set_poe_port(port, {"powerLimit": str(limit)}))
    if not t.ok(sw.set_poe_port(port, {"powerLimit": str(tight)})):
        fail("setting a lower power limit was rejected",
             "the firmware may not accept powerLimit below a class minimum")
        return
    now = poe_row(sw, port).get("powerLimit")
    if now == str(tight):
        ok("power limit written and read back", f"{limit} -> {now} mW")
    else:
        fail("power limit did not read back", f"asked {tight}, got {now}")
        return

    print(f"  {DIM}watching for the switch to cut power ...{OFF}")
    for _ in range(15):
        time.sleep(2)
        det = poe_row(sw, port).get("detectionStatus")
        if det != "3":
            ok("the switch stopped delivering over the limit",
               f"status now {t.POE_DETECT.get(det, det)}")
            break
    else:
        # Not a bug on its own: many PSEs apply the budget at
        # classification, so a device already up keeps its power. Settle
        # which it is by forcing a re-detection - cut PoE and bring it back
        # with the tight limit still in place. If the limit is a real
        # policy the device will be refused power; if it is cosmetic it
        # will come straight back at 4 W against a 2 W cap.
        finding(f"still delivering {draw/1000:.1f} W at a "
                f"{tight/1000:.1f} W limit - forcing a re-detection")
        sw.set_poe(port, False)
        time.sleep(5)
        sw.set_poe(port, True)
        redetected = None
        for _ in range(20):
            time.sleep(2)
            det = poe_row(sw, port).get("detectionStatus")
            if det in ("3", "4"):
                redetected = det
                break
        if redetected == "3":
            after = poe_row(sw, port)
            ok("power limit is NOT enforced, even at detection",
               f"device re-powered at {int(after.get('outputPower',0))/1000:.1f} W "
               f"under a {tight/1000:.1f} W limit")
            finding("powerLimit is WRITABLE AND READABLE BUT COSMETIC on "
                    "this firmware - it does not gate delivery. Do not rely "
                    "on it to cap a port.")
        elif redetected == "4":
            ok("power limit IS enforced at detection",
               "the device was refused power and reports a fault")
            finding("powerLimit gates delivery at classification only - an "
                    "already-powered device keeps its power until it "
                    "re-detects.")
        else:
            skip("power limit at re-detection",
                 f"status settled at "
                 f"{t.POE_DETECT.get(poe_row(sw, port).get('detectionStatus'), '?')}")

    sw.set_poe_port(port, {"powerLimit": str(limit)})
    print(f"  {DIM}restoring the limit and waiting for the device ...{OFF}")
    for _ in range(20):
        time.sleep(2)
        if poe_row(sw, port).get("detectionStatus") == "3":
            ok("device powered again after the limit was restored")
            break
    else:
        fail("device did not come back after restoring the limit",
             "CHECK IT BY HAND")
    wait_link(sw, port, True, 40)


def test_poe_settings(sw, port, r):
    """Exercise set_poe_port, which nothing has ever called.

    Priority only. The power LIMIT would be the more interesting test -
    setting it below the measured draw should stop delivery - but that
    drops the attached device, so it belongs with --poe-cycle rather than
    in the default run.
    """
    print(f"\n{'='*70}\nPoE PER-PORT SETTINGS on {port}\n{'='*70}")
    pe = poe_row(sw, port)
    before = pe.get("powerPriority")
    if before is None:
        skip("PoE priority", "no powerPriority field on this firmware")
        finding(f"PoE entry fields: {', '.join(sorted(pe))}")
        return
    r.add("PoE priority",
          lambda: sw.set_poe_port(port, {"powerPriority": before}))
    want = "3" if before != "3" else "2"
    if not t.ok(sw.set_poe_port(port, {"powerPriority": want})):
        fail("setting PoE priority was rejected")
        return
    now = poe_row(sw, port).get("powerPriority")
    if now == want:
        ok("PoE priority written and read back", f"{before} -> {now}")
    else:
        fail("PoE priority did not read back", f"asked {want}, got {now}")
    # Power must keep flowing - changing priority should not interrupt a
    # device that is already up.
    if poe_row(sw, port).get("detectionStatus") == "3":
        ok("the device kept its power through the change")
    else:
        fail("changing PoE priority interrupted delivery", "unexpected")


def test_vlan_on_live_link(sw, port, r):
    """Move a LIVE port into a new VLAN and prove it keeps forwarding.

    Every VLAN test so far ran against a dead port, where "it read back"
    is all you can say. With a link up and a device sending LLDP every few
    seconds, the receive counter tells you the port is still passing
    frames after the membership change.
    """
    print(f"\n{'='*70}\nVLAN MEMBERSHIP ON A LIVE LINK ({port})\n{'='*70}")
    if port_row(sw, port).get("linkState") != "1":
        skip("live VLAN membership", "no link")
        return
    VID = "3998"
    if any(v["VLANID"] == VID for v in sw.vlans()):
        skip("live VLAN membership", f"VLAN {VID} already exists")
        return

    def rx_bytes():
        s = next((x for x in sw.stats() if x["interfaceName"] == port), {})
        return int(s.get("receivePacketByteCount") or 0)

    cur = next((row for row in sw.vlan_interfaces()
                if row.get("interfaceName") == port), {})
    orig_mode = cur.get("switchportModeAdmin")
    orig_pvid = cur.get("accessPVID")
    r.add(f"VLAN {VID}", lambda: sw.vlan(VID, False))
    r.add(f"{port} VLAN membership",
          lambda: sw.set_vlan_interface(port, mode=orig_mode,
                                        access_pvid=orig_pvid))
    if not t.ok(sw.vlan(VID, True, "livetest")):
        fail(f"creating VLAN {VID} was rejected")
        return
    before = rx_bytes()
    if not t.ok(sw.set_vlan_interface(port, mode="11", access_pvid=VID)):
        fail("moving the live port to an access VLAN was rejected")
        return
    ok(f"moved {port} into VLAN {VID} as an access port")

    if not wait_link(sw, port, True, 15):
        fail("the link dropped when the VLAN changed", "unexpected")
        return
    ok("the link survived the VLAN change")

    membership = next((v for v in sw.vlans() if v["VLANID"] == VID), {})
    if port in (membership.get("untaggedPorts") or ""):
        ok(f"{port} shows as untagged in VLAN {VID}",
           membership.get("untaggedPorts"))
    else:
        fail(f"{port} is not listed in VLAN {VID}'s membership",
             f"untagged={membership.get('untaggedPorts')!r}")

    # The device emits LLDP/CDP every 30s or so; give it a window.
    print(f"  {DIM}watching the receive counter for 40s ...{OFF}")
    for _ in range(20):
        time.sleep(2)
        if rx_bytes() > before:
            ok("frames still arriving after the VLAN change",
               f"{before} -> {rx_bytes()} bytes")
            return
    skip("forwarding after the VLAN change",
         "no new frames in 40s - the device may only advertise on a timer")


def test_static_mac(sw, port, mac, r):
    print(f"\n{'='*70}\nSTATIC MAC for a real device\n{'='*70}")
    if not mac:
        skip("static MAC", "no MAC learned to pin")
        return
    vid = "1"
    r.add("static MAC", lambda: sw.del_static_mac(vid, mac))
    if not t.ok(sw.set_static_mac(vid, mac, port)):
        fail("pinning a learned MAC was rejected")
        return
    hit = [e for e in sw.static_macs()
           if e.get("MACAddress", "").lower() == mac.lower()]
    if hit:
        ok("pinned the device's real MAC", f"{mac} -> {port}")
        finding(f"static entry status "
                f"{t.MAC_STATUS.get(hit[0].get('addressStatus',''), '?')}")
    else:
        fail("static MAC did not read back", mac)


# =================================================================== main
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", help="skip discovery and use this port")
    p.add_argument("--poe-cycle", action="store_true",
                   help="power-cycle the attached device to prove PoE control")
    p.add_argument("--poe-limit", action="store_true",
                   help="set the port power budget below the device's draw "
                        "and confirm the switch cuts power. Drops the device "
                        "until the limit is restored.")
    args = p.parse_args()

    sw = t.Switch()
    try:
        sw.login()
    except t.SwitchError as e:
        sys.exit(f"{RED}cannot reach the switch:{OFF} {e}")
    print(f"connected to {sw.ip}  (tui {t.VERSION})")

    r = Restorer()
    try:
        if args.port:
            port = args.port
            if not port.startswith("gi"):
                sys.exit(f"{RED}{port} is not a front port{OFF}")
            if port_row(sw, port).get("adminState") != "1":
                r.add(f"{port} admin", lambda: sw.set_port_admin(port, False))
                sw.set_port_admin(port, True)
            if poe_row(sw, port).get("adminEnable") != "1":
                r.add(f"{port} PoE", lambda: sw.set_poe(port, False))
                sw.set_poe(port, True)
            wait_link(sw, port, True, 30)
            found = [port]
        else:
            found = discover(sw, r)

        if not found:
            fail("no link came up on any front port",
                 "is the cable in a GE1/x jack? GE0/0, GE0/1 and MGMT are "
                 "host NICs, not switch ports")
            return 1
        if len(found) > 1:
            finding(f"links on {', '.join(found)} - testing {found[0]}")
        port = found[0]
        ok(f"device found on {port}", f"front panel {t.panel_label(port)}")

        test_poe(sw, port, args.poe_cycle, r)
        mac = test_link_and_learning(sw, port)
        test_duplex(sw, port, r)
        test_duplex_admin(sw, port, r)
        test_speed(sw, port, r)
        test_poe_settings(sw, port, r)
        if args.poe_limit:
            test_poe_limit(sw, port, r)
        test_vlan_on_live_link(sw, port, r)
        test_static_mac(sw, port, mac, r)
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
