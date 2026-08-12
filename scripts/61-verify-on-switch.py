#!/usr/bin/env python3
"""Verify encs-switch-tui against a REAL ENCS 5412 switch.

RUN THIS ON THE HYPERVISOR. It talks to 169.254.1.0 over VLAN 2363, which
only the host can reach.

scripts/60-test-tui.py proves the code is internally consistent against
fixtures written from Cisco's templates. It cannot prove the firmware
agrees. This does: it reads every table the TUI uses and reports what the
switch actually returns, then optionally performs reversible writes and
checks they read back.

    python3 61-verify-on-switch.py                    # read-only probe
    python3 61-verify-on-switch.py --write gi5        # + reversible writes
    python3 61-verify-on-switch.py --write gi5 --config   # + save and check
    python3 61-verify-on-switch.py --replay           # + POST them back

--replay is separated from --config deliberately. On 2026-08-11 replaying a
freshly saved config dropped the management VLAN part-way through and the
chassis needed a physical power cycle to come back. save_config is a pure
read and always safe; posting twenty files at a live switch is not.

WHAT THIS DELIBERATELY WILL NOT DO
----------------------------------
Enable spanning tree globally.  te1 and te2 are both X710 ports to the
same host. To the switch that can look like a loop, and STP is entitled to
block one of them. If it blocks te2 the management VLAN goes with it, and
because the ASIC has no flash the only recovery is PHYSICAL AC REMOVAL for
~30 seconds - a CIMC power-off is not enough. Do that one with hands on the
box, not from a script over the link it would cut.

Enable 802.1X globally. Same shape of risk: ports that cannot authenticate
fail closed.

Touch te1-te4, the management VLAN, or any port with a live link.

Every write test records the original value, restores it, and verifies the
restore. If the script dies mid-test it restores on the way out.
"""
import argparse
import importlib.util
import os
import sys
import traceback
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
TUI = os.path.join(HERE, "..", "payload", "opt", "encs-host", "encs-switch-tui")

GREEN, RED, YELLOW, DIM, OFF = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m")

RESULTS = {"ok": 0, "fail": 0, "skip": 0}
FAILURES = []


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


