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
| `11-port-settings.xml` | description, speed, duplex, flow control | separate from `10-ports.xml` on purpose — see below |
| `15-lag.xml` | LAG membership and mode | groups must be formed before VLANs reference them |
| `20-vlans.xml` | which VLANs exist | |
| `25-vlan-ports.xml` | which ports are in them, and how they tag | needs the VLANs to already exist |
| `30-poe.xml` | PoE on/off per port | depends on nothing |
| `35-stp-global.xml` | STP on/off and mode | only written when STP is running |
| `36-stp-bridge.xml` | bridge priority and timers | |
| `37-stp-ports.xml` | per-port cost, priority, portfast, guards | needs the ports |
| `40-storm.xml` | storm-control rates | only if a rate is non-zero |
| `41-span-dest.xml` | mirror session destination | the destination creates the session |
| `42-span-src.xml` | what each session mirrors | a source naming a missing session is rejected |
| `45-lldp-global.xml` | LLDP on/off, advertise interval | only if not at defaults |
| `46-lldp-ports.xml` | per-port LLDP tx/rx state | |
| `47-cdp-ports.xml` | per-port CDP enable | |
| `50-lacp-global.xml` | LACP system priority, load balance | only if not at defaults |
| `51-lacp-ports.xml` | per-port LACP priority and timeout | |
| `55-mac-aging.xml` | aging interval | only if not the default 300s |
| `56-static-mac.xml` | static forwarding entries | needs the VLANs |
| `60-acls.xml` | ACL names and types | **before** the rules that name them |
| `61-aces.xml` | ACL rules | an ACE naming a missing ACL is rejected |
| `62-acl-bindings.xml` | which ports an ACL is bound to | needs the ACL |
| `65-qos-global.xml` | QoS mode and trust | only when QoS is not disabled |
| `66-qos-cos.xml` | per-port default CoS | |
| `67-cos-queue.xml` | CoS → egress queue map | |
| `68-policers.xml` | aggregate policers | |
| `70-radius.xml` | RADIUS servers | **before** 802.1X — see the warning below |
| `71-dot1x-global.xml` | 802.1X system auth control | only when enabled |
| `72-dot1x-ports.xml` | per-port control and host mode | |
| `75-multicast.xml` | snooping and filtering globals | only when enabled |
| `76-igmp-vlans.xml` | per-VLAN snooping settings | needs the VLANs |
| `77-igmp-routers.xml` | static and forbidden router ports | needs the snooping VLAN |
| `80-pvlan.xml` | which VLANs are private, and their role | |
| `81-pvlan-assoc.xml` | primary → secondary associations | needs the primary |
| `82-pvlan-hosts.xml` | host ports | needs the association |
| `83-pvlan-promisc.xml` | promiscuous ports | needs the association |
| `84-ip-routing.xml` | unicast routing on/off | **before** the routes it governs |
| `85-gateway.xml` | default gateway | |
| `86-routes.xml` | static routes | statics only; connected routes are skipped |
| `87-arp.xml` | static ARP entries | |
| `88-arp-timeout.xml` | ARP aging | only if not the default |

**One file is one table.** `--apply` posts each file as a single request body,
and a request body is one `DeviceConfiguration` around one table. That is why
an area spanning three tables is three files rather than one.

`11-port-settings.xml` is deliberately not folded into `10-ports.xml`, even
though both write `Standard802_3List`. `10-ports.xml` is the one file standing
between a cold power cycle and a switch that forwards nothing, and it has been
replayed against hardware in exactly its current shape. A firmware that
rejects an unfamiliar `speedAdmin` should not be able to take the port enables
down with it.

Only files that have something to say are written. No LAGs configured means no
`15-lag.xml`; STP left off means no `35`–`37` at all. A plain L2 switch still
saves five files, which is the point — replaying firmware defaults would be
harmless but would bury the handful that matter.

> ⚠ **`70-radius.xml` holds the RADIUS shared secret in clear text.** The
> switch reports `keyString` back in the clear and a server cannot be replayed
> without it. `--save` creates that file `0600` and sets `/etc/encs-switch` to
> `0700`. Think before committing the directory to version control.

You can add your own files. `90-mystuff.xml` is applied after everything
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

**Implemented** (read *and* write, saved and replayed):

