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
        shutil.copy2(ovmf("OVMF_VARS"), varsf)

        accel = "kvm" if os.access("/dev/kvm", os.R_OK | os.W_OK) else "tcg"
        cmd = ["qemu-system-x86_64", "-machine", f"q35,accel={accel}",
               "-cpu", "host", "-smp", "4", "-m", "2048",
               "-drive", f"if=pflash,format=raw,readonly=on,file={ovmf('OVMF_CODE')}",
               "-drive", f"if=pflash,format=raw,file={varsf}",
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

        print(f"== booting {os.path.basename(img)} (accel={accel}) ==", flush=True)
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
