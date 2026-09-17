# GPD Pocket 3 (i7-1195G7) CPU & Power Suite

> **GPD Pocket 3 (Intel Core i7-1195G7) CPU & Fan control for Debian using native kernel drivers**

A lightweight, hardware-locked early-boot initramfs watchdog and persistent runtime power-tuning toolkit tailored specifically for the **GPD Pocket 3 Flagship Edition** (Intel Core i7-1195G7 Tiger Lake-U) running **Debian GNU/Linux** (the only actually confirmed supported OS so far is **Debian forky/sid (unstable)**, with Linux 6.12+ kernels, UEFI, and LUKS+LVM).

---

## ⚡ The Problem

1. **The Early-Boot / LUKS Battery Drain**: Sitting at an early-boot ramdisk passphrase prompt leaves 8 CPU threads active with Intel Turbo Boost enabled, drawing unnecessary wattage and draining the battery if powered on accidentally in a bag or pocket.
2. **Missing Early ACPI Power Handling**: In the early initramfs phase before `systemd-logind` starts, tapping the hardware power button does nothing, leaving users unable to safely abort a boot.
3. **Storage Corruption Risks During Boot**: Powering off abruptly during an automatic early-boot `fsck` repair or journal replay risks metadata inconsistency.
4. **Premature Auto-Dimming & Suspend**: Unconditional inactivity timers in early boot can dim or suspend the system during disk checks (`fsck`), pre-prompt storage initialization, or post-passphrase LVM2 setup.
5. **Tiger Lake Fan & Thermal Spikes**: Unrestricted 28W+ PL2 turbo bursts ramp the active cooling fan during basic tasks (reading, terminal work, text editing) on battery.

---

## 🚀 The Architecture

Unlike older architectures that required background Python governors, core-parking, or third-party EC hacks like NBFC, this suite relies on native Linux kernel interfaces (Intel RAPL, Speed Shift HWP, and `gpd-fan`):

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│ 1. Early Boot Ramdisk (Initramfs init-top)                                              │
│    • gpd-pocket-3-power-watchdog (C Micro-Daemon)                                       │
│      - Prompt-Gated Timer: Inactivity dim (30s) / sleep (60s) ONLY runs during prompts  │
│      - Zero Dimming during fsck: Auto-detects running fsck and inhibits idle timers     │
│      - Safe ACPI Poweroff: Defers power button shutdown until active fsck completes     │
│      - Hotkey Brightness: Dynamic 20-step GNOME-matching brightness engine              │
│      - Quiet Initramfs Baseline: Turbo disabled, EPP balance_power                      │
└────────────────────────────────────────────┬────────────────────────────────────────────┘
                                             │ (Handoff to rootfs via init-bottom)
                                             ▼
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│ 2. Post-Boot Runtime (Clean Userland - No Background Daemons)                           │
│    • gpd-pocket-3-i7-1195G7-lowpower [on|off|status|apply-saved]                        │
│      - Clamps Intel RAPL power limits (8W PL1 / 12W PL2 vs 20W PL1 / 28W PL2)           │
│      - Toggles Intel Speed Shift Energy Performance Preference (EPP)                    │
│      - Caps pstate frequency ceiling without offlining cores or breaking scheduling     │
│    • Systemd Power Profile Persistence (gpd-pocket-3-power.service)                     │
│      - Oneshot unit restores saved on/off profile on boot (/etc/gpd-pocket-3-lowpower)  │
│    • Native Kernel Fan Control                                                          │
│      - Handled seamlessly by upstream Linux gpd-fan driver & hardware Fn toggle         │
│    • Multi-Machine Safe Guard                                                           │
│      - Validates i7-1195G7 & DMI strings (Pocket 3 / G1621-02); dormant on other PCs    │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 🛠️ Upstream Dependencies

