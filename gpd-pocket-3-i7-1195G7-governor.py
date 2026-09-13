#!/usr/bin/env python3
"""
GPD Pocket 3 (Intel Core i7-1195G7 Tiger Lake-U) Power Stack & Installer
-------------------------------------------------------------------------
Deploys:
  1. Early initramfs power watchdog (/usr/local/bin/gpd-pocket-3-power-watchdog)
     - Safe fsck-aware ACPI poweroff on power button press
     - GNOME-matching 20-step dynamic backlight scaling & auto-dim
     - S3/s2idle idle suspend & early-boot quiet thermal profile
  2. Hardware-tuned lowpower utility (/usr/local/bin/gpd-pocket-3-i7-1195G7-lowpower)
     - RAPL TDP clamp (8W PL1 / 12W PL2 vs 20W PL1 / 28W PL2)
     - Intel Speed Shift HWP Energy Performance Preference (EPP)
     - P-State frequency caps

Usage:
  sudo ./gpd-pocket-3-i7-1195G7-governor.py --install
"""

import os
import sys
import shutil
import subprocess

def ensure_root():
    """Auto-elevate to root using sudo if run by an unprivileged user."""
    if os.geteuid() != 0:
        try:
            args = ["sudo", sys.executable, os.path.abspath(__file__)] + sys.argv[1:]
            os.execvp("sudo", args)
        except Exception as e:
            print(f"[ERROR] Failed to auto-elevate with sudo: {e}")
            sys.exit(1)

def is_target_hardware():
    """
    Validates that the host CPU is a GPD Pocket 3 with Intel Core i7-1195G7.
    Also validates DMI identifiers (G1621-02 / Pocket 3) for multi-boot / portable drives.
    """
    try:
        with open("/proc/cpuinfo", "r") as f:
            cpuinfo = f.read()
        if any(sig in cpuinfo for sig in ["1195G7", "i7-1195G7"]):
            return True
    except Exception:
        pass

    try:
        with open("/sys/class/dmi/id/product_name", "r") as f:
            prod = f.read()
        if any(sig in prod for sig in ["Pocket 3", "G1621-02"]):
            return True
    except Exception:
        pass

    return False

