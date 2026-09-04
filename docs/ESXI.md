# Running this on ESXi — experimental

> **This works, on a real ENCS 5412** (`ENCS5412/K9`, FGL232931K9) under
> **ESXi 8.0 U3** (build 24677879). The switch boots, stays up, and is fully
> manageable — VLAN tables, ports, the lot.

Two things differ from the Proxmox path, and you need both:

1. **The work splits across [two VM roles](#two-jobs-two-vms-how-this-actually-works-on-esxi)** —
   one with the Marvell passed through, which bootstraps the ASIC once per AC
   cycle, and one *without* it, which runs the tools.
2. **The bootstrap VM is killed by an [IOMMU fault](#the-iommu-fault)**
   part-way through. This turns out not to matter: the firmware is already
   delivered by then and the ASIC finishes on its own.

Before you start, read [reverting everything](#reverting-everything) — it
exists so a failed attempt costs you a reboot, not your ESXi install.

<details>
<summary>About this branch, and why every step is written out by hand</summary>

You are on `experimental/esxi`, which carries the tooling as well as the
walkthrough: a VMDK build target and an `esxcli` installer/uninstaller for the
host side. Getting here turned up eight bugs the offline tests could not — see
[what real ESXi taught us](#what-real-esxi-taught-us). All eight are fixed, and
the ones a mock can express are now caught by `scripts/66-test-esxi.py`.

Everything below is also written as manual commands, because when an
experimental script does something you did not expect, the useful thing to have
is the list of what it was trying to do.

</details>

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

## Contents

**Read first:** [what is different](#what-is-different-from-the-proxmox-path) ·
[where things live](#where-everything-actually-lives) ·
[prerequisites](#prerequisites) · [the short version](#the-short-version)

**The walkthrough:**
[1 passthrough](#1-enable-passthrough-for-the-marvell-device) ·
[2 disk](#2-get-the-disk-onto-a-datastore) ·
[3 vSwitch](#3-build-the-switch-vswitch) ·
[4 bootstrap VM](#4-create-the-bootstrap-vm) ·
[5 bootstrap](#5-bootstrap-power-on-and-expect-the-vm-to-die) ·
[6 management role](#6-switch-the-vm-to-the-management-role) ·
[7 LAN ports](#7-vms-on-the-front-lan-ports--the-lan-net-model) ·
[8 boot order](#8-boot-ordering) ·
[9 after a reboot](#9-what-to-do-after-a-reboot-or-a-power-cut)

**Day to day:** [the TUI is the switch console](#day-to-day-the-tui-is-the-switch-console) ·
[feature parity](#feature-parity) · [reverting everything](#reverting-everything)

**Why it works the way it does** — reference, read when something surprises you:
[two jobs, two VMs](#two-jobs-two-vms-how-this-actually-works-on-esxi) ·
[the IOMMU fault](#the-iommu-fault) ·
[the COMMAND register bug](#the-command-register-bug) ·
[what real ESXi taught us](#what-real-esxi-taught-us) ·
[known unknowns](#known-unknowns)

---

## What is different from the Proxmox path

Two things, and both change *where* code runs rather than what it does.

**ESXi cannot run the host tools.** `encs-switch-tui` is Python + curses,
`encs-switch-api` is bash + curl, `install.sh` writes systemd units and
`/etc/network/interfaces`. The ESXi shell is busybox `ash` with no bash, no
systemd, no ifupdown, and a stripped Python without curses. None of that
bundle runs there.

**So the tools move into a VM.** They are already in the image at
`/opt/encs-host/` — on Proxmox you copy them *out* to the hypervisor, and here
you simply leave them where they are. The VM gets a second vNIC on a portgroup
tagged VLAN 2363, which puts it on the switch management network.

### Where everything actually lives

This is the thing most worth getting straight before you start, because it is
the reverse of the Proxmox layout:

| | Proxmox | **ESXi** |
|---|---|---|
| `encs-switch-tui` / `-api` / `-vnet` | on the **host** | **in a VM** |
| NFVIS bits (`switch-confd`, `mv_pciboot`, `remote_boot_app`, firmware) | in the bootstrap VM | in the VM image |
| Reaches `169.254.1.0` | the host | a VM, over `encs-mgmt-2363` |
| What the hypervisor itself does | everything | **only** passthrough + vSwitch + portgroups |

Nothing from `/opt/encs-host` is ever copied to the ESXi host — it cannot run
any of it. The ESXi host is pure plumbing. And because bootstrapping and
managing want opposite VM configurations, the VM side splits in two: see
[two jobs, two VMs](#two-jobs-two-vms-how-this-actually-works-on-esxi).

### What `opt/encs-esxi/install.sh` does, and does not do

It is the host-side installer only. It **does**: enable passthrough on the
Marvell, create `vSwitchENCS`, add the te2 X710 function as its uplink, and
create the `encs-mgmt-2363` and `encs-lan` portgroups. It records all of that
so `uninstall.sh` can take exactly it back out.

It **does not**: build the image, upload or import the disk, create or register
a VM, attach the PCI device, or install anything in the guest. Those are
[steps 2](#2-get-the-disk-onto-a-datastore),
[4](#4-create-the-bootstrap-vm) and [6](#6-switch-the-vm-to-the-management-role),
and they are manual.

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

- **A build host that is x86_64 Linux with KVM.** The qcow2 step installs
  AlmaLinux under QEMU, and `createrepo_c` has no Homebrew formula, so an
  arm64 Mac cannot build this at all. A VM on the ENCS itself works well and is
  what this was built on; on AlmaLinux 9 you also need two symlinks that
  `check_deps` does not mention: `qemu-system-x86_64` →
  `/usr/libexec/qemu-kvm`, and `/usr/share/OVMF/OVMF_CODE.fd` →
  `../edk2/ovmf/OVMF_CODE.fd`.
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

## The short version

Every command, in the order you actually run them, for someone who has done
this before or wants to see the shape of it first. The numbered sections below
are the same work with the detail and the failure modes — **read those the
first time**; the labels here name which step each block belongs to.

Note the execution order is not the section order: `install.sh` does
[step 1](#1-enable-passthrough-for-the-marvell-device) and
[step 3](#3-build-the-switch-vswitch) in one go, so the disk work
([step 2](#2-get-the-disk-onto-a-datastore)) happens after it.

```sh
# BUILD - prerequisites; x86_64 Linux with KVM, an arm64 Mac cannot do this
./build.sh --esxi /path/to/Cisco_NFVIS-4.15.5-FC4.iso

# HOST SIDE - steps 1 + 3: passthrough, vSwitch, portgroups
scp -r payload/opt/encs-esxi root@<esxi>:/vmfs/volumes/datastore1/
ssh root@<esxi> 'sh /vmfs/volumes/datastore1/encs-esxi/install.sh'        # plan
ssh root@<esxi> 'sh /vmfs/volumes/datastore1/encs-esxi/install.sh --yes'  # apply

# DISK - step 2
ssh root@<esxi> 'mkdir -p /vmfs/volumes/datastore1/encs-switch'
scp out/esxi/encs-switch.vmdk out/esxi/encs-switch.vmx \
    root@<esxi>:/vmfs/volumes/datastore1/encs-switch/
ssh root@<esxi> 'cd /vmfs/volumes/datastore1/encs-switch &&
    vmkfstools -i encs-switch.vmdk -d thin disk.vmdk && rm -f encs-switch.vmdk &&
    sed -i "s|^scsi0:0.fileName.*|scsi0:0.fileName = \"disk.vmdk\"|" encs-switch.vmx'

# VM - step 4: register, then attach the Marvell (Host Client, or the
#      block install.sh printed - the id is NOT the hex BDF)
ssh root@<esxi> 'vim-cmd solo/registervm \
    /vmfs/volumes/datastore1/encs-switch/encs-switch.vmx'

# BOOTSTRAP - step 5: power on, and EXPECT THE VM TO DIE ~60s in. Normal.
ssh root@<esxi> 'vim-cmd vmsvc/power.on <vmid>'

# CONFIRM - step 5: the switch came up anyway (from a VM on encs-mgmt-2363)
ping -c2 169.254.1.0

# MANAGEMENT - step 6: remove the PCI device from the VM, power it back on,
#               then inside it (and see step 9 for what to do after a reboot):
nmcli con add type ethernet ifname ens224 con-name encs-mgmt \
      ipv4.method manual ipv4.addresses 169.254.1.1/16 \
      ipv4.never-default yes ipv6.method ignore
/opt/encs-host/encs-switch-tui
```

Revert the host side with
`sh /vmfs/volumes/datastore1/encs-esxi/uninstall.sh --yes`.

**The three things that surprise people**, all covered below:

1. The bootstrap VM is **killed by an IOMMU fault** every run
   ([why](#the-iommu-fault)). The switch comes up anyway. Do not read the VM
   dying as failure.
2. Bootstrap and management are **two different VM configurations**
   ([why](#two-jobs-two-vms-how-this-actually-works-on-esxi)), and booting a VM
   that still has the Marvell attached while the switch is up **wedges it**
   until AC is pulled.
3. Bootstrap must re-run after **every host power-on or reboot** - a warm
   reboot drops the switch, though it leaves the ASIC re-bootstrappable rather
   than wedged.

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

# WITHOUT THESE the VM will not power on once a passthrough device is added:
# "No PCIe slot available for SCSI0 ... Too many PCI devices are already
# configured". The Host Client writes them for you; a hand-written VMX must
# say them itself.
pciBridge0.present = "TRUE"
pciBridge4.present = "TRUE"
pciBridge4.virtualDev = "pcieRootPort"
pciBridge4.functions = "8"
pciBridge5.present = "TRUE"
pciBridge5.virtualDev = "pcieRootPort"
pciBridge5.functions = "8"
pciBridge6.present = "TRUE"
pciBridge6.virtualDev = "pcieRootPort"
pciBridge6.functions = "8"
pciBridge7.present = "TRUE"
pciBridge7.virtualDev = "pcieRootPort"
pciBridge7.functions = "8"

# satisfies switch-confd's dmidecode platform gate by reflecting the real
# chassis SMBIOS - on an ENCS that is already ENCS5412/K9
SMBIOS.reflectHost = "TRUE"

# passthrough requires the full memory reservation
sched.mem.min = "2048"
sched.mem.minSize = "2048"
sched.mem.pin = "TRUE"
```

Add the PCI device through the Host Client (**Add other device → PCI device**)
rather than by hand. Both values are host-specific *and* the id is not the
format anyone guesses:

```ini
pciPassthru0.present  = "TRUE"
pciPassthru0.id       = "00000:013:00.0"   # NOT 0000:0d:00.0
pciPassthru0.deviceId = "0xbe00"
pciPassthru0.vendorId = "0x11ab"
pciPassthru0.systemId = "<esxcli system uuid get on THIS host>"
```

ESXi formats the id as `%05d:%03d:%02d.%d` with **every field in decimal** —
the format string is in `/usr/lib/vmware/drivers/lib/libpci_bus.so`. So bus
`0x0d` is `013`, and a device at slot `0x1d` would be `29`, not `1d`. A hex BDF
is refused with `AH No device hints found` and `Failed to generate predicates
for pciPassthru0---invalid VM configuration`, which names neither the key nor
the format.

`install.sh` prints the correct block for whatever address it found on your
host, so the reliable options are: the Host Client, `govc device.pci.add -vm
encs-switch 0000:0d:00.0`, or pasting what `install.sh` gave you.

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

## 5. Bootstrap: power on, and expect the VM to die

Power on the VM. About 60 seconds later ESXi kills it:

```
PCI passthru device 0000:0d:00.0 caused an IOMMU fault type 5 at
address 0xe0041000.  Powering off the virtual machine.
```

**That is the expected outcome, not a failure.** By the time it fires the
firmware is already in the ASIC's DDR and u-boot carries on without the VM.
[Why it happens, and why it cannot be fixed from here.](#the-iommu-fault)

You will *not* see `ROS ready!` in the guest journal, because the VM does not
live long enough — and the journal usually will not survive the kill either.
The loader writes progress to `/root/bootstrap-debug.log` as well, which does
survive; a good run ends:

```
COMMAND before: 0000
setpci rc=0
COMMAND after: 0006
Reading CPI configuration space BARs: [0] 0xc400000c, [0] 0xc000000c, [0] 0xa000000c
Loading bootstrap to service CPU SRAM... done.
Send IRQ to wake service CPU
Service CPU is not ready for FW yet.        (a few of these are normal)
Loading firmware to service CPU DDR... done.
FW upload done !!!
uboot started ...
uboot running
```

### Did it actually work?

Judge by the **switch**, not by the VM. Two independent checks from the host:

```sh
# both X710 backplane links come up only when the ASIC is running
esxcli network nic list | grep -E 'vmnic3|vmnic4'
#   vmnic4  ...  Up  10000     <- good
#   vmnic4  ...  Down    0     <- ASIC is not running
```

and, from any VM with a vNIC on `encs-mgmt-2363` holding `169.254.1.1/16`:

```sh
ping -c2 169.254.1.0
```

Give it a minute. ROS takes appreciably longer to answer than the links take to
come up, and calling it dead too early is the single easiest mistake to make
here.

**If the loader instead says `Service CPU not ready (requires reset?)`,** the
ASIC has already been bootstrapped. That is the wedge case: pull AC (a host
reboot is not enough), then try again. See
[step 9](#9-what-to-do-after-a-reboot-or-a-power-cut).

---

## 6. Switch the VM to the management role

**This step is not optional, and doing it wrong wedges the switch.**

The VM that bootstrapped the ASIC must not keep the Marvell attached.
`marvell-switch-boot` runs at every boot, and a loader run against an
already-booted ASIC wedges it until AC is physically removed. So:

1. **Remove the PCI device** from the VM (Host Client → Edit settings → the
   PCI device → remove). You can drop the memory reservation at the same time;
   it was only needed for passthrough.
2. Power the VM back on.

It is now a management VM: no PCI device, one vNIC on `encs-mgmt-2363`. Give
that vNIC the address the Proxmox host would have held:

```sh
# identify the vNIC on the 2363 portgroup - it is the one with no DHCP lease
ip -br link

nmcli con add type ethernet ifname ens224 con-name encs-mgmt \
      ipv4.method manual ipv4.addresses 169.254.1.1/16 \
      ipv4.never-default yes ipv6.method ignore
nmcli con up encs-mgmt
```

**Use a persistent connection, not a bare `ip addr add`.** NetworkManager drops
a manually added address on that interface, and the symptom is a switch that
looks dead from this VM while being perfectly healthy — an easy hour to lose.

`ipv4.never-default yes` matters too: this interface must never become the
default route. And make sure NetworkManager's own IPv4 link-local fallback is
not fighting you for `169.254.0.0/16` on some other interface.

Then:

```sh
ping -c2 169.254.1.0        # the ASIC
/opt/encs-host/encs-switch-tui
```

Everything the README says about the TUI applies unchanged — same program,
same XML API, same VLAN. Only the machine it runs on differs.

`encs-switch-status` will report FAIL for the Marvell, `mv_pciboot` and
`/dev/servicecpu` on this VM. **That is correct and expected** for a management
VM; the line that matters is the last one, `switch reachable at 169.254.1.0`.

### Install the tools properly

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
after a cold boot — the ASIC has no flash and comes back with firmware defaults
plus every front port shut. Running it in this VM is strictly better than on
the hypervisor: the VM is by definition up before the switch is.

Do **not** enable `encs-switch-startup.service` here. It is Proxmox-only — it
orders guests via `qm` and `/etc/pve/qemu-server`. The ESXi equivalent is
[step 8](#8-boot-ordering).

On this branch `encs-switch-vnet` refuses `init`, `teardown` and `startup` when
the files they edit are not there — `/etc/network/interfaces` for the first
two, plus `/etc/pve/qemu-server` for `startup` — and names the ESXi alternative
when it sees it is running in a VMware guest.

### Keeping both roles around

Nothing stops you keeping **two registered VMs** on the one disk-image lineage:
`encs-bootstrap` with the PCI device and no autostart, and `encs-switch-mgmt`
without it. That is tidier than editing one VM's hardware twice per power
cycle, and it makes the dangerous configuration something you have to
deliberately power on rather than something you might leave attached.

---

### Day to day: the TUI is the switch console

The setup above is done once. `encs-switch-tui` is not — it is how you operate
the switch from then on, the same way you would log into any managed switch.
Enabling a front port for a new device, putting jacks in a VLAN, building a
LAG, turning PoE on or off for a camera, adding storm control, setting up port
mirroring to debug something: all of it is this tool, run whenever you need it,
for as long as you own the box.

The management VM exists so that is always available. Leave it running.

SSH into it (or open its VMware console) and run the tool:

```sh
ssh root@<management-vm>
encs-switch-tui           # or /opt/encs-host/encs-switch-tui if not installed
```

It is an ordinary interactive curses program: arrow keys move, `p`/`v`/`e`/`m`
/`s`/`c` switch views, `TAB` opens the rest, `SPACE` enables or shuts the
selected port, `ENTER` opens settings, `?` is the built-in manual and `q`
quits. Any terminal that can run `top` can run this; nothing special is needed.

What you come back to it for:

| Hotkey | View | Typical reason to return |
|---|---|---|
| `p` | ports | enable/shut a jack, change speed or duplex, build a LAG |
| `v` | vlans | add a VLAN, move front ports between VLANs, tag te2 |
| `e` | poe | power a camera or AP on/off, set priority, check draw |
| `m` | mac | find which jack a device is on |
| `s` | stats | counters and errors when something is flaky |
| `c` | config | save (`w`) and restore — what gets replayed after a cold boot |
| `TAB` | more | spanning tree, storm control, port mirroring, static MACs, LLDP/CDP, LACP tuning, private VLANs, ACLs, QoS, 802.1X, RADIUS, IGMP snooping, L3 |

A live session looks like this — real hardware, front panel and backplane:

```
 ENCS 5412 switch  169.254.1.0  [connected]                          v0.2.1
 [p] PORTS  [v] vlans  [e] poe  [m] mac  [s] stats  [c] config  [?] help
 port   panel  link     admin  speed   media     lag   attached to
 gi0    GE1/0  down     DOWN   1000    copper    -     -
 ...
 te1    -      UP idle  UP     10000   backplane -     -
 te2    -      UP       UP     10000   backplane -     ens224
 SPACE up/shut  g LAG  ENTER settings  z counters  r refresh  ? help  q quit
```

`[connected]` in the header is the thing to look for. If it says otherwise,
the ASIC is not answering — check `ping 169.254.1.0`, and if that fails see
[step 9](#9-what-to-do-after-a-reboot-or-a-power-cut).

**Save every change you intend to keep.** This matters more here than on a
normal switch, and it is the one habit that will bite you if you skip it: the
ASIC has no flash, so *nothing you configure survives a power cut or a host
reboot on its own*. A LAG you built, PoE you enabled, a VLAN you added — all of
it is gone at the next cold start, and the switch comes back with firmware
defaults and every front port shut.

What carries configuration across is `encs-switch-replay.service` in the
management VM, which reapplies `/etc/encs-switch/*.xml` once the ASIC answers
after each bootstrap. So the rule is: make the change, then write it out.

```sh
# in the TUI:  press  c  for the config view, then  w  to write
#              ("saving - reading every table, this takes a few seconds ...")
# or from a shell, same thing:
encs-switch-tui --save          # capture running config to /etc/encs-switch
encs-switch-tui --apply         # replay it (what encs-switch-replay does)
```

Treat the running switch as volatile and `/etc/encs-switch/` as the real
configuration. A change you made but never wrote is a change you will lose.

**The management VM can be left running.** It never touches the loader, so it
is safe to autostart, restart or rebuild whenever — unlike the bootstrap VM.
Autostarting it is the sensible default, since it is also what replays your
configuration after every one of the "down" rows in
[step 9](#9-what-to-do-after-a-reboot-or-a-power-cut).

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

## 9. What to do after a reboot or a power cut

The ASIC has no flash, so the switch is only ever as alive as the last
bootstrap. What that costs you depends on what happened:

| What happened | Switch | What you do |
|---|---|---|
| Bootstrap VM killed by the IOMMU fault | up | nothing - this is every run |
| Management VM stopped / restarted / rebuilt | up | nothing |
| ESXi host **warm reboot** | **down** | power on the bootstrap VM again |
| Host powered off, or AC removed | **down** | power on the bootstrap VM again |
| Loader run against an already-booted ASIC | **wedged** | **pull AC**, then bootstrap |

Measured on the chassis: after a warm host reboot both X710 links read
`Link Down` and the switch is gone — the vSwitch, portgroups and passthrough
setting all survive, but the ASIC does not. The important half is that it comes
back **re-bootstrappable, not wedged**: powering the bootstrap VM on again goes
straight through to `uboot running`, the links come up at 10 Gbps and the API
answers. No AC pull needed.

Only the wedge case needs someone physically at the box, and the way to avoid
it is the rule from [step 6](#6-switch-the-vm-to-the-management-role): never
leave the Marvell attached to a VM you might boot while the switch is up.

So the operating loop is:

```
host power-on / reboot
    -> power on the bootstrap VM        (it dies; that is fine)
    -> confirm: ping 169.254.1.0
    -> power on the management VM       (no PCI device) and everything else
```

`encs-switch-replay.service` in the management VM reapplies your saved
`/etc/encs-switch/*.xml` once the ASIC answers, which is what puts VLANs, port
state and PoE back after any of the "down" rows above. Without it a cold boot
leaves you with firmware defaults and every front port shut.

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
README describes. Configure that address **persistently** (an ifcfg/nmcli
connection), not with a bare `ip addr add` - NetworkManager will drop a
manually added address on it, and the symptom is a switch that looks dead from
that VM while being perfectly healthy. `encs-switch-status` will report FAIL for the Marvell, the
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
and the switch comes up and stays up. What it costs you is the VM, not the
switch.

What the switch survives, measured rather than assumed:

| Event | Switch |
|---|---|
| The bootstrap VM being killed by the fault | **stays up** |
| The management VM stopped, restarted, rebuilt | **stays up** |
| A warm host reboot | **goes down** - both X710 links read `Link Down` |
| AC removed | goes down |

A host reboot does drop it, but leaves the ASIC **re-bootstrappable, not
wedged**: run the bootstrap VM again and it goes straight through to
`uboot running`, both X710 links come up at 10 Gbps and the API answers. No AC
pull needed. Only a *wedge* - the loader run against an already-booted ASIC -
costs you a site visit.

It took a while to get there. The fault used to land *early*, before the upload
finished, and the switch then came up only when it happened to win the race -
once in three attempts. The fix was
[the PCI COMMAND register](#the-command-register-bug): with memory decode
enabled the upload completes deterministically, and every run since has ended
with a working switch.

### Where `0xe0041000` comes from — answered

The fault is a **write**, by the device, to an address that is not RAM:

```
WARNING: VTD: IOMMU Fault IOMMU Unit #0: R/W=W, Device 0000:0d:00.0
                                         Addr = 0xe0041000
WARNING: VTD: 307: Reason = 0x5 -> PTE not set to allow Write.
```

Three facts, all from one clean run on 2026-09-03 (AC pull, then bootstrap
with `pciPassthru.allowP2P = "TRUE"`), pin it down:

| Fact | Source |
|---|---|
| The guest sees the ASIC's BARs at `0xc4000000`, `0xc0000000`, `0xa0000000` | the loader's own line: `Reading CPI configuration space BARs: [0] 0xc400000c, [0] 0xc000000c, [0] 0xa000000c` |
| The **host** has BAR2 at `0x383fe0000000` — low 32 bits **`0xe0000000`** | `vmware.log`: `barIndex 2 ... realaddr 0x383fe0000000`; `vsish` agrees |
| The fault fires in the same second as `uboot running` | host `16:59:02.634` = guest `23:59:02 uboot running` |

So the write is not from the Linux driver — it lands the instant **u-boot on
the ASIC's service CPU** starts. And its target is not the guest's BAR, which
`0xe0000000` is nowhere near. It is the **host-physical** BAR2, truncated to
32 bits, plus `0x41000` — the base of the ASIC's own MSYS register block.

The mechanism: the service CPU reads its own BAR2 back from the device side of
the PCIe core. That value is the host-physical one — ESXi does not virtualise
what a device sees of itself — and something in the firmware keeps only 32
bits of it. On bare metal and under Proxmox the BAR sits below 4 GB, so
truncation is lossless and the write goes back to the chip's own window. Here
it does not matter whether it is lossless: with Above 4G Decoding on, host BAR2
is `0x383fe0000000`; with it off (tested), it is `0xe0000000`. **The low 32
bits are `0xe0000000` either way**, so the write always targets guest-physical
`0xe0041000` — and in every ESXi VM that is the ECAM range, which the IOMMU
domain will never map for writes.

**That is why no guest-side knob ever moved the address.** `pciHole`,
`pci=nommconf`, guest RAM size, `use64bitMMIO` — all shuffle the *guest's*
address space, and the value comes from the *host's*. ECAM being at
`0xe0000000` is coincidence; if it had been RAM the write would have landed
silently on some page of guest memory instead, and nobody would have noticed
until something corrupted.

### Why Proxmox never saw this

Same chassis, same BIOS, so the host BAR2 was `0x383fe0000000` there too and
the service CPU wrote to the same truncated `0xe0041000`. It landed because
**q35 happened to place the guest's BAR2 at `0xe0000000`** (the trace:
`41824 data:0xe0000000`) and VFIO maps guest BARs into the IOMMU domain. The
truncated address was, by accident, the guest's own BAR. Nobody chose that; it
fell out of q35's 64-bit BAR placement with 2–4 GB of RAM. ESXi puts ECAM at
`0xe0000000` instead, and that is the entire difference between the platforms.

So there are exactly two ways to satisfy the firmware, and only one is open on
ESXi:

| Route | Why it works | On ESXi |
|---|---|---|
| Guest BAR2 at exactly `0xe0000000`, plus P2P mapping | truncated address == guest BAR | **shut** — ECAM lives there, and every lever that moves it is closed |
| Host BAR2 below 4 GB, at an address the guest can also host | truncation lossless *and* `useActualBases` can mirror it | **tested, shut** — the BIOS puts it at `0xe0000000`, which is ECAM again; see below |

(A third, hacky option: `setpci` the guest BAR2 to `0xe0000000` after `FW
upload done` and before u-boot writes. A 1–6 s race, ESXi may refuse a BAR
over its ECAM, and it is unknown which BAR the DDR upload uses. Ranked below
the BIOS route, not above.)

### What was tried — all of it, with results

| Lever | Result |
|---|---|
| `pciHole.dynStart` | closed — ESXi rewrites it to `2560` at power-on |
| `pci=nommconf` in the guest | closed — range stays reserved as `pnp 00:05` |
| `pciPassthru.allowP2P = "TRUE"`, Above 4G on | **no effect** — accepted into the DICT, same fault at `0xe0041000` |
| **BIOS: Above 4G Decoding off** | BAR2 moved from `0x383fe0000000` to `0xe0000000`. Same low 32 bits, so the target address did not change. Harmless, and pointless for this. |
| `useActualBases` + `allowP2P`, Above 4G off | **crashes the VMX process.** `Failed to map MMIO: Failure` then `PANIC: VERIFY bora/devices/pcipassthru/pciPassthru.c:871`, core dumped. The "actual base" is `0xe0000000`, ESXi cannot place a passthrough BAR over its own ECAM, and it asserts rather than failing cleanly. The ASIC stayed in WFI through it. |
| `allowP2P` alone, Above 4G off | **same fault**, `0xe0041000`, VM killed at ~35 s, switch up at ~70 s as always |
| A VMX knob for MMCONFIG placement | none exists — `grep -a` over `/bin/vmx` finds no `mmconfig`/`mcfg` option at all; only `pciHole.*`, which governs the hole, not ECAM |

All tested on 2026-09-03 on the real 5412, each with an AC pull where the ASIC
needed to be in WFI.

### Where that leaves it

The write must land on guest-physical `0xe0000000 + 0x41000`. In an ESXi VM
that range is ECAM, ESXi provides no way to move ECAM, and it cannot map a
passthrough BAR there even when asked to (the VERIFY crash *is* that
attempt). Proxmox is not affected only because q35 happened to put the
guest's BAR2 at that exact address.

**One idea remains untested, and it is a long shot:** the value the firmware
writes comes from the *host* BAR2, so if the host BAR2 could be placed at a
below-4 GB address *outside* `0xe0000000–0xe7ffffff`, then
`useActualBases` (mirror it into the guest — it only crashed because the
mirror landed on ECAM) plus `allowP2P` (map it) should let the write land.
Nothing tried so far moves the host BAR anywhere but `0xe0000000`: the BIOS
allocator picks it deterministically and exposes no per-device control. The
ESXi kernel options `pciBarAllocPolicy` and `pciHonorAcpiRootBridgeRes`
*might* make the VMkernel reassign BARs itself rather than honour the BIOS —
that is not established, each attempt is a host reboot plus an AC pull, and
moving every BAR on the box is not free of risk to `vmk0`. It is recorded here
so the next person does not start from zero, not because it is expected to
work.

**Status: root cause established, no fix available on ESXi 8.0 U3.** It costs
the bootstrap VM and not the switch, so the
[two-VM model](#two-jobs-two-vms-how-this-actually-works-on-esxi) stands as
the way this platform works, not as a workaround for something pending.

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

All four are answered. What remains was not on the original list:

**Can the [IOMMU fault](#the-iommu-fault) be fixed after all?** No, not
on this ESXi. The cause is known — u-boot on the ASIC's service CPU writes to
the low 32 bits of the host BAR2 plus `0x41000`, which is `0xe0041000` with
Above 4G Decoding on *or* off, and that is ECAM in every ESXi VM. Every lever
was tested on 2026-09-03: `allowP2P` does nothing, `useActualBases` crashes
the VMX process, and there is no knob for MMCONFIG placement. It costs the
bootstrap VM, not the switch; the two-VM model is simply how this platform
works.