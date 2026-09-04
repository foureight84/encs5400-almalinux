#!/usr/bin/env python3
"""Offline tests for the ESXi bundle - no ESXi, no chassis, no network.

The same idea as 60-test-tui.py running the TUI against a fake switch: these
run install.sh, uninstall.sh and encs-esxi-vnet against a fake `esxcli` that
holds its state in a JSON file. What that buys is the answer to the only
question that matters before pointing them at a real host - do they touch
ANYTHING they were not asked to, and does uninstall put it all back?

    python3 scripts/66-test-esxi.py [-v]

What this CANNOT check is whether real esxcli accepts the command lines or
prints its lists in the shape the mock does - and the first run on a real
ENCS 5412 (ESXi 8.0 U3) found four places where it did not: no `command`
builtin in busybox ash, no `tr` at all, `pcipassthru list` printed as a table
rather than as blocks, and `pcipassthru set` taking --device-id and a bare
--apply-now rather than --device and --active.

All four are now reflected here: the fake prints the real shapes, rejects
option names that `esxcli --help` does not list, and runs with PATH limited to
ESXI_BIN - what ESXi's /bin and /sbin actually hold. The two failures that a
mock cannot express, because the test host's own shell provides them, are
scanned for as text in test_posix_sh.

Nothing in this file makes the bundle "tested on ESXi" - it makes it tested
against a fake that has since been calibrated against one, which is what
stands between an unreviewed script and a host you care about.
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
    # For encs-esxi-bootstrap: the ASIC is "up" when the i40en links are Up.
    # power.on of a VMX carrying pciPassthru0 with the ASIC down bootstraps it
    # (asic -> True, VM dies: the IOMMU fault); with the ASIC already up it
    # is the wedge the script must never cause.
    "asic": False,
    "wedged": False,
    "vms": {"2": {"name": "encs-switch", "state": "off", "vmx": ""},
            "3": {"name": "web01", "state": "off", "vmx": ""}},
}

FAKE_ESXCLI = r'''#!/usr/bin/env python3
"""Fake esxcli. Output shapes were checked against ESXi 8.0 U3 on a real
ENCS 5412; the write commands follow Broadcom's esxcli reference. Anything
not implemented is an error, so a script reaching for a command this does
not know fails loudly."""
import json, os, sys

DB = os.environ["MOCKDB"]
db = json.load(open(DB))
def save(): json.dump(db, open(DB, "w"), indent=1)

args, opts = [], {}
for a in sys.argv[1:]:
    if a.startswith("--"):
        # Bare flags are real: `pcipassthru set --apply-now` takes no value.
        k, _, v = a[2:].partition("=")
        opts[k] = v if _ else True
    else:
        args.append(a)
cmd = " ".join(args)
def fail(m):
    print("Error: " + m, file=sys.stderr); sys.exit(1)

# Real esxcli rejects an option it does not know, and the exact spellings are
# not guessable: `pcipassthru set` takes --device-id and a bare --apply-now,
# not the --device/--active a reading of the other namespaces suggests. The
# mock used to take whatever it was handed, so install.sh shipped with option
# names that ESXi 8.0 U3 refuses outright. Taken from `esxcli <cmd> --help`.
ALLOWED = {
    "network vswitch standard add": {"vswitch-name", "ports"},
    "network vswitch standard set": {"vswitch-name", "mtu", "cdp-status"},
    "network vswitch standard remove": {"vswitch-name"},
    "network vswitch standard list": {"vswitch-name"},
    "network vswitch standard uplink add": {"uplink-name", "vswitch-name"},
    "network vswitch standard uplink remove": {"uplink-name", "vswitch-name"},
    "network vswitch standard portgroup add": {"portgroup-name", "vswitch-name"},
    "network vswitch standard portgroup set": {"portgroup-name", "vlan-id"},
    "network vswitch standard portgroup remove": {"portgroup-name", "vswitch-name"},
    "network vswitch standard portgroup list": set(),
    "network nic list": set(),
    "network vm list": set(),
    "network ip interface list": set(),
    "hardware pci list": set(),
    "hardware pci pcipassthru list": set(),
    "hardware pci pcipassthru set": {"device-id", "enable", "apply-now"},
    "system uuid get": set(),
}
if cmd in ALLOWED:
    for k in opts:
        if k not in ALLOWED[cmd]:
            print("Error: Invalid option --%s" % k, file=sys.stderr); sys.exit(1)
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
        link = "Up" if (drv != "i40en" or db.get("asic")) else "Down"
        print("%-8s%-14s%-8s%-14s%-13s%-7s%-8s%-19s%-6s%s"
              % (n, pci, drv, "Up", link, 10000 if link == "Up" else 0, "Full", "00:11:22:33:44:55", 9000, "NIC"))
elif cmd == "system uuid get":
    print("6a98270c-b75f-a36d-06fd-00a0c9000000")
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
    # A table, unlike every other `hardware pci` list. Checked against ESXi
    # 8.0 U3 on a real ENCS 5412; the block form this used to print is what
    # let a parser that could never read it pass.
    print("Device ID     Enabled")
    print("------------  -------")
    for addr, en in db["passthru"].items():
        print("%-14s%s" % (addr, "true" if en else "false"))
elif cmd == "hardware pci pcipassthru set":
    if opts["device-id"] not in db["passthru"]: fail("device not passthru capable")
    db["passthru"][opts["device-id"]] = opts["enable"] == "true"; save()
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

FAKE_VIMCMD = r'''#!/usr/bin/env python3
"""Fake vim-cmd: only the vmsvc calls encs-esxi-bootstrap makes. Output
shapes as on ESXi 8.0 U3."""
import json, os, sys
DB = os.environ["MOCKDB"]
db = json.load(open(DB))
def save(): json.dump(db, open(DB, "w"), indent=1)
def log(m):
    with open(os.environ["MOCKLOG"], "a") as f: f.write("vim-cmd " + m + "\n")
a = sys.argv[1:]
if not a: sys.exit(2)
log(" ".join(a))
if a[0] == "vmsvc/getallvms":
    print("Vmid       Name                         File                        Guest OS       Version   Annotation")
    for i, v in db["vms"].items():
        vmx = v["vmx"] or ("[datastore1] %s/%s.vmx" % (v["name"], v["name"]))
        if v["vmx"]:
            # tests point vmx at a real temp file; present it as a datastore path
            vmx = "[%s] %s" % ("TESTDS", os.path.basename(v["vmx"]))
        print("%-6s %-14s %-30s centos8_64Guest   vmx-19" % (i, v["name"], vmx))
    sys.exit(0)
vid = a[1] if len(a) > 1 else ""
if vid not in db["vms"]:
    print("Unable to find a VM corresponding to \"%s\"" % vid, file=sys.stderr); sys.exit(1)
vm = db["vms"][vid]
def has_pt():
    p = vm["vmx"]
    return bool(p) and os.path.exists(p) and 'pciPassthru0.present = "TRUE"' in open(p).read()
if a[0] == "vmsvc/power.getstate":
    print("Retrieved runtime info"); print("Powered " + vm["state"])
elif a[0] == "vmsvc/power.on":
    print("Powering on VM:")
    if has_pt():
        if db["asic"]:
            db["wedged"] = True; vm["state"] = "on"      # the thing that must never happen
        elif db.get("asic_never"):
            vm["state"] = "off"                           # loader failed; ASIC stays down
        else:
            db["asic"] = True; vm["state"] = "off"        # bootstrapped, then IOMMU fault
    else:
        vm["state"] = "on"
    save()
elif a[0] in ("vmsvc/power.off", "vmsvc/power.shutdown", "vmsvc/power.reset"):
    vm["state"] = "off" if a[0] != "vmsvc/power.reset" else "on"; save()
elif a[0] == "vmsvc/reload":
    pass
else:
    print("Invalid command '%s'." % a[0], file=sys.stderr); sys.exit(1)
'''

FAKE_UNAME = "#!/bin/sh\nexec /usr/bin/printf '%s\\n' " \
             "'VMkernel esxi 7.0.3 #1 SMP Release build-19193900 x86_64 ESXi'\n"


# What ESXi 8.0 U3 has in /bin and /sbin, restricted to the general-purpose
# tools a shell script might reach for. NOT here, and this is the point:
# tr, xxd, realpath, column, getopt, timeout's GNU flags, bash.
ESXI_BIN = ("sh", "python3", "awk", "basename", "cat", "chmod", "cksum", "cp", "cut", "date",
            "dd", "df", "diff", "dirname", "du", "echo", "egrep", "env",
            "expr", "false", "fgrep", "find", "grep", "gzip", "head",
            "hexdump", "hostname", "kill", "ln", "ls", "md5sum", "mkdir",
            "mkfifo", "mktemp", "more", "mv", "od", "printf", "ps", "pwd",
            "readlink", "rm", "rmdir", "sed", "seq", "sha1sum", "sha256sum",
            "sleep", "sort", "stat", "tail", "tar", "tee", "test", "touch",
            "true", "uname", "uniq", "wc", "which", "xargs")


class Host:
    """A throwaway fake ESXi, with the bundle pointed at it."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="encs-esxi-test.")
        self.db = os.path.join(self.dir, "db.json")
        self.log = os.path.join(self.dir, "esxcli.log")
        self.state = os.path.join(self.dir, "created")
        bindir = os.path.join(self.dir, "bin")
        os.makedirs(bindir)
        for name, body in (("esxcli", FAKE_ESXCLI), ("uname", FAKE_UNAME), ("vim-cmd", FAKE_VIMCMD)):
            p = os.path.join(bindir, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, 0o755)
        # Only what ESXi's /bin actually holds. `tr` is deliberately absent -
        # it is the one thing the bundle used that busybox is not built with
        # here. Taken from `ls /bin /sbin` on ESXi 8.0 U3.
        for name in ESXI_BIN:
            dst = os.path.join(bindir, name)
            src = shutil.which(name)
            # The fakes above win: `uname` is in both lists, and the real one
            # would report Darwin/Linux and fail the VMkernel check.
            if src and not os.path.exists(dst):
                os.symlink(src, dst)
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
        # PATH is the fake bindir and nothing else. Appending the real PATH
        # is what hid the `tr` calls: ESXi has no `tr`, but every developer
        # machine does, so the missing binary only ever showed up on the host.
        e["PATH"] = self.bindir
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


