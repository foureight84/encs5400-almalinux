#!/usr/bin/env python3
"""Offline tests for encs-switch-vnet - no switch, no host, no network.

This tool writes two things that are expensive to get wrong: the host's
/etc/network/interfaces (a bad stanza costs the switch management VLAN on
the next boot) and te2's VLAN membership (a bad write costs it immediately,
and the only recovery is pulling the AC cord). Everything shaped like that
is a pure function here, so it can be checked without hardware.

What this CANNOT check is whether ifupdown2 likes the stanzas or the
firmware accepts the write. Run it on the box before believing either.

    python3 scripts/64-test-vnet.py [-v]
"""
import importlib.util
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE = os.path.join(HERE, "..", "payload", "opt", "encs-host")

VERBOSE = "-v" in sys.argv
FAILURES = []
CHECKS = [0]


def load(name):
    loader = SourceFileLoader(name.replace("-", "_"),
                              os.path.abspath(os.path.join(BUNDLE, name)))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


v = load("encs-switch-vnet")
t = v.t


def check(cond, what):
    CHECKS[0] += 1
    if cond:
        if VERBOSE:
            print(f"  ok   {what}")
    else:
        FAILURES.append(what)
        print(f"  FAIL {what}")


def check_raises(fn, what):
    try:
        fn()
    except Exception:
        check(True, what)
        return
    check(False, what)


# What install.sh leaves behind, plus the Proxmox stanzas it must not touch.
LEGACY = """\
auto lo
iface lo inet loopback

iface enp15s0 inet manual

auto vmbr0
iface vmbr0 inet static
    address 10.0.0.10/24
    gateway 10.0.0.1
    bridge-ports enp15s0
    bridge-stp off
    bridge-fd 0

source /etc/network/interfaces.d/*

auto enp8s0f1np1
iface enp8s0f1np1 inet manual
#X710 backplane link to the Marvell switch ASIC (te2). Not a front port - the GE1/x jacks are behind the switch, not on this NIC.
    mtu 9216

auto sw2363
iface sw2363 inet static
#Marvell switch management VLAN 2363, link-local. encs-switch-tui reaches the ASIC at 169.254.1.0 over this. Not for VMs; do not delete.
    address 169.254.1.1/16
    vlan-raw-device enp8s0f1np1
    vlan-id 2363
    mtu 9216
"""


# ========================================================= the stanza file
def test_render():
    print("\n== render_managed")
    out = v.render_managed("enp8s0f1np1", [])
    check("bridge-ports enp8s0f1np1" in out, "the backplane NIC is the bridge port")
    check("bridge-vlan-aware yes" in out, "the bridge is VLAN aware")
    check("bridge-stp off" in out, "host STP off - the ASIC runs its own")
    check("mtu 9216" in out, "jumbo MTU carried through")

    # The management VLAN MUST move to the bridge: a VLAN subinterface of an
    # enslaved NIC receives nothing, and this is the failure that would look
    # like "the switch died" rather than "the config is wrong".
    mgmt = out.split("auto sw2363")[1]
    check("vlan-raw-device swbr0" in mgmt, "sw2363 hangs off the bridge")
    check("vlan-raw-device enp8s0f1np1" not in out,
          "sw2363 does NOT hang off the enslaved NIC")
    check("169.254.1.1/16" in mgmt, "management address preserved")
    check("bridge vlan add dev swbr0 vid 2363 self" in mgmt,
          "the bridge's own VLAN filter lets 2363 up to the host")

    check(out.startswith(v.BEGIN) and out.rstrip().endswith(v.END),
          "the block is bracketed by markers so it can be replaced exactly")
    named = v.render_managed("enp8s0f1np1", [("dmz", "100")])
    check("bridge-ports swbr0.100" in named,
          "a named bridge stacks on the VLAN-aware bridge")
    check(v.parse_vnets(named) == [("dmz", "100")],
          "parse_vnets recovers what render_managed wrote")
    check("bridge vlan add dev swbr0 vid 100 self" in named,
          "the named bridge's VLAN is allowed up to the bridge itself")
    check(v.parse_vnets(v.render_managed("x", [])) == [],
          "no named bridges parses empty")
    # Recovered from the file, not a state file, so init can regenerate
    # without silently dropping bridges someone is already using.
    two = v.render_managed("x", [("dmz", "100"), ("guest", "200")])
    check(v.parse_vnets(two) == [("dmz", "100"), ("guest", "200")],
          "several named bridges round-trip in order")


