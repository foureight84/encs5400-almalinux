# Running this on ESXi — experimental

> **This works, on a real ENCS 5412** (`ENCS5412/K9`, FGL232931K9) under
> **ESXi 8.0 U3** (build 24677879). The switch boots, stays up, and is fully
> manageable — VLAN tables, ports, the lot.
>
> Two things are different from the Proxmox path and you need both. The work
> splits across [two VM roles](#two-jobs-two-vms-how-this-actually-works-on-esxi):
> one with the Marvell passed through that bootstraps the ASIC once per AC
> cycle, and one *without* it that runs the tools. And the bootstrap VM is
> killed by an [IOMMU fault](#the-iommu-fault) part-way through — which turns
> out not to matter, because the firmware is already delivered by then and the
> ASIC finishes on its own.
>
> Getting here turned up eight bugs the offline tests could not: see
> [what real ESXi taught us](#what-real-esxi-taught-us). All eight are fixed;
> the ones a mock can express are now caught by `scripts/66-test-esxi.py`.
>
> Every step below says how much it can be trusted. Read the
> [revert](#reverting-everything) section *before* you start: it exists so a
> failed attempt costs you a reboot, not your ESXi install.
>
> **You are on the `experimental/esxi` branch**, which carries the tooling as
> well as the walkthrough: a VMDK build target and an `esxcli`
> installer/uninstaller for the host side. It stays off `main` until the
> bootstrap VM has been seen to boot the ASIC on this platform. Everything
> below is also written as manual commands, because when an experimental
> script does something you did not expect, the useful thing to have is the
> list of what it was trying to do.

| Step | Confidence |
|---|---|
| ESXi running on this chassis at all | **verified** — 8.0 U3 build 24677879, 12C/24T, all six NICs and the ASIC enumerated |
| DirectPath I/O accepting the Marvell device | **verified** — `Current Owner: VM Passthru` with **no** `passthru.map` entry and no reboot. See [step 1](#1-enable-passthrough-for-the-marvell-device) |
| vSwitch / portgroup model for VLANs | **verified** — `install.sh` and `encs-esxi-vnet` both round-trip on the host |
| `SMBIOS.reflectHost` satisfying the platform gate | **verified** — `dmidecode` in the guest returns `ENCS5412/K9` and the chassis serial |
| Managing the switch from a VM on the mgmt VLAN | **verified** — done from a VM with no passthrough device at all |
| The bootstrap itself (VM + passthrough boots the ASIC) | **verified** — deterministic since the [COMMAND fix](#the-command-register-bug); the VM is killed by an [IOMMU fault](#the-iommu-fault) but the switch comes up regardless |
| Running the tools (TUI/API/vnet) against the live switch | **verified** — from a VM with no PCI device; VLAN tables read back correctly |

---

## The short version

```sh
# build host
./build.sh --esxi /path/to/Cisco_NFVIS-4.15.5-FC4.iso
scp -r payload/opt/encs-esxi root@<esxi>:/vmfs/volumes/datastore1/
ssh root@<esxi> 'mkdir -p /vmfs/volumes/datastore1/encs-switch'
scp out/esxi/encs-switch.vmdk out/esxi/encs-switch.vmx root@<esxi>:/vmfs/volumes/datastore1/encs-switch/

# ESXi host - prints the plan and stops
ssh root@<esxi> 'sh /vmfs/volumes/datastore1/encs-esxi/install.sh'
ssh root@<esxi> 'sh /vmfs/volumes/datastore1/encs-esxi/install.sh --yes'
```

That does passthrough and networking. The VM itself is steps
[2](#2-get-the-disk-onto-a-datastore)–[6](#6-put-the-vm-on-the-switch-management-network)
below and `out/esxi/README-esxi.txt`, and the one-line revert is
`sh /vmfs/volumes/datastore1/encs-esxi/uninstall.sh --yes`.

The rest of this file is what those scripts do and why, which is worth reading
once before running them on a host you care about.

---

## What is different from the Proxmox path

Two things, and both change *where* code runs rather than what it does.

**ESXi cannot run the host tools.** `encs-switch-tui` is Python + curses,
`encs-switch-api` is bash + curl, `install.sh` writes systemd units and
`/etc/network/interfaces`. The ESXi shell is busybox `ash` with no bash, no
systemd, no ifupdown, and a stripped Python without curses. None of that
bundle runs there.

**So the tools move into the bootstrap VM.** They are already in the image at
`/opt/encs-host/` — on Proxmox you copy them *out* to the hypervisor, and here
you simply leave them where they are. The VM gets a second vNIC on a portgroup
tagged VLAN 2363, which puts it on the switch management network, and it
configures the ASIC it just booted.

> **This is not the dependency loop the README warns about.** That warning is
> about the bootstrap VM's *own* management path: put the machine that boots
> the switch behind the switch and a dead ASIC locks you out of the thing that
> would fix it. Here the VM keeps its normal management NIC on the `MGMT CPU`
> vSwitch, exactly as on Proxmox. The second vNIC is link-local only, carries
> no default route, and is used for nothing but talking to `169.254.1.0`. If
> the ASIC is dead that interface is useless — and you do not need it.

**Linux bridges become portgroups.** `swbr0` — one VLAN-aware bridge whose only
port is the te2-facing X710 function — becomes one standard vSwitch whose only
uplink is that same vmnic, with a portgroup per VLAN. `bridge=swbr0,tag=100`
becomes "attach the VM to the portgroup with VLAN ID 100". The switch half of
the job does not change at all.

---

## Prerequisites

- **An ENCS 5412 (or 5406/5408) that already works with the Proxmox path**, or
  at least a built image. Everything in the [README](../README.md) about the
  NFVIS ISO, the 4.16+ trap and the build applies unchanged — the ESXi
  difference starts after `build.sh` has produced a disk.
- **ESXi 8.0 U3 works.** It is what the host side was verified on: build
  24677879 on a Xeon D-1557 (Broadwell-DE), which sits at the old end of
  VMware's support matrix — 8.0 flags pre-Skylake CPUs as deprecated, and 9.0
  drops them. It installs and runs regardless; the deprecation warning is not a
  refusal. 7.0 U3 remains the conservative choice if you want to stay inside
  the matrix. Either way the X710 (`i40en`) and I210 (`igbn`) drivers you need
  have been in-box since 6.5.
- **VT-d enabled** in the ENCS BIOS (F2). ESXi will not offer passthrough
  without it.
- **A serial cable.** `CONSOLE` on the front panel, 115200 8N1, gives you the
  ESXi DCUI-equivalent boot output and the shell. The README says to keep one
  for this box and that goes double here, because you are about to change
  networking on a machine whose only safe management jack is `MGMT CPU`.

### The one rule that keeps this recoverable

**Leave `vmk0` where it is.** The ESXi management VMkernel port belongs on
`vSwitch0` with the I210 `MGMT CPU` jack as its uplink — the one link that
depends on no ASIC, no VM and no VLAN. Everything below builds a *second*
vSwitch and never touches the first. If you have already moved management onto
an X710 port, move it back before starting.

---

## 1. Enable passthrough for the Marvell device

**Find it.** Do not hardcode the address — the README documents it moving
between `0d:00.0` and `0e:00.0` on the same machine across reinstalls.

```sh
esxcli hardware pci list | awk '
    /^[0-9a-f][0-9a-f]*:[0-9a-f][0-9a-f]*:/ { addr=$1; vid=""; did="" }
    /^[ \t]*Vendor ID:/ { vid=$3 }
    /^[ \t]*Device ID:/ { did=$3 }
    vid == "0x11ab" && did == "0xbe00" && addr != "" { print addr; addr="" }'
# cross-check:
lspci | grep -i 11ab
```

Match on **both** IDs and anchor the field names. Each block also carries
`SubVendor ID` and `SubDevice ID`, which an unanchored `/Vendor ID:/` matches
too — and on this chassis that is not hypothetical: `0e:00.0` has been the I210
management NIC on one install and the Marvell on another
([FINDINGS §8](FINDINGS.md)), so a loose match can hand a VM the wrong device.
Call the result `$DEV` below.

`encs-esxi/install.sh` derives it exactly this way and refuses to run if it
finds nothing — it never takes a BDF as an argument, because a stale one would
hand a VM the wrong device.

**Mark it for passthrough:**

```sh
esxcli hardware pci pcipassthru set --device-id=0000:0d:00.0 --enable=true --apply-now
esxcli hardware pci pcipassthru list | grep 0000:0d:00.0
```

`--device-id`, not `--device`, and `--apply-now` is a bare flag rather than
`--active=true` — 8.0 U3 rejects both of the other spellings outright with
`Error: Invalid option --device`. Note also that `pcipassthru list` prints a
two-column **table**, unlike every other `hardware pci` list, so there is no
block to `grep -A3` for.

`--apply-now` applies it without a reboot. If the device shows as pending
rather than enabled, reboot the host — a device the VMkernel still owns cannot
be assigned to a VM.

**On the real chassis it took the device immediately.** Both `Configured
Owner` and `Current Owner` went to `VM Passthru` in the same command, with no
reboot and no `passthru.map` entry, and ESXi picked `Reset Method: Bridge
reset` on its own — the Pericom bridge below is what makes that available:

```
   Reset Method: Bridge reset
   FPT Sharable: true
   Configured Owner: VM Passthru
   Current Owner: VM Passthru
```

**The reset method is still the thing most likely to bite on a different
chassis.** ESXi refuses to pass through a device it cannot reset cleanly, and
unlike Proxmox it will not be talked out of it at power-on time. The Marvell
sits alone in its IOMMU group behind a Pericom bridge
([FINDINGS §8](FINDINGS.md)), so a bridge-level reset is available even if the
function itself advertises no FLR — which is exactly what was observed above.
If the VM refuses to power on with a passthrough or reset error, tell ESXi how
to reset it explicitly:

```sh
# /etc/vmware/passthru.map — vendor-id device-id reset-method fptShareable
echo "11ab  be00  bridge  false" >> /etc/vmware/passthru.map

# or, which also handles the persistence problem below:
sh /vmfs/volumes/datastore1/encs-esxi/install.sh --reset-method bridge --yes
```

Valid reset methods are `flr`, `d3d0`, `link`, `bridge` and `default`; try them
in that order of preference. `bridge` resets the whole upstream bridge, which
here contains only this device.

> **Edits under `/etc/vmware` do not survive a reboot on their own.** ESXi
> restores that directory from the bootbank at boot and only saves it on a
> clean shutdown or an explicit backup. After editing `passthru.map`:
>
> ```sh
> /sbin/auto-backup.sh
> ```
>
> Skip this and the file reverts, the VM stops powering on, and nothing you
> changed appears to be the cause.

**Reset semantics matter more here than on most passthrough devices.** From the
README's operational warnings: `remote_boot_app` can only bootstrap an ASIC
that is in WFI, i.e. freshly reset. Re-running the loader against a switch that
is already up wedges it in "Service CPU not ready", and the only recovery is
**physical AC removal** — a host reboot is not enough, the ASIC is on standby
power. So:

- Do not put the bootstrap VM in a vSphere HA restart policy.
- Do not vMotion it (DirectPath I/O prevents this anyway).
- Do not "power cycle the VM to see if that fixes it". Check
  `ping 169.254.1.0` first — a wedged *loader* does not mean a dead *switch*;
  the ASIC keeps forwarding.

---

## 2. Get the disk onto a datastore

`build.sh --esxi` does the conversion as part of the build and also writes a
`.vmx` with the awkward settings already in it:

```sh
./build.sh --esxi /path/to/Cisco_NFVIS-4.15.5-FC4.iso
ls out/esxi/          # encs-switch.vmdk  encs-switch.vmx  README-esxi.txt
```

It is a separate, last step that nothing else depends on: if the conversion
fails you still have a working qcow2 and a working Proxmox path. By hand, from
an existing build:

```sh
scripts/45-build-vmdk.sh out/AlmaLinux-8.9-ENCS5400-switch.qcow2 out/esxi
```

Upload it to a datastore (Host Client → Storage → Datastore browser, or `scp`
to `/vmfs/volumes/<datastore>/`), then convert it into a real VMFS disk:

```sh
cd /vmfs/volumes/datastore1
mkdir -p encs-switch
vmkfstools -i AlmaLinux-8.9-ENCS5400-switch.vmdk -d thin encs-switch/encs-switch.vmdk
```

Alternatively, skip all of this and install from
`AlmaLinux-8.9-ENCS5400-switch.iso` into an empty 16 GB disk — the ISO's
default boot entry installs unattended and **erases the disk it picks**, so
give the VM exactly one disk and no passthrough device until it has finished.
That mirrors the README's "Alternative: install from the ISO instead".

---

## 3. Build the switch vSwitch

**Find the backplane vmnic.** Both X710 functions reach the ASIC — `.0` is te1
and `.1` is te2 — and **only te2 carries the management VLAN**. Pick by PCI
address, never by vmnic name:

```sh
esxcli network nic list
# Name    PCI Device    Driver  ...
# vmnic2  0000:08:00.0  i40en   <- te1
# vmnic3  0000:08:00.1  i40en   <- te2   <- this one
```

A wrong pick here looks exactly like a switch that never booted, which is the
same trap `install.sh` warns about on Proxmox.

`install.sh` does all of this, picking the uplink by the same rule and
recording what it created. By hand:

```sh
esxcli network vswitch standard add --vswitch-name=vSwitchENCS
esxcli network vswitch standard set --vswitch-name=vSwitchENCS --mtu=9000
esxcli network vswitch standard uplink add --uplink-name=vmnic3 --vswitch-name=vSwitchENCS

# switch management VLAN - the bootstrap VM's second vNIC lands here
esxcli network vswitch standard portgroup add --portgroup-name=encs-mgmt-2363 --vswitch-name=vSwitchENCS
esxcli network vswitch standard portgroup set --portgroup-name=encs-mgmt-2363 --vlan-id=2363

# stock lan-net: untagged = switch VLAN 1 = all eight GE1/x jacks
esxcli network vswitch standard portgroup add --portgroup-name=encs-lan --vswitch-name=vSwitchENCS
```

Note the short flags differ between `portgroup add` (`-v` is the *vSwitch*) and
`portgroup set` (`-v` is the *VLAN*). Use the long forms above and the ambiguity
goes away.

MTU is 9000 rather than the 9216 `install.sh` sets on Proxmox — that is the
vSwitch maximum. The ASIC side is unaffected; frames just cap lower.

---

## 4. Create the bootstrap VM

Two vCPU, 2 GB, EFI, Secure Boot **off**, two vNICs, one passthrough device.
The image ships a generic (non-hostonly) initramfs — `dracut-config-generic` is
in the kickstart — so `vmw_pvscsi` and `vmxnet3` are present and it boots on
VMware hardware without modification.

`build.sh --esxi` writes `out/esxi/encs-switch.vmx` with all of this already
set (`VM_HWVERSION=17 build.sh --esxi ...` for an older ESXi), so registering
that file is the short way:
`vim-cmd solo/registervm /vmfs/volumes/datastore1/encs-switch/encs-switch.vmx`.
Otherwise the Host Client (New VM → guest `CentOS 8 (64-bit)`) with the
settings below. The VMX keys, for reference:

```ini
virtualHW.version = "19"        # 7.0 U2+. Use 17 on 7.0 GA/U1, or the VM
                                # will not register.
firmware = "efi"
uefi.secureBoot.enabled = "FALSE"
numvcpus = "2"
memSize = "2048"

scsi0.present = "TRUE"
scsi0.virtualDev = "pvscsi"
scsi0:0.present = "TRUE"
scsi0:0.deviceType = "scsi-hardDisk"
scsi0:0.fileName = "encs-switch.vmdk"

ethernet0.present = "TRUE"
ethernet0.virtualDev = "vmxnet3"
ethernet0.networkName = "VM Network"          # MGMT CPU side - do NOT move this

ethernet1.present = "TRUE"
ethernet1.virtualDev = "vmxnet3"
ethernet1.networkName = "encs-mgmt-2363"      # switch management VLAN

# satisfies switch-confd's dmidecode platform gate by reflecting the real
# chassis SMBIOS - on an ENCS that is already ENCS5412/K9
SMBIOS.reflectHost = "TRUE"

# passthrough requires the full memory reservation
sched.mem.min = "2048"
sched.mem.minSize = "2048"
sched.mem.pin = "TRUE"
```

Add the PCI device through the Host Client (**Add other device → PCI device**)
rather than by hand — it fills in `pciPassthru0.id` and the host-specific
`pciPassthru0.systemId` correctly, and those are easy to get subtly wrong.

Three things that will otherwise waste your afternoon:

- **Full memory reservation is mandatory.** A VM with a passthrough device and
  a partial reservation refuses to power on. `sched.mem.pin = "TRUE"` with
  `sched.mem.min` equal to `memSize` is the whole fix; the Host Client's
  "Reserve all guest memory" checkbox does the same thing.
- **Secure Boot off.** Same as the Proxmox `pre-enrolled-keys=0`.
- **The SMBIOS gate is probably not load-bearing here anyway.** It lives in
  `switch-confd`'s RPM *scriptlet*, which runs at install time, and the
  kickstart already enables `marvell-switch-boot.service` unconditionally
  regardless of what the scriptlet decided (`kickstart/ks-encs.cfg`). So if
  `reflectHost` turns out not to work on your build, try it anyway before
  concluding you are blocked — the runtime path has no `dmidecode` check in it.

**Console.** The image boots with `console=tty0 console=ttyS0,9600n8`, so the
ordinary VMware VGA console gives you a login on tty1 — no serial plumbing
needed. If you want the serial console as well, add a network serial port
(`telnet://:1234`) and open the firewall with
`esxcli network firewall ruleset set -e true -r remoteSerialPort`. Note **9600**
baud inside the VM; the 115200 in the README is the *chassis* console.

---

## 5. First boot: does the ASIC come up?

Power on and watch the guest:

```sh
journalctl -u marvell-switch-boot -f
```

Cold ASIC to operational is about 60 s, ending in `ROS ready!`. The sequence
and the failure modes are identical to the README's — nothing about them is
hypervisor-specific.

`encs-switch-status` inside the VM is the quick check.

---

## 6. Put the VM on the switch management network

This is the ESXi-specific half. Inside the bootstrap VM, give the second vNIC
the address `install.sh` would have given the Proxmox host:

```sh
# identify the vNIC on the 2363 portgroup - it is the one with no DHCP lease
ip -br link

nmcli con add type ethernet ifname ens224 con-name encs-mgmt \
      ipv4.method manual ipv4.addresses 169.254.1.1/16 \
      ipv4.never-default yes ipv6.method ignore
nmcli con up encs-mgmt
```

`ipv4.never-default yes` matters: this interface must never become the default
route. Also make sure NetworkManager's IPv4 link-local fallback is not fighting
you for the same `169.254.0.0/16` — an autoconfigured address in that range on
the wrong interface makes the switch look unreachable for reasons nothing on
screen explains.

Then:

```sh
ping -c2 169.254.1.0        # the ASIC
/opt/encs-host/encs-switch-tui
```

Everything the README says about the TUI applies unchanged from here — it is
the same program talking to the same XML API over the same VLAN. The only
difference is which machine it runs on.

**Install the tools properly** so the replay service exists:

```sh
bash /opt/encs-host/install.sh
```

On this platform `install.sh` will not find two `i40e` ports (they belong to
ESXi, not the VM) and will stop. Do the two useful parts by hand instead:

```sh
install -m 0755 /opt/encs-host/encs-switch-{api,tui,vnet} /usr/local/sbin/
install -m 0644 /opt/encs-host/encs-switch-replay.service /etc/systemd/system/
mkdir -p /etc/encs-switch
systemctl daemon-reload
systemctl enable encs-switch-replay.service
```

`encs-switch-replay.service` waits for `169.254.1.0` to answer and then applies
every `/etc/encs-switch/*.xml`, which is what restores VLANs, port state and PoE
after a **cold** boot — the ASIC has no flash and comes back with firmware
defaults plus every front port shut. Running it inside the bootstrap VM is
strictly better than running it on the hypervisor: the VM is by definition up
before the switch is.

Do **not** enable `encs-switch-startup.service` here. It is Proxmox-only —
it orders guests via `qm` and `/etc/pve/qemu-server`. The ESXi equivalent is
[step 8](#8-boot-ordering).

On this branch `encs-switch-vnet` refuses `init`, `teardown` and `startup` when
the files they edit are not there — `/etc/network/interfaces` for the first
two, plus `/etc/pve/qemu-server` for `startup` — and names the ESXi alternative
when it sees it is running in a VMware guest. Better than writing a file
nothing on an AlmaLinux guest reads and leaving you convinced a bridge exists.

---

## 7. VMs on the front LAN ports — the `lan-net` model

The two halves are the same as the README describes; only the host half changes
shape.

| | Proxmox | ESXi |
|---|---|---|
| host half | `swbr0`, VLAN-aware bridge on the te2 NIC | `vSwitchENCS`, uplink = the te2 vmnic |
| untagged (= switch VLAN 1, all 8 jacks) | `bridge=swbr0` | portgroup `encs-lan`, VLAN ID 0 |
| tagged VLAN 100 | `bridge=swbr0,tag=100` | portgroup `encs-lan-100`, VLAN ID 100 |
| switch half | `encs-switch-vnet add 100 --ports gi0` | identical, run inside the bootstrap VM |

**Untagged works out of the box** — that is switch VLAN 1, every front jack,
stock `lan-net`. Attach a guest to `encs-lan` and it comes out `GE1/0`–`GE1/7`.

**A tagged VLAN needs the switch half and the backplane fix:**

```sh
# on ESXi: the host half, one portgroup per VLAN
sh /vmfs/volumes/datastore1/encs-esxi/encs-esxi-vnet add 100 --name dmz

# in the bootstrap VM: the switch half - encs-esxi-vnet prints this line for
# you, because neither half does anything useful alone
encs-switch-vnet add 100 --ports gi0 --name dmz --fix-backplane
```

`encs-esxi-vnet` is a thin thing — it creates `encs-lan-100` with VLAN ID 100,
records it so `uninstall.sh` can take it back out, and refuses VLAN 1 (that is
the untagged `encs-lan` portgroup, where every front port already is) and VLAN
2363 (the switch's own management VLAN: anything on it can configure the ASIC,
whose only credentials are the firmware defaults). By hand it is two `esxcli`
lines:

```sh
esxcli network vswitch standard portgroup add --portgroup-name=encs-lan-100 --vswitch-name=vSwitchENCS
esxcli network vswitch standard portgroup set --portgroup-name=encs-lan-100 --vlan-id=100
```

`add` creates VLAN 100 on the ASIC, makes `GE1/0` an access port in it, enables
the port if a cold boot left it shut, merges VLAN 100 into te2's trunk list
(`--fix-backplane` — te2's firmware default is `trunk, members 1,2363` and
nothing else), and saves `/etc/encs-switch/*.xml` so a power cut does not undo
it. Read the warning it prints: that write lands on the port your management
session rides on.

**Which subcommands work here:**

| Subcommand | On ESXi |
|---|---|
| `add`, `remove` | **work** — pure switch-side, apart from a closing hint that names `qm` |
| `status` | **works**, but the host half is always reported as absent — there is no `swbr0` to find, which is correct |
| `init`, `teardown` | **Proxmox-only** — they edit `/etc/network/interfaces`, which nothing on an AlmaLinux guest reads. On this branch they refuse outright rather than writing it |
| `startup` | **Proxmox-only** — also needs `/etc/pve/qemu-server`; likewise refuses |

The same isolation caveat applies as on Proxmox, one layer over: anything on a
portgroup tagged 2363 can configure the switch, because the API has no
authentication beyond the firmware default `cisco/cisco`. Keep `encs-mgmt-2363`
for the bootstrap VM and nothing else.

---

## 8. Boot ordering

A guest on `encs-lan` that starts before the ASIC is up gets a working vNIC and
a network that forwards nothing for 60–90 s. DHCP fails and the cause is two
layers from where it shows. Proxmox gets `order=1,up=90` on the bootstrap VM;
ESXi's equivalent is autostart with a start delay:

```sh
vim-cmd hostsvc/autostartmanager/enable_autostart 1
vim-cmd vmsvc/getallvms                       # note the Vmid of each VM

# bootstrap VM: first, then wait 90s before starting anything else
vim-cmd hostsvc/autostartmanager/update_autostartentry <vmid> "PowerOn" "90" "1" "guestShutdown" "0" "systemDefault"

# each guest that depends on the switch
vim-cmd hostsvc/autostartmanager/update_autostartentry <vmid> "PowerOn" "0" "2" "guestShutdown" "0" "systemDefault"
```

As on Proxmox, **the delay goes on the bootstrap VM, not on the guest waiting**
— the delay applies before the *next* entry starts.

---

## Feature parity

| README feature | ESXi | Notes |
|---|---|---|
| ASIC bootstrap | same image, DirectPath I/O instead of `hostpci0` | |
| Platform gate | `SMBIOS.reflectHost` | or nothing at all — see step 4 |
| Switch management (`encs-switch-tui`, `encs-switch-api`) | **runs in the bootstrap VM** | ESXi cannot run them |
| All switch features (VLANs, PoE, LAG, mirroring, …) | unchanged | it is the same client against the same API |
| Cold-boot replay | `encs-switch-replay.service`, in the VM | |
| `lan-net` — VMs on front ports | portgroups on `vSwitchENCS` | step 7 |
| `encs-switch-vnet add/remove/status` | works in the VM | |
| `encs-switch-vnet init/teardown/startup` | **not applicable** | `encs-esxi-vnet` and `vim-cmd` do these jobs; the Proxmox subcommands refuse to run here |
| `install.sh` (host side) | `encs-esxi/install.sh` | passthrough + vSwitch only — the tools stay in the VM |
| — | `encs-esxi/uninstall.sh` | no Proxmox equivalent; ESXi needs one more |
| Guest boot ordering | autostart delay | step 8 |
| GUI bridge picker with a comment explaining which is which | portgroup names | `encs-lan*` vs `VM Network` is the same hint, weaker |
| Host tool updates (`encs-switch-tui --update`) | works, inside the VM | needs the VM to reach github.com |
| NIM slot | still nothing | BMC-owned; no OS can drive it |

---

## Reverting everything

```sh
sh /vmfs/volumes/datastore1/encs-esxi/uninstall.sh          # show the plan
sh /vmfs/volumes/datastore1/encs-esxi/uninstall.sh --yes    # do it
```

`uninstall.sh` removes exactly what `install.sh` recorded in
`/etc/encs-esxi/created` and nothing else, in the order below, and refuses to
remove a portgroup that still has VMs on it. It does not touch the VM — power
that off and unregister it first. If the record was lost, `--force` rebuilds
the list: portgroups and the vSwitch by their default names, and the uplink and
the passthrough device by reading them back off the vSwitch and the PCI list —
the same queries `install.sh` used to find them in the first place.

The manual version, which is also what the script does. Every step is
reversible and none of it touches `vSwitch0`, `vmk0`, or any VM you did not
create for this. Work top to bottom.

**1. Stop the VM.**

```sh
vim-cmd vmsvc/power.off <vmid>
```

Note that this does *not* turn the switch off. The ASIC keeps running and
forwarding on standby power until the chassis loses AC — so the front ports
keep whatever VLANs and PoE state you last set, and a later attempt to
re-bootstrap without a cold power cycle will wedge the loader.

**2. Remove it from autostart.**

```sh
vim-cmd hostsvc/autostartmanager/update_autostartentry <vmid> "None" "-1" "-1" "None" "-1" "systemDefault"
# and, if you turned it on for this and want it off again:
vim-cmd hostsvc/autostartmanager/enable_autostart 0
```

`-1` is the sentinel for "not in the autostart sequence" — `0` is a valid
order and would leave the VM in it with no delay.

**3. Release the passthrough device.**

```sh
esxcli hardware pci pcipassthru set --device-id=0000:0d:00.0 --enable=false --apply-now
```

Remove the `11ab be00` line from `/etc/vmware/passthru.map` if you added one,
then `/sbin/auto-backup.sh`.

**4. Unregister or delete the VM.**

```sh
vim-cmd vmsvc/unregister <vmid>              # keeps the files
# or
vim-cmd vmsvc/destroy <vmid>                 # deletes them
```

**5. Move any guests off the switch portgroups** before removing them — a VM
holding a portgroup blocks its removal, and a VM whose network disappears
underneath it loses its NIC on the next power-on.

**6. Remove the networking.** In this order:

```sh
esxcli network vswitch standard portgroup remove --portgroup-name=encs-lan-100 --vswitch-name=vSwitchENCS
esxcli network vswitch standard portgroup remove --portgroup-name=encs-lan --vswitch-name=vSwitchENCS
esxcli network vswitch standard portgroup remove --portgroup-name=encs-mgmt-2363 --vswitch-name=vSwitchENCS
esxcli network vswitch standard uplink remove --uplink-name=vmnic3 --vswitch-name=vSwitchENCS
esxcli network vswitch standard remove --vswitch-name=vSwitchENCS
```

If you added a diagnostic VMkernel port on the management VLAN, remove it
first: `esxcli network ip interface remove --interface-name=vmk1`.

**7. Persist and reboot.**

```sh
/sbin/auto-backup.sh
reboot
```

The host comes back exactly as it was: `vSwitch0` and `vmk0` were never
touched, the X710 goes back to being an unused vmnic, and the Marvell device
returns to the VMkernel — which does nothing with it, because ESXi has no
driver for it. That is the same state a stock ESXi install is in.

**What does not revert on its own:** the switch. Its configuration lives in
ASIC RAM and survives until AC is removed. If you want the front ports back to
firmware defaults — VLAN 1 and 2363 only, every port shut, PoE off — pull the
power cord. A CIMC power-off is not enough.

---

## Testing it without an ESXi host

```sh
python3 scripts/66-test-esxi.py        # -v to list every check
```

The same trick `60-test-tui.py` uses on the TUI: `install.sh`, `uninstall.sh`
and `encs-esxi-vnet` run against a **fake `esxcli`** holding its state in a JSON
file. The fake host has two I210s, two X710 functions and the Marvell — plus a
decoy device at `0e:00.0` carrying the Marvell's ids in its *SUBsystem* fields,
because on this chassis that address really has been the I210 on one install
and the Marvell on another.

What it asserts is the part you would otherwise have to trust:

- the default run writes nothing at all — the host comes back bit-identical
- `--yes` creates exactly what it printed, and records exactly that
- install → `vnet add` → uninstall is a **round trip**: the fake host ends up
  bit-identical to before it started
- `vSwitch0` keeps its uplink and portgroups, and **no write command so much as
  names it**, in every single test
- it refuses to take an uplink off another vSwitch, to adopt a portgroup it did
  not create, and to remove a portgroup with a VM still on it
- a failed removal keeps the record — listing only what is left — instead of
  deleting it and claiming success

Every `esxcli` write in the bundle was also checked against Broadcom's
published command reference, and the list parsers tolerate both the indented
and the flat output formats.

**That is not the same as being tested on ESXi, and the first real run proved
it.** A fake tells you the scripts do nothing you did not ask for. It cannot
tell you that the real `esxcli` accepts these command lines or prints its lists
in this shape — and on all four counts below, it did not.

### What real ESXi taught us

The first run on hardware failed on the very first line. Four bugs, none of
which the 111 offline checks could have caught, because in each case the fake
was more forgiving than the real thing:

| What broke | Why it was not caught earlier |
|---|---|
| `command -v esxcli` → `ERROR: esxcli not found` | busybox ash has no `command` builtin — it exits 127 with `sh: command: not found`. The `sh` the tests run under does have it. Now uses `type`. |
| Passthrough state always read as unknown | `pcipassthru list` prints a table; the fake printed one field per line like the other `hardware pci` lists. The plan claimed passthrough needed enabling on every run, and a second run recorded it twice. |
| `tr` silently missing | ESXi's busybox has no `tr`. The test prepended the fake bindir to the *developer's* `PATH`, so it always resolved. It blanked the "i40en uplinks:" line. |
| `pcipassthru set --device --active=true` rejected | The fake accepted any `--key=value` it was handed. Real options are `--device-id` and a bare `--apply-now`. |
| The shipped `.vmx` would not power on with a passthrough device | No `pciBridge*` lines: *"No PCIe slot available for SCSI0 … Too many PCI devices are already configured."* Nothing offline builds a VM, so nothing exercised it. |
| `/etc/encs-esxi/created` lost on every reboot | `/etc` is a 28 MB RAM disk, and `auto-backup.sh` archives only files with a `.#<name>` marker. `install.sh` called it believing that persisted the record. `passthru.map` survives only because it ships with its marker. The record now lives beside the bundle on VMFS. |
| `pciPassthru0.id` is not the hex BDF | ESXi writes `%05d:%03d:%02d.%d`, **all fields decimal** — the format string is in `/usr/lib/vmware/drivers/lib/libpci_bus.so`. `0000:0d:00.0` → `00000:013:00.0`; slot `0x1d` → `29`, not `1d`. A hex BDF is refused with *"AH No device hints found"*, naming neither the key nor the format. |

| `encs-switch-tui` documented the wrong machine | Its help said "RUN THIS ON THE HYPERVISOR … the bootstrap VM cannot reach the switch at all". True on Proxmox, backwards here: ESXi cannot run it, and a VM reaches the switch fine. Nothing offline runs the tools against a live switch. |

A further one was found while fixing the sixth: the conversion above was first
written with awk's `strtonum()`, which is a gawk extension — ESXi's busybox awk
answers *"Call to undefined function"*. It is POSIX parameter expansion and
`$((0x..))` now. The lesson is the same one this whole file keeps teaching:
on this platform, assume nothing is present until it has been run there.

The fixes to the test matter as much as the fixes to the bundle: the fake now
prints the real table shape, validates option names against `esxcli --help`,
and runs with `PATH` set to a curated list of what ESXi's `/bin` and `/sbin`
actually contain — `tr` deliberately absent. `command -v` and `tr` are also
scanned for statically, because the test host's shell has both.

---

## Two jobs, two VMs: how this actually works on ESXi

This is the one structural difference from the Proxmox path, and it is not
optional. On ESXi the work splits across **two roles**, because the thing that
boots the ASIC and the thing that manages it need opposite configurations.

| | Bootstrap VM | Management VM |
|---|---|---|
| Marvell passed through | **yes** | **no - never** |
| Runs | once per AC power cycle | whenever you like |
| Survives the run | no, see below | yes |
| What it does | pushes firmware over PCIe | `encs-switch-tui`, `encs-switch-api`, `encs-switch-vnet` |
| Needs | `pciPassthru0`, all memory reserved | one vNIC on `encs-mgmt-2363`, `169.254.1.1/16` |

They can be the same VM reconfigured between the two roles, but they cannot be
the same VM *at the same time*, and the reason is sharp:

> **Never power on a VM that still has the Marvell attached while the switch is
> up.** `marvell-switch-boot` runs at every boot, and a loader run against an
> already-booted ASIC wedges it in "Service CPU not ready (requires reset?)"
> until AC is physically removed. Once the switch is bootstrapped, take the PCI
> device off that VM.

### The management side needs no PCI device at all

The tools speak HTTPS to `169.254.1.0`; nothing in them touches the ASIC over
PCIe. Verified on the chassis from a VM with the Marvell **not** attached
(`lspci -d 11ab:` empty):

```
    OK  switch reachable at 169.254.1.0        # encs-switch-status
    $ encs-switch-api get '{VLANInterfaceMembershipTable}'
    <VLANID>1</VLANID>    <untaggedPorts>gi0-7,te1-4,LAG1-4</untaggedPorts>
    <VLANID>2363</VLANID> <taggedPorts>te2</taggedPorts>
```

So `encs-switch-tui` runs in a guest here, not on the host as it does on
Proxmox. Give that guest a second vNIC on `encs-mgmt-2363`, address it
`169.254.1.1/16`, give it no default route, and run the tools exactly as the
README describes. `encs-switch-status` will report FAIL for the Marvell, the
module and `/dev/servicecpu` - that is correct and expected for a management
VM, and the line that matters is the last one.

This split has an upside over the original design: the managing VM never
touches the loader, so it can be restarted, migrated or rebuilt freely. The
"no HA restart policy" warning applies only to the bootstrap VM.

## The IOMMU fault

The bootstrap VM is killed part-way through every run:

```
PCI passthru device 0000:0d:00.0 caused an IOMMU fault type 5 at
address 0xe0041000.  Powering off the virtual machine.
```

**This is survivable and, in practice, cosmetic.** By the time it fires the
firmware is already in the ASIC's DDR; u-boot carries on to ROS without the VM,
and the switch comes up and stays up - through the VM's death, and through host
reboots. What it costs you is the VM, not the switch.

It took a while to get there. The fault used to land *early*, before the upload
finished, and the switch then came up only when it happened to win the race -
once in three attempts. The fix was
[the PCI COMMAND register](#the-command-register-bug): with memory decode
enabled the upload completes deterministically, and every run since has ended
with a working switch.

### Why that address, and why it cannot be moved

`0xe0041000` is invariant across every variable tried - 2 GB and 4 GB of guest
RAM, `pciPassthru.use64bitMMIO`, `pciHole.start`, `pciHole.dynStart` and the
COMMAND fix. `/proc/iomem` in the guest says why:

```
e0000000-e7ffffff : PCI MMCONFIG 0000 [bus 00-7f]
  e0000000-e7ffffff : pnp 00:05
```

**`0xe0000000` is the guest's PCI MMCONFIG (ECAM) window.** The ASIC's register
offsets are `0x41xxx` (`regAddr=0x00041804`, `0x00041820`, ...), so a window
based at `0xe0000000` puts its register accesses inside the guest's
memory-mapped *config* space - which the IOMMU will never map for device DMA.
The offset even decodes as a valid ECAM address (bus 0, device 8, function 1).

That is the whole story: the ASIC is not DMAing somewhere random, it is DMAing
to a base that on this platform collides with ECAM. On Proxmox the same
`0xe0000000` appears in the working trace ("`2) address: ...41824
data:0xe0000000`") and is ordinary BAR space there, so nothing faults.

Both ways of moving the collision are closed:

- **From the host.** `pciHole.dynStart` is not settable. ESXi computes it
  (2560, i.e. `0xa0000000`, exactly where the BARs land) and *rewrites the VMX
  back to its own value* on power-on - a `3072` written into the file is `2560`
  again by the time the VM boots. `pciHole.start` is read but does not govern.
- **From the guest.** `pci=nommconf` does stop Linux using MMCONFIG, but the
  range stays reserved as `pnp 00:05` from the virtual firmware's ACPI tables,
  so the PCI allocator still will not place a BAR there.

Short of a VM firmware that puts ECAM somewhere else, there is nothing left to
turn. This is documented rather than fixed - and it costs the bootstrap VM,
not the switch.

### The COMMAND register bug

Worth knowing about even outside ESXi. `mv_pciboot` never calls
`pci_enable_device()` or `pci_set_master()`; it assumes whatever enumerated the
bus left the device enabled. That holds on bare metal and under Proxmox. Under
ESXi DirectPath I/O the guest sees:

```
COMMAND = 0x0000       Mem-  BusMaster-
Region 0: Memory at c4000000 (64-bit, prefetchable) [disabled]
```

Every MMIO read then returns `0xffffffff`, so the module derives its address
windows from all-ones garbage:

```
WR regAddr=0x00041824 data=0xc0000000     module writes win0_base
RD regAddr=0x00041824 data=0xffffffff     reads back all-ones
```

`setpci -d 11ab:be00 COMMAND=0006:0006` before the module loads fixes it, and
`marvell-switch-boot.service` now does exactly that. The same reads then come
back correct and the windows land on real BARs, which is what turns a coin
flip into a deterministic bootstrap.

---

## Known unknowns

Things a first attempt should watch for, so a report back is useful:

1. ~~**Does DirectPath I/O accept the device without a `passthru.map` entry?**~~
   **Answered: yes.** ESXi 8.0 U3 selected `Bridge reset` unprompted and moved
   the device to `VM Passthru` with no map entry and no reboot.
2. ~~**Does `SMBIOS.reflectHost` actually present `ENCS5412/K9` to the guest?**~~
   **Answered: yes.** `dmidecode -s system-product-name` in the bootstrap VM
   returns `ENCS5412/K9`, manufacturer `Cisco Systems, Inc.`, and the chassis
   serial. The platform gate is satisfied without any per-VM SMBIOS authoring.
3. ~~**Does the loader survive an ESXi VM restart?**~~ **Answered, but the
   question was the wrong one.** The VM does not survive its first run at all
   (the [IOMMU fault](#the-iommu-fault)), and that turns out not to matter: the
   loader has to run exactly **once per AC power cycle**, not once per VM boot.
   A restart is not something to survive, it is something to avoid — re-running
   the loader against a live ASIC wedges it. The managing VM, which has no PCI
   device, restarts freely.
4. ~~**Whether ESXi 8.0 installs and runs on this chassis at all**, given the
   deprecated CPU generation.~~ **Answered: it does.** 8.0 U3 build 24677879,
   12C/24T, `HV Support: 3`, with the I350s, X552, both X710 functions, the
   I210 and the Marvell all enumerated.

All four are answered. What remains is not on the original list and is no
longer a mystery, only unfixable from here: the
[IOMMU fault](#the-iommu-fault) is the ASIC's register window colliding with
the guest's ECAM range at `0xe0000000`, and neither the host nor the guest can
move either one. It costs the bootstrap VM, not the switch.