def stage_vm(h, passthru=False, asic=False, state="off"):
    """A VMX file for the fake VM 2, and the ASIC/VM state around it.

    The tests point the datastore path resolution at h.dir by making
    /vmfs/volumes/TESTDS a symlink; the fake vim-cmd prints "[TESTDS] file".
    """
    os.makedirs(os.path.join(h.dir, "TESTDS"), exist_ok=True)
    vmx = os.path.join(h.dir, "TESTDS", "encs-switch.vmx")
    body = 'displayName = "encs-switch"\nmemSize = "384"\nsched.mem.pin = "TRUE"\n'
    if passthru:
        body += 'pciPassthru0.present = "TRUE"\npciPassthru0.id = "00000:013:00.0"\n'
    with open(vmx, "w") as f:
        f.write(body)
    d = h.now()
    d["vms"]["2"]["vmx"] = vmx
    d["vms"]["2"]["state"] = state
    d["asic"] = asic
    d["passthru"]["0000:0d:00.0"] = True
    with open(h.db, "w") as f:
        json.dump(d, f)
    return vmx


def run_bootstrap(h, *args, **env):
    # The script resolves "[TESTDS] x.vmx" to /vmfs/volumes/TESTDS/x.vmx.
    # Redirect that through an env override rather than touching /vmfs.
    env.setdefault("VMFS", h.dir)
    env.setdefault("LOG", os.path.join(h.dir, "bootstrap.log"))
    env.setdefault("LOCAL_SH", os.path.join(h.dir, "local.sh"))
    return h.run("encs-esxi-bootstrap", *args, **env)


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
    # Not decoration: the uplink list is built with a shell idiom that ESXi's
    # missing `tr` used to blank silently, printing "i40en uplinks:" and
    # nothing. An operator reads that line to confirm te2 was picked.
    check("i40en uplinks: vmnic2 vmnic3" in r.stdout,
          f"names both uplinks in PCI order: {[l for l in r.stdout.splitlines() if 'uplinks' in l]}")
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