def test_block_in_the_main_file():
    print("\n== the block in /etc/network/interfaces")
    # Proxmox reads NOTHING but this file - "will NOT read its network
    # configuration from sourced files" is in the header it generates - so a
    # bridge in interfaces.d is one the GUI cannot offer when creating a VM.
    block = v.render_managed("enp8s0f1np1", [])
    added, replaced = v.upsert_block(LEGACY, block)
    check(not replaced, "first time appends")
    check(v.BEGIN in added and v.END in added, "markers land in the file")
    check("iface vmbr0 inet static" in added, "the rest of the file survives")
    check(added.count("10.0.0.10/24") == 1,
          "the management address is untouched, and not duplicated")

    # init runs repeatedly - re-rendering must replace, never stack.
    twice, replaced2 = v.upsert_block(added, block)
    check(replaced2 and twice == added, "second time replaces, byte for byte")
    grown, _ = v.upsert_block(added, v.render_managed("enp8s0f1np1",
                                                      [("dmz", "100")]))
    check(grown.count(v.BEGIN) == 1 and "bridge-ports swbr0.100" in grown,
          "adding a named bridge rewrites the one block")

    back, found = v.remove_block(added)
    check(found and back == LEGACY, "remove_block restores the file exactly")
    check(v.remove_block(LEGACY) == (LEGACY, False),
          "no block to remove is not an error")

    # And the case that WILL happen eventually: the GUI rewrote the file, so
    # PVE dropped our top-level markers while keeping the stanzas.
    no_markers = "\n".join(l for l in added.splitlines()
                            if not l.startswith("#"))
    stripped, gone = v.strip_ifaces(no_markers, {"swbr0", "sw2363"})
    check(sorted(gone) == ["sw2363", "swbr0"],
          "the fallback finds the stanzas by name")
    check("iface swbr0" not in stripped and "iface sw2363" not in stripped,
          "and removes them")
    check("iface vmbr0 inet static" in stripped and "10.0.0.10/24" in stripped,
          "without touching the management bridge")
    check("iface enp15s0 inet manual" in stripped,
          "or any other interface")


def test_comment_out_legacy():
    print("\n== comment_out_legacy")
    new, touched = v.comment_out_legacy(LEGACY, {"enp8s0f1np1", "sw2363", "swbr0"})
    check(sorted(touched) == ["enp8s0f1np1", "sw2363"],
          "only the stanzas we take over are touched")

    # The one thing that must never happen: losing the stanza that carries
    # the host's own management address.
    check("\nauto vmbr0" in new, "vmbr0 stanza left alone")
    check("    address 10.0.0.10/24" in new, "vmbr0's address left alone")
    check("auto lo\niface lo inet loopback" in new, "lo left alone")
    check("source /etc/network/interfaces.d/*" in new, "the source line survives")

    for line in new.splitlines():
        if "169.254.1.1/16" in line or "vlan-raw-device enp8s0f1np1" in line:
            check(line.lstrip().startswith("#"),
                  f"old management line commented: {line.strip()[:40]}")
    check("# auto sw2363" in new, "the old sw2363 stanza is commented")
    check("# auto enp8s0f1np1" in new, "the old backplane stanza is commented")
    # Commented, not deleted - whoever has to undo this at 2am needs to see
    # the address and MTU that were working.
    check("169.254.1.1/16" in new, "the old address is still readable")

    again, touched2 = v.comment_out_legacy(new, {"enp8s0f1np1", "sw2363"})
    check(touched2 == [] and again == new, "idempotent - a second pass is a no-op")