| Area | NFVIS path | wcd tables | Here |
|---|---|---|---|
| Port admin state | `/switch/interface/gigabitEthernet` | `Standard802_3List` | Ports view, `space` |
| Port description, speed, flow control | `/switch/interface/gigabitEthernet` | `Standard802_3List` | Ports view, `ENTER` |
| VLANs, including names | `/switch/vlan` | `VLANList` | VLANs view, `n` / `d` / `N` |
| VLAN port membership | `/switch/switchports` | `VLANInterfaceISList` | VLANs view, `ENTER` |
| Link aggregation | `/switch/port-channel`, `channel-group` | `Standard802_3List`, `LAGList` | Ports view, `g` |
| LACP tuning | `/switch/lacp` | `LACPGlobalSetting`, `LACPPortList` | `TAB` → lacp |
| PoE | `/switch/power` | `PoEPSEInterfaceList` | PoE view, `space` |
| MAC table, flush, counters, counter clear | `/switch/mac`, statistics | `ForwardingTable`, `StatisticsList`, `PortStatisticsClear` | MAC `f`, Stats `z` |
| Static MAC entries, aging | `/switch/mac` | `ForwardingStaticTable`, `ForwardingGlobalSetting` | `TAB` → staticmac |
| Spanning tree (STP/RSTP) | `/switch/spanning-tree` | `SpanningTreeGlobalParam`, `STP`, `RSTP` | `TAB` → stp |
| Storm control | `/switch/interface/*/storm-control` | `StormControlTable` | `TAB` → storm |
| Port mirroring, local SPAN | `/switch/monitor` | `SpanSourceTable`, `SpanDestinationTable` | `TAB` → mirror |
| LLDP / CDP advertisement | `/switch/lldp` | `LLDPGlobalSetting`, `LLDPInterfaceList`, `CDPInterfaceList` | `TAB` → lldp |
| Private VLANs | — | `PrivateVLAN*` | `TAB` → pvlan |
| MAC ACLs, rules, port bindings | `/switch/interface/*/service-acl` | `ACLList`, `ACEList`, `ACLBindingList` | `TAB` → acl |
| QoS: mode, trust, port CoS, CoS→queue, policers | `/switch/qos`, `/switch/class-map` | `QoSSettingGlobalParam`, `CoSSetting`, `CoSToQueueMappingList`, `AggregatePolicerList` | `TAB` → qos |
| 802.1X + RADIUS | `/switch/dot1x`, `/switch/radius-server` | `Standard_802_1x*`, `RadiusServerList` | `TAB` → dot1x, radius |
| IGMP/MLD snooping | `/switch/ip/igmp`, `/switch/bridge` | `IGMPMLD*`, `MulticastGlobalSetting` | `TAB` → igmp |
| L3: static routes, gateway, static ARP, routing | `/switch/arp`, `/switch/ip/route`, `/switch/ip/routing` | `ARPList`, `IPv4RouteList`, `IPv4GatewayList`, `IPv4GlobalSetting` | `TAB` → l3 |

**Writable by the client, no view.** `encs-switch-tui` can post these but
nothing in the UI reaches them; drive them from `encs-switch-api` or a hand-
written replay file:

| Area | wcd tables |
|---|---|
| MSTP regions, revisions, instances, instance→VLAN maps | `MSTP`, `MSTPGlobalSetting`, `MSTPInstanceList`, `MSTPVLANList`, `MSTPInterfaceList` |
| Class maps and policy maps (the policers *are* in the UI) | `ClassMapList`, `PolicyMapList` |
| DSCP mutation, remarking, DSCP→queue map | `DSCPMutationTable`, `DSCPRemark`, `DSCPMapping` |
| Per-port egress shaping | `QoSBandwidthList` (indexed by interface, not queue, despite the name) |
| ~~Per-port PoE power limit~~ **(now in the UI: PoE view, `ENTER`)** | `PoEPSEInterfaceList`. Priority and 4-pair are *not* on this firmware — measured 2026-08-12, the table returns only `adminEnable`, `detectionStatus`, `interfaceName`, `outputPower`, `powerClassification`, `powerLimit`. Cisco's templates write more fields than this build reports. |
| IGMP **forbidden** router ports | `IGMPMLDSnoopRouterPortList` — static router ports *are* in the UI (`TAB` → igmp, `t`) |

**Not implemented, and not simply unwritten.** These are blocked on
information that is not in the extracted source:

| Area | What is missing |
|---|---|
| IPv4 ACL rules | `switch-confd` only ever built MAC ACLs — the `<IPv4Parameters>` element names appear nowhere in it. MAC ACLs are complete. A guessed IPv4 rule would be accepted by the switch and never match, which is the worst possible failure. |
| LLDP neighbour discovery | There is no neighbour table. confd touches only `LLDPGlobalSetting` and `LLDPInterfaceList`. Nothing on this firmware answers "what is plugged into GE1/3". |
| Remote SPAN | Needs a reflector port and a remote VLAN (`sourceType` 4, `isReflector` 2). Local SPAN works. |
| Port security | `InterfaceSecurityTable` templates exist but the mode/violation enums were not pinned down. |

Two practical notes. Everything above is **volatile** like the rest of the
config, so anything you add by hand via `encs-switch-api` also needs a file in
`/etc/encs-switch/` to survive a power cycle — and one file must hold exactly
one table. And **spanning tree is off until you turn it on**: fine until you
cable two front ports to the same upstream switch, so either enable it
(`TAB` → stp → `g`) or be careful.

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
