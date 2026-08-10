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
| `10-ports.xml` | admin up/shut per `gi` port | ports must exist and be enabled first |
| `15-lag.xml` | LAG membership | groups must be formed before VLANs reference them |
| `20-vlans.xml` | VLAN creation | |
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

Port membership lives in `VLANInterfaceMembershipTable`
(`taggedPorts` / `untaggedPorts`), which the TUI displays but does not yet
write. Set it by hand if you need it, and add it as, say, `25-vlan-ports.xml`
so it lands after the VLANs exist.

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