C_WATCHDOG_PAYLOAD = '#include <stdio.h>\n#include <stdlib.h>\n#include <unistd.h>\n#include <fcntl.h>\n#include <poll.h>\n#include <signal.h>\n#include <string.h>\n#include <stdarg.h>\n#include <time.h>\n#include <glob.h>\n#include <errno.h>\n#include <dirent.h>\n#include <sys/types.h>\n#include <sys/stat.h>\n#include <sys/reboot.h>\n#include <sys/ioctl.h>\n#include <linux/input.h>\n\n#define DEFAULT_BACKLIGHT_PATH "/sys/class/backlight/intel_backlight"\n#define POWER_STATE_PATH "/sys/power/state"\n#define LOG_FILE "/run/gpd_pocket_3_power.log"\n#define TOTAL_STEPS 20 // 20 steps = 5% per step, matching GNOME\n\nstatic volatile sig_atomic_t keep_running = 1;\nstatic char active_backlight_path[256] = DEFAULT_BACKLIGHT_PATH;\nstatic int max_brightness = 100000;\nstatic int min_brightness = 1000;\nstatic int step_size = 4950;\nstatic int current_step = 5; // Step 5 = 25%\nstatic int user_brightness = 25750;\nstatic int log_fd = -1;\nstatic int kmsg_fd = -1;\n\nvoid log_init(void) {\n    log_fd = open(LOG_FILE, O_WRONLY | O_CREAT | O_TRUNC | O_SYNC, 0644);\n    kmsg_fd = open("/dev/kmsg", O_WRONLY);\n}\n\nvoid log_msg(const char *fmt, ...) {\n    char buf[512];\n    va_list args;\n    va_start(args, fmt);\n    vsnprintf(buf, sizeof(buf), fmt, args);\n    va_end(args);\n\n    if (log_fd >= 0) dprintf(log_fd, "[gpd-pocket-3-watchdog] %s\\n", buf);\n    if (kmsg_fd >= 0) dprintf(kmsg_fd, "<4>[gpd-pocket-3-watchdog] %s\\n", buf);\n}\n\nint is_target_hardware(void) {\n    // 1. Check CPU model\n    FILE *f = fopen("/proc/cpuinfo", "r");\n    if (f) {\n        char line[256];\n        while (fgets(line, sizeof(line), f)) {\n            if (strstr(line, "1195G7") || strstr(line, "i7-1195G7")) {\n                fclose(f);\n                return 1;\n            }\n        }\n        fclose(f);\n    }\n    // 2. Check DMI product name / board\n    FILE *dmi = fopen("/sys/class/dmi/id/product_name", "r");\n    if (dmi) {\n        char prod[128];\n        if (fgets(prod, sizeof(prod), dmi)) {\n            if (strstr(prod, "Pocket 3") || strstr(prod, "G1621-02")) {\n                fclose(dmi);\n                return 1;\n            }\n        }\n        fclose(dmi);\n    }\n    return 0;\n}\n\nvoid handle_signal(int sig) {\n    log_msg("Caught termination signal (%d). Handoff to rootfs / systemd.", sig);\n    keep_running = 0;\n}\n\nstatic int write_sysfs(const char *path, const char *val) {\n    int fd = open(path, O_WRONLY);\n    if (fd >= 0) {\n        ssize_t w = write(fd, val, strlen(val));\n        close(fd);\n        return (w > 0) ? 0 : -1;\n    }\n    return -1;\n}\n\nint calculate_brightness(int step) {\n    if (step <= 0) return min_brightness;\n    if (step >= TOTAL_STEPS) return max_brightness;\n    return min_brightness + (step * step_size);\n}\n\nvoid set_brightness(int val) {\n    char path[512];\n    snprintf(path, sizeof(path), "%s/brightness", active_backlight_path);\n    int fd = open(path, O_WRONLY);\n    if (fd >= 0) {\n        dprintf(fd, "%d\\n", val);\n        close(fd);\n        log_msg("Hardware brightness set to %d (step %d/%d)", val, current_step, TOTAL_STEPS);\n    }\n}\n\nvoid init_backlight(void) {\n    // Locate backlight sysfs directory\n    if (access(DEFAULT_BACKLIGHT_PATH "/max_brightness", R_OK) == 0) {\n        strncpy(active_backlight_path, DEFAULT_BACKLIGHT_PATH, sizeof(active_backlight_path) - 1);\n    } else {\n        glob_t g;\n        if (glob("/sys/class/backlight/*", 0, NULL, &g) == 0 && g.gl_pathc > 0) {\n            strncpy(active_backlight_path, g.gl_pathv[0], sizeof(active_backlight_path) - 1);\n            globfree(&g);\n        }\n    }\n\n    char path[512];\n    snprintf(path, sizeof(path), "%s/max_brightness", active_backlight_path);\n    FILE *f = fopen(path, "r");\n    if (f) {\n        if (fscanf(f, "%d", &max_brightness) == 1 && max_brightness > 0) {\n            // Match GNOME\'s formula: minimum is clamped to 1% of max (or 1)\n            min_brightness = (max_brightness >= 100) ? (max_brightness / 100) : 1;\n            step_size = (max_brightness - min_brightness) / TOTAL_STEPS;\n        }\n        fclose(f);\n    }\n\n    user_brightness = calculate_brightness(current_step);\n    log_msg("Backlight initialized on %s: Max=%d, Min=%d (1%%), StepSize=%d, Active=%d (Step %d/%d, %d%%)",\n            active_backlight_path, max_brightness, min_brightness, step_size, user_brightness, current_step, TOTAL_STEPS, current_step * 5);\n    set_brightness(user_brightness);\n}\n\nvoid brightness_step_up(void) {\n    if (current_step < TOTAL_STEPS) {\n        current_step++;\n        user_brightness = calculate_brightness(current_step);\n        log_msg("Hotkey UP -> %d (Step %d/%d, %d%%)",\n                user_brightness, current_step, TOTAL_STEPS, current_step * 5);\n    }\n    set_brightness(user_brightness);\n}\n\nvoid brightness_step_down(void) {\n    if (current_step > 0) {\n        current_step--;\n        user_brightness = calculate_brightness(current_step);\n        log_msg("Hotkey DOWN -> %d (Step %d/%d, %d%%)",\n                user_brightness, current_step, TOTAL_STEPS, current_step * 5);\n    }\n    set_brightness(user_brightness);\n}\n\nvoid apply_early_power_profile(void) {\n    log_msg("Applying GPD Pocket 3 early-boot quiet baseline...");\n    // Keep turbo disabled in initramfs to prevent fan spinup before desktop load\n    write_sysfs("/sys/devices/system/cpu/intel_pstate/no_turbo", "1\\n");\n    // Set Energy Performance Preference to balance_power\n    glob_t g;\n    if (glob("/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference", 0, NULL, &g) == 0) {\n        for (size_t i = 0; i < g.gl_pathc; i++) {\n            write_sysfs(g.gl_pathv[i], "balance_power\\n");\n        }\n        globfree(&g);\n    }\n    log_msg("Early baseline active (Tiger Lake-U quiet initramfs profile).");\n}\n\nvoid restore_full_performance(void) {\n    // Hand off clean state to systemd\n    write_sysfs("/sys/devices/system/cpu/intel_pstate/no_turbo", "0\\n");\n}\n\nstatic int is_fsck_running(void) {\n    DIR *d = opendir("/proc");\n    if (!d) return 0;\n    struct dirent *ent;\n    int running = 0;\n    pid_t my_pid = getpid();\n\n    while ((ent = readdir(d)) != NULL) {\n        if (ent->d_name[0] >= \'0\' && ent->d_name[0] <= \'9\') {\n            pid_t p = (pid_t)atoi(ent->d_name);\n            if (p == my_pid) continue;\n\n            char stat_path[512];\n            snprintf(stat_path, sizeof(stat_path), "/proc/%s/stat", ent->d_name);\n            FILE *f = fopen(stat_path, "r");\n            if (f) {\n                char buf[512];\n                if (fgets(buf, sizeof(buf), f)) {\n                    char *open_p = strchr(buf, \'(\');\n                    char *close_p = strrchr(buf, \')\');\n                    if (open_p && close_p && close_p > open_p) {\n                        *close_p = \'\\0\';\n                        char *comm = open_p + 1;\n                        char state = *(close_p + 2); // character right after ") "\n\n                        if (state != \'Z\' && state != \'X\') {\n                            if (strcmp(comm, "fsck") == 0 ||\n                                strncmp(comm, "fsck.", 5) == 0 ||\n                                strcmp(comm, "e2fsck") == 0 ||\n                                strcmp(comm, "dosfsck") == 0 ||\n                                strcmp(comm, "btrfsck") == 0 ||\n                                strcmp(comm, "xfs_repair") == 0) {\n                                running = 1;\n                                fclose(f);\n                                break;\n                            }\n                        }\n                    }\n                }\n                fclose(f);\n            }\n        }\n    }\n    closedir(d);\n    return running;\n}\n\nvoid power_off_immediate(void) {\n    if (is_fsck_running()) {\n        log_msg("Power button pressed while fsck is active! Deferring shutdown until fsck finishes...");\n        int wait_count = 0;\n        while (is_fsck_running() && wait_count < 600) { // wait up to 60s\n            usleep(100000);\n            wait_count++;\n        }\n        if (wait_count >= 600) {\n            log_msg("WARNING: fsck wait timeout exceeded (60s). Proceeding with poweroff.");\n        } else {\n            log_msg("fsck finished cleanly. Proceeding with poweroff.");\n        }\n    }\n\n    log_msg("CRITICAL: Power button confirmed! Blanking screen and issuing ACPI Poweroff...");\n    set_brightness(0);\n    sync();\n    reboot(RB_POWER_OFF);\n    exit(0);\n}\n\nint scan_inputs(struct pollfd *fds, int max_fds) {\n    for (int i = 0; i < max_fds; i++) {\n        if (fds[i].fd >= 0) {\n            close(fds[i].fd);\n            fds[i].fd = -1;\n        }\n    }\n    glob_t g;\n    int num_fds = 0;\n    if (glob("/dev/input/event*", 0, NULL, &g) == 0) {\n        for (size_t i = 0; i < g.gl_pathc && num_fds < max_fds; i++) {\n            int fd = open(g.gl_pathv[i], O_RDONLY | O_NONBLOCK);\n            if (fd >= 0) {\n                char name[128] = "Unknown Device";\n                ioctl(fd, EVIOCGNAME(sizeof(name)), name);\n                log_msg("  Discovered input: %s (%s)", g.gl_pathv[i], name);\n                fds[num_fds].fd = fd;\n                fds[num_fds].events = POLLIN;\n                num_fds++;\n            }\n        }\n        globfree(&g);\n    }\n    return num_fds;\n}\n\nint main(void) {\n    log_init();\n\n    if (!is_target_hardware()) {\n        log_msg("Non-Pocket 3 i7-1195G7 hardware detected. Bailing out cleanly with 0%% CPU.");\n        if (log_fd >= 0) close(log_fd);\n        if (kmsg_fd >= 0) close(kmsg_fd);\n        return 0;\n    }\n\n    log_msg("=== GPD Pocket 3 (i7-1195G7) Early Watchdog Active ===");\n\n    signal(SIGTERM, handle_signal);\n    signal(SIGINT, handle_signal);\n\n    apply_early_power_profile();\n\n    for (int i = 0; i < 15 && access(DEFAULT_BACKLIGHT_PATH "/brightness", W_OK) != 0; i++) {\n        usleep(200000);\n    }\n    init_backlight();\n\n    struct pollfd fds[32];\n    for (int i = 0; i < 32; i++) fds[i].fd = -1;\n    int num_fds = scan_inputs(fds, 32);\n\n    int idle_seconds = 0;\n    enum { STATE_NORMAL, STATE_DIM, STATE_SUSPEND } state = STATE_NORMAL;\n\n    while (keep_running) {\n        if (num_fds == 0) {\n            sleep(2);\n            num_fds = scan_inputs(fds, 32);\n            continue;\n        }\n\n        int ret = poll(fds, num_fds, 1000);\n\n        if (ret > 0) {\n            int user_activity = 0;\n            struct input_event ev;\n\n            for (int i = 0; i < num_fds; i++) {\n                if (fds[i].revents & POLLIN) {\n                    while (read(fds[i].fd, &ev, sizeof(ev)) == sizeof(ev)) {\n                        if (ev.type == EV_KEY && (ev.value == 1 || ev.value == 2)) {\n                            if (ev.code == KEY_POWER || ev.code == KEY_POWER2) {\n                                power_off_immediate();\n                            } else if (ev.code == KEY_BRIGHTNESSUP) {\n                                brightness_step_up();\n                                state = STATE_NORMAL;\n                                user_activity = 1;\n                                continue;\n                            } else if (ev.code == KEY_BRIGHTNESSDOWN) {\n                                brightness_step_down();\n                                state = STATE_NORMAL;\n                                user_activity = 1;\n                                continue;\n                            }\n                        }\n                        user_activity = 1;\n                    }\n                }\n            }\n\n            if (user_activity) {\n                idle_seconds = 0;\n                if (state != STATE_NORMAL) {\n                    log_msg("Activity detected: Restoring user brightness (%d).", user_brightness);\n                    set_brightness(user_brightness);\n                    state = STATE_NORMAL;\n                }\n            }\n        } else if (ret == 0) {\n            idle_seconds++;\n\n            if (idle_seconds >= 60) {\n                log_msg("60s idle reached. Suspending to RAM (S3/s2idle)...");\n                state = STATE_SUSPEND;\n                write_sysfs(POWER_STATE_PATH, "mem\\n");\n                log_msg("Resumed from suspend. Restoring user brightness (%d).", user_brightness);\n                set_brightness(user_brightness);\n                idle_seconds = 0;\n                state = STATE_NORMAL;\n            } else if (idle_seconds >= 30 && state == STATE_NORMAL) {\n                log_msg("30s idle reached. Dimming display to minimum (%d)...", min_brightness);\n                set_brightness(min_brightness);\n                state = STATE_DIM;\n            }\n        }\n    }\n\n    log_msg("Handoff to rootfs: Restoring full CPU performance for systemd.");\n    restore_full_performance();\n    set_brightness(user_brightness);\n    for (int i = 0; i < num_fds; i++) {\n        if (fds[i].fd >= 0) close(fds[i].fd);\n    }\n    if (log_fd >= 0) close(log_fd);\n    if (kmsg_fd >= 0) close(kmsg_fd);\n    return 0;\n}\n'