# ================================================================== bootstrap
def test_bootstrap_happy_path(h):
    print("\n== bootstrap: ASIC down -> attach, power on, links up, detach, power on")
    h.reset(); vmx = stage_vm(h)
    r = run_bootstrap(h, "run", START_AFTER="web01 nosuchvm")
    check(r.returncode == 0, f"exits 0: {r.stdout[-300:]} {r.stderr[-300:]}")
    d = h.now()
    check(d["asic"] is True, "the ASIC was bootstrapped")
    check(d["wedged"] is False, "never powered on with the device against a live ASIC")
    check('pciPassthru0' not in open(vmx).read(), "the device is removed from the VMX afterwards")
    check(d["vms"]["2"]["state"] == "on", "the VM is left running in the management role")
    check(d["vms"]["3"]["state"] == "on", "START_AFTER VM was powered on")
    check("no VM named nosuchvm" in r.stdout, "unknown START_AFTER names are reported, not fatal")
    ons = [c for c in h.commands() if c.startswith("vim-cmd vmsvc/power.on 2")]
    check(len(ons) == 2, f"VM 2 powered on exactly twice (bootstrap, then management): {len(ons)}")
    # the first power-on had the device, the second did not
    check("pciPassthru0.id = \"00000:013:00.0\"" in r.stdout or "00000:013:00.0" in r.stdout,
          "the decimal VMX id was used")