* **[Linux Kernel `intel_rapl` / `powercap`](https://www.kernel.org/doc/html/latest/power/powercap/powercap.html)**: Native sysfs power capping interface for Tiger Lake-U PL1 (sustained) and PL2 (burst) wattage limits.
* **[Linux Kernel `intel_pstate`](https://www.kernel.org/doc/html/latest/admin-guide/pm/intel_pstate.html)**: Hardware-controlled P-States (HWP) and Energy Performance Preference (EPP) scaling.
* **[`gpd-fan-driver` (Cryolitia/gpd-fan-driver)](https://github.com/Cryolitia/gpd-fan-driver)**: The upstream Linux kernel driver repository (`drivers/hwmon/gpd-fan.c`) providing native EC fan monitoring and PWM speed controls.
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

---

## 💻 CLI Usage

Manage runtime performance profiles persistently without background daemon overhead:

```bash
# Check active wattage limits, Turbo status, EPP, and persistent state
gpd-pocket-3-i7-1195G7-lowpower status

# Activate Whisper-Quiet Mode (8W PL1 / 12W PL2, Turbo Off, EPP=power, max_perf=50%)
# (Persists across reboots via gpd-pocket-3-power.service)
gpd-pocket-3-i7-1195G7-lowpower on

# Restore Full Performance Mode (20W PL1 / 28W PL2, Turbo On up to 5.0GHz, EPP=balance_performance)
# (Persists across reboots via gpd-pocket-3-power.service)
gpd-pocket-3-i7-1195G7-lowpower off
```
*(Automatically prompts for `sudo` if run as an unprivileged user).*

---

## ⚙️ Technical Highlights

### 1. GNOME-Matched Dynamic Backlight Engine
The C watchdog queries `/sys/class/backlight/intel_backlight/max_brightness` dynamically at runtime and computes GNOME's exact 20-step curve:
* `min_brightness = ⌊max / 100⌋` (1% minimum clamp so the screen never blanks out at step 0)
* `step_size = ⌊(max - min) / 20⌋`
* `brightness(step) = min + (step × step_size)` for steps 0 through 20 (5% per step)
* Brightness hotkeys operate during the LUKS prompt and seamlessly restore brightness after S3 suspend/resume cycles.

### 2. Prompt-Gated Inactivity Timer & `fsck` Safety
* `is_user_prompt_active()` scans `/proc` for interactive passphrase query processes (`askpass`, `cryptsetup`, `systemd-ask-password`, `plymouth`, emergency login). Inactivity auto-dim (30s) and auto-suspend (60s) **only** operate while an interactive input query is active.
* Screen brightness automatically restores to user brightness the instant the prompt completes.
* `is_fsck_running()` identifies running disk repair tasks (`fsck`, `fsck.*`, `e2fsck`, `dosfsck`, `btrfsck`, `xfs_repair`) and prevents auto-dimming. Power button presses safely wait up to 60s for active disk checks to finish before poweroff.

### 3. Debian-Safe Guard Blocks & Remnant Cleanup
* Module configuration in `/etc/initramfs-tools/modules` is enclosed in Debian-safe guard blocks (`### BEGIN GPD Pocket 3 (Intel Core i7-1195G7) MODULES ... ###`), preserving custom user entries.
* Deprecated hook scripts and obsolete binary artifacts from older versions are automatically cleaned up during installation.

---

## 📂 File Layout

```text
/usr/local/bin/
├── gpd-pocket-3-i7-1195G7-lowpower     # CLI persistent RAPL & EPP power switcher
└── gpd-pocket-3-power-watchdog         # Compiled C early-boot micro-daemon

/usr/local/src/
└── gpd-pocket-3-power-watchdog.c       # Source code for the ramdisk C watchdog

/etc/systemd/system/
└── gpd-pocket-3-power.service          # Oneshot persistence service restoring profile on boot

/etc/initramfs-tools/
├── hooks/gpd_pocket_3_power            # Copies C watchdog into initrd cpio
├── scripts/init-top/gpd_pocket_3_power # Launches watchdog post-udev in ramdisk
└── scripts/init-bottom/gpd_pocket_3_power # Hands off to systemd upon rootfs mount
```

## 🤖 Note on the code and the tools used to write it
Parts of this code were written (including some initial ones that began in other, separate projects) with assistance from LLM-integrated coding tools. If you don't like it, feel free to use other software or rewrite parts you dislike. PRs are welcome!
