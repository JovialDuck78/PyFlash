#!/usr/bin/env python3
"""
PyFlash — A lightweight Rufus alternative for Linux
GPL v3 — Single file, no Electron, no npm
Dependencies: python3, tk, p7zip, parted
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import subprocess
import threading
import os
import sys
import time
import json
import re


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

def run(cmd, timeout=30, **kwargs):
    """Run a shell command, return (returncode, stdout, stderr). Never hangs > timeout."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, **kwargs
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"timed out after {timeout}s"


def require_root():
    if os.geteuid() != 0:
        messagebox.showerror(
            "Root Required",
            "PyFlash must be run as root (sudo python3 pyflash.py)."
        )
        sys.exit(1)


# ─────────────────────────────────────────────
#  Drive detection
# ─────────────────────────────────────────────

def list_usb_drives():
    """
    Return a list of dicts: {dev, model, size_bytes, size_human, removable}
    Uses lsblk JSON output; falls back to /sys/block scan.
    Only returns whole-disk removable block devices (not partitions).
    """
    drives = []

    rc, out, _ = run(
        "lsblk -J -b -o NAME,SIZE,MODEL,HOTPLUG,TYPE,TRAN 2>/dev/null"
    )
    if rc == 0:
        try:
            data = json.loads(out)
            for dev in data.get("blockdevices", []):
                if dev.get("type") != "disk":
                    continue
                # Accept USB or hotplug drives; skip nvme/mmcblk root disks
                # to avoid wiping the system drive accidentally
                is_usb = dev.get("tran") == "usb"
                is_hotplug = str(dev.get("hotplug", "0")) == "1"
                if not (is_usb or is_hotplug):
                    continue
                size = int(dev.get("size") or 0)
                model = (dev.get("model") or "Unknown").strip()
                name = dev["name"]
                drives.append({
                    "dev": f"/dev/{name}",
                    "model": model,
                    "size_bytes": size,
                    "size_human": human_size(size),
                })
            return drives
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    # Fallback: /sys/block scan
    for name in os.listdir("/sys/block"):
        removable_path = f"/sys/block/{name}/removable"
        try:
            with open(removable_path) as f:
                if f.read().strip() != "1":
                    continue
        except OSError:
            continue
        size = get_drive_size_bytes(f"/dev/{name}")
        model = "Unknown"
        try:
            with open(f"/sys/block/{name}/device/model") as f:
                model = f.read().strip()
        except OSError:
            pass
        drives.append({
            "dev": f"/dev/{name}",
            "model": model,
            "size_bytes": size,
            "size_human": human_size(size),
        })
    return drives


def get_drive_size_bytes(dev):
    """Return drive size in bytes, or 0 on error."""
    rc, out, _ = run(f"blockdev --getsize64 {dev} 2>/dev/null")
    try:
        return int(out) if rc == 0 else 0
    except ValueError:
        return 0


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ─────────────────────────────────────────────
#  ISO detection
# ─────────────────────────────────────────────

def detect_iso_type(iso_path):
    """
    Peek inside the ISO with 7z and classify as 'windows', 'linux', or 'unknown'.
    Returns ('windows'|'linux'|'unknown', detail_string)
    """
    rc, out, _ = run(f'7z l "{iso_path}" 2>/dev/null | head -60')
    if rc != 0:
        return "unknown", "7z not available or unreadable ISO"

    lines_lower = out.lower()

    # Windows indicators
    win_markers = ["sources/install.wim", "autorun.inf", "bootmgr", "setup.exe"]
    if any(m in lines_lower for m in win_markers):
        return "windows", "Windows install ISO detected"

    # Linux indicators
    linux_markers = [
        "vmlinuz", "initrd", "casper", "live", "isolinux",
        "grub/grub.cfg", "boot/grub"
    ]
    if any(m in lines_lower for m in linux_markers):
        return "linux", "Linux live/install ISO detected"

    return "unknown", "Could not determine ISO type"


# ─────────────────────────────────────────────
#  Drive operations
# ─────────────────────────────────────────────

