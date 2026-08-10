# The switch config files

Everything the Marvell ASIC knows lives in RAM. It has **no flash**. A cold
power cycle wipes it back to VLAN 1 + 2363, every front port SHUT, PoE off and
all four LAGs empty.

So the XML files under `/etc/encs-switch/` are not a backup of the switch —
**they are the switch's real configuration**. The ASIC just holds a copy until
the next power loss. Keep them in version control.

| Event | Config |
|---|---|
| VM restart, bootstrap service restart | **survives** — the ASIC is not power-cycled |
| Cold power cycle, AC loss | **lost** — replayed from these files |

`encs-switch-replay.service` reapplies them automatically once the switch
answers after a boot.

---

## The workflow

```sh
encs-switch-tui        # set things up interactively, then press c, then w
```

or from a shell:

```sh
encs-switch-tui --save     # running switch state  -> /etc/encs-switch/*.xml
encs-switch-tui --apply    # /etc/encs-switch/*.xml -> running switch
```

`--apply` posts every `*.xml` in the directory **in filename order**. That
ordering is the whole reason for the numeric prefixes.

| File | Holds | Why it sorts there |
|---|---|---|
| `10-ports.xml` | admin UP/DOWN per `gi` port | ports must exist and be enabled first |
| `15-lag.xml` | LAG membership and mode | groups must be formed before VLANs reference them |
| `20-vlans.xml` | which VLANs exist | |
| `25-vlan-ports.xml` | which ports are in them, and how they tag | needs the VLANs to already exist |
| `30-poe.xml` | PoE on/off per port | last; depends on nothing |

Only files that have something to say are written — no LAGs configured means
no `15-lag.xml`.

You can add your own files. `40-mystuff.xml` is applied after everything
above; `05-first.xml` before all of it.

---

## The format

Every file is one `DeviceConfiguration` document posted to the switch's `wcd`
endpoint. The shape is always the same:

```xml
<?xml version='1.0' encoding='utf-8'?>
<DeviceConfiguration>
    <version>1.0</version>
    <TableName action="set">
        <Entry>...</Entry>
    </TableName>
</DeviceConfiguration>
```

`action` is `set` (creates *and* modifies), `delete`, or `add`.

Test any hand-written file before trusting it to a reboot:

```sh
encs-switch-api set myfile.xml     # expect <statusString>OK</statusString>
```

`encs-switch-api get '{TableName}'` reads a table back.

---

## 10-ports.xml — admin state

```xml
<Standard802_3List action="set">
    <Entry><interfaceName>gi0</interfaceName><adminState>1</adminState></Entry>
    <Entry><interfaceName>gi1</interfaceName><adminState>2</adminState></Entry>
</Standard802_3List>
```

| `adminState` | |
|---|---|
| `1` | up |
| `2` | shut |

Interface names are `gi0`–`gi7` (front panel), `te1`–`te4` (backplane),
`LAG1`–`LAG4`.

> Do not shut `te1`/`te2`. They are the internal backplane links carrying your
> traffic and the management session — shutting one disconnects you from the
> switch, and the only fix is a power cycle.

## 15-lag.xml — link aggregation

Membership is a property of the **member port**, not of the group. There is no
"add port to LAG" call; you set `LAGID` on the port itself.

```xml
<Standard802_3List action="set">
    <Entry>
        <interfaceName>gi3</interfaceName>
        <LACPEnabled>2</LACPEnabled>
        <LAGID>1</LAGID>
    </Entry>
</Standard802_3List>
```

| Field | |
|---|---|
| `LAGID` | `1`–`4`. `0` means "no group". |
| `LACPEnabled` | `1` = on (static bundle), `2` = auto (LACP) |

To remove a port from its group, send **both** zeros — that is the pair Cisco's
own `no channel-group` sent:

```xml
<Entry><interfaceName>gi3</interfaceName><LACPEnabled>0</LACPEnabled><LAGID>0</LAGID></Entry>
```