# ===================================================== read-only probing
# (table, tag, "what the TUI expects to find"). The tag is the element the
# client iterates - getting it wrong is exactly the bug that made the
# RADIUS view come back empty in testing, so every one is checked here.
PROBE = [
    ("Standard802_3List", "Entry", ["interfaceName", "adminState",
                                    "linkState", "LAGID"]),
    ("LAGList", "LAGEntry", ["interfaceName"]),
    ("StatisticsList", "InterfaceStatisticsEntry", ["interfaceName"]),
    ("ForwardingTable", "Entry", ["MACAddress", "interfaceName"]),
    ("PoEPSEInterfaceList", "Interface", ["interfaceName", "adminEnable"]),
    ("VLANInterfaceMembershipTable", "Entry", ["VLANID"]),
    ("VLANInterfaceISList", "Entry", ["interfaceName",
                                      "switchportModeAdmin"]),
    ("VLANList", "VLAN", ["VLANID"]),
    # --- everything added in 0.1.0, none of it seen on hardware before
    ("SpanningTreeGlobalParam", None, ["enabled", "STPOperationMode"]),
    ("STP", "InterfaceEntry", ["interfaceName", "STPEnabled", "pathCost",
                               "portState", "portRole"]),
    ("RSTP", "InterfaceEntry", []),
    ("MSTPGlobalSetting", None, []),
    ("StormControlTable", "Entry", ["interfaceName", "broadcastRateValue",
                                    "broadcastUnitType"]),
    ("SpanSourceTable", "Entry", []),
    ("SpanDestinationTable", "Entry", []),
    ("ForwardingStaticTable", "Entry", []),
    ("ForwardingGlobalSetting", None, ["agingInterval"]),
    ("LLDPGlobalSetting", None, ["LLDPEnabled"]),
    ("LLDPInterfaceList", "InterfaceEntry", ["interfaceName", "portState"]),
    ("CDPInterfaceList", "Entry", ["interfaceName", "enbl"]),
    ("LACPGlobalSetting", None, ["LACPSystemPriority"]),
    ("LACPPortList", "Entry", ["interfaceName", "actorPortPriority"]),
    ("PrivateVLANTable", "Entry", []),
    ("PrivateVLANAssociationTable", "Entry", []),
    ("PrivateVLANHostPortTable", "Entry", []),
    ("PrivateVLANPromiscuousPortTable", "Entry", []),
    ("ACLList", "ACLEntry", []),
    ("ACEList", "Entry", []),
    ("ACLBindingList", "ACLBindingEntry", []),
    ("QoSSettingGlobalParam", None, ["QoSMode"]),
    ("CoSSetting", "Interface", ["interfaceName", "CoS"]),
    ("CoSToQueueMappingList", "CoSMappingEntry", ["CoS", "queueNumber"]),
    ("AggregatePolicerList", "Entry", []),
    ("ClassMapList", "ClassMapEntry", []),
    ("PolicyMapList", "PolicyMapEntry", []),
    ("DSCPMapping", "DSCPEntry", []),
    ("Standard_802_1xGlobalSetting", None, ["enabled"]),   # verified 2026-08-11
    ("Standard_802_1xInterfaceList", "Entry", ["interfaceName"]),
    ("RadiusServerList", "RadiusServer", []),
    ("RadiusDefaultParam", None, []),
    ("MulticastGlobalSetting", None, []),
    ("IGMPMLDSnoopVLANList", "Entry", []),
    ("IGMPMLDSnoopRouterPortList", "Entry", []),
    ("ARPList", "ARPEntry", []),
    ("ARPGlobalSetting", None, ["timeout"]),
    ("IPv4GlobalSetting", None, ["unicastRoutingEnable"]),
    ("IPv4RouteList", "Entry", []),
    ("IPv4GatewayList", "GWEntry", []),
    ("IPv4InterfaceList", "Entry", []),
]


def probe(sw):
    """Read every table. Reports what the firmware really returns.

    An empty table is not a failure - most of these are unconfigured on a
    fresh switch. A table that returns rows whose element names differ from
    what the client expects IS a failure, and is the single most likely way
    this release is wrong.
    """
    print(f"\n{'='*70}\nREAD-ONLY PROBE - no writes, safe to run any time\n{'='*70}")
    unknown = []
    for table, tag, expect in PROBE:
        try:
            body = sw.get(table)
        except t.SwitchError as e:
            fail(f"{table}: GET failed", str(e)[:60])
            continue
        try:
            rows = sw._any(body, tag or table,
                           None if tag is None else table)
        except t.SwitchError as e:
            fail(f"{table}: unparseable", str(e)[:60])
            continue
        if not rows:
            # Distinguish "table exists but is empty" from "no such table".
            present = f"<{table}" in body
            if present:
                ok(f"{table}", f"exists, 0 rows (tag {tag or table})")
            else:
                skip(f"{table}", "table not present in the reply")
                unknown.append(table)
            continue
        seen = set()
        for r in rows:
            seen.update(r)
        missing = [f for f in expect if f not in seen]
        if missing:
            fail(f"{table}: {len(rows)} rows but missing {missing}",
                 f"got {sorted(seen)}")
        else:
            ok(f"{table}", f"{len(rows)} rows, {len(seen)} fields")
        # Surface fields we never modelled - they may matter.
        extra = sorted(seen - set(expect))
        if extra and os.environ.get("VERBOSE"):
            print(f"       {DIM}fields: {', '.join(extra)}{OFF}")
    if unknown:
        print(f"\n{YELLOW}Tables absent on this firmware:{OFF} "
              f"{', '.join(unknown)}")
        print(f"{DIM}The views using them will show their 'empty' message. "
              f"Not fatal, but worth recording in docs/FINDINGS.md.{OFF}")


