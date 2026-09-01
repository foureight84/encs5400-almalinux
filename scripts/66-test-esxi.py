#!/usr/bin/env python3
"""Offline tests for the ESXi bundle - no ESXi, no chassis, no network.

The same idea as 60-test-tui.py running the TUI against a fake switch: these
run install.sh, uninstall.sh and encs-esxi-vnet against a fake `esxcli` that
holds its state in a JSON file. What that buys is the answer to the only
question that matters before pointing them at a real host - do they touch
ANYTHING they were not asked to, and does uninstall put it all back?

    python3 scripts/66-test-esxi.py [-v]

What this CANNOT check is whether real esxcli accepts the command lines or
prints its lists in the shape the mock does. Every write command here was
checked against Broadcom's published esxcli reference, and the parsers are
written to tolerate both the indented and the flat list formats, but neither
of those is the same as having run it. Nothing in this file makes the bundle
"tested on ESXi" - it makes it tested against a fake, which is what stands
between an unreviewed script and a host you care about.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE = os.path.abspath(os.path.join(HERE, "..", "payload", "opt", "encs-esxi"))

VERBOSE = "-v" in sys.argv
FAILURES = []
CHECKS = [0]


def check(cond, what):
    CHECKS[0] += 1
    if cond:
        if VERBOSE:
            print(f"  ok   {what}")
    else:
        FAILURES.append(what)
        print(f"  FAIL {what}")


# =============================================================== the fake host
# One ESXi host: two igbn ports (the I210s), two i40en ports (the X710
# backplane, .0 = te1 and .1 = te2), and the Marvell ASIC. 0e:00.0 is a decoy
# with the Marvell ids in its SUBsystem fields - on this chassis that address
# really has been the I210 on one install and the Marvell on another, and an
# unanchored parse picks it up.
FAKE_HOST = {
    "vswitches": {"vSwitch0": {"mtu": 1500, "uplinks": ["vmnic0"],
                               "pgs": {"VM Network": 0,
                                       "Management Network": 0}}},
    "nics": [["vmnic0", "0000:03:00.0", "igbn"],
             ["vmnic1", "0000:03:00.1", "igbn"],
             ["vmnic2", "0000:08:00.0", "i40en"],
             ["vmnic3", "0000:08:00.1", "i40en"]],
    "pci": [["0000:08:00.0", "0x8086", "0x1572"],
            ["0000:08:00.1", "0x8086", "0x1572"],
            ["0000:0d:00.0", "0x11ab", "0xbe00"],
            ["0000:0e:00.0", "0x8086", "0x1533"]],
    "passthru": {"0000:0d:00.0": False},
    "clients": {},
}

FAKE_ESXCLI = r'''#!/usr/bin/env python3
"""Fake esxcli. Output shapes follow ESXi 7.x; the write commands follow
Broadcom's esxcli reference. Anything not implemented is an error, so a
script reaching for a command this does not know fails loudly."""
import json, os, sys

DB = os.environ["MOCKDB"]
db = json.load(open(DB))
def save(): json.dump(db, open(DB, "w"), indent=1)

args, opts = [], {}
for a in sys.argv[1:]:
    if a.startswith("--") and "=" in a:
        k, v = a[2:].split("=", 1); opts[k] = v
    else:
        args.append(a)
cmd = " ".join(args)
def fail(m):
    print("Error: " + m, file=sys.stderr); sys.exit(1)
def log(m):
    with open(os.environ["MOCKLOG"], "a") as f:
        f.write(m + "\n")

log(" ".join(sys.argv[1:]))

if cmd == "network vswitch standard list":
    if "vswitch-name" in opts:
        if opts["vswitch-name"] not in db["vswitches"]: fail("Not found")
        names = [opts["vswitch-name"]]
    else:
        names = list(db["vswitches"])
    for n in names:
        v = db["vswitches"][n]
        print(n)
        print("   Name: %s" % n)
        print("   Class: cswitch")
        print("   Num Ports: 2560")
        print("   MTU: %d" % v["mtu"])
        print("   Beacon Required By: ")
        print("   Uplinks: %s" % ", ".join(v["uplinks"]))
        print("   Portgroups: %s" % ", ".join(v["pgs"]))
        print()
elif cmd == "network vswitch standard add":
    n = opts["vswitch-name"]
    if n in db["vswitches"]: fail("already exists")
    db["vswitches"][n] = {"mtu": 1500, "uplinks": [], "pgs": {}}; save()
elif cmd == "network vswitch standard set":
    db["vswitches"][opts["vswitch-name"]]["mtu"] = int(opts["mtu"]); save()
elif cmd == "network vswitch standard remove":
    n = opts["vswitch-name"]
    if n not in db["vswitches"]: fail("not found")
    del db["vswitches"][n]; save()
elif cmd == "network vswitch standard uplink add":
    for name, sw in db["vswitches"].items():
        if opts["uplink-name"] in sw["uplinks"]:
            fail("uplink already used by " + name)
    db["vswitches"][opts["vswitch-name"]]["uplinks"].append(opts["uplink-name"]); save()
elif cmd == "network vswitch standard uplink remove":
    db["vswitches"][opts["vswitch-name"]]["uplinks"].remove(opts["uplink-name"]); save()
elif cmd == "network vswitch standard portgroup list":
    print("Name                            Virtual Switch    Active Clients  VLAN ID")
    print("------------------------------  ----------------  --------------  -------")
    for sw, v in db["vswitches"].items():
        for pg, vlan in v["pgs"].items():
            print("%-32s%-18s%-16d%d" % (pg, sw, db["clients"].get(pg, 0), vlan))
elif cmd == "network vswitch standard portgroup add":
    sw = db["vswitches"][opts["vswitch-name"]]
    if opts["portgroup-name"] in sw["pgs"]: fail("exists")
    sw["pgs"][opts["portgroup-name"]] = 0; save()
elif cmd == "network vswitch standard portgroup set":
    for v in db["vswitches"].values():
        if opts["portgroup-name"] in v["pgs"]:
            v["pgs"][opts["portgroup-name"]] = int(opts["vlan-id"]); save(); break
    else: fail("not found")
elif cmd == "network vswitch standard portgroup remove":
    v = db["vswitches"][opts["vswitch-name"]]
    if opts["portgroup-name"] not in v["pgs"]: fail("not found")
    if db["clients"].get(opts["portgroup-name"], 0): fail("portgroup in use")
    del v["pgs"][opts["portgroup-name"]]; save()
elif cmd == "network nic list":
    print("Name    PCI Device    Driver  Admin Status  Link Status  Speed  Duplex  MAC Address        MTU   Description")
    print("------  ------------  ------  ------------  -----------  -----  ------  -----------------  ----  -----------")
    for n, pci, drv in db["nics"]:
        print("%-8s%-14s%-8s%-14s%-13s%-7s%-8s%-19s%-6s%s"
              % (n, pci, drv, "Up", "Up", 10000, "Full", "00:11:22:33:44:55", 9000, "NIC"))
elif cmd == "hardware pci list":
    for addr, vid, did in db["pci"]:
        print(addr)
        print("   Address: %s" % addr)
        print("   Vendor Name: Vendor")
        print("   Device Name: Device")
        print("   Vendor ID: %s" % vid)
        print("   Device ID: %s" % did)
        print("   SubVendor ID: 0x11ab")
        print("   SubDevice ID: 0xbe00")
        print()
elif cmd == "hardware pci pcipassthru list":
    for addr, en in db["passthru"].items():
        print(addr)
        print("   Device Address: %s" % addr)
        print("   Enabled: %s" % ("true" if en else "false"))
        print()
elif cmd == "hardware pci pcipassthru set":
    if opts["device"] not in db["passthru"]: fail("device not passthru capable")
    db["passthru"][opts["device"]] = opts["enable"] == "true"; save()
elif cmd == "network vm list":
    print("World ID  Name    Num Ports  Networks")
    print("--------  ------  ---------  --------")
elif cmd == "network ip interface list":
    print("vmk0")
    print("   Name: vmk0")
    print("   Portgroup: Management Network")
else:
    fail("unknown command: " + cmd)
'''

FAKE_UNAME = "#!/bin/sh\nexec /usr/bin/printf '%s\\n' " \
             "'VMkernel esxi 7.0.3 #1 SMP Release build-19193900 x86_64 ESXi'\n"


class Host:
    """A throwaway fake ESXi, with the bundle pointed at it."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="encs-esxi-test.")
        self.db = os.path.join(self.dir, "db.json")
        self.log = os.path.join(self.dir, "esxcli.log")
        self.state = os.path.join(self.dir, "created")
        bindir = os.path.join(self.dir, "bin")
        os.makedirs(bindir)
        for name, body in (("esxcli", FAKE_ESXCLI), ("uname", FAKE_UNAME)):
            p = os.path.join(bindir, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, 0o755)
        self.bindir = bindir
        self.reset()

    def reset(self):
        with open(self.db, "w") as f:
            json.dump(FAKE_HOST, f)
        for p in (self.log, self.state):
            if os.path.exists(p):
                os.unlink(p)

    def run(self, script, *args, **env):
        e = dict(os.environ)
        e["PATH"] = self.bindir + os.pathsep + e["PATH"]
        e["MOCKDB"] = self.db
        e["MOCKLOG"] = self.log
        e["STATE"] = self.state
        e.update(env)
        return subprocess.run(["sh", os.path.join(BUNDLE, script), *args],
                              capture_output=True, text=True, env=e)

    def now(self):
        with open(self.db) as f:
            return json.load(f)

    def commands(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [l.strip() for l in f if l.strip()]

    def writes(self):
        """Only the commands that change something."""
        return [c for c in self.commands()
                if not c.endswith(" list") and " list " not in c]

    def record(self):
        if not os.path.exists(self.state):
            return []
        with open(self.state) as f:
            return [l.strip() for l in f if l.strip()]

    def attach_vm(self, pg, n=1):
        d = self.now()
        d["clients"][pg] = n
        with open(self.db, "w") as f:
            json.dump(d, f)

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def untouched(h, what):
    """vSwitch0 and its portgroups are the way back in. Nothing may touch them."""
    d = h.now()
    check("vSwitch0" in d["vswitches"], f"{what}: vSwitch0 still exists")
    check(d["vswitches"]["vSwitch0"]["uplinks"] == ["vmnic0"],
          f"{what}: vSwitch0 keeps its uplink")
    check(sorted(d["vswitches"]["vSwitch0"]["pgs"]) ==
          ["Management Network", "VM Network"],
          f"{what}: vSwitch0 keeps its portgroups")
    check(not any("vSwitch0" in c for c in h.writes()),
          f"{what}: no write command names vSwitch0")


# ==================================================================== install
def test_dry_run_writes_nothing(h):
    print("\n== install: the default run changes nothing")
    h.reset()
    r = h.run("install.sh")
    check(r.returncode == 0, "dry run succeeds")
    check("dry run" in r.stdout, "says it is a dry run")
    check(h.writes() == [], f"no write commands issued: {h.writes()}")
    check(h.now() == FAKE_HOST, "the host is bit-identical afterwards")
    check(not os.path.exists(h.state), "no record file is created")
    untouched(h, "dry run")


def test_apply(h):
    print("\n== install --yes: creates exactly what it said")
    h.reset()
    r = h.run("install.sh", "--yes")
    check(r.returncode == 0, f"apply succeeds (rc={r.returncode})")
    d = h.now()
    check("vSwitchENCS" in d["vswitches"], "vSwitchENCS created")
    sw = d["vswitches"].get("vSwitchENCS", {})
    check(sw.get("mtu") == 9000, "mtu 9000")
    check(sw.get("uplinks") == ["vmnic3"], "uplink is vmnic3 = te2, the .1 function")
    check(sw.get("pgs", {}).get("encs-mgmt-2363") == 2363, "mgmt portgroup on VLAN 2363")
    check(sw.get("pgs", {}).get("encs-lan") == 0, "lan portgroup untagged")
    check(d["passthru"]["0000:0d:00.0"] is True, "passthrough enabled on the Marvell")
    check(sorted(h.record()) == sorted([
        "vswitch vSwitchENCS", "uplink vmnic3 vSwitchENCS",
        "portgroup encs-mgmt-2363 vSwitchENCS", "portgroup encs-lan vSwitchENCS",
        "passthru 0000:0d:00.0"]), f"records what it made: {h.record()}")
    untouched(h, "apply")


def test_picks_the_right_device(h):
    print("\n== install: device and uplink selection")
    h.reset()
    r = h.run("install.sh")
    check("0000:0d:00.0" in r.stdout,
          "picks the real Marvell, not the decoy whose SUBsystem ids match")
    check("0000:0e:00.0" not in r.stdout, "the decoy at 0e:00.0 is not chosen")
    check("vmnic3" in r.stdout and "backplane    : vmnic3" in r.stdout,
          "picks the higher PCI function (te2), not te1")


def test_idempotent(h):
    print("\n== install: running it twice")
    h.reset()
    h.run("install.sh", "--yes")
    before = h.now()
    r = h.run("install.sh", "--yes")
    check(r.returncode == 0, "second run succeeds")
    check("nothing to do" in r.stdout, "second run says there is nothing to do")
    check(h.now() == before, "second run changes nothing")
    check(len(h.record()) == 5, f"no duplicate records: {h.record()}")
    check("Next" in r.stdout, "still prints the next steps")


def test_refuses_to_steal_an_uplink(h):
    print("\n== install: refuses to take an uplink off another vSwitch")
    h.reset()
    r = h.run("install.sh", "--backplane", "vmnic0", "--yes")
    check(r.returncode != 0, "fails")
    check("already an uplink of vSwitch0" in r.stderr, "names the owner")
    check(h.now() == FAKE_HOST, "nothing was created first")
    untouched(h, "uplink steal")


def test_refuses_unknown_uplink(h):
    print("\n== install: refuses an uplink the host does not have")
    h.reset()
    r = h.run("install.sh", "--backplane", "vmnic99", "--yes")
    check(r.returncode != 0, "fails")
    check("no such uplink" in r.stderr, "says so")
    check(h.now() == FAKE_HOST, "nothing changed")


def test_refuses_foreign_portgroup(h):
    print("\n== install: refuses to adopt a portgroup it did not create")
    h.reset()
    d = h.now()
    d["vswitches"]["vSwitch0"]["pgs"]["encs-lan"] = 0
    with open(h.db, "w") as f:
        json.dump(d, f)
    r = h.run("install.sh", "--yes")
    check(r.returncode != 0, "fails")
    check("already exists on vSwitch0" in r.stderr, "says where it is")
    check("vSwitchENCS" not in h.now()["vswitches"], "nothing was created first")

    h.reset()
    d = h.now()
    d["vswitches"]["vSwitch0"]["pgs"]["encs-mgmt-2363"] = 99
    with open(h.db, "w") as f:
        json.dump(d, f)
    r = h.run("install.sh", "--yes")
    check(r.returncode != 0, "also fails on a wrong VLAN")


def test_no_device(h):
    print("\n== install: on a host with no Marvell")
    h.reset()
    d = h.now()
    d["pci"] = [p for p in d["pci"] if p[1] != "0x11ab"]
    with open(h.db, "w") as f:
        json.dump(d, f)
    r = h.run("install.sh", "--yes")
    check(r.returncode != 0, "fails")
    check("no 11ab:be00 device found" in r.stderr, "says what is missing")
    check(h.writes() == [], "and had not written anything yet")


def test_not_esxi():
    print("\n== install: on something that is not ESXi")
    r = subprocess.run(["sh", os.path.join(BUNDLE, "install.sh"), "--yes"],
                       capture_output=True, text=True)
    check(r.returncode != 0, "refuses to run")
    check("not an ESXi host" in r.stderr, "says why")


# ======================================================================= vnet
def test_vnet(h):
    print("\n== encs-esxi-vnet")
    h.reset()
    h.run("install.sh", "--yes")
    r = h.run("encs-esxi-vnet", "add", "100", "--name", "dmz")
    check(r.returncode == 0, "add 100 succeeds")
    check(h.now()["vswitches"]["vSwitchENCS"]["pgs"].get("encs-lan-100") == 100,
          "creates encs-lan-100 with VLAN 100")
    check("portgroup encs-lan-100 vSwitchENCS" in h.record(), "records it")
    check("encs-switch-vnet add 100" in r.stdout and "--fix-backplane" in r.stdout,
          "prints the switch half, which is the other machine's job")

    for bad, why in (("1", "VLAN 1 is the untagged case"),
                     ("2363", "VLAN 2363 is the switch management VLAN"),
                     ("abc", "must be a number"),
                     ("9999", "must be 1-4094")):
        r = h.run("encs-esxi-vnet", "add", bad)
        check(r.returncode != 0 and why in r.stderr, f"refuses VLAN {bad}")

    h.attach_vm("encs-lan-100", 2)
    r = h.run("encs-esxi-vnet", "remove", "100")
    check(r.returncode != 0, "refuses to remove a portgroup with a VM on it")
    check("encs-lan-100" in h.now()["vswitches"]["vSwitchENCS"]["pgs"],
          "and leaves it alone")

    h.attach_vm("encs-lan-100", 0)
    r = h.run("encs-esxi-vnet", "remove", "100")
    check(r.returncode == 0, "removes it once the VM is off")
    check("encs-lan-100" not in h.now()["vswitches"]["vSwitchENCS"]["pgs"],
          "portgroup gone")
    check("portgroup encs-lan-100 vSwitchENCS" not in h.record(),
          "and out of the record")
    untouched(h, "vnet")


# ================================================================== uninstall
def test_uninstall_dry_run(h):
    print("\n== uninstall: the default run changes nothing")
    h.reset()
    h.run("install.sh", "--yes")
    before = h.now()
    r = h.run("uninstall.sh")
    check(r.returncode == 0, "dry run succeeds")
    check(h.now() == before, "host unchanged")
    check(h.record() != [], "record still there")


def test_uninstall_order(h):
    print("\n== uninstall: the plan is in the order it actually removes")
    h.reset()
    h.run("install.sh", "--yes")
    r = h.run("uninstall.sh")
    plan = [l.strip() for l in r.stdout.splitlines()
            if l.strip().startswith(("portgroup ", "uplink ", "vswitch ", "passthru "))]
    kinds = [p.split()[0] for p in plan]
    check(kinds == sorted(kinds, key=lambda k: ["portgroup", "uplink", "vswitch",
                                                "passthru", "passthru.map"].index(k)),
          f"portgroups, then uplink, then vSwitch, then passthrough: {kinds}")


def test_uninstall_refuses_busy(h):
    print("\n== uninstall: refuses while a VM is still attached")
    h.reset()
    h.run("install.sh", "--yes")
    h.attach_vm("encs-lan", 2)
    before = h.now()
    r = h.run("uninstall.sh", "--yes")
    check(r.returncode != 0, "fails")
    check("still has clients" in r.stderr, "says which")
    check(h.now() == before, "and removed nothing at all")


def test_uninstall_round_trip(h):
    print("\n== uninstall: puts the host back")
    h.reset()
    h.run("install.sh", "--yes")
    h.run("encs-esxi-vnet", "add", "100")
    r = h.run("uninstall.sh", "--yes")
    check(r.returncode == 0, f"succeeds (rc={r.returncode})")
    d = h.now()
    check(list(d["vswitches"]) == ["vSwitch0"], "only vSwitch0 is left")
    check(d["passthru"]["0000:0d:00.0"] is False, "passthrough released")
    check(d == FAKE_HOST, "the host is bit-identical to before install")
    check(not os.path.exists(h.state), "the record is gone")
    untouched(h, "round trip")


def test_uninstall_keeps_the_record_on_failure(h):
    print("\n== uninstall: an item that will not come out")
    h.reset()
    h.run("install.sh", "--yes")
    with open(h.state, "a") as f:
        f.write("portgroup ghost-pg vSwitchENCS\n")
    r = h.run("uninstall.sh", "--yes")
    check("could not remove portgroup ghost-pg" in r.stderr, "reports the failure")
    check(h.record() == ["portgroup ghost-pg vSwitchENCS"],
          f"the record keeps only what is left: {h.record()}")
    check("NOT fully removed" in r.stdout,
          "and it does not claim the host is back to stock")
    check(list(h.now()["vswitches"]) == ["vSwitch0"], "everything real still went")


def test_uninstall_force(h):
    print("\n== uninstall --force: with the record lost")
    h.reset()
    h.run("install.sh", "--yes")
    os.unlink(h.state)
    r = h.run("uninstall.sh")
    check(r.returncode != 0, "without --force it refuses to guess")
    check("will not guess" in r.stderr, "and says so")

    r = h.run("uninstall.sh", "--force", "--yes")
    check(r.returncode == 0, "--force succeeds")
    d = h.now()
    check(list(d["vswitches"]) == ["vSwitch0"], "vSwitch removed")
    check(d["passthru"]["0000:0d:00.0"] is False,
          "and passthrough released - it is rediscovered, not guessed from a name")
    check(d == FAKE_HOST, "host is bit-identical")


def test_uninstall_does_not_delete_dev_null(h):
    print("\n== uninstall: /dev/null survives --force")
    # Regression: --force once set STATE=/dev/null, which read as an empty
    # record right up until `rm -f "$STATE"` deleted the host's /dev/null.
    h.reset()
    h.run("install.sh", "--yes")
    os.unlink(h.state)
    h.run("uninstall.sh", "--force", "--yes")
    check(os.path.exists("/dev/null") and os.path.exists(h.bindir),
          "no absolute path outside the record was removed")
    check(not [l for l in code(os.path.join(BUNDLE, "uninstall.sh"))
               if "STATE=/dev/null" in l],
          "the /dev/null assignment is gone for good (comments about it are fine)")


# =================================================================== the shell
def code(path):
    """The script's actual code - comments dropped.

    Scanning the raw text instead trips over its own prose: "[[" matches the
    POSIX class [[:space:]], and a comment explaining a bug is not the bug.
    """
    out = []
    for line in open(path):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line)
    return out