def test_teardown_restores_the_original():
    print("\n== uncomment_legacy (teardown)")
    commented, _ = v.comment_out_legacy(LEGACY, {"enp8s0f1np1", "sw2363"})
    back, restored = v.uncomment_legacy(commented)
    # The strongest statement teardown can make: the file it leaves behind is
    # byte-for-byte the one install.sh produced. Anything less and "undo"
    # becomes "something else that also seems to work".
    check(back == LEGACY, "comment -> uncomment round-trips exactly")
    check(sorted(restored) == ["enp8s0f1np1", "sw2363"],
          "both stanzas are reported as restored")

    untouched, restored2 = v.uncomment_legacy(LEGACY)
    check(untouched == LEGACY and restored2 == [],
          "a file with nothing commented is left alone")

    # Someone who tidied the commented block away gets a fresh stanza rather
    # than a host with no management VLAN at all.
    fresh = v.plain_mgmt_stanzas("enp8s0f1np1")
    check("vlan-raw-device enp8s0f1np1" in fresh,
          "the fallback puts the VLAN back on the bare NIC")
    check("169.254.1.1/16" in fresh and "vlan-id 2363" in fresh,
          "the fallback keeps the address and VLAN id")
    check("bridge" not in fresh, "the fallback mentions no bridge at all")


def test_mgmt_comment():
    print("\n== the management-bridge comment")
    c = v.mgmt_comment("swbr0")
    check(c.startswith("#"), "it is a comment line ifupdown2 will keep")
    check("swbr0" in c, "it names where guests should go instead")

    out, changed = v.add_mgmt_comment(LEGACY, "vmbr0", c)
    check(changed, "added to the management bridge's stanza")
    stanza = out.split("auto vmbr0")[1].split("\nsource")[0]
    check(c in stanza, "inside vmbr0's stanza, not somewhere else in the file")
    # The stanza carries this host's address. Labelling it must not disturb
    # a single setting in it.
    for line in ("    address 10.0.0.10/24", "    gateway 10.0.0.1",
                 "    bridge-ports enp15s0"):
        check(line in out, f"vmbr0 keeps {line.strip()}")
    check(len(out.splitlines()) == len(LEGACY.splitlines()) + 1,
          "exactly one line added")

    again, changed2 = v.add_mgmt_comment(out, "vmbr0", c)
    check(again == out and not changed2, "idempotent - never doubles up")

    back, removed = v.remove_mgmt_comment(out, c)
    check(removed and back == LEGACY, "teardown removes it exactly")
    check(v.remove_mgmt_comment(LEGACY, c) == (LEGACY, False),
          "removing one that is not there changes nothing")

    # A host whose default route is not over a bridge gets no comment
    # rather than one on a guessed interface.
    check(v.add_mgmt_comment(LEGACY, None, c) == (LEGACY, False),
          "no management bridge found -> nothing written")
    check(v.add_mgmt_comment(LEGACY, "vmbr9", c) == (LEGACY, False),
          "a bridge with no stanza -> nothing written")

    # Someone else's comment stays, and stays first.
    mine = "#hands off, this is the uplink"
    withtheirs = LEGACY.replace("iface vmbr0 inet static",
                                "iface vmbr0 inet static\n" + mine)
    out2, _ = v.add_mgmt_comment(withtheirs, "vmbr0", c)
    check(out2.index(mine) < out2.index(c),
          "an existing comment is kept above ours")


def test_every_stanza_is_labelled():
    print("\n== interface comments")
    out = v.render_managed("enp8s0f1np1", [("dmz", "100")])
    # Proxmox shows these in the bridge picker when someone adds a VM NIC,
    # which is the moment the difference between vmbr0 and swbr0 matters.
    for iface, needle in (("enp8s0f1np1", "backplane link"),
                          ("swbr0", "lan-net"),
                          ("sw2363", "management VLAN"),
                          ("dmz", "VLAN 100")):
        # Stop at the next stanza or at the closing marker - the END line
        # is a comment too, and it belongs to the block, not to dmz.
        stanza = out.split(f"iface {iface} ")[1].split("\nauto ")[0]
        stanza = stanza.split(v.END)[0]
        comments = [l for l in stanza.splitlines() if l.startswith("#")]
        check(len(comments) == 1, f"{iface} has exactly one comment line")
        check(comments and needle in comments[0],
              f"{iface}'s comment says what it is ({needle})")
        # A '#' line indented like an option is a syntax error to some
        # parsers; PVE writes them flush left and so must we.
        check(comments and not comments[0].startswith(" "),
              f"{iface}'s comment is flush left")

    # install.sh writes the same two stanzas before this tool ever runs, and
    # teardown puts them back. Three copies of the same sentence is two too
    # many places to drift.
    sh = open(os.path.join(BUNDLE, "install.sh")).read()
    check(v.COMMENT_BACKPLANE.lstrip("#") in sh,
          "install.sh writes the same backplane comment")
    check(v.COMMENT_MGMT_VLAN.lstrip("#").replace("2363", "$VLAN") in sh
          or v.COMMENT_MGMT_VLAN.lstrip("#") in sh,
          "install.sh writes the same management-VLAN comment")
    fallback = v.plain_mgmt_stanzas("enp8s0f1np1")
    check(v.COMMENT_BACKPLANE in fallback and v.COMMENT_MGMT_VLAN in fallback,
          "teardown's fallback stanzas keep both comments")