# ======================================================== write testing
class Restorer:
    """Undo stack. Runs in reverse even if a test raises."""

    def __init__(self, sw):
        self.sw, self.undo = sw, []

    def add(self, what, fn):
        self.undo.append((what, fn))

    def run(self):
        print(f"\n{'-'*70}\nRestoring original state\n{'-'*70}")
        for what, fn in reversed(self.undo):
            try:
                if t.ok(fn()):
                    ok(f"restored {what}")
                else:
                    fail(f"restore of {what} was REJECTED",
                         "check this by hand")
            except Exception as e:
                fail(f"restore of {what} raised", str(e)[:60])
        self.undo = []


def check_write(sw, what, write, verify):
    """Write, read back, and confirm the switch really took it.

    A statusString of OK is necessary but not sufficient - several of these
    tables accept a write and ignore it. Only the read-back counts.
    """
    try:
        body = write()
    except t.SwitchError as e:
        fail(f"{what}: write raised", str(e)[:60])
        return False
    if not t.ok(body):
        fail(f"{what}: switch rejected the write",
             body.strip()[:80].replace("\n", " "))
        return False
    try:
        if verify():
            ok(f"{what}", "written and read back")
            return True
        fail(f"{what}: accepted but did NOT read back",
             "the element name is probably wrong")
        return False
    except t.SwitchError as e:
        fail(f"{what}: verify read failed", str(e)[:60])
        return False


def port_field(sw, table, ifname, field, tag="Entry"):
    for r in sw.read(table, tag=tag):
        if r.get("interfaceName") == ifname:
            return r.get(field)
    return None


def write_tests(sw, port, do_config, do_replay):
    print(f"\n{'='*70}\nWRITE TESTS on {port} - every change is reverted\n{'='*70}")
    r = Restorer(sw)
    try:
        _write_tests(sw, port, r, do_config, do_replay)
    finally:
        r.run()


