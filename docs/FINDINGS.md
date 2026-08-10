# NFVIS 4.15.5 → ENCS5412 driver extraction: findings

> **Prior art.** The core discovery — that the Marvell switch can be booted from
> a passthrough VM using `mv_pciboot` + `remote-bootd` out of `switch-confd`,
> with an SMBIOS override to satisfy the platform gate — is
> [yeyus](https://forums.servethehome.com/index.php?members/yeyus.35233/)'s, in
> [Switch bootstrap VM](https://yeyus.notion.site/Switch-bootstrap-VM-04cac1d64bde48b684c51e8dac524245).
> This document starts from that and works out the rest: what else is on the
> ISO, why 4.16+ cannot work, how the management plane is driven, and the
> failure modes.

Source: `Cisco_NFVIS-4.15.5-FC4.iso` (2.7 GB, built 2026-02-17 by "NFVIS Jenkins CI")

## 1. Base distro

**AlmaLinux 8.9**, stock kernel — not a Cisco kernel fork.

| Item | Value |
|---|---|
| Release package | `almalinux-release-8.9-1.el8.x86_64` |
| Kernel | `kernel-4.18.0-513.18.2.el8_9.x86_64` (stock AlmaLinux 8.9) |
| Compiler | GCC 8.5.0 (Red Hat 8.5.0-20) |
| Packages on ISO | 1174 |
| ISO layout | Standard Anaconda installer (`Packages/`, `repodata/`, `images/install.img`, `isolinux/`) |

It is a rebuilt AlmaLinux 8.9 minimal install + ~40 Cisco RPMs. All Cisco out-of-tree modules
carry `vermagic=4.18.0-513.18.2.el8_9.x86_64 SMP mod_unload modversions` and are signed by an
`NFVIS-REL` key (only relevant under Secure Boot).

Note: the earlier community writeup used NFVIS 4.13.1 on AlmaLinux **8.6**. Cisco moved the base
to 8.9 by 4.15.5, so the kernel to match is now `4.18.0-513.18.2.el8_9`.

## 2. Complete out-of-tree driver inventory

Derived from the ISO's `repodata` filelists (authoritative, covers all 1174 packages).

| Package | Modules | Relevance to ENCS5412 |
|---|---|---|
| `switch-confd-4.15.5-FC4` | `mv_pciboot.ko.xz` | **Marvell L2 switch bootstrap — the critical one** |
| `nic-xl710-i350-4.15.5-FC4` | `i40e.ko`, `i40e_csp.ko`, `i40evf.ko`, `iavf.ko`, `igb.ko`, `igb_csp.ko`, `igb_m6.ko`, `ixgbe.ko`, `tg3.ko` | **RJ45/SFP media selection + hardware service chaining** |
| `qat-1.8.L.1.2.0_00041` | `intel_qat.ko`, `qat_c3xxx*`, `qat_c4xxx`, `usdm_drv` | Optional crypto offload |
| `tabei-plat`, `tabei-m-plat` | `cpldha`, `dash_fpga_*`, `i2c-dash`, `nios_v2`, `pca955x`, `ltc4215`, `ice` | Other platform (Catalyst 8200 UCPE) — **not ENCS** |
| `kodachi` | `mcu.ko` | Other platform — not ENCS |
| `cwan-app` | `GobiNet`, `GobiSerial` | Cellular NIM modules |
| `nfvis-fwupdate` | `PEGAWMI.ko`, `SECBOOT.ko` | BIOS/firmware update tooling |

Platform gate — `switch-confd`'s RPM scriptlet checks `dmidecode -s system-product-name` against:
`CSX-1006`, `CSX-1006-P`, `CSX-1008`, `CSX-1008-P`, `ENCS5406/K9`, `ENCS5408/K9`, `ENCS5412/K9`.
Anything else → "This Platform is not CSX. Exiting". This is why the SMBIOS override is needed in a VM.

## 3. How the Marvell switch actually boots

The switch is a **Marvell BobCat2 / MSYS Prestera**, PCI ID **`11ab:be00`**. It has no onboard
flash, so the host CPU must push firmware into it on every boot.

Files, all in `/opt/switch-confd/` from `switch-confd`:

| File | Size | Role |
|---|---|---|
| `mv_pciboot.ko.xz` | 1.0 MB (mostly DWARF) | Kernel shim: resizes PCI BARs, exposes `/dev/servicecpu` |
| `remote_boot_app` | 16 KB | Userspace loader (the actual logic) |
| `booton.bin` | 200 KB | Stage-1 bootstrap → service CPU SRAM |
| `switch_firmware.bin` | 26 MB | u-boot + Linux + ROS → service CPU DDR |
| `swcli`, `switch_*.py`, `switch.fxs` | — | ConfD management layer (VLANs, PoE, STP, LLDP, 802.1X, QoS) |

Boot sequence (`/etc/init.d/remote-bootd`): `insmod mv_pciboot.ko.xz` → run `remote_boot_app` →
scan `/sys/bus/pci/devices/*/config` for `0xbe0011ab` → reconfigure BARs → load `booton.bin` to
SRAM → IRQ to wake service CPU → load `switch_firmware.bin` to DDR → u-boot → kernel → ROS ready.

### The module is small and portable

`mv_pciboot.ko` is a thin misc-char-device shim, **21 undefined symbols total**:

```
__fentry__  __stack_chk_fail  __x86_return_thunk  _copy_from_user  _copy_to_user
_dev_info  dev_printk  misc_register  misc_deregister  pci_get_device
pci_read_config_word/dword  pci_write_config_word/dword  pci_assign_resource
pcibios_bus_to_resource  pcim_iomap  pcim_iounmap  release_resource
remap_pfn_range  printk
```

Verified against Ubuntu 24.04 / kernel 6.8: **19 of 21 present unchanged**. The two misses are
`printk` and `dev_printk`, which became `_printk` in 5.15 — a source-level macro rename that
resolves automatically on recompile. No API in this module has been removed or reworked.

Its own functions: `servicecpu_{init,open,read,write,lseek,mmap,ioctl,release,setup,shutdown}`,
`mv_{resize_bar,rescan_resources,read_and_assign_bars}`, `mvSrvCpu_{init,cleanup}`, plus a local
copy of `__pci_read_base` (kernel-internal, not exported — this is the one part that would need
re-syncing against a modern kernel's version).

### Partial source is already on the ISO

`switch-confd-debugsource` ships:
- `SOURCES/remote-boot/remote_boot.c` (589 lines) — the complete userspace loader
- `SOURCES/remote-boot/remote_boot.h` (174 lines) — **the full kernel/userspace ABI**:
  all 8 `SERVICECPU_IOC_*` ioctls, MSYS register offsets, the state machine, status codes

`remote_boot.h` is **Marvell tri-licensed: Commercial / GPLv2 / BSD**. `mv_pciboot.ko` itself
declares `license=GPL`, `author=Marvell Semi.`

## 4. The RJ45/SFP "magic" the ServeTheHome thread couldn't explain

It's in Cisco's forked `igb` (`/opt/nic-pkg/igb.ko.xz`, version `5.4.0-6-k`). Unique to it:

```
parm=encs_type:LEDs behaved different between two type of ENCS
def_media / def_media_init / copper_tries
"MAS: changing media to copper"  /  "MAS: changing media to fiber/serdes"
```

Stock in-tree `igb` has Media Auto Sense, but Cisco added an explicit default-media concept plus
retry logic. `igb_csp` and `igb_m6` are variants without `encs_type` — for ENCS use plain `igb`.

`i40e` is `version=1.4.22.7-14-ciscocsx`, built from `i40e-1.4.21.7-1-ciscocsx` — an Intel
out-of-tree i40e from ~2016, forked to add floating-VEB support. Companion userspace:
`encs_i40e_switch.py`, `encs_i40e_vsi.py`, `encs_i40e_mirror.py`, `HwServiceChain.py`.

## 4b. Runtime architecture — how the switch is actually used

Three independent paths. Confirmed from `remote_boot.c`, `switch_info.py`, `switch_settings.py`
and `nic-pkg/virtintf`.

```
   8x front RJ45 LAN ports (PoE)          G0/G1 (I350, RJ45+SFP)   MGMT (I210)
              |                                    |                    |
   +----------v----------------+                   |                    |
   |   Marvell BobCat2 ASIC    |                   |                    |
   |   (11ab:be00) runs ROS    |                   |                    |
   |   L2 forwarding in HW     |                   |                    |
   +---+-------------------+---+                   |                    |
       | 2x 10G backplane  | PCIe cfg/BAR          |                    |
       |                   |                       |                    |
  +----v-------------------v-----------------------v--------------------v----+
  |                        HOST (Proxmox / AlmaLinux)                        |
  |   XL710 = data      /dev/servicecpu = boot        I350          I210     |
  +--------------------------------------------------------------------------+
```

**1. Data path — never touches the host CPU.** Front-port-to-front-port switching happens entirely
inside the ASIC at line rate. The host sees the switch only as two 10G XL710 links (the backplane).
To Proxmox those are ordinary NICs: bridge, bond, or VLAN them normally.

**2. Boot + health path — PCIe, `/dev/servicecpu`.** `remote_boot_app` is a permanent daemon:
`while(1) { state_machine(); sleep(1); }`. It pushes `booton.bin` → SRAM and
`switch_firmware.bin` → DDR, then polls a scratchpad register every second, tracking a keep-alive
counter. It detects `FW_HANG`, `FW_INVALID`, PEX link loss and can request a switch-side reset.
This is the only path that needs the passed-through PCI device.

**3. Config path — in-band IP over the backplane, not PCIe.** From `nic-pkg/virtintf`:

```sh
ifconfig int-LAN.2363 169.254.1.1 netmask 255.255.0.0 up
route add -host 169.254.1.0/32 dev int-LAN.2363
```

The host tags **VLAN 2363** on the XL710 backplane interface, takes `169.254.1.1`, and the
switch's ROS management sits at `169.254.1.0`. All configuration is then plain **HTTPS against
Marvell's "wcd" web/XML API**:

```
https://169.254.1.0/System.xml?action=login&user=$username&password=$password
https://169.254.1.0/wcd?{VLANList}  {ForwardingTable}  {ACLList}  {ARPList} ...
```

`switch-confd` is *only* a translator: it subscribes to ConfD CDB paths (`/switch/vlan`,
`/switch/interface`, `/switch/spanning-tree`, `/switch/qos`, `/switch/ip/radius`, …) and converts
each change into these HTTPS calls. It is not privileged and holds no special hardware access.

(A third TLV/BSM path exists — `sendToBMC`, `lan1_bsm_port_cfg(slot, bay, port, …)`,
`cman_bmc_agent_tlv.c`. The slot/bay addressing indicates it targets the modular NIM platforms
rather than ENCS; `switch-service.service` is explicitly *disabled* by the RPM scriptlet.)

### Consequences for a Proxmox deployment

- The bootstrap VM is **infrastructure, not a data-plane element**. No traffic traverses it.
  A 2 vCPU / 2 GB VM is sufficient regardless of switch throughput.
- **You do not need `switch-confd` or ConfD.** Once the ASIC is booted, anything with a route to
  `169.254.1.0` on VLAN 2363 can configure the switch — including Proxmox itself, or a browser on
  your laptop. That sidesteps the entire ConfD/YANG stack; you only need the boot half.
- **The VM must keep running.** The loader is a watchdog daemon, and firmware is volatile — the
  ASIC re-learns nothing across a power cycle. Set `onboot=1` with an early startup order.
- **Untested and worth verifying:** whether stopping the VM drops the switch. ROS should keep
  forwarding on its own, but VM shutdown may trigger a PCI function-level reset on the passed-through
  device, which would down the ASIC. Test before relying on it.
- **Do not put Proxmox's management interface behind the switch** — that creates a bootstrap
  dependency loop (you would need the switch up to reach the host that boots the switch). Keep
  Proxmox management on the I210 MGMT port or G0/G1.
- **VLAN 2363 is reserved** for the switch management channel. Don't reuse it.

## 4c. Verified on live hardware

Unit at `<host-ip>`, running **NFVIS 4.15.5-FC4** — the exact build this ISO installs.
Chassis `FGLxxxxxxxx`, board `FOCxxxxxxxx`. All checks read-only via `support show`.

| Question | Result |
|---|---|
| **IOMMU group of the Marvell device** | **Group 50, sole occupant** — passthrough is clean |
| **SMBIOS product name** | **`ENCS5412/K9`** — platform gate passes natively |
| Marvell switch BDF | `11ab:be00` rev 01, 1 MB + 64 MB + 4 MB BARs. **The BDF is not stable** — observed at `0d:00.0` under NFVIS and Proxmox, then `0e:00.0` after a Proxmox reinstall (where `0e:00.0` had previously been the I210 MGMT NIC). yeyus recorded `0e:00`. Always derive it: `lspci -d 11ab:be00`. |
| Backplane NIC | **X710** (not XL710) at `08:00.0/.1`, netdev `int-LAN`, MTU 9216 |
| Management path | `int-LAN.2363` @ `169.254.1.1` → switch `169.254.1.0`, **ARP REACHABLE** (`c4:f7:d5:xx:xx:9c`) |
| Front ports | 8 × `gigabitEthernet 1/0`–`1/7`, all `1G-Copper` / `MediaType RJ45` |
| PoE | Live: class-based, 200 W budget, 0 W drawn |
| I350 (G0/G1) | Enumerates as **"I350 Gigabit *Fiber* Network Connection"** at `02:00.0/.1` |

Two findings worth calling out:

**Passthrough is viable.** `0d:00.0` sits alone in IOMMU group 50. It is behind a Pericom
PI7C9X2G304 PCIe packet switch, but the bridges land in their own groups (47/48/49) — ACS is
functioning across the box (48 ACS capabilities present). Nothing needs to be co-assigned to
the VM, and no ACS-override patch is required.

**The `def_media` finding is confirmed by hardware.** The I350s enumerate as the *Fiber* variant,
yet the switch's own ports report `MediaType RJ45`. Cisco's `igb` fork is what drives the
copper/serdes media selection — the ServeTheHome thread's "NFVIS does some magic to make the
RJ45s work" is exactly this.

The management path was confirmed end-to-end: a live ARP entry for `169.254.1.0` in state
`0x2` (REACHABLE) on `int-LAN.2363`, plus routes `169.254.0.0/16 dev int-LAN.2363 src 169.254.1.1`
and `169.254.1.0 dev int-LAN.2363 scope link`. The `nic-pkg/virtintf` script is not aspirational —
it is the running configuration.

**Still untested:** whether shutting down the bootstrap VM drops the switch (PCI function-level
reset on device release). This cannot be checked without disrupting the live unit.

## 4d. Physical port map (verified on hardware)

**The X710 has no front-panel ports.** It is purely internal. The ENCS 5412 front panel exposes
11 network ports driven by three different chips:

| Front port | Chip | PCI | Linux name | MAC |
|---|---|---|---|---|
| **MGMT** | Intel I210 | `0e:00.0` | `MGMT` | `c4:f7:d5:xx:xx:98` |
| **GE0-0** (WAN) | Intel I350 | `02:00.0` | `GE0-0` | `c4:f7:d5:xx:xx:24` |
| **GE0-1** (WAN) | Intel I350 | `02:00.1` | `GE0-1` | `c4:f7:d5:xx:xx:25` |
| **GE1-0 … GE1-7** (LAN, PoE) | Marvell BobCat2 | `0d:00.0` | *none — not host NICs* | `…:9d` → `…:a4` |

Marvell switch ports as the ASIC reports them (`show switch`):

| Switch CLI | MAC | | Switch CLI | MAC |
|---|---|---|---|---|
| `gigabitEthernet 1/0` | `c4:f7:d5:xx:xx:9d` | | `1/4` | `c4:f7:d5:xx:xx:a1` |
| `1/1` | `c4:f7:d5:xx:xx:9e` | | `1/5` | `c4:f7:d5:xx:xx:a2` |
| `1/2` | `c4:f7:d5:xx:xx:9f` | | `1/6` | `c4:f7:d5:xx:xx:a3` |
| `1/3` | `c4:f7:d5:xx:xx:a0` | | `1/7` | `c4:f7:d5:xx:xx:a4` |

Switch management interface: `c4:f7:d5:xx:xx:9c` @ `169.254.1.0`.

**These 8 ports have no Linux interface on the host at all** — they exist only inside the ASIC.
That is why they disappear completely without `switch_firmware.bin`: there is no host-side driver
to fall back to.

### Internal X710 (both functions)

| Linux name | PCI | MAC | Connects to |
|---|---|---|---|
| `int-LAN` | `08:00.1` | `c4:f7:d5:xx:xx:2b` | **Marvell switch backplane, 10G** — verified, = `te2` |
| `int-ngio` | `08:00.0` | `c4:f7:d5:xx:xx:2a` | NIM slot — **not wired to the switch**, see below |

`int-LAN` is confirmed by the live ARP entry for `169.254.1.0` on `int-LAN.2363` and by Cisco's
kickstart (`change_intf_name "enp8s0f1" "int-LAN"`). An X552 at `05:00.0` appears unconnected.

### Mapping te1-te4, and why link state lies (tested 2026-08-10)

**Both X710 ports go to the switch. `int-ngio` does not go to the NIM slot** — the name misleads.

Method that works — stamp a unique MAC on the NIC, generate a frame, read `{ForwardingTable}`:

```sh
ip link set enp8s0f0np0 down
ip link set enp8s0f0np0 address 02:00:00:de:ad:01
ip link set enp8s0f0np0 up
ping -c2 -I enp8s0f0np0 224.0.0.1;  arping -c3 -I enp8s0f0np0 169.254.99.99
encs-switch-api get '{ForwardingTable}'     # -> 02:00:00:de:ad:01 on te1
```

| Port | Far end | Evidence |
|---|---|---|
| `te1` | `int-ngio`, X710 `08:00.0` | synthetic MAC `02:00:00:de:ad:01` learned on `te1` |
| `te2` | `int-LAN`, X710 `08:00.1` | `c4:f7:d5:xx:xx:2b` learned on `te2`; VLAN 2363 tagged on `te2` only |
| `te3`, `te4` | **unproven** | down, autoneg admin = 2 and *cannot be enabled* — see below |

The firmware will not let you turn autonegotiation on for `te3`/`te4`, and says why:

```
POST autoNegotiationAdminEnabled=1 for te3
  -> statusCode 3
     "Fiber Ethernet port te3 doesn't support Auto Negotiation enabled mode"
   (the identical write against gi0 returns statusCode 0 / OK)
```

So **te3/te4 are fiber ports**, which by itself excludes them being any RJ45 jack on the panel.
Combined with the SFP test above — which proved both `GE0/x` cages belong to the I350 — and with
`int-ngio` proven to be `te1`, every front-panel connector is accounted for and **none of them is
te3 or te4**.

#### te3/te4 are the expansion-module (NIM) ports — per Cisco's own code

Elimination only gets you so far. `switch-confd` states the mapping outright.

`switch_main.py:1201` — `lan1_bsm_l2_op_cfg(slot, bay, vid, port, opcode, mac_sz, mac)`, addressed
by **slot/bay/port** as a modular chassis would be:

```python
if (port == 0):
    switch_te_mac_setup("te3", mac, 2350)      # module port 0 -> te3
    switch_te_mac_setup("te3", mac, 2351)
    switch_cp_dp_mac_settings('aa:bb:cc:dd:ee:f0', 2351)   # CP
    switch_cp_dp_mac_settings('aa:bb:cc:dd:ee:f1', 2350)   # DP
    switch_te_mac_setup("te1", 'aa:bb:cc:dd:ee:f0', 2351)  # host end of the fabric
    switch_te_mac_setup("te1", 'aa:bb:cc:dd:ee:f1', 2350)
elif (port == 1):
    switch_te_mac_setup("te4", mac, 2350)      # module port 1 -> te4
```

`switch_te_mac_setup()` installs a **permanent** (static) forwarding entry. `switch_lan1_settings()`
comments the scheme: *"First pair is for CP, the rest is DP for module 1,2,3"* — so **VLAN 2351 is
the control plane and 2350 the data plane** of an internal module fabric.

`set_module_default_setting()` (`switch_interfaces.py:2411`) configures all four accordingly:

| Port | Config | Reading |
|---|---|---|
| `te1` | PVID 2351, **tagged** 2350+2351, LLDP/CDP off | the **host's** trunk into the module fabric |
| `te2` | trunk `2-2349,2363,2450-4093` | ordinary data + the 2363 management VLAN |
| `te3` | PVID 2351, **untagged** 2350+2351, LLDP/CDP off | module port 0 |
| `te4` | PVID 4095, untagged 2350, LLDP/CDP off | module port 1 |

`switch_main.py:1742` reserves the whole band by creating VLANs 2350–2353 plus 2363 at startup, and
`switch_util.py:290` refuses user VLANs inside 2350–2449.

**This also resolves the "NGIO" naming.** `int-ngio` is physically `te1` (proven by MAC learning),
but its *purpose* is to reach the expansion module — via the ASIC on VLANs 2350/2351, not
point-to-point. The old "int-ngio goes to the NIM slot" inference was functionally right and
physically wrong; both halves now have evidence.

Caveats, because this is code-reading rather than measurement: the sibling `lan1_bsm_port_cfg()`
and `lan1_bsm_port_enable_cfg()` are stubbed out (`return`), the RPM scriptlet disables
`switch-service.service` on ENCS, and **no NIM is fitted on the test chassis** (filler panel in
place), so te3/te4 have never been seen up. The mapping is Cisco's stated intent, not an observed
link.

This confirms the §4 topology diagram ("2× 10G backplane") and **retires the earlier `int-ngio` =
NIM-slot inference.** With no NIM fitted here, te3/te4 remain the only candidates for a NIM path,
and that is still a guess. Anyone with a populated NIM slot can settle it in one read.

#### Do not map these ports by link state

An earlier pass concluded the opposite — that `int-ngio` was not wired to the switch — because
toggling it changed no switch port. **That method is invalid on this hardware.** On a 10GBASE-KR
backplane link the serdes stays trained regardless of the netdev's admin state, so:

- `te1` reports `linkState` UP permanently, even with `enp8s0f0np0` administratively down
- bringing that NIC up or down produces **no** change in the switch's link state
- the switch happily floods broadcast out `te1` into a dead end

Counters make it obvious, and are the honest signal:

```
port      rx bytes    rx pkts     tx bytes    tx pkts
te1           1694          0      4157513      39361   <- rx frozen, tx climbing
te2        3287576      41141      2248727       1608   <- both moving
te3/te4          0          0            0          0
```

**MAC learning is the only reliable way to map a backplane port.** `encs-switch-tui` now derives
the mapping this way at runtime (cross-referencing `{ForwardingTable}` against the host's own NIC
MACs from sysfs) and flags a trained-but-silent link as `UP idle` in yellow, precisely because
`UP` next to `UP` gave no way to tell a working link from a dead one.

### Front-panel labels — verified by cabling (2026-08-10)

The API says `gi0`..`gi7`; the chassis silkscreen says **`GE1/0`..`GE1/7`** (slash, matching
Cisco's CLI form — `switch_port_info.py` `port_xlate` maps `gi` → `gi1/`). The TUI shows the panel
label alongside the API name.

Method — shut every port, enable exactly one, see what links:

| Cable in | Port enabled | Result |
|---|---|---|
| `GE1/0`, top-left jack | `gi0` only | link UP 1000 |
| `GE1/7`, bottom-right jack | `gi7` only | link UP 1000, learned `3c:ec:ef:xx:xx:xx` |
| `GE1/4`, top row 3rd column | `gi4` only | link UP 1000 |

All three land where the naming predicts, so **`gi<n>` = `GE1/<n>`**. Each test enabled exactly one
port with the other seven left shut, so a link could only come from the jack under test.

The physical arrangement is **column-major** — four vertically stacked RJ45 pairs, numbered top
then bottom within each pair, not left to right across the row. The silkscreen heads the block
`GE LAN` and numbers the jacks `1/0`..`1/7` (commonly written `GE1/0`, and `gi1/0` in Cisco's CLI):

```
   GE LAN
   1/0   1/2   1/4   1/6
   1/1   1/3   1/5   1/7
```

Cross-checked against Cisco's own published ENCS **5406** front-panel diagram, which shows the same
`1/0`/`1/6` top and `1/1`/`1/7` bottom numbering — so the layout is not 5412-specific.

`GE1/1` is directly *below* `GE1/0`. This is worth knowing before someone counts along the top row
and unplugs the wrong customer. It also explains why the two endpoint tests could not settle the
layout on their own: top-left and bottom-right are index 0 and 7 under both row-major and
column-major, so they were consistent with either. The layout above came from reading the
silkscreen; the cabling only anchored the two ends.

None of that changes the API mapping, which is index → **printed label** — the label being what the
operator actually reads. With both ends and an interior point confirmed, `gi1`/`gi2`/`gi3`/`gi5`/
`gi6` are interpolated across a regular four-pair structure; a permutation that fixes 0, 4 and 7
while scrambling the rest is not a credible failure mode. Only the 5412 has been checked — 5406 and
5408 are unconfirmed.

#### The rest of the front panel is not on the switch

Everything except the eight `GE1/x` jacks bypasses the Marvell ASIC entirely. None of these MACs
ever appear in the switch's forwarding table.

| Panel label | Hardware | State on this box |
|---|---|---|
| **CONSOLE** (top) | `ttyS0` — real 16550A, `0x3F8` IRQ 4 | serial console to the host CPU, 115200 8N1. See below. |
| **CIMC** (bottom) | the BMC's serial port | out-of-band CLI to the CIMC |
| **MGMT CPU** (top) | I210, `0f:00.0`, `enp15s0` | `Port: Twisted Pair`, link up 1000. Proxmox at `<host-ip>`. This is the jack NFVIS used for its own management. |
| **MGMT CIMC** (bottom) | the BMC's own NIC — invisible to the host, absent from `lspci` | CIMC at `<cimc-ip>` |
| **GE0/0** (top row) | I350 `02:00.0`, `enp2s0f0` | RJ45 **and** SFP jack, one logical port |
| **GE0/1** (bottom row) | I350 `02:00.1`, `enp2s0f1` | RJ45 **and** SFP jack, one logical port |

#### The CONSOLE port is a full out-of-band recovery path

Verified 2026-08-10: an RJ45→USB serial adapter on **CONSOLE** at **115200 8N1** lands straight on
Proxmox. Confirmed from both ends — a login prompt on the client, and on the host
`serial-getty@ttyS0` respawning at the moment of connection plus a `root ttyS0` entry in `last`.
`/proc/tty/driver/serial` shows `ttyS0` as a real `16550A` at `0x3F8` with live tx/rx counters;
`ttyS1` has no hardware behind it.

This survives a Proxmox reinstall because the installer writes
`/etc/default/grub.d/installer.cfg`:

```
GRUB_TERMINAL_INPUT="console serial"
GRUB_TERMINAL_OUTPUT="gfxterm serial"
GRUB_SERIAL_COMMAND="serial --unit=0 --speed=115200"
GRUB_CMDLINE_LINUX="$GRUB_CMDLINE_LINUX console=ttyS0,115200"
```

Note the setting is **not** in `/etc/default/grub` itself, which looks alarming — but a
`grub-mkconfig` dry run confirms the snippet is picked up and `console=ttyS0,115200` is preserved,
so a kernel update will not silently cost you the console. GRUB's own menu is on the serial line
too, so the boot menu and the `single` recovery entry are both reachable.

That matters on this platform more than most: the switch management VLAN is link-local, the switch
config is volatile, and a mis-set `BACKPLANE` or a shut `te2` can cut you off from the ASIC. The
CONSOLE port depends on none of that.

**MGMT CPU and MGMT CIMC are two physically separate jacks**, not one port shared over a BMC
sideband. The host reaches `<cimc-ip>` out `vmbr0`/`enp15s0` — i.e. out of the MGMT CPU jack,
across the external LAN, and back in through the MGMT CIMC jack. They share a subnet only because
both are patched into the same LAN. (An earlier revision of this document claimed the CIMC shared
the I210 PHY via NC-SI. It does not.)

**`GE0/0` and `GE0/1` are dual-media**: each is one I350 function fronted by both an RJ45 and an
SFP cage — top row is `GE0/0`, bottom row is `GE0/1`. Under a stock in-tree `igb` both report
`Supported ports: [ FIBRE ]` and `Port: FIBRE`, and **the RJ45 side will not link at all**.
Selecting copper needs Cisco's `igb` fork and its `def_media` knob — see §5 Q3.

Confirmed positively by walking one 1000BASE-T copper SFP between the two cages and watching which
function could read its EEPROM (`ethtool -m`) — the module identifies as
`Identifier 0x03 (SFP) / Transceiver type: Ethernet: 1000BASE-T`:

| SFP in | `enp2s0f0` (`02:00.0`) | `enp2s0f1` (`02:00.1`) |
|---|---|---|
| `GE0/0` cage | **reads EEPROM** | `Input/output error` |
| `GE0/1` cage | `Input/output error` | **reads EEPROM** |

The readout follows the module, so `GE0/0` = `02:00.0` and `GE0/1` = `02:00.1`. **No switch port
changed state in either position.** Together with the firmware's own refusal message for `te3`
(below), that rules out any `GE0/x` jack — copper or optical — being a switch port.

This is positive evidence rather than absence: an empty cage returns an I/O error, so a successful
EEPROM read proves that cage is wired to that specific I350 function.

Note the SFP cage is the *only* way to get a `GE0/x` port up on stock Proxmox, since the RJ45 side
is unreachable without Cisco's `igb`. Otherwise MGMT CPU is the only usable uplink.

#### Why the RJ45 side is dead, precisely

Cisco documents these as dual-media with **fiber taking priority over copper when both are live at
boot**. That is not the mechanism at work under Proxmox, and the distinction matters — do not
expect "pull the SFP and the RJ45 wakes up". Measured with an *empty* cage, the copper side still
never linked. The reason is further down the stack:

| Evidence | Meaning |
|---|---|
| PCI ID `8086:1522` = *I350 Gigabit **Fiber** Network Connection* | the NVM presents a fiber-variant device, not a dual-media one |
| `Supported ports: [ FIBRE ]`, `Supported link modes: 1000baseKX/Full` | only a SerDes/backplane mode is exposed — no copper mode exists to select |
| in-tree `igb` params are `max_vfs` and `debug` only | **no `def_media` knob**; nothing to override with |
| driver `igb` 6.8.12-41-pve, NVM `1.63, 0x80000e2f` | stock Proxmox 8.4 kernel driver |

So on stock Proxmox the copper connector is not deprioritised — it is simply not exposed. Media
priority is a Cisco-`igb`/NFVIS behaviour and is **not verified here**.

For context on the NFVIS side (reported, not verified by this project): NFVIS 3.10+ binds `GE0-0`
to `wan-br` with DHCP enabled out of the box, which is why `GE0/0` is "the WAN port" in Cisco's
documentation and why its panel annotation reads *"NFVIS and VNF Management via WAN"*.

```
FRONT PANEL
  MGMT CPU    GE0/0: RJ45 SFP     GE1/0 GE1/2 GE1/4 GE1/6   <- stacked pairs,
  MGMT CIMC   GE0/1: RJ45 SFP     GE1/1 GE1/3 GE1/5 GE1/7      numbered DOWN
     |  |       |       |           |  |  |  |                 then across
     |  |    (one I350 function per row,  |  |
     |  |     two jacks, pick one media)  |  |
     |  |       |                 +----+--+--+--+------+
   I210 BMC   I350 02:00.0/.1     |  Marvell BobCat2   |
  0f:00.0                         |  gi0..gi7 = GE1/n  |
     |  |       |                 +---------+----------+
     |  |       |               te1 |  te2  |   ^ PCIe (boot only)
  +--+--+-------+-------------------+-------+---+------------------+
  |  |  ^ BMC, not visible to the host      |   |                  |
  |  |                    HOST CPU          |   |                  |
  |  enp15s0   enp2s0f0/f1     int-ngio  int-LAN        Marvell    |
  |  = Proxmox  = fibre-only    08:00.0   08:00.1       passthru   |
  |              on stock igb   = X710, BOTH to the switch         |
  +----------------------------------------------------------------+
        ^ Proxmox owns these                            ^ bootstrap VM
```

Both X710 functions land on the ASIC (`08:00.0` = `te1`, `08:00.1` = `te2`); only `te2` carries the
management VLAN. `te3`/`te4` exist on the ASIC but are down and unattributed. See §"Mapping
te1-te4" for how that was established and why link state is the wrong tool for it.

Design consequences:

- **GE0-0/GE0-1 are the only routable uplinks** — 2 × 1G, and they are the dual-media I350 ports
  that need Cisco's `igb` for RJ45 mode.
- **All 8 LAN ports share one 10G backplane.** Port-to-port traffic among them never crosses it
  (switched in the ASIC at line rate); only host/VM-bound traffic uses the 10G.

## 5. Answers

**Q1 — AlmaLinux version?** 8.9, kernel `4.18.0-513.18.2.el8_9`, stock.

**Q2 — Extract drivers and build your own AlmaLinux without NFVIS?** Yes, and this is the
low-risk path. Install AlmaLinux 8.9, pin `releasever` to 8.9, install the stock 8.9 kernel, then
drop in `switch-confd` (+ `nic-xl710-i350`). Every module already matches that exact vermagic, so
nothing needs recompiling. The only obstacle is the `dmidecode` platform gate — on real ENCS5412
hardware the SMBIOS product name is already `ENCS5412/K9`, so it passes natively.

**Q3 — Debian/Proxmox?** Split by component:

- **Firmware blobs + userspace** (`booton.bin`, `switch_firmware.bin`, `remote_boot_app`,
  all the Python) — architecture-independent data and a plain dynamically-linked x86-64 ELF.
  These move to Debian unchanged; `alien` or a manual `.deb` works.
- **`mv_pciboot.ko`** — cannot be force-loaded (vermagic + `modversions` CRCs + 4.18→6.8).
  Must be rebuilt from source. Feasible: only 21 stable symbols, ~500 lines, GPL, and the full
  ABI header is already in hand. Source routes: Cisco's GPL/open-source request portal (they are
  obligated — the module is GPL), Marvell's MSYS/Prestera SDK, or reimplementation from
  `remote_boot.h` + the exported function list above. Then package as DKMS.
- **Cisco's `i40e`/`igb` forks** — will *not* build on kernel 6.8. The i40e base is from 2016;
  no kcompat shim stretches 8 years. But mainline 6.8 has perfectly good in-tree `i40e`/`igb`,
  so the XL710 backplane links and SFP ports work. What you lose is the `def_media` RJ45
  selection and floating-VEB hardware service chaining.

### Recommended path for Proxmox

Don't port the module — sidestep it, which is exactly what the yeyus writeup did:

1. Proxmox (Debian, kernel 6.x) on bare metal, IOMMU enabled.
2. Small AlmaLinux 8.9 VM, Q35, 2 vCPU / 2 GB.
3. PCI-passthrough the Marvell device (`11ab:be00`) to that VM.
4. Set the VM's SMBIOS type-1 product to `ENCS5412/K9` to satisfy the platform gate.
5. Install `switch-confd` in the VM; `service remote-bootd start` boots the switch.
6. The switch stays up as long as the VM runs; Proxmox itself uses the XL710 backplane ports
   with in-tree `i40e`.

This needs zero recompilation and is already proven on 4.13.1/AlmaLinux 8.6 — the only change
for 4.15.5 is targeting AlmaLinux 8.9 instead.

A single "custom Proxmox ISO with drivers baked in" is only achievable after obtaining
`mv_pciboot` source and DKMS-ifying it. The VM approach gets you a working switch today.

## 6. Native Proxmox vs. AlmaLinux VM

### Why native-on-Proxmox is blocked (and on what, exactly)

Not on kernel API compatibility — that part is fine (19/21 symbols unchanged on 6.8). The blocker
is narrower: **`mv_pciboot.ko` ships as a binary only.** `switch-confd-debugsource` contains
`remote_boot.c`/`.h` (the *userspace* loader and the ABI) but no module source. Options:

| Route | Effort | Risk |
|---|---|---|
| Cisco GPL source request (module declares `license=GPL`) | Days of waiting, zero technical work | May be slow or refused |
| Marvell MSYS/Prestera SDK source | Low if obtainable | Availability |
| Reimplement from `remote_boot.h` | ~400–600 lines of kernel C | Bounded — full register map and all 8 ioctls are documented |
| VFIO userspace rewrite — no module at all | Highest | Unproven (see below) |

The VFIO route is the most interesting long-term: `remote_boot.c` already pokes PCI config space
directly through `/sys/bus/pci/devices/*/config`, and most of what the module does is program the
device's *internal* MSYS windows (`0x41804`, `0x41808`, `0x41820`–`0x41864`) — plain MMIO writes
that VFIO permits. That would make the bootstrap distro- and kernel-independent forever.
**Caveat: unproven.** The module also does kernel-side BAR reassignment (`pci_assign_resource`,
`mv_resize_bar`, a private `__pci_read_base`), and live `lspci` shows BAR1/BAR2 as `[virtual]` —
i.e. the kernel did *not* assign them, the module did. Whether that is reproducible from userspace
under VFIO is the open question.

Cisco's forked `i40e` (2016 base) will not build on 6.8 either, but in-tree `i40e` drives the X710
backplane fine. The open question there is whether in-tree `igb` can work the G0/G1 RJ45s without
the `def_media` patch — the ports enumerate as *Fiber*, so possibly not.

**Verdict:** native Proxmox is a reverse-engineering project of days-to-weeks with genuine
uncertainty. The VM is an afternoon. Recommended play: file the Cisco GPL request now (costs
nothing), run the VM meanwhile, go native if source arrives — at which point DKMS is easy,
because the API compatibility work is already done.

### Custom AlmaLinux 8.9 image — worth doing, and Cisco hands you the template

The ISO root ships Cisco's entire build recipe: `ks.cfg` (61 KB), `base_rpm.list`,
`kickstart_scripts/{pre,post}script_functions.sh`, and `fake_dmidecode_data.bin`.

Key facts that make this much simpler than expected:

- **`switch-confd` has no meaningful dependencies** — only `/bin/bash`, glibc, pthread. No ConfD,
  no NFVIS stack. `rpm -i` it standalone and `remote-bootd` works.
- Leaner still: the bootstrap needs just **4 files + an init script** — `mv_pciboot.ko.xz`,
  `remote_boot_app`, `booton.bin`, `switch_firmware.bin`. No RPM required at all.
- Custom NIC drivers install by plain overwrite (`post_chroot::copy_i40e_and_igb_drivers`):
  `cp /opt/nic-pkg/{i40e,igb}.ko.xz /lib/modules/$KVER/kernel/drivers/net/ethernet/intel/{i40e,igb}/`

Build outline (**untested — this is the recipe, not a verified result**):

1. Base: AlmaLinux 8.9 minimal. Use the NFVIS ISO's own `Packages/` as a local repo — it carries
   the exact `kernel-4.18.0-513.18.2.el8_9` the modules' vermagic demands.
2. **Pin the kernel or everything breaks:** `echo 8.9 > /etc/yum/vars/releasever` and
   `exclude=kernel*` in `/etc/dnf/dnf.conf`. A kernel update silently kills `mv_pciboot`.
3. Install `switch-confd`; optionally `nic-xl710-i350` + the driver copy above.
4. Interface naming: Cisco maps `enp8s0f1` → `int-LAN`, but **BDFs vary between units** (this box
   has the Marvell at `0d:00.0`, the Notion writeup had `0e:00`). Use Cisco's
   `post_chroot::add_network_rename_udev_rule`, which matches on MAC, not bus address.
5. Network: `int-LAN.2363` @ `169.254.1.1/16` + `route add -host 169.254.1.0/32 dev int-LAN.2363`.
6. `chkconfig --add /etc/init.d/remote-bootd`.

The same image works **bare metal and as a Proxmox guest**:

- Bare metal: SMBIOS already reads `ENCS5412/K9`, gate passes untouched.
- Proxmox guest: set SMBIOS type-1 product to `ENCS5412/K9` in VM Options — cleaner than Cisco's
  own approach, which replaces `/sbin/dmidecode` with a wrapper reading a canned dump
  (`fake_dmidecode_data.bin` is a fake SMBIOS table for `C8200-UCPEVM` — Cisco ships NFVIS
  virtualized this way themselves, so the technique is theirs, not a hack).

## 7. Lifecycle — why this matters

Cisco's position is correct and confirmed from two independent documents:

- **ENCS 5400 EoL bulletin:** "The last supported software release for ENCS5400 will be
  NFVIS 4.15 and IOS-XE 17.15."
- **NFVIS 4.15.X itself has its own EoS/EoL announcement.**

NFVIS **4.18.x does exist**, and ENCS 5400 is not on its documented platform list — 4.18 targets
C8200-UCPE, ENCS 5100, UCS C-series, CSP.

**However, Cisco did publish 4.18.2 under the ENCS 5400 download page.** Per user `peramus` on
ServeTheHome (thread page 11), Cisco "had the newer software (Up to 4.18.2) on the download page
specifically for the 5400 ENCS," now apparently removed. So both statements are true at once:
4.15 is the last *supported* release, but newer images were downloadable for the platform.

**"Supported" was doing real work.** The same post reports: *"Any NFVIS version after 4.15.X will
disable the Marvell switch completely."* That is confirmed — but **not** by the platform-gate
mechanism predicted earlier in this document. The actual mechanism is below, established by
diffing `Cisco_NFVIS-4.18.1-FC2.iso` against 4.15.5.

### How Cisco actually disabled the switch in 4.18.x — one deleted file

4.18.1 (built 2025-08-05, *earlier* than 4.15.5's 2026-02-17 — 4.15.x is the long-term ENCS
branch) runs the **same AlmaLinux 8.9 and the same kernel** `4.18.0-513.18.2.el8_9`, and ships
an **identical package set**: `switch-confd`, `nic-xl710-i350`, `qat`, `tabei-*`, all present.

Things that did *not* change:

- **The platform gate is byte-for-byte identical** — still whitelists `ENCS5406/5408/5412`.
- **`mv_pciboot.ko` is byte-identical** (same SHA-256, `srcversion=B8F7749FA5B99EDF34B470D`).
- `ks.cfg` still carries all 7 `ENCS54`/`CSX-` platform branches.
- 39 of 43 `switch-confd` payload files are byte-identical; `remote_boot_app` differs in only
  841 of 16864 bytes (a recompile — same size, same strings, same paths).

The single functional change:

> **`switch_firmware.bin` (26 MB) is absent from the entire 4.18.1 ISO.**
> No package ships it. Only `booton.bin`, the stage-1 bootstrap, remains.

`remote_boot_app` in 4.18.1 **still hardcodes `/opt/switch-confd/switch_firmware.bin`** — the
open() simply fails. The boot sequence gets as far as loading `booton.bin` into service-CPU SRAM,
then has no OS to hand the ASIC. The switch is inert. That is precisely "disabled completely,"
achieved by deleting one file rather than rearchitecting anything.

**Consequences:**

1. `Cisco_NFVIS-4.15.5-FC4.iso` is the **only** source of `switch_firmware.bin`.
   SHA-256 `e7f4500d1f2808d6ed4711f1927dcc60ebb3dad55aa24e5756effa94f7983b46`, 26,663,887 bytes.
   This one file is the irreplaceable artifact of the entire project — back it up independently
   of the ISO.
2. Version choice is otherwise irrelevant for the switch stack — the module, loader, gate and
   management layer are the same in both releases.
3. It follows that 4.18.1 + the 4.15.5 `switch_firmware.bin` copied into place *should* work
   (identical loader path, identical module, gate still passes). **Untested, and the §7 BIOS
   lockdown warning still applies to installing 4.18.x** — so this is an observation, not a
   recommendation.

### ⚠ BIOS lockdown hazard — read before flashing anything

Also from thread page 11: *"Gotta be careful with loading the newer NFVIS images, they will update
(and lock down) the BIOS where you can't F2 into it during bootup."*

This directly threatens the Proxmox plan, because **you need BIOS access to enable VT-d/IOMMU and
change boot order** — without it, PCI passthrough of the Marvell device is impossible and the
whole §6 design collapses. It is also not obviously reversible.

**Current firmware state of the unit at `<host-ip>` — verified unmodified:**

| | |
|---|---|
| BIOS | `ENCS54_2.5.022720181334` (v2.5, dated 2018-02-27), DMI revision 5.1, "upgradeable" |
| CIMC | `3.2(14.26)`, reachable at `<cimc-ip>` |
| PID / SN | `ENCS5412/K9` / `FGLxxxxxxxx`, hardware-version M3 |
| CPU / RAM / disk | Xeon D-1557, 12 cores / 64 GB / 200 GB |
| Hypervisor stack | QEMU 6.2.0, libvirt 8.0.0, OVS 2.17.6 |

Both BIOS and CIMC are at their original 2018-era versions — NFVIS 4.15.5 did not reflash them.
Every command run against this unit during this investigation was read-only (`show`,
`support show`); no firmware operation was invoked and nothing was written.

Practical rules:
- **Do not install any NFVIS newer than 4.15.5 on this box.** There is no upside — the switch
  stops working anyway — and a real risk of losing BIOS setup access permanently.
- **Verify F2 access works before wiping to Proxmox**, while NFVIS is still there to fall back on.
- Fresh install of 4.15.5 does not appear to auto-flash BIOS — `ks.cfg` only creates
  `/data/fwupdate/{common,register,package}` directories. Firmware update is a separate explicit
  operation via `nfvis-fwupdate` (which ships `biosup_tabei_m`, `fwup_tabei_m`, `SECBOOT.ko`).

Precedent worth noting: `peramus` reports running **Proxmox instead of NFVIS** for stability —
so the target configuration has been attempted by others in that thread.

**Consequence: `Cisco_NFVIS-4.15.5-FC4.iso` is the terminal artifact for this hardware.** There
will be no further Cisco firmware, driver, or security updates for the ENCS 5400 — ever. That
reframes this project from optional to necessary:

- The extracted `switch-confd` payload (`mv_pciboot.ko`, `remote_boot_app`, `booton.bin`,
  `switch_firmware.bin`) is the **final** version of the switch bootstrap that will ever exist.
  Archive the ISO and these artifacts deliberately.
- Pinning to AlmaLinux 8.9 (required by vermagic) means **no kernel security updates**. Fine for
  an isolated lab switch; not fine for anything internet-facing.
- That makes the `mv_pciboot` porting effort (§6) strategically valuable rather than merely
  interesting — it is the only route to a maintained kernel on this box. AlmaLinux 8 itself runs
  to 2029, but only if you can move off the pinned 8.9 point release.

## 8. The custom ISO build

Build assets live on the build server at `<build-server>:<build-dir>/build/`.
Output: `AlmaLinux-8.9-ENCS5412-switch.iso`, volume label `ENCS_SW_89`.

### Strategy

Reuse Cisco's NFVIS ISO **wholesale** as both the package repo and the Anaconda installer,
replacing only the kickstart and adding a small runtime payload. This is deliberate:

- It guarantees the exact kernel `4.18.0-513.18.2.el8_9` that `mv_pciboot`'s vermagic demands.
  AlmaLinux 8.9 is a superseded point release; mirrors serve 8.10 and 8.9 lives only in vault.
- `Packages/` and `repodata/` stay untouched, so no `createrepo` run is needed.
- The UEFI + BIOS El Torito boot layout is already known-good.

### Package selection

`@core` + `switch-confd` + `nic-xl710-i350`. Cisco's comps defines a `platform-encs` group
(`kodachi`, `nfvis-fwupdate`, `nic-xl710-i350`, `switch-confd`) but we do **not** use it wholesale.

Deliberately excluded:

| Excluded | Why |
|---|---|
| **`nfvis-fwupdate`** | **The BIOS/CIMC flasher** (`biosup_tabei_m`, `fwup_tabei_m`, `SECBOOT.ko`). Given the §7 lockdown reports, it stays out of the image entirely. |
| `@flavor` | The NFVIS management stack — docker-ce, vdaemon, apcupsd, tam-service, singleip-overlay. |
| `kodachi` | Different platform (not ENCS). |

### What `%post` does

1. **Pins the kernel** — `echo 8.9 > /etc/yum/vars/releasever` and
   `exclude=kernel* redhat-release* almalinux-release*` in dnf.conf/yum.conf. Without this a
   routine `dnf update` silently kills the switch: the new kernel's vermagic won't match and
   `mv_pciboot` simply refuses to load, with no obvious symptom.
2. **Installs the runtime payload** from `/run/install/repo/encs/`.
3. **Enables `marvell-switch-boot.service` unconditionally.** This matters: `switch-confd`'s own
   RPM scriptlet gates on `dmidecode -s system-product-name`, which *fails inside a VM* that has
   no SMBIOS override at install time. Relying on the scriptlet would yield a VM that installs
   cleanly and never boots the switch.
4. **Copies Cisco's forked `igb`/`i40e`** into `/lib/modules/$KVER/kernel/drivers/net/ethernet/intel/`
   (the `def_media` RJ45 logic) and runs `depmod`. Meaningful on bare metal, harmless in a VM.
5. **Writes `int-LAN.2363` @ `169.254.1.1/16`** — used on bare metal, inert in a passthrough VM
   where the hypervisor owns the X710.
6. **Disables `switch-confd`/`switch-service`** — they import `_confd`, which we don't ship, so
   they would fail noisily every boot. Switch config goes over HTTPS instead (§4b).

### Runtime payload

| File | Purpose |
|---|---|
| `/etc/systemd/system/marvell-switch-boot.service` | Loads `mv_pciboot.ko.xz`, waits for `/dev/servicecpu`, runs `remote_boot_app` (a permanent watchdog daemon) |
| `/usr/local/sbin/encs-switch-status` | Health check: PCI device, module, char device, firmware blob, daemon, **kernel-vs-vermagic mismatch**, switch reachability |
| `/usr/local/sbin/encs-switch-api` | Client for the Marvell ROS `wcd` XML API |

### Switch credentials

Traced from `switch_settings.py:switch_login()`:

```python
if default_password:
    username = "cisco"
    password = "cisco"
```

**Firmware default is `cisco`/`cisco`** at `https://169.254.1.0`. NFVIS replaces it later and
stores the replacement encrypted in the ConfD CDB (`/switch/authenticate/operpassword`), which is
why that credential is unrecoverable without ConfD — and why it doesn't matter for our build.

(The `5w1tch!` in the community writeup was that author's *VM root password*, not the switch's.)

### Boot menu

Three entries: automated install (marked **ERASES DISK**), interactive install with manual disk
selection, and rescue. Kickstart uses `clearpart --all` + `autopart --type=lvm`, restricted to
`sda,vda,nvme0n1` via `ignoredisk --only-use`.

### Build result (2026-08-06)

```
<build-dir>/AlmaLinux-8.9-ENCS5412-switch.iso
2,705,516,544 bytes   1316 files
sha256  f082a7e60e2009078f063679f436ac1b50ea2154a82ef5f4a8b36d90e92e78b0
```

Post-build verification, all passing:

| Check | Result |
|---|---|
| Volume ID | `ENCS_SW_89` |
| El Torito — BIOS | `/isolinux/isolinux.bin`, boot-info-table, isohybrid-suitable |
| El Torito — UEFI | `/images/efiboot.img` |
| Boot config consistency | 8 × `LABEL=ENCS_SW_89`, 2 × `ks-encs.cfg`, no stale refs |
| `isolinux` menu deps | `vesamenu.c32`, `ldlinux.c32`, `libcom32.c32`, `libutil.c32` all present |
| Cisco kickstarts removed | `ks.cfg`, `anaconda-ks.cfg`, `iso_upgrade_ks.cfg` — 0 matches at root |
| Repo intact | 1174 RPMs, incl. `kernel-4.18.0-513.18.2.el8_9`, `switch-confd`, `nic-xl710-i350` |
| `%packages` resolvable | All 15 named packages exist in the ISO repo — none missing |
| Runtime payload | `encs/etc/systemd/system/marvell-switch-boot.service`, `encs/usr/local/sbin/encs-switch-{status,api}` |

Cosmetic leftover: `isolinux/anaconda-ks.cfg` (Cisco's) remains but is referenced by no boot entry.

### 8b. Slim build — and why the first ISO was 2.7 GB

The full build came out at 2.71 GB because it used `@core`. **Cisco redefined the `core` comps
group on this ISO** — it is not a minimal base. It contains:

| In Cisco's `@core` | Size |
|---|---|
| `cisco-esc-lite` | 189.3 MB |
| `cosign` | 48.4 MB |
| `java-1.8.0-openjdk-headless` | 36.1 MB |
| `confd` | 19.6 MB |
| 12 more Cisco RPMs (`cisco-vbm`, `nfvos-confd`, `smart-licensing`, …) | ~8 MB |
| plus stock-but-NFVIS-only: `nodejs`, `etcd`, `kubectl1.18`, `openvswitch2.17` | ~48 MB |

**~302 MB of Cisco stack, pulled in by `@core` alone.** The kickstart now enumerates the base
explicitly and carries a comment saying why, so nobody restores `@core` as a "simplification".

Resolving that explicit base + the ENCS packages against the repo's dependency graph:

```
331 packages   588 MB        (was 1174 packages / 1714 MB)
Packages/      1638 MB -> 562 MB on disk
```

`linux-firmware` (276 MB, 47% of what remains) was checked against both the `requires` and
`recommends` tables — it is a **hard `Requires` of `kernel-core`**, not a weak dep, so
`--exclude-weakdeps` cannot remove it. It stays.

Also dropped from the ISO: `upgrade/`, `kickstart_scripts/`, `base_rpm.list`,
`fake_dmidecode_data.bin`, `isolinux/anaconda-ks.cfg`.

**A bug worth recording:** the first slim build silently lost `modules.yaml`. The script captured
its path, then `rm -rf`'d `repodata/` — which is where that file lives — before using it. This
matters because `nic-xl710-i350` requires `/usr/bin/python3.8`, and the four `python38*` packages
are **modular** (`module_el8`); without module metadata dnf can refuse to see them. The build now
copies both `comps.xml` and `modules.yaml` out first, re-adds modules with `modifyrepo_c`, and
**hard-fails** if `type="modules"` is not present in the regenerated `repomd.xml`.

### 8c. Artefacts

| Artefact | Size | Notes |
|---|---|---|
| `AlmaLinux-8.9-ENCS5412-switch.iso` | 2.71 GB | full package set; fallback |
| `AlmaLinux-8.9-ENCS5412-switch-slim.iso` | **1.56 GB** | 331 pkgs, modules metadata verified |
| `AlmaLinux-8.9-ENCS5412-switch.qcow2` | — | pre-installed disk for Proxmox import |

slim ISO sha256 `4fe87cc3d259cf9948bf55657bff981bf0d04f9aa041240954230bb3a32e7566`

~1.56 GB is the floor for an *installer* ISO: `images/install.img` (Anaconda stage2, 723 MB) and
`initrd.img` (103 MB) now dominate and cannot be trimmed without rebuilding Anaconda's runtime.
The qcow2 avoids all of that by shipping an already-installed system.

Building the qcow2 runs the slim ISO's kickstart end-to-end under QEMU/KVM (q35 + OVMF, SMBIOS
type-1 product `ENCS5412/K9`), so **a successful qcow2 build is also proof the slim ISO installs**.

### 8d. Defects found by actually booting it

Building the qcow2 runs the slim ISO's kickstart end-to-end under QEMU/KVM, which turned it into
a test harness. Three defects surfaced that static verification could not have caught:

**1. `ignoredisk --only-use=sda,vda,nvme0n1` — fatal everywhere.**
Anaconda requires *every* disk named there to exist; it is not a preference list. The VM has only
`vda`, and the ENCS M.2 presents as `sda` or `nvme0n1` — never all three. The install aborted at
kickstart validation:

```
An error occurred during reading the kickstart file:
Disk "sda" given in ignoredisk command does not exist.
```

**This kickstart could not have installed on any machine.** Replaced with runtime detection: a
`%pre` block enumerates real block devices via `lsblk` (skipping `loop*`/`sr*`/`ram*`/`zram*`),
writes `/tmp/disk.ks` with the right `ignoredisk`/`clearpart`/`autopart`/`bootloader` lines, and
the body pulls it in with `%include /tmp/disk.ks`.

**2. `ls a b | head -1` under `set -e -o pipefail`.**
`ls` returns non-zero if *any* argument is missing, so probing for
`OVMF_CODE_4M.fd` *or* `OVMF_CODE.fd` killed the build script with exit 2 and **zero output** —
only the `_4M` variants exist on Ubuntu 24.04. Needs an explicit `|| true`.

**3. QEMU `-smbios` splits on commas.**
`manufacturer="Cisco Systems, Inc."` is rejected (`Invalid parameter ' Inc.'`) because the parser
splits the option string on `,` before honouring quotes. Only `product` matters for the platform
gate anyway, so the manufacturer is now `Cisco`.

What the failed run *did* prove, before dying at kickstart validation: the slim ISO boots through
OVMF/UEFI, GRUB resolves `LABEL=ENCS_SW_89`, `inst.stage2` loads off the trimmed ISO, and Anaconda
33.16.8.9 starts and reads the kickstart.

### 8e. Final artefacts (built and boot-tested 2026-08-07)

```
AlmaLinux-8.9-ENCS5412-switch-slim.iso   1,563,527,168   1.56 GB
  sha256 4d9afdf4ed8172de4a3d5832c494809edca4e56fa1ff56e2d5f41cfc4f50591b

AlmaLinux-8.9-ENCS5412-switch.qcow2        986,054,656   937 MiB (16 GiB virtual)
  sha256 4d273c28ab4e5ac076ed05b6416da2c02f46e5eee98304aabc17826f808c8a96

AlmaLinux-8.9-ENCS5412-switch.iso        2,705,516,544   2.71 GB  (untrimmed fallback)
```

Verified by booting the qcow2 headless and driving its serial console:

| Check | Result |
|---|---|
| Boots, correct pinned kernel | `4.18.0-513.18.2.el8_9.x86_64` |
| `switch_firmware.bin` | 26,663,887 bytes — exact match to source |
| `mv_pciboot.ko.xz`, `booton.bin`, `remote_boot_app` | present |
| `marvell-switch-boot.service` | enabled, symlink in `multi-user.target.wants` |
| `encs-switch-status`, `encs-switch-api` | present, mode 0755 |
| Kernel pin | `releasever=8.9`; `exclude=kernel*` in dnf.conf **and** yum.conf |
| Cisco `igb`/`i40e` | copied into the module tree |
| Module vermagic vs running kernel | match |
| `int-LAN` + `int-LAN.2363` ifcfgs | created |

### 8f. The insmod EPERM — expected, not a defect

In the VM the bootstrap service loops on:

```
insmod: ERROR: could not insert module mv_pciboot.ko.xz: Operation not permitted
```

This looks alarming and is **correct behaviour**. `sig_enforce=Y` on this kernel, so the first
hypothesis was an untrusted signature (the module is signed `NFVIS-REL`). A discriminator settles it:

| Test | Result |
|---|---|
| signed stock module (`dummy`) | loads, rc=0 |
| **un**signed stock module (signature stripped) | `Required key not available` — **ENOKEY** |
| `mv_pciboot` | `Operation not permitted` — **EPERM** |

**Different errno.** Signature rejection is ENOKEY; `mv_pciboot` returns EPERM, which is its own
`init_module` failing — there is no Marvell `11ab:be00` on the bus in a VM without passthrough.
A PCI bootstrap driver with no hardware *should* fail. Consistent with the live ENCS, where the
same module on the same kernel loads fine.

Note for deployment: Secure Boot is off in this image's test environment
(`mokutil: This system doesn't support Secure Boot`) and `sig_enforce=Y` comes from the kernel
build, not from SB. Stock-signed modules load normally, so no MOK enrolment is needed.

**Consequently the switch bootstrap is unproven until the real device is attached** — that is the
one remaining test, and it needs the actual ENCS (passthrough VM or bare metal).

### Status / caveats

- **Root password placeholder is `encs`** — change it.
- **The ISO has not been test-booted.** Treat first run as a validation pass; a Proxmox VM is the
  low-risk way in.
- Unverified items carried over: FLR-on-VM-shutdown behaviour (§4c) and whether ROS config
  persists across a bootstrap (§4b) — if it doesn't, a replay script driving `encs-switch-api`
  is needed.

## 8g. CONFIRMED WORKING on real hardware (2026-08-07)

Proxmox VE installed on the ENCS 5412; the qcow2 imported as VM 900 with
`0000:0d:00.0` passed through and SMBIOS type-1 product `ENCS5412/K9`.

Host-side pre-flight, all green:

| Check | Result |
|---|---|
| DMA remapping | `DMAR: Intel(R) Virtualization Technology for Directed I/O` |
| IOMMU groups | 53 populated (no GRUB changes needed — PVE 8 / kernel 6.8 enables it by default) |
| Marvell BDF | `0000:0d:00.0` — same as under NFVIS |
| IOMMU group | **group 50, single device** — clean, no ACS override |

Inside the guest:

```
OK   Marvell 11ab:be00 present at 06:10.0      <- re-enumerated inside the VM
OK   mv_pciboot loaded
OK   /dev/servicecpu present
OK   switch_firmware.bin present (26663887 bytes)
OK   marvell-switch-boot running
OK   kernel 4.18.0-513.18.2.el8_9.x86_64 (matches module vermagic)
```

Bootstrap log: `Reading CPI configuration space BARs … Loading bootstrap to service
CPU SRAM... done. Send IRQ to wake service CPU / IRQ sent, waiting for sync`.

**This closes §8f.** The `insmod … Operation not permitted` seen in the device-less VM was
indeed `init_module` failing for want of hardware — not the `NFVIS-REL` signature. With the real
device attached the module loads immediately. The ENOKEY-vs-EPERM discriminator was correct.

Proxmox VM config that worked:

```sh
qm create 900 --name encs-switch --machine q35 --bios ovmf \
    --memory 2048 --cores 2 --net0 virtio,bridge=vmbr0 \
    --serial0 socket --vga serial0
qm importdisk 900 /root/AlmaLinux-8.9-ENCS5412-switch.qcow2 local-lvm
qm set 900 --scsihw virtio-scsi-pci --virtio0 local-lvm:vm-900-disk-0
qm set 900 --efidisk0 local-lvm:0,efitype=4m,pre-enrolled-keys=0
qm set 900 --boot order=virtio0
qm set 900 --smbios1 product=RU5DUzU0MTIvSzk=,base64=1   # base64: '/' breaks the parser
qm set 900 --hostpci0 0000:0d:00.0
qm set 900 --onboot 1 --startup order=1
```

## 8h. END-TO-END SUCCESS — switch running under Proxmox (2026-08-08)

```
root@proxmox:~# ping -c3 169.254.1.0
64 bytes from 169.254.1.0: icmp_seq=1 ttl=64 time=3.14 ms
64 bytes from 169.254.1.0: icmp_seq=2 ttl=64 time=1.01 ms
64 bytes from 169.254.1.0: icmp_seq=3 ttl=64 time=1.08 ms
3 packets transmitted, 3 received, 0% packet loss
```

The Marvell ASIC is running ROS and answering on the management VLAN, driven entirely by our own
AlmaLinux image — no NFVIS anywhere. The full chain works:

`switch_firmware.bin` (extracted from the 4.15.5 ISO) → custom AlmaLinux 8.9 qcow2 →
Proxmox VM with `0000:0d:00.0` passed through → `mv_pciboot` → service CPU → u-boot → ROS →
reachable at `169.254.1.0`.

### Proxmox host interface map (as enumerated by PVE)

| Interface | MAC | Driver | Role |
|---|---|---|---|
| `enp8s0f1np1` | `…:2b` | i40e | **X710 → switch backplane** (was `int-LAN`) |
| `enp8s0f0np0` | `…:2a` | i40e | X710 → NIM slot (`int-ngio`) |
| `enp2s0f0` / `enp2s0f1` | `…:24`/`…:25` | igb | GE0-0 / GE0-1 (I350 WAN) |
| `enp14s0` | `…:98` | igb | MGMT (I210) — `vmbr0` sits here, correctly not behind the switch |
| `enp5s0` | `00:a0:c9:00:00:00` | ixgbe | X552, unprogrammed MAC, unconnected |

### Management VLAN on the host

**Gotcha:** `enp8s0f1np1.2363` is 16 chars and exceeds the 15-char `IFNAMSIZ` limit —
`ip link add … name enp8s0f1np1.2363` fails with *"name not a valid ifname"*. Give the VLAN its
own short name:

```sh
ip link add link enp8s0f1np1 name sw2363 type vlan id 2363
ip addr add 169.254.1.1/16 dev sw2363
ip link set sw2363 up mtu 9216
```

Persistent form in `/etc/network/interfaces` needs the raw-device syntax:

```
auto enp8s0f1np1
iface enp8s0f1np1 inet manual
    mtu 9216

auto sw2363
iface sw2363 inet static
    address 169.254.1.1/16
    vlan-raw-device enp8s0f1np1
    vlan-id 2363
    mtu 9216
```

### Known defect in the shipped image: buffered bootstrap log

`remote_boot_app` uses `printf`. Under NFVIS's init script stdout was a tty (line-buffered); under
our systemd unit it is a journal socket, so glibc switches to **full** buffering and the progress
log lags by minutes — it only flushed "Loading firmware to service CPU DDR..." when the process was
killed, four minutes after the event. **The switch works; only the log is misleading.**

Fix (fold into the unit):

```
ExecStart=/usr/bin/stdbuf -oL -eL /opt/switch-confd/remote_boot_app
```

Diagnostic that cut through it: `dmesg` shows `servicecpu_write` totals of
`0x30c78` (199,800 = `booton.bin`) and `12 × 0x200000 + 0x16dbcf` = **26,663,887 =
`switch_firmware.bin` exactly** — proving both blobs transferred in full while the log still said
"Service CPU is not ready for FW yet."

BARs pass through VFIO intact (`Region 0/2/4` = 1M/64M/512M, `Mem+ BusMaster+`), so the feared
BAR-reassignment problem does not materialise.

## 8i. Management API working — and the traps along the way

Confirmed working end to end on 2026-08-08:

```
encs-switch-api get '{FullInterfaceList}'  -> gi0 .. gi7  (8 front ports)
encs-switch-api get '{VLANList}'           -> VLAN 1, VLAN 2363
```

VLAN 2363 exists in the firmware defaults, so the switch is **not** unconfigured after a bare
bootstrap — an earlier assumption in §8h that turned out wrong.

Writes confirmed too — creating VLAN 100 with Cisco's own template
(`switch_vlan.py:47`) returned `statusCode 0 / OK`, and `{VLANList}` then showed 1, 100, 2363:

```xml
<DeviceConfiguration>
    <version>1.0</version>
    <VLANList action="set">
        <VLAN><VLANID>100</VLANID></VLAN>
    </VLANList>
</DeviceConfiguration>
```

Actions are `set` (creates and modifies), `delete`, `add`. Every XML body Cisco ever sent exists as
an `xml_*_str` variable in `payload/extract/opt/switch-confd/switch_*.py`, paired with a
`url_*_str` naming its `wcd?{Table}` endpoint — VLANs, ports, PoE, STP, LACP, QoS, ACLs, 802.1X,
RADIUS. Nothing needs to be guessed.

### Link aggregation: the write targets the member port, not the LAG

`LAG1`–`LAG4` show up in `{Standard802_3List}` as ordinary interfaces (`interfaceType` 2) and are
what the Ports view lists. They are **empty until ports are bound to them**, which is why a fresh
switch shows them as `n/p` with no media.

Membership is not a property of the group. It is set on the **member port's own**
`Standard802_3List` entry (`xml_post_channel_group_str`, `switch_interfaces.py:1035`, reached via
`SET_IFACE_CHANNEL_GROUP` = 925):

```xml
<DeviceConfiguration>
    <version>1.0</version>
    <Standard802_3List action="set">
        <Entry>
            <interfaceName>gi3</interfaceName>
            <LACPEnabled>2</LACPEnabled>   <!-- 1 = on/static, 2 = auto/LACP -->
            <LAGID>1</LAGID>
        </Entry>
    </Standard802_3List>
</DeviceConfiguration>
```

| Field | Meaning |
|---|---|
| `LAGID` | 1–4. Max 4 groups (`switchports_cdb_util.py:51`, "Max 4 Port Channels"). |
| `LACPEnabled` | `s_cg_mode_on=1` / `s_cg_mode_auto=2` (`switch_ns.py:298,338`). |
| unbind | `LAGID` 0 **and** `LACPEnabled` 0 — what confd sends for `no channel-group` (`switch_interfaces.py:7061`). |

#### `LAGID` is membership; `membershipType` is not

Measured on hardware by binding two shut ports and reading everything back:

| Port state | `LAGID` | `LACPEnabled` | `membershipType` |
|---|---|---|---|
| unbound | `0` | `0` | absent from LAGList |
| bound to LAG2, mode **auto**, port shut | `2` | `2` | **3** — "not candidate" |
| bound to LAG2, mode **on**, port shut | `2` | `1` | **2** — "inactive" |
| unbound again | `0` | `0` | absent |

**`LAGID` on the port's own `Standard802_3List` entry is the configuration** — unambiguous, and
independent of admin/link state. `membershipType` is an *operational* signal describing whether the
port can currently aggregate, and it varies with the mode: the same shut port reads 3 under
auto/LACP but 2 under on/static.

This bit us. `encs-switch-tui` 0.0.1–0.0.3 derived membership from `membershipType ∈ {1,2}`, so a
port bound in LACP mode while shut (state 3) was treated as **not a member**: the Ports view showed
no LAG, and — worse — `save_config` omitted `15-lag.xml` entirely, so the membership was silently
lost on the next cold power cycle. Since every front port comes up SHUT after a bootstrap, that was
the *normal* path, not an edge case. Fixed in 0.0.4 by keying membership off `LAGID` and using
`membershipType` only for the active/inactive/not-candidate display.

**Reading membership back is a different table.** `{LAGList}` returns `LAGEntry` →
`PortList` → `PortEntry` with `portName` and `membershipType` (1 = active, 2 = inactive,
3 = not candidate — `switch_port_info.py:573-579`). Cisco fetches it in the *same* GET as the port
list, `wcd?{Standard802_3List}{LAGList}` (`switch_port_info.py:550`), and `encs-switch-tui` does the
same.

Confirmed on hardware 2026-08-10 (switch idle, nothing bundled):

```xml
<LAGEntry>
  <interfaceName>LAG1</interfaceName>
  <interfaceType>2</interfaceType>   <!-- 2 = port-channel -->
  <interfaceID>1</interfaceID>
  <LACPType>3</LACPType>
  <PortList></PortList>              <!-- empty until ports are bound -->
</LAGEntry>
```

All four groups always exist with an empty `PortList`; `LAGEntry` also carries an undocumented
group-level `LACPType` (3 on an unconfigured group) that we do not currently use.

`Standard802_3List` **does** report `LAGID` on every entry, and `LACPEnabled` on the 12 physical
ports — both read `0` on an unbound port, which is the same pair that means "no channel-group" on
write. So membership *could* be derived from the ports table alone. `{LAGList}` is still the right
source because only `PortEntry` carries `membershipType`, i.e. whether a bound port is actually
forwarding. (Note the two places confd reads `LAGID` — `switch_port_info.py:3035`, `:4563` — are
parsing the `{STP}` table's `InterfaceEntry`, not this one.)

Two operational traps:

- **Never bundle `te1`/`te2`.** They are the internal backplane links carrying both the data path
  and the management session on VLAN 2363 — bundling one cuts the wire you are managing over.
  Cisco's own display code skips `te` ports when listing LAG members (`is_te_channel`), and
  `encs-switch-tui` refuses them outright.
- **`on` against an LACP far end black-holes traffic.** Neither side errors; frames just vanish.
  `auto` is the right default.

`{LACPPortList}`/`{LACPLAGList}` expose actor/partner LACP detail (system priority, MAC, admin and
oper keys) for a `show lacp` equivalent, but **not** the on/auto mode — so a saved config records
`LACPEnabled` from the port entry when the firmware reports it and falls back to `auto` otherwise.

### Root cause of the "empty ActionStatus" dead end: curl globbing

Every `wcd?{Table}` query returned an empty `ActionStatus` for hours. The cause was **not** the
switch, the session, the header name, or a missing init step — all of which were investigated and
disproved. `{` and `}` are **curl's URL-globbing syntax**; curl expanded `{FullInterfaceList}`
as a single-item set and sent `wcd?FullInterfaceList` **without the braces**, which the switch
does not recognise.

The tell was visible in the very first response and read past repeatedly:

```xml
<requestURL>FullInterfaceList</requestURL>   <- no braces; we sent them
```

**Fix: `curl -g` (`--globoff`).** POSTs were unaffected because their URL is plain `wcd?`.

**And it came back.** `encs-switch-api` shipped without `-g` and every `get` returned that same
empty `ActionStatus` — found on hardware 2026-08-10, fixed in v0.0.2. The TUI was never affected
because it speaks urllib, not curl, which is exactly why the regression went unnoticed: the tool
everyone uses kept working. If you are diagnosing this class of bug, check `<requestURL>` in the
response first — braces missing there means the shell or curl ate them, not the switch.

### Session mechanics (confirmed)

| Aspect | Reality |
|---|---|
| Login | `GET /System.xml?action=login&user=cisco&password=cisco` |
| Token | **`sessionID` response *header***, not the body — `UserId=<client-ip>&<num>&;path=/` |
| Reuse | Send verbatim as a `sessionid` request header; the `;path=/` suffix is harmless |
| Binding | Bound to the **client IP** — a session minted on the hypervisor only works from it |
| Expiry | Short by default; `switch-confd` POSTs `EWSServiceTable maxIdleTimeOut=0` right after login |
| Web server | `GoAhead-Webs`; `/` → 302 → `/csffffffff/` where `ffffffff` = "no session" |

The `EWSServiceTable` POST is genuine init that `switch_connection_setup()` performs, and is worth
replicating for long-lived sessions — but it was **not** the cause of the empty queries.

### Hypotheses tested and disproved (recorded so they are not re-tried)

1. Untrusted `NFVIS-REL` module signature → no; ENOKEY vs EPERM discriminator (§8f)
2. Half-reset ASIC from a service restart → no; symptoms identical after a clean cold boot
3. `sessionid` header case/`;path=/` suffix → no; all four variants behaved identically
4. Session token needed in the URL path (`/cs<hex>/wcd?…`) → no
5. Missing `EWSServiceTable` init → no; the POST succeeded (`statusCode 0`) while GETs still failed

## 8j. Re-bootstrap limitation, and a failed workaround (DO NOT REPEAT)

### The limitation

`remote_boot_app` **cannot reset the ASIC**. Its state machine detects the service CPU is not in
WFI, prints `Service CPU not ready (requires reset?)`, and loops forever:

```c
case REMOTE_BOOT_STATE_REQUEST_RESET_E:
    /* Do nothing until state changes by external task */
    //printf("Please perform reset and press enter when ready...");   // commented out
    remote_boot_state = REMOTE_BOOT_STATE_RESET_DONE_E;
    break;
```

The "external task" is the BMC on a real NFVIS system (`sendToBMC`, `cman_bmc_agent_tlv`). We do
not run that layer, so **once bootstrapped, the ASIC cannot be re-bootstrapped in software.**

The module offers no help: `objdump` shows `mv_pciboot` references `0x20980` (the scratchpad) but
**never `0x20800`** (`MSYS_SW_RESET_CPU0_REG`). `rmmod`/`insmod` does not reset the service CPU.

### Reading the scratchpad

`0x000022a4` decodes as: low byte `0xa4` = `SERVICE_CPU_ROS_READY`; upper bits = a **keep-alive
counter** that increments while ROS is healthy (`count = scpu_status >> 8` in `remote_boot.c`).
A rising counter means the service CPU firmware is alive — but see below: that is **not** proof
the switch is forwarding.

### The failed workaround

An `encs-switch-reset` tool was written to poke `MSYS_SW_RESET_CPU0_REG` (0x20800) via
`/sys/bus/pci/devices/<bdf>/resource0`, pulsing `MSYS_SW_RESET_ENABLE_MASK` and verifying the
scratchpad returned to WFI.

**It does not work, and it is harmful.** Against a running switch:

- the scratchpad stayed at `0xa4` with the counter still incrementing — no reset occurred
- **the switch stopped forwarding and became unreachable** on the management VLAN

So the write disturbed the datapath while leaving the service CPU running. Recovery required a
cold power cycle. The register semantics inferred from the header name are wrong, or CPU0 is not
the right target, or a bare MMIO write is not the correct access path.

**Do not run this tool.** It has been removed from the image payload. Anyone revisiting this needs
real documentation for the MSYS reset block, not inference from a header.

### Operational rules that follow

- **Never `systemctl restart marvell-switch-boot`** and avoid `qm stop` on the bootstrap VM.
- `Service CPU not ready (requires reset?)` in the log is **not by itself** an outage — verify with
  `ping 169.254.1.0` and an API call from the hypervisor. The loader complaining and the switch
  working are independent conditions.
- Recovery from a wedged ASIC requires **physical AC removal for ~30s**. Confirmed on hardware:
  - `reboot` — insufficient, the ASIC stays powered
  - `qm stop` / VM restart — insufficient
  - **CIMC `power off` / web UI Host Power Off — insufficient.** It drops the host rails only;
    the Marvell sits on standby power and never cold-starts.
  - Unplugging AC (both cords if dual-PSU) — the only method that works.

  This makes the "never restart the bootstrap service" rule serious rather than merely
  inconvenient: **a remote mistake requires physical access to the chassis.** Anyone operating
  this remotely should treat VM 900 as untouchable.

### Config persistence — RESOLVED

Clean test finally completed: create VLAN 100 → confirm present → `qm stop 900` / `qm start 900`
→ wait 60s → **VLAN 100 still present, switch still reachable at 1.1 ms**.

| Event | ASIC | Config |
|---|---|---|
| **VM restart** (`qm stop`/`start`) | keeps running — never reset | **survives** |
| **Bootstrap service restart** | keeps running | survives (loader wedges, switch fine) |
| **Cold power cycle / AC loss** | re-bootstraps blank | **lost** — VLAN 1 + 2363 defaults only |

The ASIC has no flash, so config lives in its RAM and persists exactly as long as it has power.
`qm stop` does not remove power from it, which is why restarts are non-destructive.

**This also settles the availability question**: VM restarts cause no switch outage. The
`Service CPU not ready (requires reset?)` message is purely the loader's own state machine and is
**not** an indicator of switch health — verify with `ping` and an API call instead.

A replay mechanism is therefore needed **only for cold boots** (power loss / AC pull), not for
routine VM restarts — a much narrower requirement than originally assumed.

## 8k. PoE CONFIRMED WORKING (2026-08-08)

A PoE wireless AP on `gi0`, with PoE enabled purely through the `wcd` XML API:

```
t+ 5s   gi0  det=DELIVERING  class=5  2.3W   (link still down - AP booting)
t+10s   gi0  link=UP         class=5  3.2W
t+20s   gi0  link=UP         class=5  3.9W
```

The PSE detected the device, classified it **class 5** (802.3bt Type 2, up to 45 W) and delivered
power; the other seven ports correctly stayed in `searching`. Link came up once the AP booted
*on switch-supplied power*.

**This disproves the ServeTheHome claim that PoE requires Cisco's proprietary software on
non-NFVIS installs.** It requires Cisco's *API*, which is fully usable without any Cisco stack.

### PoE facts

| | |
|---|---|
| Per-port limit | 30 W (`powerLimit` 30000 mW), `maxPowerAllocAllowed` 60 W |
| Chassis budget | 200 W (per NFVIS `show switch`) |
| Global settings | `powerLimitMode 5`, `legacyModeSupported enabled`, `inrushTest enabled` |
| Enable | `PoEPSEInterfaceList action="set"` → `adminEnable` 1=on, 2=off |
| `detectionStatus` | 1 disabled · 2 searching · **3 delivering** · 4 fault · 5 test · 6 other |

**After a cold bootstrap PoE is `adminEnable=2` (off) on every port**, exactly like the front ports
themselves being `adminState=2` (shut). A bare bootstrap therefore yields a switch that boots but
neither forwards nor powers anything until both are enabled — which is what Cisco's
`switch_init.xml` did and we do not. Both belong in the replay set (`encs-switch-tui --save`).

## 8l. Management tooling

Three tools, all stdlib-only Python/bash, shipped **inside the VM image at `/opt/encs-host/`** but
intended for the **hypervisor** — the VM cannot reach the switch, since management rides VLAN 2363
on the host's X710.

| Tool | Runs on | Purpose |
|---|---|---|
| `encs-switch-status` | **VM** | bootstrap health: PCI device, module, `/dev/servicecpu`, firmware blob, daemon, kernel-vs-vermagic |
| `encs-switch-tui` | host | curses UI — Ports / VLANs / PoE / MAC / Stats / Config, with a built-in manual (`?`) |
| `encs-switch-api` | host | low-level `wcd` XML client for scripting |
| `install.sh` | host | auto-detects the backplane NIC, creates `sw2363`, persists it, installs the tools, enables replay |
| `encs-switch-replay.service` | host | reapplies `/etc/encs-switch/*.xml` after a cold boot |

Distribution model: the qcow2/ISO is the artifact; the host bundle rides inside it and is copied
out once — `scp -r root@<vm-ip>:/opt/encs-host /root/ && bash /root/encs-host/install.sh`.

Verified on hardware: `--status`, `--save`, `install.sh` (idempotent — correctly skipped existing
network config), curses rendering, and all three write paths (port admin, VLAN create/delete,
PoE toggle), each restored to its original state afterwards.

`install.sh` finds the backplane by **driver and enumeration order** (second `i40e` port), never by
name — it is `enp8s0f1np1` on this unit and will differ elsewhere.

## 9. Where everything lives (as of 2026-08-07)

### Mac — `<project-dir>/`

| Path | Size | What |
|---|---|---|
| `AlmaLinux-8.9-ENCS5412-switch-slim.iso` | 1.56 GB | **deliverable** — installer ISO, UEFI+BIOS |
| `AlmaLinux-8.9-ENCS5412-switch.qcow2` | 937 MB | **deliverable** — pre-installed disk for Proxmox |
| `Cisco_NFVIS-4.15.5-FC4.iso` | 2.71 GB | source; the ONLY source of `switch_firmware.bin` |
| `Cisco_NFVIS-4.18.1-FC2.iso` | 2.66 GB | comparison build (firmware blob removed) |
| `payload/extract/opt/switch-confd/` | 31 MB | extracted switch bootstrap — the critical files |
| `payload/rpms/` | 44 MB | `mv_pciboot.ko` + 6 Cisco RPMs |
| `build/` | 168 KB | kickstart + build/verify scripts (reproducible) |
| `FINDINGS.md` | — | this document |

Transfer verified by checksum after copy:

```
4d9afdf4ed8172de4a3d5832c494809edca4e56fa1ff56e2d5f41cfc4f50591b  slim.iso
4d273c28ab4e5ac076ed05b6416da2c02f46e5eee98304aabc17826f808c8a96  qcow2
e7f4500d1f2808d6ed4711f1927dcc60ebb3dad55aa24e5756effa94f7983b46  switch_firmware.bin
```

`build/` contents worth keeping:

| File | Purpose |
|---|---|
| `ks-encs.cfg` | the kickstart — carries comments explaining each of the 4 defects fixed |
| `build-iso-slim.sh` | trim + repodata regen + remaster (needs `bsdtar`, `xorriso`, `createrepo_c`) |
| `build-iso.sh` | untrimmed variant |
| `build-qcow2.sh` | runs the kickstart under QEMU/KVM to produce the disk image |
| `verify-qcow2.py` | **boots the image and asserts the post-install state — re-run after any kickstart change** |
| `sigtest.py` | the ENOKEY-vs-EPERM discriminator from §8f |
| `keep.txt` | the 331-package closure |
| `files/` | runtime payload (systemd unit + both helper scripts) |

### Build server — `<build-server>:<build-dir>/` (5.2 GB after cleanup)

Kept: `Cisco_NFVIS-4.15.5-FC4.iso` (2.6 GB), the untrimmed
`AlmaLinux-8.9-ENCS5412-switch.iso` (2.6 GB, superseded — safe to delete),
`extract/`, `rpms/`, `build/`. Removed: the extracted ISO tree, the raw qcow2
intermediate, verify copies, OVMF vars, and the two deliverables (now on the Mac).

Tools installed on that server for the build: `libarchive-tools`, `xorriso`,
`isomd5sum`, `createrepo-c`, `qemu-system-x86`, `qemu-utils`, `ovmf`; user added to `kvm`.

## Artifacts

Local (session scratchpad, clears on reboot):
`<scratch-dir>/.../scratchpad/` — `x/switch-confd/`, `x/nic/`, `x/dbgsrc/`, `repo/`

Remote `<build-server>:<build-dir>/` (74 MB):
- `extract/opt/switch-confd/` — all switch bootstrap files
- `rpms/` — mv_pciboot.ko + 6 Cisco RPMs

The ISO itself was **not** copied to the remote (not needed; the rsync also failed because
macOS's bundled rsync 2.6.9 rejects `--info=progress2`).