def test_prune_backplane_replay():
    print("\n== pruning the te2 replay file")
    with tempfile.TemporaryDirectory() as d:
        check(v.prune_backplane_replay("100", d) == (None, None),
              "no file is not an error")

        # Two user VLANs: deleting one leaves the other, and the management
        # VLAN is never touched - it is the one this session rides on.
        v.write_backplane_replay("trunkMemberVLANs", "1,100,200,2363", d)
        action, remaining = v.prune_backplane_replay("100", d)
        check(action == "rewritten", "the file is rewritten, not removed")
        check(t.vlan_list_parse(remaining) == {1, 200, 2363},
              "only the deleted VLAN goes")
        body = open(os.path.join(d, "26-backplane-vlans.xml")).read()
        check("200" in body and "2363" in body and "<te2>" not in body,
              "and that is what lands on disk")

        # Down to the firmware defaults: the file has nothing left to say,
        # and a stale one replaying a VLAN nobody has is worse than none.
        action, _ = v.prune_backplane_replay("200", d)
        check(action == "deleted", "the last user VLAN takes the file with it")
        check(not os.path.exists(os.path.join(d, "26-backplane-vlans.xml")),
              "the file is gone")

        # A file that only ever held defaults is removed on first prune.
        v.write_backplane_replay("trunkMemberVLANs", "1,300,2363", d)
        check(v.prune_backplane_replay("300", d)[0] == "deleted",
              "1 and the management VLAN alone are not worth replaying")


def test_startup_plan():
    print("\n== boot ordering")
    check(v.parse_startup("order=2,up=90") == {"order": "2", "up": "90"},
          "startup string parses")
    check(v.parse_startup("1") == {"order": "1"}, "a bare order parses")
    check(v.parse_startup("") == {}, "unset parses to nothing")
    check(v.format_startup({"up": "90", "order": "1"}) == "order=1,up=90",
          "order is written first, the way Proxmox writes it")

    boot = {"onboot": True, "startup": "order=1", "bridges": {"vmbr0"}}
    # Nothing on our bridges yet: the delay would postpone every other guest
    # at boot and buy nobody anything.
    vms = {"900": dict(boot), "5412": {"onboot": True, "startup": "",
                                       "bridges": {"vmbr0"}}}
    check(v.startup_plan(vms, ["swbr0"], "900") == [],
          "no dependents -> no delay is proposed")

    # One guest on swbr0: now both ends need setting.
    vms["901"] = {"onboot": True, "startup": "", "bridges": {"swbr0"}}
    plan = {p[0]: p[2] for p in v.startup_plan(vms, ["swbr0"], "900")}
    # The delay belongs on the BOOTSTRAP VM: qm(1) says up= waits before
    # starting the NEXT guest, so on the dependent VM it would do nothing
    # for that VM at all. This is the whole reason this function exists.
    check(plan.get("900") == "order=1,up=90",
          "the wait goes on the bootstrap VM, not the dependent one")
    check(plan.get("901") == "order=2",
          "the dependent VM only gets an order, no delay")

    # A guest on a named per-VLAN bridge counts exactly the same.
    vms["902"] = {"onboot": True, "startup": "", "bridges": {"dmz"}}
    plan2 = {p[0]: p[2] for p in v.startup_plan(vms, ["swbr0", "dmz"], "900")}
    check(plan2.get("902") == "order=2", "named bridges are covered too")

    # Already correct, and a manual guest, are both left alone.
    vms["903"] = {"onboot": True, "startup": "order=5", "bridges": {"swbr0"}}
    vms["904"] = {"onboot": False, "startup": "", "bridges": {"swbr0"}}
    got = {p[0] for p in v.startup_plan(vms, ["swbr0", "dmz"], "900")}
    check("903" not in got, "a guest already ordered after the bootstrap VM")
    check("904" not in got, "a guest that does not start at boot")

    # Symmetry: the last dependent leaving takes the delay with it, or
    # every other guest pays 90s at every boot for a bridge nothing uses.
    gone = {"900": {"onboot": True, "startup": "order=1,up=90",
                    "bridges": {"vmbr0"}},
            "5412": {"onboot": True, "startup": "", "bridges": {"vmbr0"}}}
    plan4 = {p[0]: p[2] for p in v.startup_plan(gone, ["swbr0"], "900")}
    check(plan4.get("900") == "order=1",
          "no dependents left -> the delay comes back off")
    # But only when it is exactly ours. Someone else's number is theirs.
    gone["900"]["startup"] = "order=1,up=45"
    check(v.startup_plan(gone, ["swbr0"], "900") == [],
          "a delay we did not set is left alone")

    # An existing longer delay is not shortened.
    vms["900"]["startup"] = "order=1,up=120"
    plan3 = {p[0]: p[2] for p in v.startup_plan(vms, ["swbr0"], "900")}
    check("900" not in plan3, "a longer delay already set is left alone")
    check(v.startup_plan(vms, ["swbr0"], None) == [],
          "no bootstrap VM found -> no plan, rather than a guess")