def _write_tests(sw, port, r, do_config, do_replay):
    # ---- port description. The safest possible write: cosmetic, and it
    # proves Standard802_3List accepts a field beyond adminState.
    before = port_field(sw, "Standard802_3List", port, "interfaceDescription")
    r.add("port description",
          lambda: sw.set_port(port, {"interfaceDescription": before or ""}))
    check_write(
        sw, "port description",
        lambda: sw.set_port(port, {"interfaceDescription": "encs-verify"}),
        lambda: port_field(sw, "Standard802_3List", port,
                           "interfaceDescription") == "encs-verify")

    # ---- storm control
    before = port_field(sw, "StormControlTable", port, "broadcastRateValue")
    if before is None:
        skip("storm control", "no StormControlTable on this firmware")
    else:
        unit = port_field(sw, "StormControlTable", port,
                          "broadcastUnitType") or t.STORM_LEVEL
        r.add("storm control",
              lambda: sw.set_storm(port, "broadcast", before or "0", unit))
        check_write(
            sw, "storm control broadcast",
            lambda: sw.set_storm(port, "broadcast", "42", t.STORM_LEVEL),
            lambda: port_field(sw, "StormControlTable", port,
                               "broadcastRateValue") == "42")

    # ---- per-port STP settings. Writing these does NOT start spanning
    # tree; it only sets what the port would use if STP were running. The
    # global enable is the dangerous one and is not done here.
    before = port_field(sw, "STP", port, "pathCost", tag="InterfaceEntry")
    if before is None:
        skip("per-port STP", "no STP interface table")
    else:
        r.add("STP path cost",
              lambda: sw.set_stp_port(port, {"pathCost": before}))
        check_write(
            sw, "STP path cost",
            lambda: sw.set_stp_port(port, {"pathCost": "12345"}),
            lambda: port_field(sw, "STP", port, "pathCost",
                               tag="InterfaceEntry") == "12345")

    # ---- LLDP per port
    before = port_field(sw, "LLDPInterfaceList", port, "portState",
                        tag="InterfaceEntry")
    if before is None:
        skip("LLDP per port", "no LLDPInterfaceList")
    else:
        r.add("LLDP port state",
              lambda: sw.set_lldp_port(port, before))
        want = t.LLDP_TX if before != t.LLDP_TX else t.LLDP_RX
        check_write(
            sw, "LLDP port state",
            lambda: sw.set_lldp_port(port, want),
            lambda: port_field(sw, "LLDPInterfaceList", port, "portState",
                               tag="InterfaceEntry") == want)

    # ---- LACP port priority
    before = port_field(sw, "LACPPortList", port, "actorPortPriority")
    if before is None:
        skip("LACP port priority", "no LACPPortList")
    else:
        r.add("LACP port priority",
              lambda: sw.set_lacp_port(port, {"actorPortPriority": before}))
        check_write(
            sw, "LACP port priority",
            lambda: sw.set_lacp_port(port, {"actorPortPriority": "7"}),
            lambda: port_field(sw, "LACPPortList", port,
                               "actorPortPriority") == "7")

    # ---- a scratch VLAN, its name, and this port's membership in it.
    # 3999 to stay clear of the 2350-2449 range the firmware reserves.
    VID = "3999"

    def vlan_exists():
        return any(v["VLANID"] == VID for v in sw.vlans())

    if vlan_exists():
        skip("scratch VLAN", f"VLAN {VID} already exists - not touching it")
    else:
        r.add(f"VLAN {VID}", lambda: sw.vlan(VID, False))
        if check_write(sw, f"create VLAN {VID} with a name",
                       lambda: sw.vlan(VID, True, "encs-verify"),
                       vlan_exists):
            # Does the name actually stick? This is what 20-vlans.xml
            # now replays, and it was silently dropped before.
            name = next((v.get("VLANName") for v in sw.vlans()
                         if v["VLANID"] == VID), None)
            if name == "encs-verify":
                ok("VLAN name", "stored and read back")
            else:
                fail("VLAN name was not stored", f"read back {name!r}")

            # Membership, merged rather than replacing - the bug that
            # would silently drop the port out of its other VLANs.
            cur = next((row for row in sw.vlan_interfaces()
                        if row.get("interfaceName") == port), {})
            orig_tagged = cur.get("generalTaggedVLANs", "")
            orig_mode = cur.get("switchportModeAdmin")

            def restore_membership():
                # If the port had NO tagged VLANs, the restore is a clear,
                # not a set-to-empty. Setting an empty list here is what
                # hung the switch on 2026-08-11 - twice, at this exact
                # step - and needed AC removal to recover.
                if orig_tagged:
                    return sw.set_vlan_interface(port, mode=orig_mode,
                                                 tagged=orig_tagged)
                if orig_mode:
                    sw.set_vlan_interface(port, mode=orig_mode)
                return sw.clear_vlan_interface(port, "generalTaggedVLANs")
            r.add(f"{port} VLAN membership", restore_membership)
            merged = t.vlan_list_add(orig_tagged, VID)
            check_write(
                sw, f"{port} tagged in VLAN {VID}",
                lambda: sw.set_vlan_interface(port, mode="10", tagged=merged),
                lambda: VID in t.vlan_list_format(
                    t.vlan_list_parse(next(
                        (row.get("generalTaggedVLANs", "")
                         for row in sw.vlan_interfaces()
                         if row.get("interfaceName") == port), ""))).split(","))
            # ...and that the VLANs it was already in survived.
            now = t.vlan_list_parse(next(
                (row.get("generalTaggedVLANs", "")
                 for row in sw.vlan_interfaces()
                 if row.get("interfaceName") == port), ""))
            lost = t.vlan_list_parse(orig_tagged) - now
            if lost:
                fail("adding a VLAN DROPPED other memberships",
                     f"lost {sorted(lost)}")
            else:
                ok("other VLAN memberships survived", f"now {sorted(now)}")

    # ---- static MAC
    if any(True for _ in [0]):
        MAC = "00:00:5e:00:53:01"        # RFC 5737-style documentation MAC
        r.add("static MAC", lambda: sw.del_static_mac("1", MAC))
        check_write(
            sw, "static MAC entry",
            lambda: sw.set_static_mac("1", MAC, port),
            lambda: any(e.get("MACAddress", "").lower() == MAC
                        for e in sw.static_macs()))

    # ---- an ACL, a rule in it, and a binding to this port
    NAME = "encsverify"
    if any(a.get("ACLName") == NAME for a in sw.acls()):
        skip("ACL", f"{NAME} already exists")
    else:
        r.add(f"ACL {NAME}",
              lambda: sw.acl(NAME, t.ACL_MAC, create=False))
        if check_write(sw, "create a MAC ACL",
                       lambda: sw.acl(NAME, t.ACL_MAC),
                       lambda: any(a.get("ACLName") == NAME
                                   for a in sw.acls())):
            params = t.el("MACParameters",
                          t.mac_ace_param("source", "any") + "\n"
                          + t.mac_ace_param("dest", "any"), 12)
            check_write(
                sw, "add a rule to the ACL",
                lambda: sw.set_ace(NAME, "10", t.ACL_DENY, params),
                lambda: any(a.get("ACLName") == NAME for a in sw.aces()))

    # ---- QoS: per-port CoS
    before = port_field(sw, "CoSSetting", port, "CoS", tag="Interface")
    if before is None:
        skip("port CoS", "no CoSSetting table")
    else:
        r.add("port CoS", lambda: sw.set_port_cos(port, before))
        check_write(
            sw, "port default CoS",
            lambda: sw.set_port_cos(port, "5"),
            lambda: port_field(sw, "CoSSetting", port, "CoS",
                               tag="Interface") == "5")

    # ---- MAC aging
    fg = sw.forwarding_global()
    before = fg.get("agingInterval")
    if not before:
        skip("MAC aging", "no ForwardingGlobalSetting")
    else:
        r.add("MAC aging", lambda: sw.set_mac_aging(before))
        check_write(
            sw, "MAC aging interval",
            lambda: sw.set_mac_aging("400"),
            lambda: sw.forwarding_global().get("agingInterval") == "400")

    if do_config:
        config_test(sw, do_replay)