def test_posix_sh():
    print("\n== the scripts are POSIX sh (the ESXi shell is busybox ash)")
    # [[ ]] tests, here-strings, `declare` and `function f {}` are bash;
    # busybox ash has none of them. $(( )) and `local` are fine - both are in
    # ash. The patterns are anchored because the loose forms match the scripts'
    # own output ("==>" in say()) and awk programs ($1 == p), which are neither
    # bash nor wrong.
    BASHISMS = (
        (r"\[\[ ", "[[ ]] test"),
        (r"<<<", "here-string"),
        (r"^\s*declare\s", "declare"),
        (r"^\s*function\s+\w+", "function keyword"),
        (r"\[\s[^]]*\s==\s", "== inside [ ]"),
    )
    for f in ("install.sh", "uninstall.sh", "encs-esxi-vnet"):
        p = os.path.join(BUNDLE, f)
        r = subprocess.run(["sh", "-n", p], capture_output=True, text=True)
        check(r.returncode == 0, f"{f} parses under sh -n")
        for pat, name in BASHISMS:
            hits = [l.strip() for l in code(p) if re.search(pat, l)]
            check(not hits, f"{f} has no {name}: {hits[:1]}")


def main():
    h = Host()
    try:
        for fn in (test_dry_run_writes_nothing, test_apply,
                   test_picks_the_right_device, test_idempotent,
                   test_refuses_to_steal_an_uplink, test_refuses_unknown_uplink,
                   test_refuses_foreign_portgroup, test_no_device,
                   test_vnet,
                   test_uninstall_dry_run, test_uninstall_order,
                   test_uninstall_refuses_busy, test_uninstall_round_trip,
                   test_uninstall_keeps_the_record_on_failure,
                   test_uninstall_force,
                   test_uninstall_does_not_delete_dev_null):
            fn(h)
        test_not_esxi()
        test_posix_sh()
    finally:
        h.cleanup()
    print(f"\n{CHECKS[0]} checks, {len(FAILURES)} failed")
    for f in FAILURES:
        print(f"  FAILED: {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
