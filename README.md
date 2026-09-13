# GPD Pocket 3 CPU & Power Suite

> **GPD Pocket 3 CPU & Fan control for Debian using gpd-fan**

A lightweight, hardware-locked early-boot initramfs watchdog and runtime power-tuning toolkit tailored specifically for the **GPD Pocket 3 Flagship Edition** (Intel Core i7-1195G7 Tiger Lake-U) running **Debian GNU/Linux** (Trixie/Sid, Linux 6.12+ kernels, UEFI, and LUKS+LVM).

---

## ⚡ The Problem

1. **The Early-Boot / LUKS Battery Drain**: Sitting at an early-boot ramdisk passphrase prompt leaves 8 CPU threads active with Intel Turbo Boost enabled, drawing unnecessary wattage and draining the battery if powered on accidentally in a bag or pocket.
2. **Missing Early ACPI Power Handling**: In the early initramfs phase before `systemd-logind` starts, tapping the hardware power button does nothing, leaving users unable to safely abort a boot.
3. **Storage Corruption Risks During Boot**: Powering off abruptly during an automatic early-boot `fsck` repair or journal replay risks metadata inconsistency.
4. **Tiger Lake Fan & Thermal Spikes**: Unrestricted 28W+ PL2 turbo bursts ramp the active cooling fan during basic tasks (reading, terminal work, text editing) on battery.

---

## 🚀 The Architecture

Unlike older architectures that required background Python governors, core-parking, or third-party EC hacks like NBFC, this suite relies on native Linux kernel interfaces (Intel RAPL, Speed Shift HWP, and `gpd-fan`):

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│ 1. Early Boot Ramdisk (Initramfs init-top)                                              │
│    • gpd-pocket-3-power-watchdog (C Micro-Daemon)                                       │
│      - 0.00% CPU kernel poll() on /dev/input/event*                                     │
│      - Enforces quiet initramfs profile (Turbo disabled, EPP balance_power)             │
│      - 30s Inactivity ──► Dims display to step floor (1% minimum clamp)                 │
│      - 60s Inactivity ──► Enters S3 / s2idle low-power sleep                            │
│      - Power Button Tap ──► Waits for active fsck to finish, then issues ACPI Poweroff  │
│      - Hotkey Brightness ──► Dynamic 20-step GNOME-matching brightness engine           │
└────────────────────────────────────────────┬────────────────────────────────────────────┘
                                             │ (Handoff to rootfs via init-bottom)
                                             ▼
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│ 2. Post-Boot Runtime (Clean Userland - No Background Daemons)                           │
│    • gpd-pocket-3-i7-1195G7-lowpower [on|off|status]                                    │
│      - Clamps Intel RAPL power limits (8W PL1 / 12W PL2 vs 20W PL1 / 28W PL2)           │
│      - Toggles Intel Speed Shift Energy Performance Preference (EPP)                    │
│      - Caps pstate frequency ceiling without offlining cores or breaking scheduling     │
│    • Native Kernel Fan Control                                                          │
│      - Handled seamlessly by the upstream Linux gpd-fan driver & hardware Fn toggle     │
│    • Multi-Machine Safe Guard                                                           │
│      - Validates i7-1195G7 & DMI strings (Pocket 3 / G1621-02); dormant on other PCs    │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 🛠️ Upstream Dependencies

