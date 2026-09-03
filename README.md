# encs5400-almalinux

Run **Proxmox** (or any hypervisor, or bare-metal Linux) on a **Cisco ENCS 5400
series** appliance with the built-in **Marvell PoE switch fully working** — no
Cisco NFVIS, no licensing.

Cisco's own `switch-confd` accepts `ENCS5406/K9`, `ENCS5408/K9`, `ENCS5412/K9`
and the `CSX-1006/1008` variants, so all of those should work. **Tested on an
ENCS 5412/K9** — reports from the other models are welcome.

This repository contains **build tooling only**. It ships no Cisco software.
You point it at an NFVIS ISO you already have, and it extracts the drivers and
firmware on your machine to produce a bootable image.

```
./build.sh /path/to/Cisco_NFVIS-4.15.5-FC4.iso
```

**Confirmed working on real hardware:** ASIC bootstrap, L2 forwarding, VLANs,
MAC learning, 10 G backplane, and **802.3bt PoE** (a class-5 AP powered up and
linked over a port enabled purely through the API).

**New here:** [Requirements](#requirements) then
[Build the image](#build-the-image). If you want to know why the LAN ports need
any of this in the first place, that is [How it works](#how-it-works).

---

## Contents

**Getting it running**
[Requirements](#requirements) ·
[Build the image](#build-the-image) ·
[Deploy on Proxmox](#deploying-on-proxmox) ·
[Deploy on ESXi](#deploying-on-esxi-experimental)

**Using it**
[Managing the switch](#managing-the-switch) ·
[VMs on the front ports](#putting-vms-on-the-front-lan-ports-the-nfvis-lan-net-model) ·
[Link aggregation](#link-aggregation-lag--port-channel) ·
[Updating the tools](#updating-the-host-tools) ·
[The front panel](#the-front-panel) ·
[Credentials](#credentials)

**Before you trust it**
[Operational warnings](#operational-warnings) ·
[What has not been tested](#what-has-not-been-tested)

**Why any of this is necessary**
[How it works](#how-it-works) ·
[Repository layout](#repository-layout) ·
[Legal](#legal) ·
[Credits](#credits)

---

## Requirements

**An NFVIS ISO, version 4.15.x or older.**

This is not negotiable and the build refuses to proceed otherwise. Cisco
**removed `switch_firmware.bin` in NFVIS 4.16+**. Everything else — kernel
module, loader, platform gate — is byte-identical, so a 4.18 build installs
and looks completely normal, but the ASIC never boots and the front ports
never appear. ENCS 5400 support ended at 4.15.x.

Known-good: `Cisco_NFVIS-4.15.5-FC4.iso` (sha256 `36c7d642…`).

**A Linux build host** with ~15 GB free and KVM (a VM install runs during the
build). Dependencies:

```sh
# Debian / Ubuntu
sudo apt-get install -y libarchive-tools xorriso createrepo-c \
                        qemu-system-x86 qemu-utils ovmf
sudo usermod -aG kvm $USER      # then log out and back in

# Fedora / RHEL
sudo dnf install -y libarchive xorriso createrepo_c qemu-kvm edk2-ovmf
```

`build.sh` checks all of these up front and tells you exactly what is missing.

---

## Build the image

```sh
git clone https://github.com/foureight84/encs5400-almalinux
cd encs5400-almalinux
ROOT_PASSWORD='pick-something' ./build.sh ~/Cisco_NFVIS-4.15.5-FC4.iso
```

Roughly 15 minutes. You get:

| Artifact | Size | Use |
|---|---|---|
| `out/AlmaLinux-8.9-ENCS5400-switch.qcow2` | ~940 MB | **import into Proxmox — recommended**, no install step |
| `out/AlmaLinux-8.9-ENCS5400-switch.iso` | ~1.6 GB | install into a VM yourself, or bare metal on the ENCS |

Both are covered below: [importing the qcow2](#deploying-on-proxmox) and
[installing from the ISO](#alternative-install-from-the-iso-instead).

Options: `--iso-only`, `--qcow2-only`, `--full` (skip package trimming),
`--no-verify`, `--out DIR`, `--work DIR`.

The build ends by **booting the image it just made** and asserting the
post-install state. That is deliberate — earlier revisions produced images
that installed perfectly and silently never started the switch.

---

## Deploying on Proxmox

Install Proxmox on the ENCS as normal, then:

**1. Confirm IOMMU and find the switch**

```sh
dmesg | grep -i "Directed I/O"          # expect: DMAR: Intel(R) Virtualization Technology
ls /sys/kernel/iommu_groups | wc -l     # expect: > 0
lspci -nn | grep 11ab                   # the Marvell switch

# it must be ALONE in its IOMMU group - expect exactly one line:
for d in /sys/kernel/iommu_groups/*/devices/*; do
  g=$(basename $(dirname $(dirname $d))); b=$(basename $d)
  echo "group $g  $b  $(lspci -nns $b | cut -d' ' -f2-)"
done | grep -i 11ab
```

**Do not hardcode the BDF.** It moves between firmware and OS installs — it has
been observed at both `0d:00.0` and `0e:00.0` on the same machine, and on one
install `0e:00.0` was the I210 management NIC instead. Always derive it:
`lspci -d 11ab:be00 | cut -d' ' -f1`. The IOMMU group number renumbers with it.

If more than one device shares the group, they must all be passed through
together — on a 5412 the Marvell is normally isolated, so a single line is the
expected result.

If IOMMU is off: enable VT-d in the ENCS BIOS (F2), then add
`intel_iommu=on iommu=pt` to `GRUB_CMDLINE_LINUX_DEFAULT`, `update-grub`, reboot.
On Proxmox 8 (kernel 6.8) it is usually on already.

**2. Import the VM**

```sh
qm create 900 --name encs-switch --machine q35 --bios ovmf \
    --memory 2048 --cores 2 --net0 virtio,bridge=vmbr0 \
    --serial0 socket --vga serial0
qm importdisk 900 AlmaLinux-8.9-ENCS5400-switch.qcow2 local-lvm
qm set 900 --scsihw virtio-scsi-pci --virtio0 local-lvm:vm-900-disk-0
qm set 900 --efidisk0 local-lvm:0,efitype=4m,pre-enrolled-keys=0
qm set 900 --boot order=virtio0
qm set 900 --smbios1 product=RU5DUzU0MTIvSzk=,base64=1   # 'ENCS5412/K9'
qm set 900 --hostpci0 0000:$(lspci -d 11ab:be00 | cut -d' ' -f1)
qm set 900 --onboot 1 --startup order=1
qm start 900
```

Two things that will otherwise waste your afternoon:

- **SMBIOS must be base64.** `switch-confd` gates on
  `dmidecode -s system-product-name` matching `ENCS5412/K9`, and the `/` breaks
  Proxmox's SMBIOS parser. `RU5DUzU0MTIvSzk=` is that string base64-encoded.
- **`pre-enrolled-keys=0`.** The image expects Secure Boot off.

**3. Watch it boot** (`qm terminal 900`, login `root`)

```sh
journalctl -u marvell-switch-boot -f
```

Cold ASIC to operational takes ~60 s:

```
Loading bootstrap to service CPU SRAM... done.
Loading firmware to service CPU DDR... done.
uboot started ... Starting kernel ... Initializing ROS
ROS ready!
```

**4. Install the host tools** (on Proxmox)

The management tools ship *inside* the VM image at `/opt/encs-host/`, but they
must run on the **hypervisor** — the VM cannot reach the switch, because the
management VLAN lives on the host's X710 backplane NIC. So copy the bundle
across once.

*Find the VM's address* — it takes DHCP on `vmbr0`. From the VM console
(`qm terminal 900`, login `root`):

```sh
ip -4 -br addr show scope global      # e.g. enp6s18  UP  <dhcp-address>/24
```

*Then on the Proxmox host:*

```sh
scp -r root@<vm-ip>:/opt/encs-host /root/
bash /root/encs-host/install.sh
```

**If the VM has no usable network** (no DHCP lease, different subnet, whatever),
pull the identical bundle straight off the ISO instead — no VM involvement:

```sh
mount -o loop,ro AlmaLinux-8.9-ENCS5400-switch.iso /mnt
cp -r /mnt/encs/opt/encs-host /root/
umount /mnt
bash /root/encs-host/install.sh
```

Either way, `install.sh` does the rest: finds the X710 backplane port **by
driver rather than by name** (it is `enp8s0f1np1` on one box and something else
on the next), creates the `sw2363` VLAN interface at `169.254.1.1/16` with MTU
9216, persists it to `/etc/network/interfaces`, installs `encs-switch-tui`,
`encs-switch-api` and `encs-switch-vnet` into `/usr/local/sbin`, enables the
config-replay unit, and pings the switch to confirm.

Then it **offers** to create `swbr0`, the bridge that puts VM traffic on the 8
front LAN ports instead of on your management NIC — explaining what changes
before you answer. Saying no costs nothing: `encs-switch-vnet init` does the
same thing whenever you want it, and you can equally build the bridge by hand.
The prompt is skipped entirely when stdin is not a terminal, or with
`ENCS_NO_VNET=1`, so an unattended install never rearranges the network on its
own. See [below](#putting-vms-on-the-front-lan-ports-the-nfvis-lan-net-model).

It is idempotent — safe to re-run, and it skips anything already in place. Once
`swbr0` exists, re-running it leaves the network config alone entirely.

### Alternative: install from the ISO instead

If you would rather install into a VM yourself, or you are going bare metal,
use `AlmaLinux-8.9-ENCS5400-switch.iso`.

**As a Proxmox VM** — upload the ISO to a storage that holds ISO images
(`local` by default: *Datacenter → local → ISO Images → Upload*, or just
`scp` it into `/var/lib/vz/template/iso/`), then:

```sh
qm create 900 --name encs-switch --machine q35 --bios ovmf \
    --memory 2048 --cores 2 --net0 virtio,bridge=vmbr0 \
    --serial0 socket --vga serial0
qm set 900 --scsihw virtio-scsi-pci --virtio0 local-lvm:16
qm set 900 --efidisk0 local-lvm:0,efitype=4m,pre-enrolled-keys=0
qm set 900 --ide2 local:iso/AlmaLinux-8.9-ENCS5400-switch.iso,media=cdrom
qm set 900 --boot 'order=ide2;virtio0'
qm set 900 --smbios1 product=RU5DUzU0MTIvSzk=,base64=1
qm start 900 && qm terminal 900
```

The default boot entry installs unattended and **erases the disk it picks** —
which is why the disk is created empty above. When it reboots, drop the CD and
fix the boot order:

```sh
qm set 900 --ide2 none --boot order=virtio0
qm set 900 --hostpci0 0000:$(lspci -d 11ab:be00 | cut -d' ' -f1)   # the Marvell switch
qm set 900 --onboot 1 --startup order=1
qm start 900
```

Passthrough is added *after* installation deliberately — the installer has no
use for the switch, and leaving it out keeps the install from touching it.

**On bare metal** (AlmaLinux directly on the ENCS, no hypervisor): write the
ISO to a USB stick and boot it.

```sh
sudo dd if=AlmaLinux-8.9-ENCS5400-switch.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

No SMBIOS override is needed — a real ENCS already reports
`ENCS5412/K9`, so the platform gate passes untouched. Run
`bash /opt/encs-host/install.sh` locally afterwards; it works the same whether
"the host" is Proxmox or the ENCS itself.

The boot menu offers three entries: automated install (marked **ERASES DISK**),
an interactive install if you want to choose the disk yourself, and rescue.

---

## Deploying on ESXi (experimental)

**[docs/ESXI.md](docs/ESXI.md) is the walkthrough.** It is marked experimental
because none of it has been run on hardware — the Proxmox path above is
verified on a real 5412, that one is reasoned from it. It is written down
anyway, because the mechanism is hypervisor-agnostic: a passthrough VM pushing
firmware into the ASIC over PCIe. Nothing in that is Proxmox-specific.

The short version of what changes:

| | Proxmox | ESXi |
|---|---|---|
| Passthrough | `hostpci0` | DirectPath I/O, possibly a `/etc/vmware/passthru.map` reset override |
| Platform gate | `--smbios1 product=...` | `SMBIOS.reflectHost = "TRUE"` |
| **The switch tools** | on the hypervisor | **inside the bootstrap VM** — ESXi has no bash, systemd or curses to run them |
| `swbr0` (= NFVIS `lan-br`) | VLAN-aware bridge on the te2 NIC | a standard vSwitch whose only uplink is that vmnic |
| `bridge=swbr0,tag=100` | | a portgroup with VLAN ID 100 |
| Guest boot ordering | `--startup order=1,up=90` | autostart entry with a 90 s delay |

Everything switch-side — VLANs, PoE, LAG, mirroring, cold-boot replay — is
unchanged, because it is the same client speaking the same XML API over the
same VLAN. Only the machine it runs on moves.

The guide grades every step by how much it can be trusted, and ends with a
[revert procedure](docs/ESXI.md#reverting-everything) that puts the host back
exactly as it was — it never touches `vSwitch0` or `vmk0`. Read that before
starting rather than after.

**This is the `experimental/esxi` branch**, which carries the automation as
well as the guide:

```sh
./build.sh --esxi <nfvis.iso>      # also writes out/esxi/{vmdk,vmx,README}
scp -r payload/opt/encs-esxi root@<esxi>:/vmfs/volumes/datastore1/
ssh root@<esxi> 'sh /vmfs/volumes/datastore1/encs-esxi/install.sh'        # plan
ssh root@<esxi> 'sh /vmfs/volumes/datastore1/encs-esxi/install.sh --yes'  # apply
```

`install.sh` does passthrough and networking and records everything it creates;
`uninstall.sh` takes exactly that back out. `--esxi` is a last, separate build
step — if the conversion fails you still have a working qcow2 and an untouched
Proxmox path.

`python3 scripts/66-test-esxi.py` runs both against a **fake `esxcli`** — the
same trick `60-test-tui.py` uses on the TUI. It asserts that the dry run writes
nothing, that install → uninstall is a bit-identical round trip, and that no
write command ever names `vSwitch0`. The fake's output shapes and option names
were checked against ESXi 8.0 U3 on a real 5412, and it runs with `PATH` cut
down to what ESXi's `/bin` actually holds — the first real run found four bugs
the earlier, more forgiving fake could not, and those are what that hardening
is for.

**Verified on hardware, and the verdict is mixed.** The host side works:
passthrough, the vSwitch, the portgroups and `encs-esxi-vnet` all run on a real
ENCS 5412 under ESXi 8.0 U3, DirectPath I/O takes the Marvell with no
`passthru.map` entry, and `SMBIOS.reflectHost` gives the guest `ENCS5412/K9`.
The **bootstrap is intermittent**: an IOMMU fault kills the VM on every run,
and only sometimes does the firmware upload finish first — one success in
three. The switch does come up and stay up when it wins that race, and
managing it afterwards needs no passthrough at all. This stays off `main`
until the fault is understood. [docs/ESXI.md](docs/ESXI.md) grades every step
and documents the fault; reports from other chassis are welcome.

Note the build host must be **x86_64 Linux with KVM**: the qcow2 step installs
AlmaLinux under QEMU, and `createrepo_c` has no Homebrew formula, so an arm64
Mac cannot build this at all. On AlmaLinux 9, `check_deps` passes but two
symlinks are still needed — `qemu-system-x86_64` → `/usr/libexec/qemu-kvm`,
and `/usr/share/OVMF/OVMF_CODE.fd` → `../edk2/ovmf/OVMF_CODE.fd`.

---

## Managing the switch

Everything below runs on the **hypervisor**, not in the bootstrap VM — the
switch answers on `169.254.1.0` over VLAN 2363 on the X710 backplane, and that
NIC belongs to the host. (ESXi is the exception, and
[docs/ESXI.md](docs/ESXI.md) says so.)

```sh
encs-switch-tui        # press ? for the built-in manual
```

Ports, VLANs, PoE, link aggregation, MAC table, statistics, and config
save/replay. The active view is highlighted in the tab bar, the running
version sits at the right-hand end of the title bar, and if a newer release
exists the title bar says so.

![Ports view](https://raw.githubusercontent.com/foureight84/encs5400-almalinux/main/docs/img/tui-ports.png)

*Ports view. `panel` gives the label silkscreened on the chassis, `attached to`
is worked out live from the MAC table, and `UP idle` marks a backplane link
that is trained but carrying nothing. MAC addresses are partially redacted in
these screenshots.*

![PoE view](https://raw.githubusercontent.com/foureight84/encs5400-almalinux/main/docs/img/tui-poe.png)

*PoE view — per-port state, class, draw and limit. 802.3bt has been confirmed
working on real hardware.*

Two columns that are easy to confuse: **`link`** is whether the PHY has a
signal; **`admin`** is whether *you* have enabled the port (`UP` / `DOWN`,
`space` toggles it). A port can be `admin UP` with no link, or — on the
internal backplane ports — show `link UP` while disabled.

### What this does and does not do

NFVIS's `switch-confd` drove 23 top-level ConfD paths and 91 `wcd` tables.
This implements the subset needed to get a switch forwarding and keep it that
way across a power cycle.

Seven views have a tab and a hotkey; the rest are behind `TAB`, which opens a
grouped menu. Inside any menu view the grammar is the same: `ENTER` edits the
selected row, `SPACE` toggles its main setting, `g` reaches that view's global
settings, `n`/`d` create and delete. `ESC` cancels, and nothing is written
until the last prompt is answered.

The **Tested** column is deliberate. Everything below is implemented and every
write has been checked against a real 5412 — but "the switch accepted the
write and read it back" is a weaker claim than "traffic behaved differently",
and the table says which one applies.

- **data plane** — observed changing what the switch does with frames
- **on hardware** — written to a real switch and read back
- **writes only** — accepted by the switch, effect never measured

| Implemented | Where | Tested |
|---|---|---|
| Port enable/disable | Ports view, `space` | **data plane** |
| Port description, speed, duplex, flow control | Ports view, `ENTER` | **data plane** (speed + duplex forced on a live link) |
| VLAN create/delete/rename | VLANs view, `n` / `d` / `N` | **on hardware** |
| VLAN port membership | VLANs view, `ENTER` — access, trunk or general | **data plane** (isolation measured) |
| VM networks — bridge on the backplane, jack per VLAN | `TAB` → vnet, or `encs-switch-vnet` | **on hardware** (bridge built and `GE1/0` moved to a tagged VLAN on a live 5412; no VM traffic pushed through it yet) |
| PoE on/off | PoE view, `space` | **data plane** (two PDs, 7.5 W) |
| PoE per-port power limit | PoE view, `ENTER` | **data plane** (enforced at classification) |
| Port mirroring (local SPAN) | `TAB` → mirror | **data plane** (copies counted) |
| Static MAC entries, aging | `TAB` → staticmac | **on hardware** |
| Storm control | `TAB` → storm | **on hardware** — rate limiting not measured |
| LLDP / CDP advertisement | `TAB` → lldp | **on hardware** — no neighbour table exists |
| LACP tuning — system/port priority, timeout | `TAB` → lacp | **on hardware** |
| MAC ACLs — rules and port bindings | `TAB` → acl | **on hardware** — filtering not measured |
| Spanning tree (STP/RSTP) | `TAB` → stp | **on hardware** per port — see the warning below |
| Link aggregation | Ports view, `g` | **on hardware** (0.0.4) — negotiation untested |
| MAC table, counters | read-only views, `f` flush, `z` zero | **on hardware** reads; flush/zero writes only |
| QoS — mode, trust, port CoS, CoS→queue, policers | `TAB` → qos | writes only |
| 802.1X + RADIUS | `TAB` → dot1x, radius | writes only |
| IGMP/MLD snooping | `TAB` → igmp | writes only |
| Private VLANs | `TAB` → pvlan | writes only |
| L3 — static routes, gateway, static ARP | `TAB` → l3 | writes only |

Every one of these is saved and replayed. The config directory only grows
with what you actually configure. See [docs/CONFIG.md](docs/CONFIG.md).

### Link aggregation (LAG / port-channel)

The ASIC supports **four groups, `LAG1`–`LAG4`**. They show up in the Ports
view from the start but stay empty — `link n/p`, no media — until you put
ports in them.

Select a `gi` port in the Ports view and press **`g`**:

```
add gi3 to which LAG? 1-4 (0 = none):  1
mode: (a)uto = LACP, (o)n = static:    a
```

Press `g` on a member and answer `0` to take it back out. The `lag` column
shows each port's group, and the group rows show a member count; select a
`LAG` row to see its members and their state in the detail panel.

| Mode | Behaviour |
|---|---|
| `auto` | LACP — negotiates with the far end. **Use this unless you know otherwise.** |
| `on` | Static bundle. Forwards immediately, negotiates nothing. Only correct if the far end is also static. |

> **A static `on` bundle facing an LACP peer silently black-holes traffic.**
> Neither side logs an error; frames just disappear. When in doubt, `auto`.

Member state, shown as `LAG1` or `LAG1(i)` in the `lag` column:

| State | Meaning |
|---|---|
| active | in the group and forwarding |
| inactive `(i)` | in the group, not forwarding — usually the far end is not bundling this port |
| not candidate | listed against the group but not a member; not shown as membership |

Two things the TUI enforces for you:

- **Only the eight `gi` front ports can be bundled.** `te1`/`te2` are the
  internal 10G backplane links that carry both your data path and the
  management session on VLAN 2363 — bundling one cuts the wire you are
  managing over. The TUI refuses them outright.
- **Membership is volatile like everything else.** Save it (`c`, then `w`)
  or a cold power cycle takes it with the rest of the config. It lands in
  `/etc/encs-switch/15-lag.xml` and is replayed before VLAN membership.

The write goes to the *member port*, not to the group — it sets `LACPEnabled`
and `LAGID` on the port's own `Standard802_3List` entry, which is exactly what
Cisco's `switch-confd` sent for `channel-group`. See
[docs/CONFIG.md](docs/CONFIG.md) for the file format and
[docs/FINDINGS.md](docs/FINDINGS.md) for how it was derived.

### Putting VMs on the front LAN ports (the NFVIS `lan-net` model)

Out of the box a Proxmox VM lands on `vmbr0`, which is the **MGMT CPU** jack —
so guest traffic shares the one interface you manage the hypervisor over. That
is not what NFVIS does, and it is not necessary here either.

**How NFVIS actually does it.** Its `lan-br` — the bridge behind the `lan-net`
network you pick when creating a VM — has exactly one physical port, `int-LAN`,
which is the X710 backplane function that lands on `te2`. The `GE1/x` jacks are
*not* bridge members and do not appear in NFVIS's network model at all; they are
switch ports sitting in access VLAN 1. A VM reaches `GE1/0` purely because its
frames leave `te2` in a VLAN that `gi0` is also in. Two independent halves, both
of which this project can already drive. See
[docs/FINDINGS.md §8n](docs/FINDINGS.md) for the extracted configs that prove it.

So the equivalent on Proxmox is one VLAN-aware bridge on the backplane NIC.
There are three ways in, all doing the same thing: `install.sh` offers it at
the end, the TUI has it under `TAB` → **vnet** (`n` creates it, `D` removes it,
`ENTER` assigns a jack), or from a shell:

```sh
encs-switch-vnet init          # shows the plan, writes nothing
encs-switch-vnet init --yes    # write it, then: ifreload -a
```

Nothing about this is privileged — it is an ordinary `bridge-vlan-aware yes`
stanza plus a VLAN on the ASIC. If you would rather hand-roll your own bridge
over the backplane NIC and set the front-port VLANs in the TUI, that works
exactly as well; the tool exists to keep the two halves consistent and to save
the switch side for replay.

That creates `swbr0` with the backplane NIC as its only port — Proxmox's
`lan-br` — and **moves `sw2363` onto the bridge**, which is required: once the
NIC is a bridge port, a VLAN subinterface of the NIC receives nothing.

The stanzas go in `/etc/network/interfaces`, between markers, because **Proxmox
reads nothing else** — its own generated header says it "will NOT read its
network configuration from sourced files", so a bridge in `interfaces.d` would
never appear in the GUI or in the VM Bridge dropdown ([docs/FINDINGS.md
§8n](docs/FINDINGS.md)). `teardown` removes the block again, falling back to
matching interfaces by name if a GUI network change has since rewritten the
file and dropped the markers.

Your Proxmox management interface keeps every setting it has. The only mark
`init` leaves there is a **comment line** on the bridge carrying the default
route — found by looking, not by assuming it is called `vmbr0` — which Proxmox
shows as that interface's comment: *"Proxmox management and the bootstrap VM.
Attach guests to `swbr0` instead."* `teardown` removes it again. That is the
cheap version of renaming `vmbr0`, without rewriting every guest config or
risking the one interface you manage the host over.

Then give a jack to a VM:

```sh
encs-switch-vnet add 100 --ports gi0 --name dmz --fix-backplane
qm set 901 --net1 virtio,bridge=swbr0,tag=100
```

`--fix-backplane` is not optional on this platform for a *tagged* VLAN — see
the note below. Leave it off and `add` tells you what it would have done.

or, in the GUI's **Create VM → Network** tab, the same two fields it already
has: **Bridge** = `swbr0`, **VLAN Tag** = `100`. That dropdown shows each
bridge's comment, which is where the label on `vmbr0` earns its keep — the
distinction is in front of you at the moment you pick.

`add` creates VLAN 100 on the ASIC, makes `GE1/0` an access port in it, enables
the port if a cold boot left it shut, and saves `/etc/encs-switch/*.xml` so the
replay service restores all of it after a power cut. `--ports gi0,gi3` puts
several jacks in the same VLAN; `--named-bridge dmz` also creates a per-VLAN
bridge so the network shows up by name in the Proxmox GUI instead of needing a
tag typed in.

Untagged is VLAN 1, which every front port is in by default — the direct
equivalent of NFVIS's stock `lan-net`:

```sh
qm set 901 --net1 virtio,bridge=swbr0        # all 8 LAN ports, flat
```

`encs-switch-vnet status` — and the TUI's **vnet** view — shows both halves at
once, including which VM is on which VLAN and which front ports that VLAN
reaches, and flags a VM tagged into a VLAN the switch does not have, which
otherwise looks like a broken NIC.

**Changed your mind?** `encs-switch-vnet teardown` (or `D` in the vnet view)
puts `sw2363` back on the bare NIC and deletes the bridge, restoring the exact
stanzas `install.sh` wrote — it refuses while a VM is still attached, and
leaves the *switch* untouched, so front-port VLANs and their replay files
survive. VMs go back to `vmbr0`.

One caveat on isolation, since Proxmox differs from NFVIS here: a plain Linux
bridge is visible to anyone who can edit a VM's hardware, so nothing stops a VM
being put on `vmbr0` alongside the hypervisor and the bootstrap VM. NFVIS could
hide its internal networks; Proxmox has no per-bridge ACL for ordinary bridges
(SDN VNets do sit under the permission system, but that is a heavier setup). On
a single-admin box the practical answer is that `swbr0` is obviously named and
a VM on the wrong bridge simply gets the wrong network.

> **Tagged VLANs need one extra step, measured on hardware.** `te2` has to
> carry the VLAN for any of this to work, and its firmware default is exactly
> `trunk, members 1,2363` — VLAN 1 and the management VLAN, nothing else.
> (Under NFVIS `switch-confd` widens that at every startup; we do not run it.)
> So **untagged works out of the box** — that is switch VLAN 1, every front
> jack, stock `lan-net`. Any *tagged* VLAN needs `--fix-backplane`, which merges
> the new id into `te2`'s list rather than replacing it. That write lands on
> the port your management session rides on, so read the warning it prints.

The bootstrap VM stays on `vmbr0`. It carries no traffic, and putting the
machine that boots the switch behind the switch is exactly the dependency loop
to avoid.

### Boot ordering

A guest on `swbr0` that autostarts comes up with a working vNIC and a bridge
that forwards nothing, for the ~60–90 s the ASIC takes to boot. DHCP fails, the
guest shrugs, and the cause is two layers from where it shows. `swbr0` itself
needs no help — it is an ordinary bridge over a host NIC, built by ifupdown2 in
the first seconds of boot whether the switch exists or not — but the *guests*
on it do:

**This is automatic.** `encs-switch-startup.service` runs `--fix` on every
boot, ordered `After=pve-cluster` and `Before=pve-guests`, so attaching a guest
to `swbr0` in the GUI is all anyone has to do — the ordering is right on the
next boot without being asked for. It says nothing in the journal on the boots
where nothing changed. To inspect or drive it by hand:

```sh
encs-switch-vnet startup         # what needs changing, and why
encs-switch-vnet startup --fix   # apply it now
systemctl disable encs-switch-startup   # opt out entirely
```

It finds the bootstrap VM by which guest has the Marvell device passed through,
never by a hardcoded VMID, and covers every bridge the tool owns — `swbr0` plus
any `--named-bridge`. `status` and the TUI's vnet view warn when it becomes
relevant.

**The delay goes on the bootstrap VM, not on the guest waiting.** `qm`'s `up=`
delays *the next* guest in the sequence, so `--startup order=2,up=90` on the
dependent VM does nothing for that VM at all — the intuitive placement is the
wrong one. The right pair is `order=1,up=90` on the bootstrap VM and `order=2`
on everything that needs the switch. And the delay is only proposed once
something actually depends on it: with no guests on these bridges it would just
postpone every other VM at boot. That is symmetric — when the last guest leaves
these bridges, the delay is taken back off, so an unused bridge stops costing
every other guest 90 s. A delay you set yourself, to a different value, is
never touched.

### Updating the host tools

The switch tools on the hypervisor are **completely independent of the disk
image**. The ISO and qcow2 only carry the bootstrap VM — kernel, Marvell
module, firmware loader. `encs-switch-tui` and `encs-switch-api` run on
Proxmox. **Updating them never requires rebuilding anything.**

On startup the TUI asks GitHub once, in the background, whether a newer
release exists; if so the title bar says
`v0.0.1 -> v0.1.0 available`. A host with no route to github.com simply sees
nothing — no error, no delay. Set `ENCS_NO_UPDATE_CHECK=1` to skip the request
entirely.

```sh
encs-switch-tui --version         # what is installed
encs-switch-tui --check-update    # is there anything newer?
sudo encs-switch-tui --update     # fetch and install it
```

`--update` fetches the release tarball over verified HTTPS, checks it against
the published `SHA256SUMS`, refuses to install anything that does not compile,
backs up what it replaces into `/var/backups/encs-switch/<timestamp>/`, and
swaps each file in atomically. It only writes to `/usr/local/sbin`,
`/etc/systemd/system` and `/opt/encs-host` — it never touches your switch
config, the VLAN interface, or `/etc/encs-switch`. Roll back by copying the
files back out of the backup directory.

#### Updating by hand

If the host has no internet access, or you would rather see exactly what lands
where, do it manually. **You do not need to rebuild the ISO or the qcow2 for
any of this.**

*From a release tarball:*

```sh
V=0.1.0
curl -fsSLO https://github.com/foureight84/encs5400-almalinux/releases/download/v$V/encs-host-$V.tar.gz
curl -fsSLO https://github.com/foureight84/encs5400-almalinux/releases/download/v$V/SHA256SUMS
sha256sum -c SHA256SUMS --ignore-missing     # must print OK
tar xzf encs-host-$V.tar.gz && cd encs-host-$V

install -m 0755 encs-switch-tui encs-switch-api /usr/local/sbin/
install -m 0644 encs-switch-replay.service /etc/systemd/system/
systemctl daemon-reload
encs-switch-tui --version
```

The tarball's `MANIFEST` lists every file and its destination, so that install
loop is just those five lines spelled out.

*From a git checkout* — no release needed, and this is the right path if you
are running your own modifications:

```sh
git clone https://github.com/foureight84/encs5400-almalinux
scp -r encs5400-almalinux/payload/opt/encs-host root@<proxmox>:/root/
ssh root@<proxmox> 'bash /root/encs-host/install.sh'
```

`install.sh` is idempotent — it reinstalls the tools and skips the VLAN and
network config that is already in place.

*Copying just the one file* is also perfectly fine — the TUI is a single
self-contained Python script with no dependencies beyond the standard library:

```sh
scp payload/opt/encs-host/encs-switch-tui root@<proxmox>:/usr/local/sbin/
```

Nothing here restarts the bootstrap VM or touches the ASIC, so updating the
tools is safe on a live switch. A running TUI keeps its old code until you
quit and relaunch it.

### What has not been tested

Nothing here is known broken — it is unmeasured, and the missing piece is
hardware rather than code. Grouped by what you would need:

| Needs | Untested |
|---|---|
| **A second switch** | LAG/LACP negotiation (including the `auto` vs `on` black-hole trap), STP loop breaking, root guard, BPDU guard |
| **A traffic generator** | Storm-control rate limiting, QoS marking and queueing, policers |
| **Endpoints with IP addresses** | ACL filtering, IGMP snooping, private VLAN isolation, L3 routing and static ARP |
| **Nothing — just never run** | MAC flush, counter clear, MSTP, DSCP maps, per-port shaping |

> ⚠ **Spanning tree has never been enabled globally on real hardware.** `te1`
> and `te2` are both X710 ports to the same host, so the switch may see a loop
> and block one of them — and if it blocks `te2` the management VLAN goes with
> it, recoverable only by physical AC removal. Set `STPEnabled=2` on `te1`–`te4`
> first, and do it with hands on the chassis.

**Still not implemented.** Three of these are blocked on missing information
rather than effort:

| Area | Why |
|---|---|
| IPv4 ACL rules | `switch-confd` only ever built **MAC** ACLs. The element names inside `<IPv4Parameters>` appear nowhere in the extracted source, and a guessed rule is one the switch accepts and never matches. MAC ACLs are complete. |
| LLDP neighbour table | Does not exist. confd touches only `LLDPGlobalSetting` and `LLDPInterfaceList`, so there is nothing that answers "what is plugged into GE1/3". LLDP can be enabled and timed; it cannot be read back. |
| Remote SPAN | Needs a reflector port and a remote VLAN. Local SPAN works. |
| MSTP instances | The client can write region, revision, instance priorities and instance→VLAN maps; there is no view. MSTP on an 8-port edge switch with one region is not worth the screen — use `encs-switch-api`. |
| DSCP mutation/remark, per-port shaping, class/policy maps | The client can write all of them; there is no view. Reach them with `encs-switch-api`, or see [docs/CONFIG.md](docs/CONFIG.md#what-nfvis-could-do-that-this-cannot). |
| Port security | `InterfaceSecurityTable` templates exist, but the mode and violation enums were never pinned down. |

**The NIM slot cannot be driven from the host — by any OS.** Extracting the
CIMC firmware settles why: the FPGA that powers the slot (`dash_fpga`) is
**BMC-owned**, not a host peripheral. The BMC runs the NIM insertion and
status threads, reads the module's IDPROM over I²C, **authenticates it**, and
controls power enable. NFVIS contains no ENCS NIM code at all, which fits.

So a `NIM-SSD` from an ISR 4000 gets identified in the CIMC inventory and goes
no further: measured across a full cold start with a disk fitted, no block
device, no PCI change, no SMBIOS change, no kernel event. Not a driver
problem, and not fixable from Proxmox or NFVIS. See
[docs/FINDINGS.md §8m](docs/FINDINGS.md).

`encs-switch-api` can drive anything the TUI does not. Whatever you configure
that way is as volatile as the rest, so it needs its own file in
`/etc/encs-switch/` to survive a power cycle — the replay service applies
every `*.xml` there in filename order, and one file must hold exactly one
table. Full detail with the ConfD paths is in
[docs/CONFIG.md](docs/CONFIG.md#what-nfvis-could-do-that-this-cannot).

**Verified on hardware (2026-08-12).** All 49 wcd tables read back as expected; VLAN isolation,
port mirroring, PoE on two ports, speed forcing and live VLAN changes all confirmed on a real
5412 with two PoE devices attached. The duplex enums were resolved (`duplexAdminMode` and
`duplexOperMode` are inverted relative to each other), and two ways to hang the ASIC were found
and blocked. Full detail in [docs/FINDINGS.md §8n](docs/FINDINGS.md).

**Testing without the hardware.** `scripts/60-test-tui.py` runs the TUI
against a fake switch — no network, no chassis. It checks that every view
fetches and renders (including at 40 columns and with tables missing), that
every write produces the element names `switch-confd` used, that config
save/replay round-trips in dependency order, and that cancelling a prompt
writes nothing. What it *cannot* check is whether the firmware accepts those
writes: the fixtures come from Cisco's templates, not from a capture. Run it
before a release; verify on the box before believing a new view works.

```sh
python3 scripts/60-test-tui.py        # -v to list every check
```

---

## The front panel

The API calls the switch ports `gi0`–`gi7`; the chassis silkscreen calls them
`GE1/0`–`GE1/7`, and the TUI shows both. They are four vertically stacked
pairs, **numbered downwards, not across**:

```
   GE1/0   GE1/2   GE1/4   GE1/6      <- gi0  gi2  gi4  gi6
   GE1/1   GE1/3   GE1/5   GE1/7      <- gi1  gi3  gi5  gi7
```

`GE1/1` is directly below `GE1/0`. Count along the top row and you will
unplug the wrong thing. Verified on a 5412 by cabling `GE1/0`, `GE1/4` and
`GE1/7` individually; 5406/5408 are unconfirmed.

**Everything else on the panel bypasses the switch** — those jacks go to the
host CPU or the BMC, and none of their MACs ever appear in the switch's MAC
table:

| Panel label | What it really is |
|---|---|
| `CONSOLE` (top) | serial console to the host CPU — `ttyS0` at **115200 8N1** |
| `CIMC` (bottom, serial) | out-of-band serial CLI to the BMC |
| `MGMT CPU` (top) | I210 → the host. This is the NFVIS management port; under Proxmox it is your normal management NIC. |
| `MGMT CIMC` (bottom) | a **separate** physical jack straight to the BMC. Invisible to the host — the host reaches the CIMC out `MGMT CPU` and back in via your LAN. |
| `GE0/0` (top row) | I350 `02:00.0` = `enp2s0f0`, fronted by **both** an RJ45 and an SFP jack |
| `GE0/1` (bottom row) | I350 `02:00.1` = `enp2s0f1`, likewise RJ45 + SFP |

`GE0/0` and `GE0/1` are dual-media: two jacks, one logical port. Under a stock
in-tree `igb` both report `Port: FIBRE` and **the RJ45 side will not link at
all** — and pulling the SFP does not wake it up. The NVM presents these as
fiber-variant devices (`8086:1522`, `Supported link modes: 1000baseKX/Full`)
and in-tree `igb` has no media-select parameter, so the copper connector is
never exposed. Cisco's `igb` fork and its `def_media` knob are what select it.

**So on stock Proxmox your only working uplinks are `MGMT CPU`, or a `GE0/x`
SFP.** Both cage-to-function mappings above were confirmed by moving one
transceiver between them and watching which port could read its EEPROM.

**Keep a serial cable for this box.** The `CONSOLE` RJ45 at **115200 8N1**
gives you the GRUB menu *and* a Proxmox login, courtesy of the settings the
Proxmox installer drops in `/etc/default/grub.d/installer.cfg`. It depends on
no network, no VLAN and no switch state — which matters here, because the
switch management VLAN is link-local, its config is volatile, and shutting
`te2` or mis-detecting the backplane NIC will cut you off from the ASIC
entirely. That port is the way back in.

```sh
screen /dev/ttyUSB0 115200        # Linux
screen /dev/cu.usbserial-XXXX 115200   # macOS
```

---

## Credentials

There are **two separate logins** here, and they are unrelated.

| What | User | Password | Where it comes from |
|---|---|---|---|
| **Bootstrap VM** (AlmaLinux) | `root` | `encs` | set by the kickstart — override with `ROOT_PASSWORD` at build time |
| **Marvell switch** (XML API / `encs-switch-tui`) | `cisco` | `cisco` | **the switch firmware's own default** |

**Set your own VM password at build time:**

```sh
ROOT_PASSWORD='something-better' ./build.sh /path/to/nfvis.iso
```

If you don't, it is `encs` — change it on first boot with `passwd`.

**The switch credentials are `cisco`/`cisco` because that is the ROS firmware
default.** They are not something this project chose. Cisco's own
`switch_settings.py` hardcodes exactly this for the un-provisioned case:

```python
if default_password:
    username = "cisco"
    password = "cisco"
```

NFVIS would normally replace that and store the new one encrypted in its ConfD
database — which we do not run, so the default stands. Since **the ASIC has no
flash**, a changed switch password is also lost on every cold boot, back to
`cisco`/`cisco`.

Override for a session with environment variables:

```sh
SW_USER=admin SW_PASS=secret encs-switch-tui
```

**Security note:** the switch management VLAN (2363, `169.254.1.0/16`) is
link-local on the internal backplane and is not routable from your LAN unless
you deliberately bridge or NAT it. Given the credentials are fixed defaults
that reset on power loss, keep it that way — don't expose `169.254.1.0` to a
wider network.

---

## Operational warnings

**Never restart the bootstrap service, and avoid stopping the VM.**

The loader can only bootstrap an ASIC in WFI (freshly reset). Cisco's state
machine has no reset path — it waits for "an external task" that only exists on
real NFVIS (the BMC). Re-running it against a live switch wedges it in
`Service CPU not ready (requires reset?)`, and recovery requires **physical AC
removal for ~30 s**. A CIMC power-off is *not* enough — the ASIC sits on
standby power. The shipped unit therefore has `Restart=no`.

**A wedged loader does not mean a dead switch.** The ASIC keeps forwarding.
Judge switch health with `ping 169.254.1.0` or `encs-switch-tui --status`,
never from that log line.

**Config is volatile.** The ASIC has no flash.

| Event | Config |
|---|---|
| VM restart / service restart | survives (no power cycle of the ASIC) |
| cold power cycle / AC loss | **lost** — back to VLAN 1 + 2363, all ports shut, PoE off, LAGs empty |

Save it (`encs-switch-tui`, `c`, `w`) into `/etc/encs-switch/*.xml` — those
files are the switch's real source of truth, and `encs-switch-replay.service`
reapplies them after a power loss. **[docs/CONFIG.md](docs/CONFIG.md)** covers
the file format, the apply ordering, and every enum value, for when you want to
hand-write one or keep them in version control.

**Never update the kernel.** The Marvell module is built for exactly
`4.18.0-513.18.2.el8_9` with `modversions` CRCs. The image pins `releasever`
and excludes `kernel*`; if you defeat that, the switch dies silently.

**`nfvis-fwupdate` is deliberately excluded.** It flashes BIOS/CIMC firmware,
and newer NFVIS images reportedly lock the BIOS so F2 setup becomes
unreachable — which would make PCI passthrough impossible to configure.

**Front ports come up disabled (`admin DOWN`) with PoE off** after a bootstrap —
that is the firmware default, and NFVIS's own init is what used to enable them.
Open the Ports view and press `space`, or apply a saved config.

**The TUI refuses to disable `te1`–`te4`.** Those are internal backplane ports
with no front-panel jack, and the switch owns its end of each link — it will
accept the shut. `te2` carries the management VLAN, so shutting it ends your
session, and with no flash on the ASIC only a cold power cycle undoes it.
Worse, the serdes stays trained regardless, so a shut backplane port still
reports `link UP` and nothing on screen looks wrong. Enabling is always
allowed; only shutting is blocked. Override with `ENCS_ALLOW_TE_SHUT=1`.

---

## How it works

### The problem this solves

The ENCS 5400's 8 front-panel ports are not NICs. They belong to a **Marvell
BobCat2 switch ASIC that has no flash** — no firmware of its own at all. On
every power-on, the host CPU must push a bootstrap into its SRAM and a 26 MB
firmware image into its DDR over PCIe.

Only NFVIS does that. Install anything else and the 8 LAN ports simply do not
exist. That is why every "I put Proxmox/ESXi on my ENCS" thread ends with the
switch unused.

This tooling extracts that bootstrap path and packages it as a tiny AlmaLinux
VM whose only job is to boot the ASIC — leaving the rest of the machine free
for whatever hypervisor you actually want.

That the switch *can* be booted this way was discovered by
**[yeyus](https://forums.servethehome.com/index.php?members/yeyus.35233/)** in
[Switch bootstrap VM](https://yeyus.notion.site/Switch-bootstrap-VM-04cac1d64bde48b684c51e8dac524245).
This repository automates it and adds switch management — see [Credits](#credits).

### The three paths

```
 front gi0-gi7 ──┐
 (8x RJ45, PoE)  │
                 ▼
          Marvell BobCat2 ASIC ──── te1/te2 (2x 10G) ──── host X710 ──── Proxmox
                 ▲                                                        │
                 │ PCIe 11ab:be00 (passed through)                        │
                 │                                                        │
          bootstrap VM ◄────────────────────────────────────────────────┘
          (boots the ASIC; carries no traffic)
```

Three separate paths, which is the key to understanding everything else:

| Path | Who owns it | What it does |
|---|---|---|
| **Data** | Marvell ASIC | port-to-port switching at line rate; never touches the host CPU |
| **Boot** | bootstrap VM | pushes firmware over PCIe; a permanent watchdog daemon |
| **Management** | Proxmox host | HTTPS XML API on `169.254.1.0` over VLAN 2363 |

The VM is *infrastructure*, not a data-plane element — 2 vCPU / 2 GB is enough
regardless of switch throughput. Management deliberately lives on the host,
because the backplane NIC does.

---

## Repository layout

```
build.sh                      orchestrator
scripts/
  lib.sh                      shared helpers, dependency checks
  10-inspect-iso.sh           validates the NFVIS ISO (incl. the 4.16+ trap)
  20-resolve-packages.py      minimal package closure (~330 pkgs, not @core)
  30-build-iso.sh             trim, regenerate repodata, remaster
  40-build-qcow2.sh           run the install under QEMU/KVM
  45-build-vmdk.sh            qcow2 -> ESXi VMDK + .vmx (experimental)
  50-verify-qcow2.py          boot the result and assert its state
  60-test-tui.py              offline tests for the TUI and the config files
  64-test-vnet.py             offline tests for encs-switch-vnet
  66-test-esxi.py             offline tests for the ESXi bundle (fake esxcli)
  release.sh                  package + publish the host tools to GitHub
kickstart/ks-encs.cfg         the kickstart, heavily commented
payload/                      our code, installed into the image
  usr/local/sbin/encs-switch-status        (VM) bootstrap health check
  etc/systemd/system/marvell-switch-boot.service
  opt/encs-esxi/                           (ESXi host bundle - experimental)
    install.sh / uninstall.sh              passthrough + vSwitch, and its undo
    encs-esxi-vnet                         portgroup per VLAN (the host half)
  opt/encs-host/                           (host bundle, copied to Proxmox)
    install.sh                             host installer
    encs-switch-tui                        curses UI, built-in manual
    encs-switch-api                        low-level XML API client
    encs-switch-vnet                       VM <-> front-port networks
    encs-switch-replay.service             cold-boot config replay
docs/
  FINDINGS.md                 the full reverse-engineering writeup
  CONFIG.md                   the /etc/encs-switch/*.xml files, field by field
  ESXI.md                     running it on ESXi instead (experimental)
```

`docs/FINDINGS.md` is worth reading if you want the why: how the bootstrap was
found, why `@core` is a trap on this media, the `curl` globbing bug that made
the API look broken, and every dead end (with evidence) so nobody retreads them.

`docs/CONFIG.md` is the practical companion: what each config file holds, the
enum values, and how to hand-write one the replay service will accept.

### Cutting a release

The host tools version independently of the image. Bump `VERSION` in
`payload/opt/encs-host/encs-switch-tui`, then:

```sh
./scripts/release.sh              # build + self-verify into out/, publish nothing
./scripts/release.sh --publish    # tag and create the GitHub release
```

The build step compiles every script, packages the bundle with a `MANIFEST`,
generates `SHA256SUMS`, then unpacks its own tarball and checks that the
manifest paths and the reported version are right — all before anything is
published. Existing hosts then pick it up with `encs-switch-tui --update`.

---

## Legal

This repository contains **no Cisco software** — only build scripts,
a kickstart, and management tools written for this project.

The images it produces contain Cisco proprietary firmware, kernel modules and
RPMs extracted from the NFVIS ISO **you** supply. Those images are almost
certainly **not redistributable**. Build them yourself, for hardware you own,
and do not publish the output.

`mv_pciboot.ko` declares `license=GPL` (author: Marvell Semi.), and the
`remote_boot.h` shipped in Cisco's own debugsource package is tri-licensed
Commercial / GPLv2 / BSD — so requesting the corresponding source from Cisco's
open-source portal is a legitimate avenue if you want to port the module to a
modern kernel.

Project code is **Apache-2.0** (see `LICENSE` and `NOTICE`). Use at your own
risk on end-of-life hardware.

---

## Credits

This project stands on **[yeyus](https://forums.servethehome.com/index.php?members/yeyus.35233/)**'s
work. Their write-up —
**[Switch bootstrap VM](https://yeyus.notion.site/Switch-bootstrap-VM-04cac1d64bde48b684c51e8dac524245)** —
is the original discovery that the Marvell switch in an ENCS 5400 can be booted
from a passthrough VM at all, and it is the reason this repository exists.

Everything here follows from that insight. yeyus identified the pieces that
matter and that nothing else documents:

- the Marvell PCI device `11ab:be00` and that it must be passed through
- the out-of-tree `mv_pciboot` kernel module and the `remote-bootd` loader
- that `switch-confd` is where those live, and that it must come from NFVIS
- that `switch-confd`'s scriptlet gates on SMBIOS, so a VM has to present
  `system-product-name = ENCS5412/K9`
- that the kernel must be pinned, because the module is built against one exact
  version

Without that groundwork, none of this would have had a starting point. What
this repository adds is automation of the extraction and build, a management
layer for the switch once it is running, and documentation of the failure modes
found along the way.

Thanks also to the **[ServeTheHome ENCS 5412/K9 thread](https://forums.servethehome.com/index.php?threads/127-cisco-encs5412-k9-xeon-d-1557-12-core-32g-ram.42638/)**
community, whose hardware archaeology mapped the NIC layout and the internal
switch topology — and whose report that NFVIS 4.16+ "disables the Marvell switch
completely" led directly to finding the removed `switch_firmware.bin` that
`10-inspect-iso.sh` now checks for.

(That thread also held that PoE needed Cisco's proprietary software on
non-NFVIS installs. It turns out to need Cisco's *API*, which is usable without
any Cisco stack — see §8k of `docs/FINDINGS.md`.)