def test_boot_unit_ships_and_is_ordered():
    print("\n== encs-switch-startup.service")
    unit = open(os.path.join(BUNDLE, "encs-switch-startup.service")).read()
    # The entire value of this unit is landing before the guests start. Get
    # the ordering wrong and it fixes the boot AFTER the one that broke.
    check("Before=pve-guests.service" in unit,
          "runs before Proxmox starts the guests")
    check("After=pve-cluster.service" in unit,
          "runs after pmxcfs, or /etc/pve/qemu-server is not there to read")
    check("--fix" in unit and "--quiet" in unit,
          "applies the plan, and only speaks when it changed something")
    # A failure here must never stop guests from booting: bad ordering is a
    # slow network for a minute, a failed dependency is no VMs at all.
    check("SuccessExitStatus=0 1" in unit, "a failure cannot block the guests")

    sh = open(os.path.join(BUNDLE, "install.sh")).read()
    check("encs-switch-startup" in sh, "install.sh enables it")
    rel = open(os.path.join(HERE, "release.sh")).read()
    check(rel.count("encs-switch-startup.service") >= 2,
          "release.sh stages it and lists it in the MANIFEST")


def test_staged_gui_changes():
    print("\n== the Proxmox staging file")
    # The GUI never writes /etc/network/interfaces directly: it stages
    # interfaces.new and copies it over on Apply Configuration. An apply
    # landing between our write and the operator's ifreload would restore
    # the old sw2363 stanza while the bridge in interfaces.d stayed - two
    # definitions of one interface, and no management VLAN.
    real = v.IFACES_NEW
    try:
        with tempfile.TemporaryDirectory() as d:
            v.IFACES_NEW = os.path.join(d, "interfaces.new")
            check(v.staged_gui_changes() is False,
                  "no staging file -> nothing pending")
            open(v.IFACES_NEW, "w").write("auto lo\n")
            check(v.staged_gui_changes() is True,
                  "a staging file is detected")
    finally:
        v.IFACES_NEW = real

    src = open(os.path.join(BUNDLE, "encs-switch-vnet")).read()
    # Both commands write /etc/network/interfaces, so both have to check.
    for fn in ("def cmd_init", "def cmd_teardown"):
        body = src.split(fn)[1].split("\ndef ")[0]
        check("staged_gui_changes()" in body,
              f"{fn[4:]} refuses to write while a GUI change is staged")
        check('"force"' in body or "force" in body,
              f"{fn[4:]} has an override for it")


