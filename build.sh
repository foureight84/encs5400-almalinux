#!/bin/bash
# Build an AlmaLinux 8.9 switch-bootstrap image for the Cisco ENCS 5412
# from a Cisco NFVIS ISO that you supply.
#
#   ./build.sh /path/to/Cisco_NFVIS-4.15.5-FC4.iso
#
# No Cisco software is redistributed by this repository. Everything
# proprietary is extracted from the ISO you provide, on your machine.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/scripts/lib.sh"

usage() {
    cat <<EOF
usage: $0 [options] <nfvis.iso>

  --iso-only        build the installer ISO, skip the qcow2
  --qcow2-only      build the qcow2 from an already-built ISO
  --full            keep every package (2.7GB ISO instead of ~1.6GB)
  --no-verify       skip the post-build boot verification
  --out DIR         output directory (default: ./out)
  --work DIR        scratch directory (default: ./work)

environment:
  ROOT_PASSWORD     root password for the built image (default: encs)
  QCOW_SIZE         virtual disk size (default: 16G)

The ISO must be NFVIS 4.15.x or older. Cisco removed the Marvell switch
firmware in 4.16+, which makes newer media unusable here - build.sh checks
for this and refuses early.
EOF
    exit 1
}

OUT_DIR="$HERE/out"; WORK_DIR="$HERE/work"
DO_ISO=1; DO_QCOW=1; DO_VERIFY=1; MODE=""
ISO=""
while [ $# -gt 0 ]; do
    case "$1" in
        --iso-only)   DO_QCOW=0 ;;
        --qcow2-only) DO_ISO=0 ;;
        --full)       MODE="--full" ;;
        --no-verify)  DO_VERIFY=0 ;;
        --out)        OUT_DIR="$2"; shift ;;
        --work)       WORK_DIR="$2"; shift ;;
        -h|--help)    usage ;;
        -*)           die "unknown option: $1" ;;
        *)            ISO="$1" ;;
    esac
    shift
done
[ -n "$ISO" ] || usage
[ -f "$ISO" ] || die "no such file: $ISO"

BUILT_ISO="$OUT_DIR/AlmaLinux-8.9-ENCS5412-switch.iso"
BUILT_QCOW="$OUT_DIR/AlmaLinux-8.9-ENCS5412-switch.qcow2"

say "Checking build dependencies"
check_deps || die "install the missing packages listed above, then re-run"
info "all present"

"$HERE/scripts/10-inspect-iso.sh" "$ISO"

mkdir -p "$OUT_DIR" "$WORK_DIR"

if [ "$DO_ISO" -eq 1 ]; then
    "$HERE/scripts/30-build-iso.sh" "$ISO" "$WORK_DIR" "$BUILT_ISO" "$MODE"
fi

if [ "$DO_QCOW" -eq 1 ]; then
    [ -f "$BUILT_ISO" ] || die "installer ISO not found: $BUILT_ISO (build it first)"
    "$HERE/scripts/40-build-qcow2.sh" "$BUILT_ISO" "$WORK_DIR" "$BUILT_QCOW"

    if [ "$DO_VERIFY" -eq 1 ]; then
        say "Verifying the built image by booting it"
        python3 "$HERE/scripts/50-verify-qcow2.py" "$BUILT_QCOW" \
            || warn "verification reported problems - review the output above"
    fi
fi

say "Artifacts in $OUT_DIR"
ls -la "$OUT_DIR"

cat <<EOF

Next steps
----------
1. Copy the qcow2 to your Proxmox host and import it:

     qm create 900 --name encs-switch --machine q35 --bios ovmf \\
         --memory 2048 --cores 2 --net0 virtio,bridge=vmbr0 \\
         --serial0 socket --vga serial0
     qm importdisk 900 $(basename "$BUILT_QCOW") local-lvm
     qm set 900 --scsihw virtio-scsi-pci --virtio0 local-lvm:vm-900-disk-0
     qm set 900 --efidisk0 local-lvm:0,efitype=4m,pre-enrolled-keys=0
     qm set 900 --boot order=virtio0
     qm set 900 --smbios1 product=\$(echo -n 'ENCS5412/K9' | base64),base64=1
     qm set 900 --hostpci0 \$(lspci -d 11ab:be00 | cut -d' ' -f1)
     qm set 900 --onboot 1 --startup order=1
     qm start 900

2. Install the host-side tools (from the Proxmox shell):

     scp -r root@<vm-ip>:/opt/encs-host /root/
     bash /root/encs-host/install.sh
     encs-switch-tui            # press ? for the built-in manual

See README.md for the full walkthrough and the operational warnings.
EOF
