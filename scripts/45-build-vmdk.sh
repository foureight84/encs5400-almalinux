#!/bin/bash
# Convert the built qcow2 into something ESXi can import, plus a .vmx that
# already has the awkward settings in it.  EXPERIMENTAL - see docs/ESXI.md.
#
#   45-build-vmdk.sh <input.qcow2> <outdir>
#
# Produces:
#   <outdir>/encs-switch.vmdk    streamOptimized - upload, then vmkfstools -i
#   <outdir>/encs-switch.vmx     reference config, minus the host-specific bits
#   <outdir>/README-esxi.txt     the four commands that follow
#
# streamOptimized rather than monolithicSparse: it is compressed (a ~940 MB
# qcow2 lands in the same ballpark instead of expanding to the full 16 G),
# vmkfstools imports it directly, and it is the subformat an OVA would carry
# if we built one.  ESXi cannot run a VM from it as-is - the import step is
# not optional.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/lib.sh"

QCOW="${1:?usage: 45-build-vmdk.sh <input.qcow2> <outdir>}"
OUT="${2:?}"
NAME="${VM_NAME:-encs-switch}"
# 256 MB, not 2048. Measured on the 5412 under ESXi 8.0 U3 (2026-09-03): the
# image idles at 150 MB and bootstraps the ASIC at 256 - with the 21 MB
# initramfs this tree builds (payload/etc/dracut.conf.d/90-encs-slim.conf).
# An image built before that has the 62 MB generic initramfs, which GRUB
# cannot place below ~300 MB ("can't allocate initrd"): give those 384. With
# passthrough the whole allocation is pinned, so this is memory the host
# gets back.
MEM="${VM_MEM:-256}"
CPUS="${VM_CPUS:-2}"
# 19 is ESXi 7.0 U2+. An older host refuses to register a VMX whose hardware
# version it does not know, so 7.0 GA/U1 needs 17.
HW="${VM_HWVERSION:-19}"

[ -f "$QCOW" ] || die "no such file: $QCOW"
command -v qemu-img >/dev/null 2>&1 || die "qemu-img not found (qemu-utils)"
mkdir -p "$OUT"

say "Converting to streamOptimized VMDK"
rm -f "$OUT/$NAME.vmdk"
qemu-img convert -p -f qcow2 -O vmdk -o subformat=streamOptimized \
    "$QCOW" "$OUT/$NAME.vmdk"
qemu-img info "$OUT/$NAME.vmdk" | head -4
sha256sum "$OUT/$NAME.vmdk"

# The VMX deliberately omits pciPassthru*.  Its id and systemId are
# host-specific and easy to get subtly wrong by hand; the Host Client fills
# them in correctly from "Add other device -> PCI device".  Everything else
# here is a setting that is either non-default or easy to forget.
say "Writing $NAME.vmx"
cat > "$OUT/$NAME.vmx" <<EOF
.encoding = "UTF-8"
config.version = "8"
virtualHW.version = "$HW"
displayName = "$NAME"
guestOS = "centos8-64"
numvcpus = "$CPUS"
memSize = "$MEM"

# EFI, Secure Boot off - the image expects it, same as the Proxmox
# efidisk0 pre-enrolled-keys=0.
firmware = "efi"
uefi.secureBoot.enabled = "FALSE"

# Without these, adding ANY pciPassthru device fails power-on with
# "No PCIe slot available for SCSI0 ... Too many PCI devices are already
# configured". The Host Client writes them when it creates a VM; a
# hand-written VMX has to say them itself. Verified on ESXi 8.0 U3.
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

scsi0.present = "TRUE"
scsi0.virtualDev = "pvscsi"
scsi0:0.present = "TRUE"
scsi0:0.deviceType = "scsi-hardDisk"
scsi0:0.fileName = "$NAME.vmdk"

# vNIC 0: normal management network, on the MGMT CPU jack. Do NOT move this
# behind the switch - it is how you reach the VM when the ASIC is wedged.
ethernet0.present = "TRUE"
ethernet0.virtualDev = "vmxnet3"
ethernet0.networkName = "VM Network"
ethernet0.addressType = "generated"

# vNIC 1: the switch management VLAN. This is the ESXi-specific half - ESXi
# cannot run the switch tools, so they run in this VM and reach 169.254.1.0
# through here. Link-local only; give it no default route.
ethernet1.present = "TRUE"
ethernet1.virtualDev = "vmxnet3"
ethernet1.networkName = "encs-mgmt-2363"
ethernet1.addressType = "generated"

# Reflect the chassis SMBIOS into the guest, so system-product-name reads
# ENCS5412/K9 and switch-confd's platform gate passes the way it does on bare
# metal. The Proxmox equivalent is --smbios1 product=RU5DUzU0MTIvSzk=,base64=1.
SMBIOS.reflectHost = "TRUE"

# A VM with a passthrough device will not power on without its full memory
# reserved. This is the single most common "it just refuses to start".
sched.mem.min = "$MEM"
sched.mem.minSize = "$MEM"
sched.mem.pin = "TRUE"