* **[Linux Kernel `intel_rapl` / `powercap`](https://www.kernel.org/doc/html/latest/power/powercap/powercap.html)**: Native sysfs power capping interface for Tiger Lake-U PL1 (sustained) and PL2 (burst) wattage limits.
* **[Linux Kernel `intel_pstate`](https://www.kernel.org/doc/html/latest/admin-guide/pm/intel_pstate.html)**: Hardware-controlled P-States (HWP) and Energy Performance Preference (EPP) scaling.
* **[Linux Kernel `gpd-fan`](https://git.kernel.org/)**: Mainline Linux kernel driver providing native EC fan curve support for modern GPD hardware.
* **[Debian `initramfs-tools`](https://wiki.debian.org/initramfs-tools)**: Hook architecture used to inject the C watchdog and required input/display modules into the boot ramdisk.

---

## 📦 Compatibility

| Hardware Model | CPU Architecture | Supported | Notes |
| :--- | :--- | :---: | :--- |
| **GPD Pocket 3 (Flagship)** | Intel Core i7-1195G7 | ✅ | Full support (Iris Xe GT2, 4C/8T, 8-inch portrait panel) |
| **Other GPD Models / PCs** | Any Non-i7-1195G7 | 🛡️ *Safe* | Multi-boot USB safe. Scripts verify CPU and DMI tables, exiting with 0% overhead on foreign hosts. |

---

## 📥 Installation

```bash
# 1. Clone repository
git clone https://github.com/f-fix/gpd-pocket-3-i7-1195G7-governor.git
cd gpd-pocket-3-i7-1195G7-governor

# 2. Run installer (auto-elevates with sudo)
chmod +x gpd-pocket-3-i7-1195G7-governor.py
./gpd-pocket-3-i7-1195G7-governor.py --install
```

The installer will compile `/usr/local/bin/gpd-pocket-3-power-watchdog`, register the `initramfs-tools` hooks, rebuild the initramfs (`update-initramfs -u`), and deploy the `gpd-pocket-3-i7-1195G7-lowpower` CLI tool.

---

## 💻 CLI Usage

Manage runtime performance profiles without background daemon overhead:

```bash
# Check active wattage limits, Turbo status, and EPP
gpd-pocket-3-i7-1195G7-lowpower status

# Activate Whisper-Quiet Mode (8W PL1 / 12W PL2, Turbo Off, EPP=power, max_perf=50%)
gpd-pocket-3-i7-1195G7-lowpower on

# Restore Full Performance Mode (20W PL1 / 28W PL2, Turbo On up to 5.0GHz, EPP=balance_performance)
gpd-pocket-3-i7-1195G7-lowpower off
```
*(Automatically prompts for `sudo` if run as an unprivileged user).*

### Checking Watchdog Logs

To verify early-boot execution or investigate boot-time events:
```bash
# Read dedicated ramdisk log
cat /run/gpd_pocket_3_power.log

# Inspect kernel ring buffer logs
sudo dmesg | grep gpd-pocket-3-watchdog
```

---

## ⚙️ Technical Highlights

### 1. GNOME-Matched Dynamic Backlight Engine
The C watchdog queries `/sys/class/backlight/intel_backlight/max_brightness` dynamically at runtime and computes GNOME's exact 20-step curve:
* `min_brightness = ⌊max / 100⌋` (1% minimum clamp so the screen never blanks out at step 0)
* `step_size = ⌊(max - min) / 20⌋`
* `brightness(step) = min + (step × step_size)` for steps 0 through 20 (5% per step)
* Brightness hotkeys (Fn+F5/Fn+F6) operate during the LUKS prompt and seamlessly restore brightness after S3 suspend/resume cycles.

### 2. `fsck`-Aware Safe Power-Off
When the physical power button is pressed during early boot, the watchdog inspects `/proc/[pid]/stat` for active filesystem checks (`fsck`, `e2fsck`, `dosfsck`, `btrfsck`, `xfs_repair`). If a repair task is running, it defers shutdown until disk repairs complete cleanly, issues `sync()`, and safely invokes `reboot(RB_POWER_OFF)`.

---

## 📂 File Layout

```text
/usr/local/bin/
├── gpd-pocket-3-i7-1195G7-lowpower     # CLI RAPL & EPP power profile switcher
└── gpd-pocket-3-power-watchdog         # Compiled C early-boot micro-daemon

/usr/local/src/
└── gpd-pocket-3-power-watchdog.c       # Source code for the ramdisk C watchdog

/etc/initramfs-tools/
├── hooks/gpd_pocket_3_power            # Copies C watchdog into initrd cpio
├── scripts/init-top/gpd_pocket_3_power # Launches watchdog post-udev in ramdisk
└── scripts/init-bottom/gpd_pocket_3_power # Hands off to systemd upon rootfs mount
```