def config_test(sw, do_replay):
    """save_config against the real switch, and optionally replay it.

    Replay is NOT part of --config, and that separation was learned the
    hard way: on 2026-08-11 replaying a freshly saved config took the
    management VLAN down mid-run and the switch needed a physical power
    cycle. save_config is a pure read and is always safe; posting ~20 files
    back at a live switch is not, so it needs its own flag.
    """
    import tempfile
    import xml.etree.ElementTree as ET
    print(f"\n{'='*70}\nCONFIG SAVE{' AND REPLAY' if do_replay else ''}\n"
          f"{'='*70}")
    with tempfile.TemporaryDirectory() as d:
        try:
            files = t.save_config(sw, d)
        except Exception as e:
            fail("save_config raised", str(e)[:80])
            return
        ok("save_config", f"{len(files)} files")

        # Static checks that cost nothing and catch the dangerous shapes
        # without posting anything at all.
        for f in sorted(files):
            name = os.path.basename(f)
            body = open(f).read()
            print(f"       {DIM}{name}  {os.path.getsize(f)}b{OFF}")
            try:
                root = ET.fromstring(body)
            except ET.ParseError as e:
                fail(f"{name} is malformed XML", str(e)[:60])
                continue
            if len([c for c in root if c.tag != "version"]) != 1:
                fail(f"{name} holds more than one table",
                     "apply posts one file as one request")
            hit = [te for te in ("te1", "te2", "te3", "te4")
                   if f"<interfaceName>{te}</interfaceName>" in body]
            if hit:
                fail(f"{name} writes to backplane port(s) {hit}",
                     "this would break management on every cold boot")

        if not do_replay:
            print(f"\n{DIM}Not replaying. Add --replay to post these back "
                  f"at the switch.{OFF}")
            return

        print(f"\n{YELLOW}Replaying {len(files)} files at a live switch. "
              f"If the management VLAN drops, recovery is a physical power "
              f"cycle.{OFF}")
        for name, good in t.apply_config(sw, d):
            if good:
                ok(f"replay {name}")
            else:
                fail(f"replay {name} was REJECTED",
                     "this file would fail silently after a power cycle")