# The image boots with console=tty0 console=ttyS0,9600n8, so the ordinary VGA
# console gives a login. Uncomment for a network serial console as well, and
# open the firewall: esxcli network firewall ruleset set -e true -r remoteSerialPort
#serial0.present = "TRUE"
#serial0.fileType = "network"
#serial0.fileName = "telnet://:1234"
#serial0.yieldOnMsrRead = "TRUE"

# pciPassthru0.* is NOT here on purpose, and the reason is sharper than
# "host-specific": pciPassthru0.id is NOT the hex BDF. ESXi formats it as
# %05d:%03d:%02d.%d - every field DECIMAL - the format string that lives in
# /usr/lib/vmware/drivers/lib/libpci_bus.so. So 0000:0d:00.0 becomes
# "00000:013:00.0", and a device at slot 0x1d becomes ":29.0", not ":1d.0".
# A hand-written hex BDF is rejected with "AH No device hints found" and
# "Failed to generate predicates for pciPassthru0---invalid VM configuration",
# which names neither the key nor the format.
#
# systemId is `esxcli system uuid get` on THIS host, and the BDF moves between
# reinstalls (see opt/encs-esxi/install.sh), so neither value is portable.
# Let the Host Client fill both in (Add other device -> PCI device), or use
# govc, which drives the same API:
#     govc device.pci.add -vm $NAME 0000:0d:00.0
# install.sh prints the correct line for the device it found, if you would
# rather paste it in.
EOF

cat > "$OUT/README-esxi.txt" <<EOF
Deploying $NAME.vmdk on ESXi          (experimental - see docs/ESXI.md)

1. Prepare the host (once):
     scp -r payload/opt/encs-esxi root@<esxi>:/vmfs/volumes/datastore1/
     ssh root@<esxi> 'sh /vmfs/volumes/datastore1/encs-esxi/install.sh'
     # prints the plan and stops; re-run with --yes to apply

2. Upload and import the disk:
     ssh root@<esxi> 'mkdir -p /vmfs/volumes/datastore1/$NAME'
     scp $NAME.vmdk $NAME.vmx root@<esxi>:/vmfs/volumes/datastore1/$NAME/
     ssh root@<esxi>
     cd /vmfs/volumes/datastore1/$NAME
     vmkfstools -i $NAME.vmdk -d thin disk.vmdk && rm -f $NAME.vmdk
     sed -i 's/^scsi0:0.fileName.*/scsi0:0.fileName = "disk.vmdk"/' $NAME.vmx

3. Register and add the PCI device:
     vim-cmd solo/registervm /vmfs/volumes/datastore1/$NAME/$NAME.vmx
     # then, in the Host Client: Edit settings -> Add other device ->
     # PCI device -> the Marvell 11ab:be00, and confirm "Reserve all guest
     # memory" is ticked.
     #
     # Adding it by hand instead? pciPassthru0.id is NOT the hex BDF - ESXi
     # writes it as %05d:%03d:%02d.%d, all decimal, so 0000:0d:00.0 is
     # "00000:013:00.0". install.sh prints the exact block for your host.
     # "govc device.pci.add -vm $NAME 0000:0d:00.0" also gets it right.

4. Power on. EXPECT THE VM TO DIE - that is normal here:
     vim-cmd vmsvc/power.on <vmid>
     # ~60s in, ESXi kills it:
     #   "PCI passthru device caused an IOMMU fault at address 0xe0041000"
     # By then the firmware is already in the ASIC and u-boot finishes on its
     # own, so the SWITCH comes up regardless and stays up - through the VM's
     # death and through host reboots. Confirm from any VM on encs-mgmt-2363:
     #   ping 169.254.1.0
     # Why it happens, and why it cannot be fixed from here: docs/ESXI.md.

5. Switch to the MANAGEMENT role - this is not optional:
     # Remove the PCI device from the VM (Host Client -> Edit settings), then
     # power it back on. Now it can run the tools. Address the mgmt vNIC
     # persistently - NetworkManager drops a bare 'ip addr add':
     #   nmcli con add type ethernet ifname <encs-mgmt-2363 vNIC> \
     #       con-name encs-mgmt ipv4.method manual \
     #       ipv4.addresses 169.254.1.1/16 ipv4.never-default yes ipv6.method ignore
     #   encs-switch-tui
     #
     # NEVER boot a VM that still has the Marvell attached while the switch is
     # up. marvell-switch-boot runs at every boot, and a loader run against an
     # already-booted ASIC wedges it until AC is physically pulled. Bootstrap
     # once per AC power cycle; manage from a VM with no PCI device.

Revert everything the host side did:
     sh /vmfs/volumes/datastore1/encs-esxi/uninstall.sh          # plan
     sh /vmfs/volumes/datastore1/encs-esxi/uninstall.sh --yes    # do it
EOF

say "Artifacts"
ls -la "$OUT"
