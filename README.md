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

---

## The problem this solves

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

**Confirmed working on real hardware:** ASIC bootstrap, L2 forwarding, VLANs,
MAC learning, 10 G backplane, and **802.3bt PoE** (a class-5 AP powered up and
linked over a port enabled purely through the API).

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

## Usage

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
9216, persists it to `/etc/network/interfaces`, installs `encs-switch-tui` and
`encs-switch-api` into `/usr/local/sbin`, enables the config-replay unit, and
pings the switch to confirm.

It is idempotent — safe to re-run, and it skips anything already in place.

**5. Manage the switch**

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

| Implemented | Where |
|---|---|
| Port enable/disable | Ports view, `space` |
| VLAN create/delete | VLANs view, `n` / `d` |
| VLAN port membership | saved and replayed — no UI editor yet |
| Link aggregation | Ports view, `g` |
| PoE on/off | PoE view, `space` |
| MAC table, counters | read-only views |

**Not implemented.** Every one of these has a working `wcd` table and a Cisco
XML template sitting in `switch-confd`, so they are unwritten rather than
blocked — the same pattern used for LAG and VLAN membership applies:

| Area | wcd tables that exist |
|---|---|
| Spanning tree (STP/RSTP/MSTP) | `STP`, `RSTP`, `MSTP*` |
| QoS — class/policy maps, policers, queueing | `ClassMapList`, `PolicyMapList`, `AggregatePolicerList`, `CoS*`, `DSCP*` |
| ACLs | `ACLList`, `ACEList`, `ACLBindingList` |
| 802.1X + RADIUS | `Standard_802_1x*`, `RadiusServerList` |
| IGMP/MLD snooping, multicast filtering | `IGMPMLD*`, `MulticastGlobalSetting` |
| Storm control | `StormControlTable` |
| LLDP / CDP | `LLDP*`, `CDPInterfaceList` |
| L3 — ARP, static routes, default gateway | `ARPList`, `IPv4RouteList`, `IPv4GatewayList` |
| Port mirroring (SPAN) | `SpanDestinationTable` |
| Private VLANs | `PrivateVLAN*` |
| Static MAC entries, aging | `ForwardingStaticTable` |
| LACP tuning — system/port priority, timeout | `LACPGlobalSetting`, `LACPPortList` |

> **No spanning tree is running.** Fine for a flat L2 deployment — until
> someone cables two front ports to the same upstream switch and there is
> nothing to break the loop.

`encs-switch-api` can drive any of these by hand today. Anything you configure
that way is as volatile as the rest, so it needs its own file in
`/etc/encs-switch/` to survive a power cycle — the replay service applies
every `*.xml` there, so `40-acl.xml` works fine. Full detail with the ConfD
paths is in [docs/CONFIG.md](docs/CONFIG.md#what-nfvis-could-do-that-this-cannot).

**Reading the front panel.** The API calls the switch ports `gi0`–`gi7`; the
chassis silkscreen calls them `GE1/0`–`GE1/7`, and the TUI shows both. They are
four vertically stacked pairs, **numbered downwards, not across**:

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

## How it works

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
  50-verify-qcow2.py          boot the result and assert its state
  release.sh                  package + publish the host tools to GitHub
kickstart/ks-encs.cfg         the kickstart, heavily commented
payload/                      our code, installed into the image
  usr/local/sbin/encs-switch-status        (VM) bootstrap health check
  etc/systemd/system/marvell-switch-boot.service
  opt/encs-host/                           (host bundle, copied to Proxmox)
    install.sh                             host installer
    encs-switch-tui                        curses UI, built-in manual
    encs-switch-api                        low-level XML API client
    encs-switch-replay.service             cold-boot config replay
docs/
  FINDINGS.md                 the full reverse-engineering writeup
  CONFIG.md                   the /etc/encs-switch/*.xml files, field by field
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