def test_vm_parsing():
    print("\n== parse_vm_nets")
    conf = """\
boot: order=virtio0
memory: 2048
net0: virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0
net1: virtio=AA:BB:CC:DD:EE:02,bridge=swbr0,tag=100
net2: virtio=AA:BB:CC:DD:EE:03,bridge=swbr0
name: test

[snapshot-old]
net1: virtio=AA:BB:CC:DD:EE:02,bridge=swbr0,tag=999
"""
    nets = v.parse_vm_nets(conf)
    check(("net1", "swbr0", "100") in nets, "a tagged vNIC is found")
    check(("net2", "swbr0", None) in nets, "an untagged vNIC has no tag")
    check(("net0", "vmbr0", None) in nets, "other bridges are reported too")
    # Snapshots are appended to the same file and hold stale copies of
    # everything; counting them would report VMs on VLANs nobody is using.
    check(all(tag != "999" for _, _, tag in nets),
          "snapshot sections are ignored")


# ========================================================== switch guards
def test_norm_port():
    print("\n== norm_port")
    for given, want in (("gi0", "gi0"), ("GE1/0", "gi0"), ("ge1/7", "gi7"),
                        ("GE1-3", "gi3"), ("5", "gi5"), (" gi2 ", "gi2")):
        check(v.norm_port(given) == want, f"{given!r} -> {want}")
    for bad in ("gi8", "te2", "GE0/0", "vmbr0", "", "gi"):
        check(v.norm_port(bad) is None, f"{bad!r} refused")


def test_parse_ports():
    print("\n== parse_ports")
    # One parser for --ports and for the interactive picker: a list that is
    # valid on the command line has to stay valid at the prompt.
    check(v.parse_ports("gi0,gi3")[0] == ["gi0", "gi3"], "comma separated")
    check(v.parse_ports("0 3")[0] == ["gi0", "gi3"], "space separated")
    check(v.parse_ports("GE1/0, GE1/7")[0] == ["gi0", "gi7"], "panel names")
    check(v.parse_ports(" gi2 ")[0] == ["gi2"], "surrounding space")
    check(v.parse_ports("gi1,gi1,gi1")[0] == ["gi1"], "duplicates collapse")
    check(v.parse_ports("gi5,gi2")[0] == ["gi5", "gi2"],
          "order is preserved, not sorted")
    for bad, why in (("", "empty"), ("   ", "whitespace only"),
                     ("gi0,te2", "a backplane port"), ("gi9", "out of range"),
                     ("vmbr0", "a host bridge")):
        ports, err = v.parse_ports(bad)
        check(ports is None and err, f"{why} refused with a reason")


def test_check_vlan():
    print("\n== check_vlan")
    check(v.check_vlan("100") is None, "100 is a usable VLAN")
    check(v.check_vlan("4094") is None, "4094 is a usable VLAN")
    # Each of these is a way to lose something: VLAN 1 is not what the caller
    # thinks it is, 2363 is this session, and 2350-2449 is the module fabric
    # that switch_util.py:290 refuses outright.
    check("untagged" in (v.check_vlan("1") or ""),
          "VLAN 1 explains the untagged attachment instead")
    check("management" in (v.check_vlan(t.MGMT_VLAN) or ""),
          "the management VLAN is refused")
    for bad in ("2350", "2400", "2449"):
        check("reserved" in (v.check_vlan(bad) or ""), f"{bad} is reserved")
    check(v.check_vlan("2450") is None, "2450 is just outside the band")
    for bad in ("0", "4095", "abc", "-1"):
        check(v.check_vlan(bad) is not None, f"{bad!r} is not a VLAN id")


def test_carried_on_backplane():
    print("\n== carried_on_backplane")
    trunk = {"switchportModeAdmin": "12",
             "trunkMemberVLANs": "2-2349,2363,2450-4093",
             "trunkNativeVID": "1"}
    ok, how = v.carried_on_backplane(trunk, "100")
    check(ok and "trunk" in how, "a trunk carrying the range carries the VLAN")
    check(v.carried_on_backplane(trunk, "2400")[0] is False,
          "a VLAN outside the trunk list is not carried")
    check(v.carried_on_backplane(trunk, "1")[0] is True,
          "the native VLAN counts as carried")

    gen = {"switchportModeAdmin": "10", "generalTaggedVLANs": "100,200"}
    check(v.carried_on_backplane(gen, "200")[0] is True, "general/tagged carries")
    check(v.carried_on_backplane(gen, "300")[0] is False,
          "general without the tag does not carry")

    acc = {"switchportModeAdmin": "11", "accessPVID": "1"}
    check(v.carried_on_backplane(acc, "100")[0] is False,
          "an access port carries only its own VLAN")
    # Unknown mode returns None, not False: "cannot tell" and "no" lead to
    # different advice, and guessing here would send someone to fix the
    # wrong end.
    check(v.carried_on_backplane({"switchportModeAdmin": "13"}, "100")[0] is None,
          "an unreasonable mode reports unknown rather than guessing")


