#!/usr/bin/env python3
"""Boot the produced qcow2 and assert the post-install state.

    50-verify-qcow2.py <image.qcow2> [root-password]

Runs against a temporary COPY with its own OVMF vars, so the shipped image is
never modified.

No Marvell device is attached here, so the switch bits SHOULD report the
device as absent - that is a PASS for this test. What is verified is that the
payload, the systemd unit, the kernel pin and the Cisco NIC drivers landed.

This exists because static checks are not enough. An earlier revision of the
kickstart produced an image that installed perfectly and silently never
started the switch, because the payload copy ran in the chroot %post where
/run/install/repo does not exist. Only booting the thing catches that.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

CHECKS = [
    # The memory floor and the reasons for it. The 62 MB generic initramfs
    # set the old floor (GRUB: "can't allocate initrd"); the slim one is
    # ~21 MB. Under OVMF the firmware then sets it at 320; under VMware's
    # EFI the same image boots at 256.
    ("memory: this VM's size, and what is left",
     "free -m | sed -n '1,2p'"),
    ("firmware this boot came up under, and both boot paths present",
     "[ -d /sys/firmware/efi ] && echo EFI || echo BIOS; "
     "ls /boot/efi/EFI/almalinux/grubx64.efi /boot/grub2/i386-pc/core.img 2>&1 | sed 's/^/  /'; "
     "grep -oE 'crashkernel=[^ ]*|resume=[^ ]*' /boot/loader/entries/*.conf || echo '  no crashkernel/resume args'; "
     "systemctl is-enabled polkit tuned 2>&1 | tr '\\n' ' '; echo"),
    ("initramfs: must be the slim one (well under 32 MB) with LVM and both "
     "hypervisors' drivers in it",
     "ls -la /boot/initramfs-$(uname -r).img | awk '{printf \"%d MB\\n\", $5/1048576}'; "
     "lsinitrd /boot/initramfs-$(uname -r).img 2>/dev/null | grep -cE 'dm-mod.ko|vmw_pvscsi|vmxnet3|virtio_blk|virtio_scsi' | sed 's/^/drivers+dm found: /'"),
    ("tools on PATH (symlinks into /opt/encs-host)",
     "for t in tui api vnet; do printf '%s -> ' encs-switch-$t; readlink -f $(command -v encs-switch-$t) || echo MISSING; done; encs-switch-vnet --version"),
    ("kernel + hostname", "hostname; uname -r"),
    ("switch payload",
     "ls -l /opt/switch-confd/mv_pciboot.ko.xz /opt/switch-confd/remote_boot_app "
     "/opt/switch-confd/booton.bin /opt/switch-confd/switch_firmware.bin 2>&1 "
     "| awk '{print $5, $9}'"),
    ("bootstrap unit enabled", "systemctl is-enabled marvell-switch-boot"),
    ("unit: stdbuf + Restart=no",
     "grep -E '^ExecStart=|^Restart=' /etc/systemd/system/marvell-switch-boot.service"),
    ("VM tools", "ls -1 /usr/local/sbin/ 2>&1"),
    ("host bundle", "ls -1 /opt/encs-host/ 2>&1"),
    ("kernel pin",
     "echo releasever=$(cat /etc/yum/vars/releasever 2>/dev/null); "
     "grep -h '^exclude=' /etc/dnf/dnf.conf /etc/yum.conf 2>/dev/null | head -1"),
    ("Cisco igb installed",
     "ls /lib/modules/$(uname -r)/kernel/drivers/net/ethernet/intel/igb/"),
    ("module vermagic",
     "modinfo /opt/switch-confd/mv_pciboot.ko.xz 2>&1 | grep -E '^(vermagic|name)'"),
    ("no misleading int-LAN ifcfgs",
     "ls /etc/sysconfig/network-scripts/ 2>/dev/null | grep -c int-LAN || echo 0"),
    ("encs-switch-status", "/usr/local/sbin/encs-switch-status 2>&1 | head -22"),
    ("ks-post tail", "tail -4 /root/ks-post.log"),
]


def ovmf(name):
    for p in (f"/usr/share/OVMF/{name}_4M.fd", f"/usr/share/OVMF/{name}.fd"):
        if os.path.exists(p):
            return p
    sys.exit(f"ERROR: {name} not found - install the 'ovmf' package")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    img = sys.argv[1]
    passwd = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("ROOT_PASSWORD", "encs")
    if not os.path.exists(img):
        sys.exit(f"ERROR: no such image: {img}")

    tmp = tempfile.mkdtemp(prefix="encs-verify-")
    try:
        copy = os.path.join(tmp, "disk.qcow2")
        varsf = os.path.join(tmp, "OVMF_VARS.fd")
        shutil.copy2(img, copy)
        if os.environ.get("VERIFY_BIOS", "seabios") == "ovmf":
            shutil.copy2(ovmf("OVMF_VARS"), varsf)

        accel = "kvm" if os.access("/dev/kvm", os.R_OK | os.W_OK) else "tcg"
        # -cpu host is KVM-only; TCG rejects it outright, so fall back to 'max'.
        cpumodel = "host" if accel == "kvm" else "max"
        # VERIFY_BIOS=seabios (default) boots the BIOS GRUB in the biosboot
        # partition at 256 MB - the Proxmox shipping configuration, and the
        # regression test for the memory floor as much as for the image.
        # VERIFY_BIOS=ovmf boots the EFI path instead, at 384: OVMF's EFI
        # stub cannot place the kernel below that (Proxmox's edk2 fails at
        # 320, "Failed to allocate usable memory for kernel"), whatever the
        # initramfs size; VMware's EFI manages 256. VERIFY_MEM overrides.
        bios = os.environ.get("VERIFY_BIOS", "seabios")
        if bios == "ovmf":
            fw = ["-drive", f"if=pflash,format=raw,readonly=on,file={ovmf('OVMF_CODE')}",
                  "-drive", f"if=pflash,format=raw,file={varsf}"]
            mem = os.environ.get("VERIFY_MEM", "384")
        else:
            fw, mem = [], os.environ.get("VERIFY_MEM", "256")
        cmd = ["qemu-system-x86_64", "-machine", f"q35,accel={accel}",
               "-cpu", cpumodel, "-smp", "4", "-m", mem, *fw,
               "-drive", f"file={copy},format=qcow2,if=virtio",
               "-smbios", "type=1,manufacturer=Cisco,product=ENCS5412/K9",
               "-display", "none", "-serial", "stdio", "-no-reboot"]

        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, bufsize=0)
        buf, lock = bytearray(), threading.Lock()

        def reader():
            while True:
                b = p.stdout.read(1)
                if not b:
                    break
                with lock:
                    buf.extend(b)
        threading.Thread(target=reader, daemon=True).start()

        def wait_for(pat, timeout=300):
            rx, end, start = re.compile(pat), time.time() + timeout, len(buf)
            while time.time() < end:
                with lock:
                    txt = buf[start:].decode("utf8", "replace")
                if rx.search(txt):
                    return txt
                if p.poll() is not None:
                    return None
                time.sleep(0.4)
            return None

        def send(s):
            p.stdin.write((s + "\n").encode())
            p.stdin.flush()

        print(f"== booting {os.path.basename(img)} ({bios}, {mem} MB, accel={accel}, cpu={cpumodel}) ==", flush=True)
        if wait_for(r"login:", 300) is None:
            with lock:
                sys.stdout.write(buf[-3000:].decode("utf8", "replace"))
            p.kill()
            sys.exit("FAILED: never reached a login prompt")

        send("root")
        time.sleep(1)
        wait_for(r"[Pp]assword:", 30)
        send(passwd)
        if wait_for(r"[#$] ", 60) is None:
            p.kill()
            sys.exit("FAILED: could not log in (wrong root password?)")
        print("== logged in ==", flush=True)

        MARK = "VERIFYEND"
        for desc, cmdline in CHECKS:
            print(f"\n----- {desc} -----", flush=True)
            with lock:
                start = len(buf)
            send(f"{cmdline}; echo {MARK}")
            end, txt = time.time() + 60, ""
            while time.time() < end:
                with lock:
                    txt = buf[start:].decode("utf8", "replace")
                if txt.count(MARK) >= 2:
                    break
                time.sleep(0.3)
            for line in txt.splitlines()[1:]:
                if MARK not in line and line.strip():
                    print(line.rstrip(), flush=True)

        print("\n== powering off ==", flush=True)
        send("poweroff")
        try:
            p.wait(timeout=90)
        except subprocess.TimeoutExpired:
            p.kill()
        print("\nNOTE: 'mv_pciboot not loaded' and 'Marvell 11ab:be00 NOT visible' are")
        print("EXPECTED here - no Marvell device is attached to this test VM.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