LOWPOWER_PAYLOAD = '#!/usr/bin/env python3\nimport sys\nimport os\nimport glob\nimport subprocess\n\ndef ensure_root():\n    if os.geteuid() != 0:\n        try:\n            args = ["sudo", sys.executable, os.path.abspath(__file__)] + sys.argv[1:]\n            os.execvp("sudo", args)\n        except Exception as e:\n            print(f"[ERROR] Failed to auto-elevate with sudo: {e}")\n            sys.exit(1)\n\ndef is_target_hardware():\n    try:\n        with open("/proc/cpuinfo", "r") as f:\n            c = f.read()\n            if any(k in c for k in ["1195G7", "i7-1195G7"]):\n                return True\n    except Exception:\n        pass\n    try:\n        with open("/sys/class/dmi/id/product_name", "r") as f:\n            p = f.read()\n            if any(k in p for k in ["Pocket 3", "G1621-02"]):\n                return True\n    except Exception:\n        pass\n    return False\n\nensure_root()\n\nif not is_target_hardware():\n    print("[ERROR] Strictly hardware-locked to GPD Pocket 3 (i7-1195G7).")\n    print("        Refusing execution on non-target hardware.")\n    sys.exit(1)\n\ndef write_file(path, val):\n    try:\n        if os.path.exists(path):\n            with open(path, "w") as f:\n                f.write(str(val).strip() + "\\n")\n            return True\n    except Exception:\n        pass\n    return False\n\ndef set_rapl_limits(pl1_watts, pl2_watts):\n    base = "/sys/class/powercap/intel-rapl/intel-rapl:0"\n    pl1_uw = int(pl1_watts * 1000000)\n    pl2_uw = int(pl2_watts * 1000000)\n    pl1_set = write_file(f"{base}/constraint_0_power_limit_uw", pl1_uw)\n    pl2_set = write_file(f"{base}/constraint_1_power_limit_uw", pl2_uw)\n    return pl1_set or pl2_set\n\ndef set_epp(mode):\n    paths = glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference")\n    for p in paths:\n        write_file(p, mode)\n\ndef set_turbo(enabled):\n    write_file("/sys/devices/system/cpu/intel_pstate/no_turbo", "0" if enabled else "1")\n\ndef set_pstate_max_perf(pct):\n    write_file("/sys/devices/system/cpu/intel_pstate/max_perf_pct", str(pct))\n\ndef show_status():\n    print("--- GPD Pocket 3 (i7-1195G7) Power Status ---")\n    no_turbo = "0"\n    try:\n        with open("/sys/devices/system/cpu/intel_pstate/no_turbo") as f:\n            no_turbo = f.read().strip()\n    except Exception:\n        pass\n    print(f" * Intel Turbo Boost: {\'Disabled\' if no_turbo == \'1\' else \'Enabled\'}")\n\n    epps = glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference")\n    if epps:\n        try:\n            with open(epps[0]) as f:\n                print(f" * Energy Perf Pref:  {f.read().strip()}")\n        except Exception:\n            pass\n\n    try:\n        with open("/sys/devices/system/cpu/intel_pstate/max_perf_pct") as f:\n            print(f" * Max Perf Pct:      {f.read().strip()}%")\n    except Exception:\n        pass\n\n    base = "/sys/class/powercap/intel-rapl/intel-rapl:0"\n    try:\n        with open(f"{base}/constraint_0_power_limit_uw") as f:\n            pl1 = int(f.read().strip()) / 1000000.0\n            print(f" * RAPL PL1 (Sustained): {pl1:.1f} W")\n    except Exception:\n        pass\n    try:\n        with open(f"{base}/constraint_1_power_limit_uw") as f:\n            pl2 = int(f.read().strip()) / 1000000.0\n            print(f" * RAPL PL2 (Burst):     {pl2:.1f} W")\n    except Exception:\n        pass\n\nif len(sys.argv) < 2 or sys.argv[1].lower() not in ["on", "off", "status"]:\n    print("Usage: gpd-pocket-3-i7-1195G7-lowpower [on|off|status]")\n    sys.exit(1)\n\naction = sys.argv[1].lower()\n\nif action == "status":\n    show_status()\n\nelif action == "on":\n    print("Activating Whisper-Quiet Low-Power Mode (GPD Pocket 3 i7-1195G7)...")\n    set_turbo(False)\n    set_epp("power")\n    set_pstate_max_perf(50)\n    set_rapl_limits(8.0, 12.0)\n    print("[SUCCESS] Power profile clamped:")\n    print("  * Intel Turbo: Disabled")\n    print("  * Speed Shift EPP: power")\n    print("  * Max Perf: 50% (~2.5 GHz ceiling)")\n    print("  * RAPL Powercap: 8W sustained (PL1) / 12W burst (PL2)")\n    print("  * Fan noise minimized; optimal for reading/coding on battery.")\n\nelif action == "off":\n    print("Restoring Full Performance Mode (GPD Pocket 3 i7-1195G7)...")\n    set_turbo(True)\n    set_epp("balance_performance")\n    set_pstate_max_perf(100)\n    set_rapl_limits(20.0, 28.0)\n    print("[SUCCESS] Full Performance profile active:")\n    print("  * Intel Turbo: Enabled (up to 5.0 GHz)")\n    print("  * Speed Shift EPP: balance_performance")\n    print("  * Max Perf: 100%")\n    print("  * RAPL Powercap: 20W sustained (PL1) / 28W burst (PL2)")\n'