# ===================================================================== main
def main():
    p = argparse.ArgumentParser(
        description="Verify encs-switch-tui against real hardware.")
    p.add_argument("--write", metavar="PORT",
                   help="also run reversible write tests on this port "
                        "(a gi port with nothing plugged into it)")
    p.add_argument("--config", action="store_true",
                   help="also test save_config (a pure read) and check the "
                        "files it produces")
    p.add_argument("--replay", action="store_true",
                   help="POST the saved files back at the switch. Risky: a "
                        "bad file can drop the management VLAN, and recovery "
                        "is a physical power cycle. Implies --config.")
    p.add_argument("--force", action="store_true",
                   help="allow write tests on a port that has a live link")
    args = p.parse_args()

    sw = t.Switch()
    try:
        sw.login()
    except t.SwitchError as e:
        sys.exit(f"{RED}cannot reach the switch:{OFF} {e}\n\n"
                 f"  Run this ON THE HYPERVISOR. Checks:\n"
                 f"    ip -br addr show sw2363   -> expect 169.254.1.1/16\n"
                 f"    ping -c2 {t.SW_IP}\n")
    print(f"connected to {sw.ip}  (tui {t.VERSION})")

    try:
        probe(sw)

        if args.write:
            port = args.write
            ports = {r["interfaceName"]: r for r in sw.ports()}
            if port not in ports:
                sys.exit(f"{RED}{port} is not a port on this switch.{OFF} "
                         f"Have: {', '.join(sorted(ports))}")
            if not port.startswith("gi"):
                sys.exit(f"{RED}refusing to write to {port}.{OFF} Only the "
                         f"gi front ports are safe; te1-te4 are the "
                         f"backplane links carrying this session.")
            if ports[port]["linkState"] == "1" and not args.force:
                sys.exit(f"{RED}{port} has a live link.{OFF} Pick a port "
                         f"with nothing plugged in, or pass --force if you "
                         f"are sure it carries nothing you care about.")
            write_tests(sw, port, args.config or args.replay,
                        args.replay)
        elif args.config or args.replay:
            config_test(sw, args.replay)
        else:
            print(f"\n{DIM}Read-only. Add --write giN (a port with nothing "
                  f"plugged in) to test writes.{OFF}")
    except KeyboardInterrupt:
        print(f"\n{YELLOW}interrupted{OFF}")
    except Exception:
        traceback.print_exc()
    finally:
        sw.logout()

    print(f"\n{'='*70}")
    print(f"{RESULTS['ok']} ok, {RESULTS['fail']} failed, "
          f"{RESULTS['skip']} skipped")
    for f in FAILURES:
        print(f"  {RED}FAILED{OFF} {f}")
    print(f"\n{YELLOW}NOT covered here - needs hands on the box:{OFF}")
    print("  * enabling spanning tree globally (could block te2 and cut the")
    print("    management VLAN; recovery is physical AC removal)")
    print("  * enabling 802.1X globally (ports fail closed)")
    print("  * anything needing a cable: LAG negotiation, PoE delivery,")
    print("    mirroring actually copying frames, storm control actually")
    print("    limiting, link-dependent STP states")
    return 1 if RESULTS["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