def test_bootstrap_refuses_when_up(h):
    print("\n== bootstrap: ASIC already up -> never attaches the device")
    h.reset(); vmx = stage_vm(h, asic=True)
    r = run_bootstrap(h, "run")
    check(r.returncode == 0, "exits 0")
    check("already up" in r.stdout, "says why it did nothing")
    check(h.now()["wedged"] is False, "no wedge")
    check('pciPassthru0' not in open(vmx).read(), "VMX untouched")
    check(h.now()["vms"]["2"]["state"] == "on", "management VM is brought up anyway")


def test_bootstrap_leftover_device_when_up(h):
    print("\n== bootstrap: ASIC up but the VMX still carries the device -> strip it first")
    h.reset(); vmx = stage_vm(h, passthru=True, asic=True)
    r = run_bootstrap(h, "run")
    check(r.returncode == 0, "exits 0")
    check(h.now()["wedged"] is False, "device stripped BEFORE power on - no wedge")
    check('pciPassthru0' not in open(vmx).read(), "device removed")


def test_bootstrap_timeout(h):
    print("\n== bootstrap: ASIC never comes up -> device removed, exit 1")
    h.reset(); vmx = stage_vm(h)
    d = h.now(); d["asic_never"] = True
    with open(h.db, "w") as f: json.dump(d, f)
    r = run_bootstrap(h, "run", WAIT="5")
    check(r.returncode == 1, "exits 1")
    check("did not come up" in r.stdout, "says the ASIC never came up")
    check('pciPassthru0' not in open(vmx).read(), "device removed so the next manual power-on is safe")
    check(h.now()["vms"]["2"]["state"] == "off", "VM left off, not silently restarted as management")


def test_bootstrap_needs_passthru(h):
    print("\n== bootstrap: passthrough not enabled -> refuses before touching the VMX")
    h.reset(); vmx = stage_vm(h)
    d = h.now(); d["passthru"]["0000:0d:00.0"] = False
    with open(h.db, "w") as f: json.dump(d, f)
    r = run_bootstrap(h, "run")
    check(r.returncode == 1 and "not enabled for passthrough" in r.stdout, "names the cause")
    check('pciPassthru0' not in open(vmx).read(), "VMX untouched")


def test_bootstrap_status(h):
    print("\n== bootstrap: status")
    h.reset(); stage_vm(h, passthru=True)
    r = run_bootstrap(h, "status")
    check(r.returncode == 0 and "asic=down" in r.stdout and "role=bootstrap" in r.stdout,
          f"status line: {r.stdout.strip()}")


def test_bootstrap_hook(h):
    print("\n== bootstrap: install-hook is idempotent and remove-hook is clean")
    h.reset()
    local = os.path.join(h.dir, "local.sh")
    orig = "#!/bin/sh\n\n# local configuration options\n\n# Note: modify at your own risk!\n\nexit 0\n"
    with open(local, "w") as f: f.write(orig)
    r1 = run_bootstrap(h, "install-hook", START_AFTER="web01")
    r2 = run_bootstrap(h, "install-hook")
    txt = open(local).read()
    check(r1.returncode == 0 and r2.returncode == 0, "both installs exit 0")
    check(txt.count("encs-esxi-bootstrap run") == 1, "exactly one hook after two installs")
    check('START_AFTER="web01"' in txt, "START_AFTER is baked into the hook")
    check(txt.rstrip().endswith("exit 0"), "exit 0 is still last")
    check("nohup" not in txt, "no nohup (ESXi has none)")
    r3 = run_bootstrap(h, "remove-hook")
    check(r3.returncode == 0 and open(local).read() == orig, "remove-hook restores the file byte for byte")


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
        # `command` is a POSIX builtin that busybox ash does not implement: on
        # ESXi 8.0 U3, `command -v x` exits 127 with "sh: command: not found".
        # The sh this test runs under does have it, so only a text scan
        # catches it. `type` is the builtin ESXi does provide.
        (r"\bcommand\s+-v\b", "command -v (busybox ash has no `command`)"),
        # No `tr` in ESXi's busybox either. It is absent from ESXI_BIN too, so
        # a load-bearing use fails the run outright; this catches the rest.
        (r"(^|[|(;&`]|\$\()\s*tr\s", "tr (not on ESXi)"),
    )
    for f in ("install.sh", "uninstall.sh", "encs-esxi-vnet", "encs-esxi-bootstrap"):
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
                   test_uninstall_does_not_delete_dev_null,
                   test_bootstrap_happy_path, test_bootstrap_refuses_when_up,
                   test_bootstrap_leftover_device_when_up, test_bootstrap_timeout,
                   test_bootstrap_needs_passthru,
                   test_bootstrap_status, test_bootstrap_hook):
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
