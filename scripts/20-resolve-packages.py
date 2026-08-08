#!/usr/bin/env python3
"""Work out the minimal RPM set for the bootstrap image.

    20-resolve-packages.py <extracted-iso-root> <output-keep.txt>

WHY THIS EXISTS
---------------
The obvious approach - install @core - produces a 2.7GB ISO, because Cisco
REDEFINED the 'core' comps group on the NFVIS media. Their @core is not a
minimal base; it contains the whole NFVIS platform:

    cisco-esc-lite                189 MB
    cosign                         48 MB
    java-1.8.0-openjdk-headless    36 MB
    confd                          20 MB
    + 12 more Cisco RPMs, plus nodejs / etcd / kubectl / openvswitch

That is ~300MB of Cisco stack dragged into a VM whose only job is to push
firmware into an ASIC over PCIe.

So instead we enumerate a genuine el8 base explicitly and resolve its
dependency closure against the ISO's own repodata. That lands around 330
packages / 590MB instead of 1174 / 1714MB.

The resolver picks ONE provider per capability (preferring an exact name
match, then a seed member, then the smallest candidate). That is a heuristic,
not libsolv - but the closure is verified end-to-end by actually installing
it, which is what the build does.
"""
import os
import shutil
import sqlite3
import sys
import tempfile

# A real minimal el8 base. Deliberately NOT "@core".
SEED = """
basesystem filesystem setup rootfiles bash glibc glibc-common glibc-langpack-en
systemd systemd-udev rpm dnf yum coreutils util-linux procps-ng iproute iputils
kernel kernel-core kernel-modules dracut dracut-config-generic dracut-network
grub2-common grub2-efi-x64 grub2-pc grub2-pc-modules grub2-tools
grub2-tools-minimal grub2-tools-extra shim-x64 efibootmgr
lvm2 e2fsprogs xfsprogs parted
NetworkManager NetworkManager-team openssh-server openssh-clients chrony
sudo passwd shadow-utils authselect selinux-policy-targeted policycoreutils
audit dbus kbd hostname ca-certificates curl tar xz gzip bzip2 less vim-minimal
platform-python dmidecode pciutils kmod which findutils gawk sed grep
rsyslog cronie crontabs tuned firewalld iptables
switch-confd nic-xl710-i350
""".split()

# Never pull these in to satisfy a dependency.
EXCLUDE = set("""
cisco-esc-lite confd cosign java-1.8.0-openjdk-headless java-11-openjdk-headless
javapackages-tools npm nodejs etcd kubectl1.18 openvswitch2.17
network-scripts-openvswitch2.17 docker-ce docker-ce-cli containerd.io
webkit2gtk3 llvm-libs adwaita-icon-theme adwaita-cursor-theme gtk3
qat dpdk dpdk-tools Twisted vdaemon kodachi tabei-plat tabei-m-plat
anaconda anaconda-core anaconda-gui anaconda-tui
nfvis-fwupdate
""".split())
# nfvis-fwupdate is excluded on purpose: it flashes BIOS/CIMC firmware. Reports
# exist of newer NFVIS images locking the BIOS so F2 setup becomes unreachable,
# which would make PCI passthrough impossible to configure. It stays out.


def find(root, pattern, tmpdir):
    """Locate a repodata sqlite DB, decompressing it if needed.

    The NFVIS media ships these bzip2-compressed (*.sqlite.bz2), so a naive
    glob for *.sqlite finds nothing.
    """
    import bz2
    import glob
    import shutil

    hits = glob.glob(os.path.join(root, "repodata", pattern))
    if hits:
        return hits[0]

    hits = glob.glob(os.path.join(root, "repodata", pattern + ".bz2"))
    if not hits:
        sys.exit(f"ERROR: no {pattern}[.bz2] under {root}/repodata")

    out = os.path.join(tmpdir, os.path.basename(hits[0])[:-4])
    with bz2.open(hits[0], "rb") as src, open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return out


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    root, out = sys.argv[1], sys.argv[2]

    tmpdir = tempfile.mkdtemp(prefix="encs-repodata-")
    try:
        resolve(root, out, tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def resolve(root, out, tmpdir):
    c = sqlite3.connect(find(root, "*primary.sqlite", tmpdir))
    c.execute(f"attach '{find(root, '*filelists.sqlite', tmpdir)}' as f")

    pkgs = {k: (n, s, l) for k, n, s, l in c.execute(
        "select pkgKey,name,size_package,location_href from packages")}
    byname = {}
    for k, (n, _, _) in pkgs.items():
        byname.setdefault(n, []).append(k)

    prov = {}
    for k, n in c.execute("select pkgKey,name from provides"):
        prov.setdefault(n, []).append(k)
    for k, d, fns in c.execute("select pkgKey,dirname,filenames from f.filelist"):
        for fn in fns.split("/"):
            prov.setdefault(f"{d}/{fn}", []).append(k)

    req = {}
    for k, n in c.execute("select pkgKey,name from requires"):
        req.setdefault(k, []).append(n)

    seed = {n for n in SEED if n in byname}
    for n in SEED:
        if n not in byname:
            print(f"  note: seed package not on this ISO, skipping: {n}")

    def rank(pk, cap):
        n, s, _ = pkgs[pk]
        return (0 if n == cap else 1, 0 if n in seed else 1, s)

    chosen, satisfied, blocked = set(), set(), []
    work = [k for n in seed for k in byname[n]]
    while work:
        k = work.pop(0)
        if k in chosen:
            continue
        chosen.add(k)
        for cap in req.get(k, []):
            if cap in satisfied:
                continue
            cands = prov.get(cap)
            if not cands:
                continue
            satisfied.add(cap)
            if any(p in chosen for p in cands):
                continue
            allowed = [p for p in cands if pkgs[p][0] not in EXCLUDE]
            if not allowed:
                blocked.append((pkgs[k][0], cap))
                continue
            work.append(min(allowed, key=lambda p: rank(p, cap)))

    tot = sum(pkgs[k][1] for k in chosen)
    allsz = sum(v[1] for v in pkgs.values())
    print(f"  keep  {len(chosen):5d} packages  {tot/1e6:8.1f} MB")
    print(f"  drop  {len(pkgs)-len(chosen):5d} packages  {(allsz-tot)/1e6:8.1f} MB")
    if blocked:
        print(f"  note: {len(blocked)} dependencies could only be met by excluded "
              f"packages (ignored): {blocked[:4]}")

    with open(out, "w") as fh:
        fh.write("\n".join(sorted(pkgs[k][2].split("/")[-1] for k in chosen)) + "\n")
    print(f"  wrote {out}")


if __name__ == "__main__":
    main()