def _stop_automounters(log_cb):
    """
    Send SIGKILL (not SIGTERM) to automounters with a subprocess timeout
    so we can never block. Runs each kill with a 2s hard timeout.
    """
    killed = []
    for name in ("udisksd", "udisks-daemon", "udiskie", "gvfs-udisks2-volume-monitor"):
        try:
            r = subprocess.run(["pgrep", "-x", name], capture_output=True, timeout=2)
            if r.returncode == 0 and r.stdout.strip():
                subprocess.run(["pkill", "-KILL", "-x", name],
                               capture_output=True, timeout=2)
                killed.append(name)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    if killed:
        log_cb(f"Stopped: {', '.join(killed)}")


def _start_automounters():
    """Restart udisks2 after we're done (fire and forget)."""
    try:
        subprocess.Popen(["systemctl", "start", "udisks2"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass


def _kill_holders(dev, log_cb):
    """Kill every process that has dev or any of its partitions open."""
    base = os.path.basename(dev)

    # Collect all related nodes from /proc/partitions (more reliable than lsblk)
    nodes = [dev]
    try:
        with open("/proc/partitions") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[3].startswith(base) and parts[3] != base:
                    nodes.append(f"/dev/{parts[3]}")
    except OSError:
        pass

    # Unmount anything listed in /proc/mounts
    try:
        with open("/proc/mounts") as f:
            mounted = {line.split()[0] for line in f if line.split()}
    except OSError:
        mounted = set()

    for node in nodes:
        if node in mounted:
            run(f"umount -f {node} 2>/dev/null")
            run(f"umount -l {node} 2>/dev/null")

    # Kill by fuser
    for node in nodes:
        run(f"fuser -km {node} 2>/dev/null")

    # Also kill by lsof pid list (catches processes fuser misses)
    rc, out, _ = run(f"lsof -t {dev}* 2>/dev/null")
    for pid in out.splitlines():
        pid = pid.strip()
        if pid.isdigit():
            run(f"kill -9 {pid} 2>/dev/null")

    time.sleep(0.5)


def _partitions_visible(dev):
    """Check /proc/partitions directly — faster and more reliable than lsblk."""
    base = os.path.basename(dev)
    try:
        with open("/proc/partitions") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4:
                    name = parts[3]
                    if name.startswith(base) and name != base:
                        return True
    except OSError:
        pass
    return False


def unmount_drive(dev, log_cb):
    """Unmount, stop automounters, kill holders, force kernel re-read."""
    _stop_automounters(log_cb)
    time.sleep(0.5)
    _kill_holders(dev, log_cb)
    run(f"blockdev --rereadpt {dev} 2>/dev/null")
    time.sleep(0.5)

    log_cb("Waiting for kernel to re-register drive...")
    for _ in range(20):
        if get_drive_size_bytes(dev) > 0:
            log_cb("Drive ready.")
            return True
        time.sleep(0.5)

    log_cb("WARNING: drive size still 0 after 10 s — proceeding anyway.")
    return False


def _wait_partitions_gone(dev, log_cb, timeout=20):
    """
    Poll /proc/partitions until NO child partitions are listed.
    Also repeatedly kills holders and nudges the kernel.
    Must return True before parted can safely run.
    """
    log_cb("Waiting for old partition nodes to disappear...")
    for i in range(timeout * 2):
        if not _partitions_visible(dev):
            log_cb("Partition table is clear.")
            return True
        # Every 2 s, re-kill holders and re-read
        if i % 4 == 0:
            # Log exactly what is holding the device open
            rc, out, _ = run(f"fuser -v {dev}* 2>&1 || true")
            if out.strip():
                log_cb(f"Holders: {out.strip()[:200]}")
            rc2, out2, _ = run(f"lsof {dev} 2>/dev/null | tail -5")
            if out2.strip():
                log_cb(f"lsof: {out2.strip()[:200]}")
            _kill_holders(dev, log_cb)
            run(f"blockdev --rereadpt {dev} 2>/dev/null")
            run(f"udevadm settle --timeout=2 2>/dev/null")
        time.sleep(0.5)
    # Final diagnosis before giving up
    rc, out, _ = run(f"fuser -v {dev}* 2>&1 || true")
    log_cb(f"Still held by: {out.strip()[:300] if out.strip() else 'unknown'}")
    log_cb("WARNING: old partitions still visible — will use sfdisk instead of parted.")
    return False


def wipe_partition_table(dev, log_cb):
    """
    Zero front + back of disk, then wait for kernel to clear partition nodes.
    """
    log_cb("Wiping existing partition table...")
    run(f"dd if=/dev/zero of={dev} bs=1M count=2 conv=fsync 2>/dev/null")
    size = get_drive_size_bytes(dev)
    if size > 4 * 1024 * 1024:
        skip = (size // (1024 * 1024)) - 2
        run(f"dd if=/dev/zero of={dev} bs=1M count=2 seek={skip} conv=fsync 2>/dev/null")
    run(f"sync")
    run(f"blockdev --rereadpt {dev} 2>/dev/null")
    run(f"udevadm settle --timeout=5 2>/dev/null")
    _wait_partitions_gone(dev, log_cb)


def create_partition_table(dev, scheme, log_cb):
    """
    Create a fresh partition table and single partition.
    Tries parted first, falls back to sfdisk (which uses BLKRRPART ioctl
    and is more tolerant of devices that are busy at the VFS level).
    """
    pt_parted = "msdos" if scheme == "MBR" else "gpt"
    pt_sfdisk = "dos" if scheme == "MBR" else "gpt"
    log_cb(f"Creating {scheme} partition table on {dev}...")

    # --- attempt with parted ---
    _kill_holders(dev, log_cb)
    run(f"udevadm settle --timeout=3 2>/dev/null")
    rc_p, _, err_p = run(f"parted -s {dev} mklabel {pt_parted}")
    if rc_p == 0:
        rc_p2, _, err_p2 = run(f"parted -s -a optimal {dev} mkpart primary 1MiB 100%")
        if rc_p2 == 0:
            run(f"partprobe {dev} 2>/dev/null")
            run(f"udevadm settle --timeout=5 2>/dev/null")
            log_cb("Partition table created (parted).")
            return

    log_cb(f"parted failed ({err_p or err_p2}), trying sfdisk...")

    # --- fallback: sfdisk ---
    # sfdisk accepts a script on stdin and uses a different kernel ioctl
    # that succeeds even when the old partition is still open at VFS level.
    sfdisk_script = f"label: {pt_sfdisk}\nstart=2048, type=83\n"
    rc_s, _, err_s = run(
        f"echo '{sfdisk_script}' | sfdisk --no-reread --force {dev} 2>&1",
    )
    if rc_s == 0:
        run(f"partprobe {dev} 2>/dev/null")
        run(f"udevadm settle --timeout=5 2>/dev/null")
        log_cb("Partition table created (sfdisk).")
        return

    log_cb(f"sfdisk also failed: {err_s}")

    # --- last resort: dd a pre-built MBR/GPT then re-read ---
    log_cb("Trying direct ioctl re-read after sfdisk...")
    run(f"blockdev --rereadpt {dev} 2>/dev/null")
    run(f"udevadm settle --timeout=5 2>/dev/null")
    if _partitions_visible(dev) or os.path.exists(guess_partition_node(dev)):
        log_cb("Partition node appeared after re-read.")
        return

    raise RuntimeError(
        f"Could not create partition table. parted: {err_p} | sfdisk: {err_s}\n"
        "Try: sudo umount -f /dev/sdcX && sudo fuser -km /dev/sdc"
    )


def wait_for_partition(part, timeout=15):
    """Poll until partition node appears in /dev."""
    for _ in range(timeout * 2):
        if os.path.exists(part):
            return True
        run(f"udevadm settle --timeout=2 2>/dev/null")
        time.sleep(0.5)
    return False


def guess_partition_node(dev):
    """
    Given /dev/sdX return /dev/sdX1 (or /dev/sdXp1 for mmcblk/nvme).
    """
    if re.search(r"\d$", dev):
        return dev + "p1"
    return dev + "1"


def format_partition(part, fs, label, log_cb):
    """Format a partition with the given filesystem."""
    log_cb(f"Formatting {part} as {fs} (label: {label})…")
    label = label[:11] if fs in ("FAT32", "NTFS") else label[:16]

    if fs == "FAT32":
        # mkfs.fat and mkfs.vfat are the same tool; name varies by distro
        fat_bin = "mkfs.fat" if run("which mkfs.fat 2>/dev/null")[0] == 0 else "mkfs.vfat"
        cmd = f'{fat_bin} -F32 -n "{label}" {part}'
    elif fs == "exFAT":
        # Try mkfs.exfat (util-linux) then exfatprogs fallback
        exfat_bin = "mkfs.exfat" if run("which mkfs.exfat 2>/dev/null")[0] == 0 else "mkexfatfs"
        cmd = f'{exfat_bin} -n "{label}" {part}'
    elif fs == "NTFS":
        cmd = f'mkfs.ntfs -f -L "{label}" {part}'
    elif fs == "ext4":
        cmd = f'mkfs.ext4 -L "{label}" -F {part}'
    else:
        raise ValueError(f"Unknown filesystem: {fs}")

    rc, _, err = run(cmd)
    if rc != 0:
        raise RuntimeError(f"mkfs failed: {err}")
    run("sync")


def fix_efi_casing(mountpoint, log_cb):
    """
    Ensure /EFI/BOOT/ (uppercase) exists and contains all bootloader files.
    Some firmware (HP etc.) only looks for the uppercase path.
    """
    src_candidates = [
        os.path.join(mountpoint, "efi", "boot"),
        os.path.join(mountpoint, "EFI", "boot"),
        os.path.join(mountpoint, "efi", "BOOT"),
    ]
    dst = os.path.join(mountpoint, "EFI", "BOOT")

    for src in src_candidates:
        if os.path.isdir(src) and src.lower() != dst.lower():
            log_cb(f"Fixing EFI casing: {src} → {dst}")
            os.makedirs(dst, exist_ok=True)
            for fname in os.listdir(src):
                src_file = os.path.join(src, fname)
                dst_file = os.path.join(dst, fname.upper())
                if os.path.isfile(src_file) and not os.path.exists(dst_file):
                    import shutil
                    shutil.copy2(src_file, dst_file)
            return

    # Also check top-level /EFI/BOOT already exists; ensure uppercase filenames
    if os.path.isdir(dst):
        for fname in os.listdir(dst):
            if fname != fname.upper():
                src_file = os.path.join(dst, fname)
                dst_file = os.path.join(dst, fname.upper())
                if not os.path.exists(dst_file):
                    import shutil
                    shutil.copy2(src_file, dst_file)


# ─────────────────────────────────────────────
#  Write operations
# ─────────────────────────────────────────────

def raw_write(iso_path, dev, progress_cb, log_cb, cancel_event):
    """
    dd-style raw write using O_SYNC for accurate speed reporting.
    Returns True on success.
    """
    iso_size = os.path.getsize(iso_path)
    log_cb(f"Raw writing {iso_path} → {dev} ({human_size(iso_size)})…")

    CHUNK = 4 * 1024 * 1024  # 4 MB
    written = 0
    start = time.time()

    try:
        src_fd = open(iso_path, "rb")
        dst_fd = os.open(dev, os.O_WRONLY | os.O_SYNC)
    except OSError as e:
        raise RuntimeError(f"Could not open device: {e}")

    try:
        while True:
            if cancel_event.is_set():
                log_cb("Cancelled.")
                return False
            chunk = src_fd.read(CHUNK)
            if not chunk:
                break
            os.write(dst_fd, chunk)
            written += len(chunk)
            elapsed = time.time() - start
            speed = written / elapsed if elapsed > 0 else 0
            remaining = (iso_size - written) / speed if speed > 0 else 0
            pct = written / iso_size * 100
            progress_cb(pct, speed, remaining)
    finally:
        src_fd.close()
        os.close(dst_fd)

    log_cb("Flushing buffers…")
    run("sync")

    # Fix EFI casing on any EFI partition found on the drive
    _fix_efi_on_drive(dev, log_cb)

    log_cb("Raw write complete.")
    return True


def format_and_copy(iso_path, dev, fs, scheme, label, progress_cb, log_cb, cancel_event):
    """
    Format the drive, then extract the ISO contents onto it.
    This is the correct path for Linux ISOs on UEFI systems.
    """
    import shutil, tempfile

    log_cb("=== Format + Copy mode ===")
    unmount_drive(dev, log_cb)

    if cancel_event.is_set():
        return False

    # Safety poll: make sure kernel sees the drive before partitioning
    log_cb("Verifying drive is accessible…")
    for _ in range(20):
        if get_drive_size_bytes(dev) > 0:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("Drive reported 0 bytes — cannot partition. Try reinserting.")

    wipe_partition_table(dev, log_cb)

    if cancel_event.is_set():
        return False

    create_partition_table(dev, scheme, log_cb)

    part = guess_partition_node(dev)
    log_cb(f"Waiting for {part} to appear…")
    if not wait_for_partition(part):
        raise RuntimeError(f"Partition {part} did not appear after partitioning.")

    format_partition(part, fs, label, log_cb)

    if cancel_event.is_set():
        return False

    # Mount the partition
    mnt = tempfile.mkdtemp(prefix="pyflash_")
    log_cb(f"Mounting {part} at {mnt}…")
    rc, _, err = run(f"mount {part} {mnt}")
    if rc != 0:
        os.rmdir(mnt)
        raise RuntimeError(f"Mount failed: {err}")

    try:
        # Extract ISO contents
        log_cb("Extracting ISO contents (this may take a while)…")
        iso_size = os.path.getsize(iso_path)
        # Switch progress bar to pulse mode for the extraction phase
        progress_cb(-1, 0, 0)

        proc = subprocess.Popen(
            ["7z", "x", "-y", f"-o{mnt}", iso_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )

        while True:
            if cancel_event.is_set():
                proc.terminate()
                return False
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            line = line.strip()
            if line:
                log_cb(line)

        if proc.returncode != 0:
            raise RuntimeError("7z extraction failed.")

        # Fix EFI casing
        fix_efi_casing(mnt, log_cb)

        log_cb("Written ISO to RAM!")
        log_cb("Copying from RAM to disk — this will take a long time…")
        log_cb("  Estimated flush time per GB of ISO:")
        log_cb("    USB 2.0  (~15 MB/s) :  ~1 min 8s per GB")
        log_cb("    USB 3.0  (~80 MB/s) :  ~13s per GB")
        run("sync", timeout=7200)
        log_cb("Extraction complete.")
        return True

    finally:
        log_cb("Unmounting…")
        run(f"umount -l {mnt} 2>/dev/null", timeout=30)
        time.sleep(0.5)
        try:
            os.rmdir(mnt)
        except OSError:
            pass


def _fix_efi_on_drive(dev, log_cb):
    """After raw write, mount any EFI partition found and fix casing."""
    import tempfile, shutil
    rc, out, _ = run(f"lsblk -no NAME,PARTTYPE {dev} 2>/dev/null")
    efi_parts = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            name = parts[0].strip()
            ptype = parts[1].strip().lower()
            if "efi" in ptype or ptype == "c12a7328-f81f-11d2-ba4b-00a0c93ec93b":
                efi_parts.append(f"/dev/{name}")

    for part in efi_parts:
        mnt = tempfile.mkdtemp(prefix="pyflash_efi_")
        rc2, _, _ = run(f"mount {part} {mnt} 2>/dev/null")
        if rc2 == 0:
            try:
                fix_efi_casing(mnt, log_cb)
                run("sync")
            finally:
                run(f"umount {mnt} 2>/dev/null")
                try:
                    os.rmdir(mnt)
                except OSError:
                    pass


def format_drive_only(dev, fs, scheme, label, progress_cb, log_cb, cancel_event):
    """Format drive for plain storage use (Format Drive tab)."""
    log_cb("=== Format Drive mode ===")
    unmount_drive(dev, log_cb)

    if cancel_event.is_set():
        return False

    for _ in range(20):
        if get_drive_size_bytes(dev) > 0:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("Drive reported 0 bytes — cannot format. Try reinserting.")

    progress_cb(10, 0, 0)
    wipe_partition_table(dev, log_cb)
    progress_cb(20, 0, 0)

    if cancel_event.is_set():
        return False

    create_partition_table(dev, scheme, log_cb)
    progress_cb(50, 0, 0)

    part = guess_partition_node(dev)
    log_cb(f"Waiting for {part}…")
    if not wait_for_partition(part):
        raise RuntimeError(f"Partition {part} did not appear.")

    format_partition(part, fs, label, log_cb)
    progress_cb(100, 0, 0)
    log_cb("Format complete.")
    return True


# ─────────────────────────────────────────────
#  GUI
# ─────────────────────────────────────────────

class PyFlash(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PyFlash")
        self.resizable(False, False)
        self.configure(bg="#1e1e2e")

        self._cancel_event = threading.Event()
        self._worker = None
        self._drives = []

        # Must set tk option_add BEFORE widgets are created
        self.option_add("*TCombobox*Listbox.background", "#313244")
        self.option_add("*TCombobox*Listbox.foreground", "#cdd6f4")
        self.option_add("*TCombobox*Listbox.selectBackground", "#89b4fa")
        self.option_add("*TCombobox*Listbox.selectForeground", "#1e1e2e")
        self.option_add("*TCombobox*Listbox.relief", "flat")

        self._build_ui()
        self.after(100, self._refresh_drives)

    # ── UI construction ──────────────────────

    def _build_ui(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        # Colours
        bg = "#1e1e2e"
        fg = "#cdd6f4"
        acc = "#89b4fa"
        inp = "#313244"
        dis = "#45475a"

        style.configure(".", background=bg, foreground=fg, font=("Segoe UI", 10))
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("TNotebook", background=bg, borderwidth=0)
        style.configure("TNotebook.Tab", background=inp, foreground=fg, padding=[12, 4])
        style.map("TNotebook.Tab", background=[("selected", acc)], foreground=[("selected", bg)])
        style.configure("TCombobox",
                        fieldbackground=inp, background=inp,
                        foreground=fg, selectbackground=acc,
                        selectforeground=bg, insertcolor=fg)
        style.map("TCombobox",
                  fieldbackground=[("readonly", inp), ("disabled", dis)],
                  foreground=[("readonly", fg), ("disabled", dis)],
                  selectbackground=[("readonly", inp)],
                  selectforeground=[("readonly", fg)])
        style.configure("TProgressbar", troughcolor=inp, background=acc)
        style.configure("TButton", background=inp, foreground=fg, relief="flat", padding=[8, 4])
        style.map("TButton", background=[("active", acc), ("disabled", dis)])
        style.configure("Accent.TButton", background=acc, foreground=bg)
        style.map("Accent.TButton", background=[("active", "#74c7ec"), ("disabled", dis)])

        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)

        # Drive row
        drive_row = ttk.Frame(outer)
        drive_row.pack(fill="x", pady=(0, 8))
        ttk.Label(drive_row, text="USB Drive:").pack(side="left")
        self._drive_var = tk.StringVar()
        self._drive_cb = ttk.Combobox(drive_row, textvariable=self._drive_var,
                                       state="readonly", width=38)
        self._drive_cb.pack(side="left", padx=6)
        ttk.Button(drive_row, text="⟳ Refresh", command=self._refresh_drives).pack(side="left")

        # Notebook
        self._nb = ttk.Notebook(outer)
        self._nb.pack(fill="both", expand=True)

        self._build_flash_tab()
        self._build_format_tab()

        # Log box (sits directly under the notebook, no progress bar)
        log_frame = ttk.Frame(outer)
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        self._log = tk.Text(log_frame, height=8, bg="#11111b", fg="#a6e3a1",
                            relief="flat", font=("Monospace", 9), state="disabled")
        scroll = ttk.Scrollbar(log_frame, command=self._log.yview)
        self._log.configure(yscrollcommand=scroll.set)
        self._log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # Buttons
        btn_row = ttk.Frame(outer)
        btn_row.pack(fill="x", pady=(10, 0))
        self._start_btn = ttk.Button(btn_row, text="▶  Start", style="Accent.TButton",
                                      command=self._start)
        self._start_btn.pack(side="left", padx=(0, 6))
        self._cancel_btn = ttk.Button(btn_row, text="✕  Cancel",
                                       command=self._cancel, state="disabled")
        self._cancel_btn.pack(side="left")
        ttk.Label(btn_row, text="PyFlash · GPL v3", foreground="#585b70").pack(side="right")

    def _build_flash_tab(self):
        tab = ttk.Frame(self._nb, padding=12)
        self._nb.add(tab, text=" Flash ISO ")

        # ISO path
        iso_row = ttk.Frame(tab)
        iso_row.pack(fill="x", pady=(0, 8))
        ttk.Label(iso_row, text="ISO File:").pack(side="left")
        self._iso_var = tk.StringVar()
        ttk.Entry(iso_row, textvariable=self._iso_var, width=34).pack(side="left", padx=6)
        ttk.Button(iso_row, text="Browse…", command=self._browse_iso).pack(side="left")

        # Mode row
        mode_row = ttk.Frame(tab)
        mode_row.pack(fill="x", pady=(0, 8))
        ttk.Label(mode_row, text="Write Mode:").pack(side="left")
        self._mode_var = tk.StringVar(value="Auto-detect")
        mode_cb = ttk.Combobox(mode_row, textvariable=self._mode_var,
                                values=["Auto-detect", "Raw Write (dd)", "Format + Copy (FAT32)",
                                        "Format + Copy (NTFS)", "Format + Copy (exFAT)"],
                                state="readonly", width=28)
        mode_cb.pack(side="left", padx=6)
        mode_cb.bind("<<ComboboxSelected>>", self._on_mode_change)

        # Scheme row
        scheme_row = ttk.Frame(tab)
        scheme_row.pack(fill="x", pady=(0, 4))
        ttk.Label(scheme_row, text="Partition Scheme:").pack(side="left")
        self._scheme_var = tk.StringVar(value="GPT (UEFI)")
        ttk.Combobox(scheme_row, textvariable=self._scheme_var,
                     values=["GPT (UEFI)", "MBR (Legacy BIOS)"],
                     state="readonly", width=20).pack(side="left", padx=6)

        self._scheme_warn = ttk.Label(tab, foreground="#f9e2af",
                                       text="")
        self._scheme_warn.pack(fill="x")

    def _build_format_tab(self):
        tab = ttk.Frame(self._nb, padding=12)
        self._nb.add(tab, text=" Format Drive ")

        fs_row = ttk.Frame(tab)
        fs_row.pack(fill="x", pady=(0, 8))
        ttk.Label(fs_row, text="File System:").pack(side="left")
        self._fmt_fs_var = tk.StringVar(value="exFAT")
        ttk.Combobox(fs_row, textvariable=self._fmt_fs_var,
                     values=["FAT32", "exFAT", "NTFS", "ext4"],
                     state="readonly", width=12).pack(side="left", padx=6)

        scheme_row = ttk.Frame(tab)
        scheme_row.pack(fill="x", pady=(0, 8))
        ttk.Label(scheme_row, text="Partition Scheme:").pack(side="left")
        self._fmt_scheme_var = tk.StringVar(value="GPT (UEFI)")
        ttk.Combobox(scheme_row, textvariable=self._fmt_scheme_var,
                     values=["GPT (UEFI)", "MBR (Legacy BIOS)"],
                     state="readonly", width=20).pack(side="left", padx=6)

        label_row = ttk.Frame(tab)
        label_row.pack(fill="x", pady=(0, 8))
        ttk.Label(label_row, text="Volume Label:").pack(side="left")
        self._fmt_label_var = tk.StringVar(value="PYFLASH")
        ttk.Entry(label_row, textvariable=self._fmt_label_var, width=16).pack(side="left", padx=6)

        ttk.Label(tab, text="⚠  ALL DATA ON THE DRIVE WILL BE ERASED.",
                  foreground="#f38ba8").pack(anchor="w", pady=(4, 0))

    # ── Drive management ────────────────────

    def _refresh_drives(self):
        self._drives = list_usb_drives()
        labels = [f"{d['dev']}  {d['model']}  [{d['size_human']}]" for d in self._drives]
        self._drive_cb["values"] = labels
        if labels:
            self._drive_cb.current(0)
        else:
            self._drive_var.set("(no USB drives detected)")

    def _selected_dev(self):
        idx = self._drive_cb.current()
        if idx < 0 or idx >= len(self._drives):
            return None
        return self._drives[idx]["dev"]

    # ── Browse ──────────────────────────────

    def _browse_iso(self):
        path = filedialog.askopenfilename(
            title="Select ISO",
            filetypes=[("ISO images", "*.iso *.img"), ("All files", "*")]
        )
        if path:
            self._iso_var.set(path)

    def _on_mode_change(self, _=None):
        mode = self._mode_var.get()
        if mode == "Raw Write (dd)":
            self._scheme_warn.config(
                text="ℹ  Raw write stamps the ISO's built-in partition table onto the drive.\n"
                     "   GPT/MBR selection is ignored for raw writes."
            )
        else:
            self._scheme_warn.config(text="")

    # ── Start / Cancel ───────────────────────

    def _start(self):
        tab = self._nb.index("current")
        dev = self._selected_dev()
        if not dev:
            messagebox.showerror("No Drive", "Please select a USB drive.")
            return

        if tab == 0:  # Flash ISO tab
            iso = self._iso_var.get().strip()
            if not iso or not os.path.isfile(iso):
                messagebox.showerror("No ISO", "Please select a valid ISO file.")
                return
            if not messagebox.askyesno(
                "Confirm",
                f"This will ERASE ALL DATA on {dev}.\n\nAre you sure?"
            ):
                return
            self._run_flash(dev, iso)

        else:  # Format Drive tab
            if not messagebox.askyesno(
                "Confirm",
                f"This will ERASE ALL DATA on {dev}.\n\nAre you sure?"
            ):
                return
            self._run_format(dev)

    def _cancel(self):
        self._cancel_event.set()
        self._log_append("Cancellation requested…")

    # ── Flash worker ────────────────────────

    def _run_flash(self, dev, iso_path):
        mode = self._mode_var.get()
        scheme_str = self._scheme_var.get()
        scheme = "GPT" if "GPT" in scheme_str else "MBR"

        self._log_clear()
        self._set_busy(True)
        self._cancel_event.clear()

        def worker():
            try:
                if mode == "Auto-detect":
                    iso_type, detail = detect_iso_type(iso_path)
                    self._log_append(f"ISO type: {detail}")
                    if iso_type == "windows":
                        actual_mode = "ntfs"
                    elif iso_type == "linux" and scheme == "GPT":
                        actual_mode = "fat32"
                    else:
                        actual_mode = "raw"
                    self._log_append(f"Auto-selected mode: {actual_mode.upper()}")
                elif mode == "Raw Write (dd)":
                    actual_mode = "raw"
                elif "FAT32" in mode:
                    actual_mode = "fat32"
                elif "NTFS" in mode:
                    actual_mode = "ntfs"
                elif "exFAT" in mode:
                    actual_mode = "exfat"
                else:
                    actual_mode = "raw"

                if actual_mode == "raw":
                    raw_write(iso_path, dev, self._update_progress,
                              self._log_append, self._cancel_event)
                else:
                    fs_map = {"fat32": "FAT32", "ntfs": "NTFS", "exfat": "exFAT"}
                    format_and_copy(iso_path, dev,
                                    fs_map.get(actual_mode, "FAT32"),
                                    scheme, "PYFLASH",
                                    self._update_progress, self._log_append,
                                    self._cancel_event)

                if not self._cancel_event.is_set():
                    self.after(0, lambda: messagebox.showinfo("Done", "Operation completed successfully!"))
            except Exception as e:
                self._log_append(f"ERROR: {e}")
                self.after(0, lambda: messagebox.showerror("Error", str(e)))
            finally:
                _start_automounters()
                self.after(0, lambda: self._set_busy(False))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    # ── Format worker ────────────────────────

    def _run_format(self, dev):
        fs = self._fmt_fs_var.get()
        scheme_str = self._fmt_scheme_var.get()
        scheme = "GPT" if "GPT" in scheme_str else "MBR"
        label = self._fmt_label_var.get().strip() or "PYFLASH"

        self._log_clear()
        self._set_busy(True)
        self._cancel_event.clear()

        def worker():
            try:
                format_drive_only(dev, fs, scheme, label,
                                   self._update_progress, self._log_append,
                                   self._cancel_event)
                if not self._cancel_event.is_set():
                    self.after(0, lambda: messagebox.showinfo("Done", "Format completed successfully!"))
            except Exception as e:
                self._log_append(f"ERROR: {e}")
                self.after(0, lambda: messagebox.showerror("Error", str(e)))
            finally:
                _start_automounters()
                self.after(0, lambda: self._set_busy(False))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    # ── UI helpers ───────────────────────────

    def _set_busy(self, busy):
        state_on = "disabled" if busy else "normal"
        state_off = "normal" if busy else "disabled"
        self._start_btn.config(state=state_on)
        self._cancel_btn.config(state=state_off)

    def _update_progress(self, pct, speed_bps, eta_secs):
        # Progress bar removed; this is a no-op kept for call-site compatibility
        pass

    def _log_append(self, msg):
        def _write():
            self._log.config(state="normal")
            self._log.insert("end", msg + "\n")
            self._log.see("end")
            self._log.config(state="disabled")
        self.after(0, _write)

    def _log_clear(self):
        self._log.config(state="normal")
        self._log.delete("1.0", "end")
        self._log.config(state="disabled")


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    require_root()
    app = PyFlash()
    app.mainloop()