class FakePost:
    """Records posts; every write in this tool goes through set_vlan_interface."""

    def __init__(self):
        self.bodies = []

    def set_vlan_interface(self, ifname, **kw):
        vals = "".join(f"<{k}>{x}</{k}>" for k, x in sorted(kw.items())
                       if x is not None)
        body = (f"<DeviceConfiguration><VLANInterfaceISList action=\"set\">"
                f"<Entry><interfaceName>{ifname}</interfaceName>{vals}</Entry>"
                f"</VLANInterfaceISList></DeviceConfiguration>")
        self.bodies.append((ifname, kw, body))
        return body


def test_fix_backplane_merges():
    print("\n== fix_backplane")
    sw = FakePost()
    trunk = {"switchportModeAdmin": "12", "trunkMemberVLANs": "2-10,2363"}
    _, (field, merged) = v.fix_backplane(sw, trunk, "100")
    # action="set" replaces the WHOLE list. Sending just the new id would
    # drop te2 out of VLAN 2363 - i.e. out of the session doing the writing.
    check(field == "trunkMemberVLANs", "trunk mode writes the trunk list")
    ids = t.vlan_list_parse(merged)
    check(2363 in ids and 100 in ids and 2 in ids,
          "the existing VLANs survive the merge")
    check(sw.bodies[0][0] == "te2", "the write targets te2")
    check("switchportModeAdmin" not in sw.bodies[0][1],
          "the port's mode is left alone")

    sw = FakePost()
    gen = {"switchportModeAdmin": "10", "generalTaggedVLANs": "2363"}
    _, (field, merged) = v.fix_backplane(sw, gen, "100")
    check(field == "generalTaggedVLANs", "general mode writes the tagged list")
    check(t.vlan_list_parse(merged) == {100, 2363}, "merged, not replaced")

    # An access-mode te2 is not something this can reason about, and the
    # cost of being wrong is a cold power cycle.
    check_raises(lambda: v.fix_backplane(FakePost(), {"switchportModeAdmin": "11"},
                                         "100"),
                 "an access-mode backplane port is refused, not guessed at")


def test_backplane_replay_file():
    print("\n== 26-backplane-vlans.xml")
    with tempfile.TemporaryDirectory() as d:
        f = v.write_backplane_replay("trunkMemberVLANs", "2-10,100,2363", d)
        check(os.path.basename(f) == "26-backplane-vlans.xml",
              "sorts straight after 25-vlan-ports.xml")
        body = open(f).read()
        root = ET.fromstring(body)
        check(root.tag == "DeviceConfiguration", "one DeviceConfiguration")
        table = root.find("VLANInterfaceISList")
        check(table is not None and table.get("action") == "set",
              "one table, action=set")
        check(table.findtext("Entry/interfaceName") == "te2", "te2 only")
        check(table.findtext("Entry/trunkMemberVLANs") == "2-10,100,2363",
              "the merged list is what replays")
        # The empty-value element is what hangs the ASIC until AC is pulled.
        # Nothing this tool writes may contain one.
        for el in root.iter():
            if len(el) == 0 and el.tag != "version":
                check(el.text not in (None, ""),
                      f"<{el.tag}> is not an empty set element")


def main():
    for fn in (test_render, test_block_in_the_main_file,
               test_comment_out_legacy,
               test_teardown_restores_the_original, test_mgmt_comment,
               test_every_stanza_is_labelled, test_prune_backplane_replay,
               test_startup_plan, test_boot_unit_ships_and_is_ordered,
               test_staged_gui_changes,
               test_vm_parsing,
               test_norm_port, test_parse_ports, test_check_vlan,
               test_carried_on_backplane,
               test_fix_backplane_merges, test_backplane_replay_file):
        fn()
    print(f"\n{CHECKS[0]} checks, {len(FAILURES)} failed")
    for f in FAILURES:
        print(f"  FAILED: {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
