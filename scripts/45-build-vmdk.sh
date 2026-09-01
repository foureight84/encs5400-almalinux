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
MEM="${VM_MEM:-2048}"
CPUS="${VM_CPUS:-2}"

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
virtualHW.version = "19"
displayName = "$NAME"
guestOS = "centos8-64"
numvcpus = "$CPUS"
memSize = "$MEM"

# EFI, Secure Boot off - the image expects it, same as the Proxmox
# efidisk0 pre-enrolled-keys=0.
firmware = "efi"
uefi.secureBoot.enabled = "FALSE"

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

# pciPassthru0.* is NOT here on purpose - add the Marvell device through the
# Host Client (Add other device -> PCI device) so its id and the host-specific
# systemId are filled in for you.
EOF

cat > "$OUT/README-esxi.txt" <<EOF
Deploying $NAME.vmdk on ESXi          (experimental - see docs/ESXI.md)

1. Prepare the host (once):
     scp -r payload/opt/encs-esxi root@<esxi>:/vmfs/volumes/datastore1/
     ssh root@<esxi> 'sh /vmfs/volumes/datastore1/encs-esxi/install.sh'
     # prints the plan and stops; re-run with --yes to apply

2. Upload and import the disk:
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

4. Power on and watch it:
     vim-cmd vmsvc/power.on <vmid>
     # in the VM console: journalctl -u marvell-switch-boot -f
     # ~60s from a cold ASIC to "ROS ready!"

Revert everything the host side did:
     sh /vmfs/volumes/datastore1/encs-esxi/uninstall.sh          # plan
     sh /vmfs/volumes/datastore1/encs-esxi/uninstall.sh --yes    # do it
EOF

say "Artifacts"
ls -la "$OUT"