Read membership back from a different table — `{LAGList}`, not
`{Standard802_3List}`:

```sh
encs-switch-api get '{LAGList}'
```

```xml
<LAGEntry>
    <interfaceName>LAG1</interfaceName>
    <PortList>
        <PortEntry><portName>gi3</portName><membershipType>1</membershipType></PortEntry>
    </PortList>
</LAGEntry>
```

`membershipType`: `1` active, `2` inactive, `3` not candidate.

> A static `on` bundle facing an LACP peer black-holes traffic with no error on
> either side. Prefer `auto`.

## 20-vlans.xml — VLANs

```xml
<VLANList action="set">
    <VLAN><VLANID>100</VLANID></VLAN>
</VLANList>
```

`action="delete"` removes one. **Never delete VLAN 2363** — it carries your
management session — or VLAN 1, the default. The TUI refuses both; a
hand-written file will not.

This file only says which VLANs *exist*. Which ports are in them is the next
file.

## 25-vlan-ports.xml — port membership

Membership is a property of the **port**, written to `VLANInterfaceISList`:

```xml
<VLANInterfaceISList action="set">
    <Entry>
        <interfaceName>gi3</interfaceName>
        <switchportModeAdmin>10</switchportModeAdmin>
        <generalPVID>1</generalPVID>
        <generalTaggedVLANs>100,200</generalTaggedVLANs>
        <generalUntaggedVLANs>300</generalUntaggedVLANs>
    </Entry>
</VLANInterfaceISList>
```

| `switchportModeAdmin` | |
|---|---|
| `10` | general — mix of tagged and untagged, PVID for ingress |
| `11` | access — single untagged VLAN, from `generalPVID` |
| `12` | trunk — tagged members plus a native VLAN |
| `13` / `15` | private-vlan / customer |

`generalTaggedVLANs` and `generalUntaggedVLANs` take a comma/range list
(`100,200-204`). The read-back view is `VLANInterfaceMembershipTable`, which
presents the same information per *VLAN* rather than per port — useful for
checking your work:

```sh
encs-switch-api get '{VLANInterfaceMembershipTable}'
```

> **`te1`–`te4` are deliberately not saved.** `te2` carries the management
> VLAN this tool talks over, and `te1`/`te3`/`te4` are the module fabric that
> the firmware configures itself on VLANs 2350/2351. Replaying a stale copy of
> those is a good way to cut your own session. `save`/`--save` writes `gi` and
> `LAG` interfaces only.

Verified end to end on hardware: create a VLAN, tag a port into it, save,
delete both, replay — VLAN and membership both come back identical.

## 30-poe.xml — Power over Ethernet

```xml
<PoEPSEInterfaceList action="set">
    <Interface><interfaceName>gi0</interfaceName><adminEnable>1</adminEnable></Interface>
</PoEPSEInterfaceList>
```

| `adminEnable` | |
|---|---|
| `1` | on |
| `2` | off |

Only the `gi` front ports are PoE-capable. 802.3bt has been confirmed working
on real hardware.

---

## What NFVIS could do that this cannot

`switch-confd` subscribed to 23 top-level ConfD paths and drove 91 distinct
`wcd` tables. This project implements a deliberate subset — the things needed
to get a switch forwarding and keep it that way across a power cycle.

**Implemented** (read *and* write):

| Area | NFVIS path | Here |
|---|---|---|
| Port admin state | `/switch/interface/gigabitEthernet` | Ports view, `space` |
| VLANs | `/switch/vlan` | VLANs view, `n` / `d` |
| VLAN port membership | `/switch/switchports` | saved + replayed (no UI editor yet) |
| Link aggregation | `/switch/port-channel`, `channel-group` | Ports view, `g` |
| PoE | `/switch/power` | PoE view, `space` |
| MAC table, counters | `/switch/mac`, statistics | read-only views |

**Not implemented.** All of these have working `wcd` tables and Cisco XML
templates in `switch-confd`, so any of them is a tractable addition — nothing
here is blocked, just unwritten:

| Area | NFVIS path | wcd tables |
|---|---|---|
| Spanning tree (STP/RSTP/MSTP) | `/switch/spanning-tree` | `STP`, `RSTP`, `MSTP*`, `SpanningTreeGlobalParam` |
| QoS: class maps, policy maps, policers, queueing | `/switch/qos`, `/switch/class-map`, `/switch/policy-map`, `/switch/priority-queue`, `/switch/wrr-queue` | `ClassMapList`, `PolicyMapList`, `AggregatePolicerList`, `CoS*`, `DSCP*`, `QoSBandwidthList` |
| ACLs | `/switch/interface/*/service-acl` | `ACLList`, `ACEList`, `ACLBindingList`, `IPStandardACLList` |
| 802.1X + RADIUS | `/switch/dot1x`, `/switch/radius-server` | `Standard_802_1x*`, `RadiusServerList`, `EAPStatisticsList` |
| IGMP/MLD snooping, multicast filtering | `/switch/ip/igmp`, `/switch/bridge` | `IGMPMLD*`, `MulticastGlobalSetting`, `UnregedMulticastList` |
| Storm control | `/switch/interface/*/storm-control` | `StormControlTable` |
| LLDP / CDP | `/switch/lldp` | `LLDP*`, `CDPInterfaceList` |
| L3: ARP, static routes, default gateway | `/switch/arp`, `/switch/ip/route`, `/switch/ip/routing` | `ARPList`, `IPv4RouteList`, `IPv4GatewayList`, `IPv4InterfaceList` |
| Port mirroring (SPAN) | `/switch/monitor` | `SpanDestinationTable` |
| Private VLANs | — | `PrivateVLAN*` |
| Static MAC entries, aging | `/switch/mac` | `ForwardingStaticTable`, `ForwardingGlobalSetting` |
| LACP system priority, port priority/timeout | `/switch/lacp` | `LACPGlobalSetting`, `LACPPortList` |

Two practical notes. Everything above is **volatile** like the rest of the
config, so anything you add by hand via `encs-switch-api` also needs a file in
`/etc/encs-switch/` to survive a power cycle. And the switch defaults are
sane for a flat L2 deployment — no STP is running, which is fine until you
create a loop, so be careful cabling two front ports to the same upstream
switch.

## Enum reference

Values the TUI translates for you, for when you are reading raw XML.

| Field | Values |
|---|---|
| `adminState` | 1 up · 2 shut |
| `linkState` | 1 up · 2 down · 6 not present |
| `mediaType` | 1 copper · 2 fiber (the `te` ports report "fiber"; they are backplane traces) |
| `duplexOperMode` | 2 full · 3 half · 4 n/a |
| `LACPEnabled` | 1 on/static · 2 auto/LACP |
| `membershipType` | 1 active · 2 inactive · 3 not candidate |
| `adminEnable` (PoE) | 1 on · 2 off |
| `detectionStatus` (PoE) | 1 disabled · 2 searching · 3 delivering · 4 fault · 5 test · 6 other |
| `addressType` (MAC) | 1 static · 2/3 dynamic · 4 self |

---

## Gotchas

**Curly braces are curl globbing.** `encs-switch-api` handles this, but if you
call the API with raw `curl`, `{VLANList}` gets expanded and the braces are
stripped before the request goes out — the switch then returns an empty
`ActionStatus` and you will chase it for hours. Use `curl -g`, or quote it.

**The credentials reset on every cold boot.** They are `cisco`/`cisco`, the ROS
firmware default. A changed password lives in RAM like everything else. This is
also why the management VLAN must stay off your routable network.

**`--apply` is not a diff.** It posts every file. Applying a config that omits
something does not remove that something from a running switch — it just
does not set it. To truly reset, power-cycle and replay.

**Test before you rely on replay.** A file that fails at boot fails silently
from your point of view; you find out when a port does not come up. Check with:

```sh
encs-switch-tui --apply && systemctl status encs-switch-replay
```