def run_install():
    ensure_root()

    print("\n====================================================================")
    print(" GPD Pocket 3 (i7-1195G7) Power Watchdog & Low-Power Installer      ")
    print("====================================================================\n")

    if not is_target_hardware():
        print("[WARNING] Host hardware does NOT match GPD Pocket 3 (i7-1195G7).")
        print("          Proceeding with installation for portable USB multi-boot.")
        print("          (Hardware locks will keep binaries dormant on other PCs).\n")

    # Step 1: Check Toolchain
    print("[1/5] Verifying build toolchain...")
    pkgs = ["build-essential", "gcc", "make", "pkg-config"]
    needs_apt = any(shutil.which(p) is None for p in ["gcc", "make", "pkg-config"])
    if needs_apt:
        print("  Installing missing build packages via apt...")
        subprocess.run(["apt-get", "update", "-qq"], check=True)
        subprocess.run(["apt-get", "install", "-y", "-qq"] + pkgs, check=True)
    else:
        print("  [OK] Build toolchain already present.")

    # Step 2: Compile Early Power Watchdog C binary
    print("[2/5] Compiling namespaced early power watchdog C micro-daemon...")
    os.makedirs("/usr/local/src", exist_ok=True)
    c_src = "/usr/local/src/gpd-pocket-3-power-watchdog.c"
    with open(c_src, "w") as f:
        f.write(C_WATCHDOG_PAYLOAD)

    bin_target = "/usr/local/bin/gpd-pocket-3-power-watchdog"
    subprocess.run(["gcc", "-O2", c_src, "-o", bin_target], check=True)
    os.chmod(bin_target, 0o755)
    print(f"  [OK] {bin_target} compiled successfully.")

    # Step 3: Install Namespaced Initramfs Hooks
    print("[3/5] Installing Initramfs early power management hooks...")
    
    # 3a. Binary Hook
    hook_path = "/etc/initramfs-tools/hooks/gpd_pocket_3_power"
    with open(hook_path, "w") as f:
        f.write("#!/bin/sh\nPREREQ=\"\"\nprereqs() { echo \"$PREREQ\"; }\ncase \"$1\" in prereqs) prereqs; exit 0;; esac\n. /usr/share/initramfs-tools/hook-functions\nif [ -f /usr/local/bin/gpd-pocket-3-power-watchdog ]; then\n    copy_exec /usr/local/bin/gpd-pocket-3-power-watchdog /bin\nfi\nexit 0\n")
    os.chmod(hook_path, 0o755)

    # 3b. Init-top Script
    top_path = "/etc/initramfs-tools/scripts/init-top/gpd_pocket_3_power"
    with open(top_path, "w") as f:
        f.write("#!/bin/sh\nPREREQ=\"udev\"\nprereqs() { echo \"$PREREQ\"; }\ncase \"$1\" in prereqs) prereqs; exit 0;; esac\n. /scripts/functions\nif [ -x /bin/gpd-pocket-3-power-watchdog ]; then\n    /bin/gpd-pocket-3-power-watchdog &\n    echo \"$!\" > /run/gpd_pocket_3_watchdog.pid\nfi\n")
    os.chmod(top_path, 0o755)

    # 3c. Init-bottom Script
    bottom_path = "/etc/initramfs-tools/scripts/init-bottom/gpd_pocket_3_power"
    with open(bottom_path, "w") as f:
        f.write("#!/bin/sh\nPREREQ=\"\"\nprereqs() { echo \"$PREREQ\"; }\ncase \"$1\" in prereqs) prereqs; exit 0;; esac\n. /scripts/functions\nPIDFILE=\"/run/gpd_pocket_3_watchdog.pid\"\nif [ -f \"$PIDFILE\" ]; then\n    PID=$(cat \"$PIDFILE\")\n    if [ -n \"$PID\" ]; then\n        kill -TERM \"$PID\" 2>/dev/null || true\n    fi\n    rm -f \"$PIDFILE\"\nfi\n")
    os.chmod(bottom_path, 0o755)

    # Ensure initramfs modules
    required_modules = ["i915", "button", "i8042", "evdev", "intel_lpss_pci", "coretemp"]
    modules_file = "/etc/initramfs-tools/modules"
    existing = ""
    if os.path.exists(modules_file):
        with open(modules_file, "r") as f:
            existing = f.read()
    with open(modules_file, "a") as f:
        for mod in required_modules:
            if mod not in existing:
                f.write(f"{mod}\n")

    # Step 4: Rebuild Ramdisk
    print("[4/5] Rebuilding Initramfs...")
    subprocess.run(["update-initramfs", "-u"], check=True)

    # Step 5: Install Low-Power CLI Utility
    print("[5/5] Installing /usr/local/bin/gpd-pocket-3-i7-1195G7-lowpower...")
    lp_path = "/usr/local/bin/gpd-pocket-3-i7-1195G7-lowpower"
    with open(lp_path, "w") as f:
        f.write(LOWPOWER_PAYLOAD)
    os.chmod(lp_path, 0o755)

    print("\n[SUCCESS] GPD Pocket 3 (i7-1195G7) Power Stack Installed!")
    print("--------------------------------------------------------------------")
    print(" * Watchdog Binary:   /usr/local/bin/gpd-pocket-3-power-watchdog")
    print(" * Initramfs Hook:    /etc/initramfs-tools/hooks/gpd_pocket_3_power")
    print(" * Initramfs Top:     /etc/initramfs-tools/scripts/init-top/gpd_pocket_3_power")
    print(" * Initramfs Bottom:  /etc/initramfs-tools/scripts/init-bottom/gpd_pocket_3_power")
    print(" * Low-Power Toggle:  /usr/local/bin/gpd-pocket-3-i7-1195G7-lowpower [on|off|status]")
    print(" * Backlight Engine:  GNOME-matching 20-step dynamic curve (1% to 100%)")
    print(" * fsck Safety:       Auto-defers poweroff until active disk repair finishes")
    print("--------------------------------------------------------------------\n")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--install":
        run_install()
    else:
        print(__doc__)
        print("Usage: sudo ./gpd-pocket-3-i7-1195G7-governor.py --install")
