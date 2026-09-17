#!/usr/bin/env python3
"""
GPD Pocket 3 (Intel Core i7-1195G7 Tiger Lake-U) Power Stack & Installer
-------------------------------------------------------------------------
Script: gpd-pocket-3-i7-1195G7-governor.py
Hardware: GPD Pocket 3 (Intel Core i7-1195G7 Tiger Lake-U Flagship Edition)

Deploys:
  1. Early initramfs power watchdog (/usr/local/bin/gpd-pocket-3-power-watchdog)
     - Safe fsck-aware ACPI poweroff on power button press
     - GNOME-matching 20-step dynamic backlight scaling & auto-dim
     - Prompt-gated inactivity timer (dims/suspends only during active prompts)
     - Early-boot quiet thermal profile (Turbo disabled, EPP balance_power)
  2. Hardware-tuned lowpower utility (/usr/local/bin/gpd-pocket-3-i7-1195G7-lowpower)
     - Persistent power mode across reboots (/etc/gpd-pocket-3-lowpower.state)
     - RAPL TDP clamp (8W PL1 / 12W PL2 vs 20W PL1 / 28W PL2)
     - Intel Speed Shift HWP Energy Performance Preference (EPP)
     - P-State frequency caps
  3. Systemd persistence service (/etc/systemd/system/gpd-pocket-3-power.service)

Usage:
  sudo ./gpd-pocket-3-i7-1195G7-governor.py --install
"""

import os
import sys
import re
import shutil
import subprocess

SCRIPT_NAME = "gpd-pocket-3-i7-1195G7-governor.py"
MODEL_NAME = "GPD Pocket 3 (Intel Core i7-1195G7)"
STATE_FILE = "/etc/gpd-pocket-3-lowpower.state"


def ensure_root():
    """Auto-elevate to root using sudo if run by an unprivileged user."""
    if os.geteuid() != 0:
        try:
            args = ["sudo", sys.executable, os.path.abspath(__file__)] + sys.argv[1:]
            os.execvp("sudo", args)
        except Exception as e:
            print(f"[ERROR] [{SCRIPT_NAME}] Failed to auto-elevate with sudo: {e}")
            sys.exit(1)


def is_target_hardware():
    """
    Validates that the host CPU is specifically a GPD Pocket 3 with Intel Core i7-1195G7.
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


C_WATCHDOG_PAYLOAD = r"""/*
 * GPD Pocket 3 (Intel Core i7-1195G7 Tiger Lake-U) Early Power Watchdog
 * Installed and managed by gpd-pocket-3-i7-1195G7-governor.py
 */

#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <string.h>
#include <stdarg.h>
#include <time.h>
#include <glob.h>
#include <errno.h>
#include <dirent.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <sys/reboot.h>
#include <sys/ioctl.h>
#include <linux/input.h>

#define DEFAULT_BACKLIGHT_PATH "/sys/class/backlight/intel_backlight"
#define POWER_STATE_PATH "/sys/power/state"
#define LOG_FILE "/run/gpd_pocket_3_power.log"
#define TOTAL_STEPS 20 // 20 steps = 5% per step, matching GNOME

static volatile sig_atomic_t keep_running = 1;
static char active_backlight_path[256] = DEFAULT_BACKLIGHT_PATH;
static int max_brightness = 100000;
static int min_brightness = 1000;
static int step_size = 4950;
static int current_step = 5; // Step 5 = 25%
static int user_brightness = 25750;
static int log_fd = -1;
static int kmsg_fd = -1;

void log_init(void) {
    log_fd = open(LOG_FILE, O_WRONLY | O_CREAT | O_TRUNC | O_SYNC, 0644);
    kmsg_fd = open("/dev/kmsg", O_WRONLY);
}

void log_msg(const char *fmt, ...) {
    char buf[512];
    va_list args;
    va_start(args, fmt);
    vsnprintf(buf, sizeof(buf), fmt, args);
    va_end(args);

    if (log_fd >= 0) dprintf(log_fd, "[gpd-pocket-3-watchdog] %s\n", buf);
    if (kmsg_fd >= 0) dprintf(kmsg_fd, "<4>[gpd-pocket-3-watchdog] %s\n", buf);
}

int is_target_hardware(void) {
    // 1. Check CPU model specifically for i7-1195G7
    FILE *f = fopen("/proc/cpuinfo", "r");
    if (f) {
        char line[256];
        while (fgets(line, sizeof(line), f)) {
            if (strstr(line, "1195G7") || strstr(line, "i7-1195G7")) {
                fclose(f);
                return 1;
            }
        }
        fclose(f);
    }
    // 2. Check DMI product name / board
    FILE *dmi = fopen("/sys/class/dmi/id/product_name", "r");
    if (dmi) {
        char prod[128];
        if (fgets(prod, sizeof(prod), dmi)) {
            if (strstr(prod, "Pocket 3") || strstr(prod, "G1621-02")) {
                fclose(dmi);
                return 1;
            }
        }
        fclose(dmi);
    }
    return 0;
}

void handle_signal(int sig) {
    log_msg("Caught termination signal (%d). Handoff to rootfs / systemd.", sig);
    keep_running = 0;
}

static int write_sysfs(const char *path, const char *val) {
    int fd = open(path, O_WRONLY);
    if (fd >= 0) {
        ssize_t w = write(fd, val, strlen(val));
        close(fd);
        return (w > 0) ? 0 : -1;
    }
    return -1;
}

int calculate_brightness(int step) {
    if (step <= 0) return min_brightness;
    if (step >= TOTAL_STEPS) return max_brightness;
    return min_brightness + (step * step_size);
}

void set_brightness(int val) {
    char path[512];
    snprintf(path, sizeof(path), "%s/brightness", active_backlight_path);
    int fd = open(path, O_WRONLY);
    if (fd >= 0) {
        dprintf(fd, "%d\n", val);
        close(fd);
        log_msg("Hardware brightness set to %d (step %d/%d)", val, current_step, TOTAL_STEPS);
    }
}

void init_backlight(void) {
    if (access(DEFAULT_BACKLIGHT_PATH "/max_brightness", R_OK) == 0) {
        strncpy(active_backlight_path, DEFAULT_BACKLIGHT_PATH, sizeof(active_backlight_path) - 1);
    } else {
        glob_t g;
        if (glob("/sys/class/backlight/*", 0, NULL, &g) == 0 && g.gl_pathc > 0) {
            strncpy(active_backlight_path, g.gl_pathv[0], sizeof(active_backlight_path) - 1);
            globfree(&g);
        }
    }

    char path[512];
    snprintf(path, sizeof(path), "%s/max_brightness", active_backlight_path);
    FILE *f = fopen(path, "r");
    if (f) {
        if (fscanf(f, "%d", &max_brightness) == 1 && max_brightness > 0) {
            min_brightness = (max_brightness >= 100) ? (max_brightness / 100) : 1;
            step_size = (max_brightness - min_brightness) / TOTAL_STEPS;
        }
        fclose(f);
    }

    user_brightness = calculate_brightness(current_step);
    log_msg("Backlight initialized on %s: Max=%d, Min=%d (1%%), StepSize=%d, Active=%d (Step %d/%d, %d%%)",
            active_backlight_path, max_brightness, min_brightness, step_size, user_brightness, current_step, TOTAL_STEPS, current_step * 5);
    set_brightness(user_brightness);
}

void brightness_step_up(void) {
    if (current_step < TOTAL_STEPS) {
        current_step++;
        user_brightness = calculate_brightness(current_step);
        log_msg("Hotkey UP -> %d (Step %d/%d, %d%%)",
                user_brightness, current_step, TOTAL_STEPS, current_step * 5);
    }
    set_brightness(user_brightness);
}

void brightness_step_down(void) {
    if (current_step > 0) {
        current_step--;
        user_brightness = calculate_brightness(current_step);
        log_msg("Hotkey DOWN -> %d (Step %d/%d, %d%%)",
                user_brightness, current_step, TOTAL_STEPS, current_step * 5);
    }
    set_brightness(user_brightness);
}

void apply_early_power_profile(void) {
    log_msg("Applying GPD Pocket 3 (i7-1195G7) early-boot quiet baseline...");
    write_sysfs("/sys/devices/system/cpu/intel_pstate/no_turbo", "1\n");
    glob_t g;
    if (glob("/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference", 0, NULL, &g) == 0) {
        for (size_t i = 0; i < g.gl_pathc; i++) {
            write_sysfs(g.gl_pathv[i], "balance_power\n");
        }
        globfree(&g);
    }
    log_msg("Early baseline active (Tiger Lake-U i7-1195G7 quiet initramfs profile).");
}

void restore_full_performance(void) {
    write_sysfs("/sys/devices/system/cpu/intel_pstate/no_turbo", "0\n");
}

static int is_fsck_running(void) {
    DIR *d = opendir("/proc");
    if (!d) return 0;
    struct dirent *ent;
    int running = 0;
    pid_t my_pid = getpid();

    while ((ent = readdir(d)) != NULL) {
        if (ent->d_name[0] >= '0' && ent->d_name[0] <= '9') {
            pid_t p = (pid_t)atoi(ent->d_name);
            if (p == my_pid) continue;

            char comm_path[512];
            snprintf(comm_path, sizeof(comm_path), "/proc/%s/comm", ent->d_name);
            FILE *f = fopen(comm_path, "r");
            if (f) {
                char comm[128];
                if (fgets(comm, sizeof(comm), f)) {
                    comm[strcspn(comm, "\r\n")] = 0;
                    if (strcmp(comm, "fsck") == 0 ||
                        strncmp(comm, "fsck.", 5) == 0 ||
                        strcmp(comm, "e2fsck") == 0 ||
                        strcmp(comm, "dosfsck") == 0 ||
                        strcmp(comm, "btrfsck") == 0 ||
                        strcmp(comm, "xfs_repair") == 0) {
                        running = 1;
                        fclose(f);
                        break;
                    }
                }
                fclose(f);
            }
        }
    }
    closedir(d);
    return running;
}

static int is_user_prompt_active(void) {
    if (is_fsck_running()) return 0;

    DIR *sd = opendir("/run/systemd/ask-password");
    if (sd) {
        struct dirent *ent;
        while ((ent = readdir(sd)) != NULL) {
            if (strncmp(ent->d_name, "ask.", 4) == 0) {
                closedir(sd);
                return 1;
            }
        }
        closedir(sd);
    }

    DIR *d = opendir("/proc");
    if (!d) return 0;
    struct dirent *ent;
    int prompt_found = 0;
    pid_t my_pid = getpid();

    while ((ent = readdir(d)) != NULL) {
        if (ent->d_name[0] >= '0' && ent->d_name[0] <= '9') {
            pid_t p = (pid_t)atoi(ent->d_name);
            if (p == my_pid) continue;

            char comm_path[512];
            snprintf(comm_path, sizeof(comm_path), "/proc/%s/comm", ent->d_name);
            FILE *f = fopen(comm_path, "r");
            if (f) {
                char comm[128];
                if (fgets(comm, sizeof(comm), f)) {
                    comm[strcspn(comm, "\r\n")] = 0;
                    if (strcmp(comm, "askpass") == 0 ||
                        strcmp(comm, "cryptsetup") == 0 ||
                        strcmp(comm, "passprompt") == 0 ||
                        strncmp(comm, "systemd-ask-", 12) == 0 ||
                        strncmp(comm, "systemd-tty-ask", 15) == 0 ||
                        strcmp(comm, "sulogin") == 0 ||
                        strcmp(comm, "login") == 0 ||
                        strcmp(comm, "agetty") == 0 ||
                        strcmp(comm, "getty") == 0 ||
                        strcmp(comm, "whiptail") == 0 ||
                        strcmp(comm, "dialog") == 0) {
                        prompt_found = 1;
                        fclose(f);
                        break;
                    }
                    if (strcmp(comm, "plymouth") == 0) {
                        char cmd_path[512];
                        snprintf(cmd_path, sizeof(cmd_path), "/proc/%s/cmdline", ent->d_name);
                        FILE *cf = fopen(cmd_path, "r");
                        if (cf) {
                            char cmd[256];
                            size_t n = fread(cmd, 1, sizeof(cmd) - 1, cf);
                            cmd[n] = 0;
                            for (size_t i = 0; i < n; i++) {
                                if (strstr(cmd + i, "ask-for-password") ||
                                    strstr(cmd + i, "watch-keystroke") ||
                                    strstr(cmd + i, "ask-question")) {
                                    prompt_found = 1;
                                    break;
                                }
                            }
                            fclose(cf);
                            if (prompt_found) { fclose(f); break; }
                        }
                    }
                }
                fclose(f);
            }
        }
    }
    closedir(d);
    return prompt_found;
}

void power_off_immediate(void) {
    if (is_fsck_running()) {
        log_msg("Power button pressed while fsck is active! Deferring shutdown until fsck finishes...");
        int wait_count = 0;
        while (is_fsck_running() && wait_count < 600) {
            usleep(100000);
            wait_count++;
        }
        if (wait_count >= 600) {
            log_msg("WARNING: fsck wait timeout exceeded (60s). Proceeding with poweroff.");
        } else {
            log_msg("fsck finished cleanly. Proceeding with poweroff.");
        }
    }

    log_msg("CRITICAL: Power button confirmed! Blanking screen and issuing ACPI Poweroff...");
    set_brightness(0);
    sync();
    reboot(RB_POWER_OFF);
    exit(0);
}

int scan_inputs(struct pollfd *fds, int max_fds) {
    for (int i = 0; i < max_fds; i++) {
        if (fds[i].fd >= 0) {
            close(fds[i].fd);
            fds[i].fd = -1;
        }
    }
    glob_t g;
    int num_fds = 0;
    if (glob("/dev/input/event*", 0, NULL, &g) == 0) {
        for (size_t i = 0; i < g.gl_pathc && num_fds < max_fds; i++) {
            int fd = open(g.gl_pathv[i], O_RDONLY | O_NONBLOCK);
            if (fd >= 0) {
                char name[128] = "Unknown Device";
                ioctl(fd, EVIOCGNAME(sizeof(name)), name);
                log_msg("  Discovered input: %s (%s)", g.gl_pathv[i], name);
                fds[num_fds].fd = fd;
                fds[num_fds].events = POLLIN;
                num_fds++;
            }
        }
        globfree(&g);
    }
    return num_fds;
}

int main(void) {
    log_init();

    if (!is_target_hardware()) {
        log_msg("Non-Pocket 3 i7-1195G7 hardware detected. Bailing out cleanly with 0%% CPU.");
        if (log_fd >= 0) close(log_fd);
        if (kmsg_fd >= 0) close(kmsg_fd);
        return 0;
    }

    log_msg("=== GPD Pocket 3 (i7-1195G7) Early Watchdog Active (managed by gpd-pocket-3-i7-1195G7-governor.py) ===");

    signal(SIGTERM, handle_signal);
    signal(SIGINT, handle_signal);

    apply_early_power_profile();

    for (int i = 0; i < 15 && access(DEFAULT_BACKLIGHT_PATH "/brightness", W_OK) != 0; i++) {
        usleep(200000);
    }
    init_backlight();

    struct pollfd fds[32];
    for (int i = 0; i < 32; i++) fds[i].fd = -1;
    int num_fds = scan_inputs(fds, 32);

    int idle_seconds = 0;
    enum { STATE_NORMAL, STATE_DIM, STATE_SUSPEND } state = STATE_NORMAL;

    while (keep_running) {
        if (num_fds == 0) {
            sleep(2);
            num_fds = scan_inputs(fds, 32);
            continue;
        }

        int ret = poll(fds, num_fds, 1000);

        if (ret > 0) {
            int user_activity = 0;
            struct input_event ev;

            for (int i = 0; i < num_fds; i++) {
                if (fds[i].revents & POLLIN) {
                    while (read(fds[i].fd, &ev, sizeof(ev)) == sizeof(ev)) {
                        if (ev.type == EV_KEY && (ev.value == 1 || ev.value == 2)) {
                            if (ev.code == KEY_POWER || ev.code == KEY_POWER2) {
                                power_off_immediate();
                            } else if (ev.code == KEY_BRIGHTNESSUP) {
                                brightness_step_up();
                                state = STATE_NORMAL;
                                user_activity = 1;
                                continue;
                            } else if (ev.code == KEY_BRIGHTNESSDOWN) {
                                brightness_step_down();
                                state = STATE_NORMAL;
                                user_activity = 1;
                                continue;
                            }
                        }
                        user_activity = 1;
                    }
                }
            }

            if (user_activity) {
                idle_seconds = 0;
                if (state != STATE_NORMAL) {
                    log_msg("Activity detected: Restoring user brightness (%d).", user_brightness);
                    set_brightness(user_brightness);
                    state = STATE_NORMAL;
                }
            }
        } else if (ret == 0) {
            int prompt_active = is_user_prompt_active();
            if (prompt_active) {
                idle_seconds++;

                if (idle_seconds >= 60) {
                    log_msg("60s idle reached during user prompt. Suspending to RAM (S3/s2idle)...");
                    state = STATE_SUSPEND;
                    write_sysfs(POWER_STATE_PATH, "mem\n");
                    log_msg("Resumed from suspend. Restoring user brightness (%d).", user_brightness);
                    set_brightness(user_brightness);
                    idle_seconds = 0;
                    state = STATE_NORMAL;
                } else if (idle_seconds >= 30 && state == STATE_NORMAL) {
                    log_msg("30s idle reached during user prompt. Dimming display to minimum (%d)...", min_brightness);
                    set_brightness(min_brightness);
                    state = STATE_DIM;
                }
            } else {
                idle_seconds = 0;
                if (state != STATE_NORMAL) {
                    log_msg("Prompt concluded or inactive: Restoring normal brightness (%d).", user_brightness);
                    set_brightness(user_brightness);
                    state = STATE_NORMAL;
                }
            }
        }
    }

    log_msg("Handoff to rootfs: Restoring full CPU performance for systemd.");
    restore_full_performance();
    set_brightness(user_brightness);
    for (int i = 0; i < num_fds; i++) {
        if (fds[i].fd >= 0) close(fds[i].fd);
    }
    if (log_fd >= 0) close(log_fd);
    if (kmsg_fd >= 0) close(kmsg_fd);
    return 0;
}
"""

LOWPOWER_PAYLOAD = r"""#!/usr/bin/env python3
# GPD Pocket 3 (Intel Core i7-1195G7 Tiger Lake-U) Low-Power Control Utility
# Installed and managed by gpd-pocket-3-i7-1195G7-governor.py

import sys
import os
import glob
import subprocess

STATE_FILE = "/etc/gpd-pocket-3-lowpower.state"
SCRIPT_PARENT = "gpd-pocket-3-i7-1195G7-governor.py"
HARDWARE_MODEL = "GPD Pocket 3 (Intel Core i7-1195G7)"

def ensure_root():
    if os.geteuid() != 0:
        try:
            args = ["sudo", sys.executable, os.path.abspath(__file__)] + sys.argv[1:]
            os.execvp("sudo", args)
        except Exception as e:
            print(f"[ERROR] [gpd-pocket-3-i7-1195G7-lowpower] Failed to auto-elevate with sudo: {e}")
            sys.exit(1)

def is_target_hardware():
    try:
        with open("/proc/cpuinfo", "r") as f:
            c = f.read()
            if any(k in c for k in ["1195G7", "i7-1195G7"]):
                return True
    except Exception:
        pass
    try:
        with open("/sys/class/dmi/id/product_name", "r") as f:
            p = f.read()
            if any(k in p for k in ["Pocket 3", "G1621-02"]):
                return True
    except Exception:
        pass
    return False

ensure_root()

if not is_target_hardware():
    print(f"[ERROR] Strictly hardware-locked to {HARDWARE_MODEL}.")
    print("        Refusing execution on non-target hardware.")
    sys.exit(1)

def write_file(path, val):
    try:
        if os.path.exists(path):
            with open(path, "w") as f:
                f.write(str(val).strip() + "\n")
            return True
    except Exception:
        pass
    return False

def set_rapl_limits(pl1_watts, pl2_watts):
    base = "/sys/class/powercap/intel-rapl/intel-rapl:0"
    pl1_uw = int(pl1_watts * 1000000)
    pl2_uw = int(pl2_watts * 1000000)
    pl1_set = write_file(f"{base}/constraint_0_power_limit_uw", pl1_uw)
    pl2_set = write_file(f"{base}/constraint_1_power_limit_uw", pl2_uw)
    return pl1_set or pl2_set

def set_epp(mode):
    paths = glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference")
    for p in paths:
        write_file(p, mode)

def set_turbo(enabled):
    write_file("/sys/devices/system/cpu/intel_pstate/no_turbo", "0" if enabled else "1")

def set_pstate_max_perf(pct):
    write_file("/sys/devices/system/cpu/intel_pstate/max_perf_pct", str(pct))

def apply_lowpower_profile():
    set_turbo(False)
    set_epp("power")
    set_pstate_max_perf(50)
    set_rapl_limits(8.0, 12.0)

def apply_fullperf_profile():
    set_turbo(True)
    set_epp("balance_performance")
    set_pstate_max_perf(100)
    set_rapl_limits(20.0, 28.0)

def show_status():
    print(f"--- {HARDWARE_MODEL} Power Status (Managed by {SCRIPT_PARENT}) ---")
    persisted = "off"
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                persisted = f.read().strip().lower()
    except Exception:
        pass
    print(f" * Persistent Low-Power State: {'Enabled (ON)' if persisted == 'on' else 'Disabled (OFF)'}")

    no_turbo = "0"
    try:
        with open("/sys/devices/system/cpu/intel_pstate/no_turbo") as f:
            no_turbo = f.read().strip()
    except Exception:
        pass
    print(f" * Intel Turbo Boost:           {'Disabled' if no_turbo == '1' else 'Enabled'}")

    epps = glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference")
    if epps:
        try:
            with open(epps[0]) as f:
                print(f" * Energy Perf Preference:      {f.read().strip()}")
        except Exception:
            pass

    try:
        with open("/sys/devices/system/cpu/intel_pstate/max_perf_pct") as f:
            print(f" * Max Performance Pct:         {f.read().strip()}%")
    except Exception:
        pass

    base = "/sys/class/powercap/intel-rapl/intel-rapl:0"
    try:
        with open(f"{base}/constraint_0_power_limit_uw") as f:
            pl1 = int(f.read().strip()) / 1000000.0
            print(f" * RAPL PL1 (Sustained):        {pl1:.1f} W")
    except Exception:
        pass
    try:
        with open(f"{base}/constraint_1_power_limit_uw") as f:
            pl2 = int(f.read().strip()) / 1000000.0
            print(f" * RAPL PL2 (Burst):            {pl2:.1f} W")
    except Exception:
        pass

if len(sys.argv) < 2 or sys.argv[1].lower() not in ["on", "off", "status", "apply-saved"]:
    print(f"Usage: gpd-pocket-3-i7-1195G7-lowpower [on|off|status|apply-saved] (Managed by {SCRIPT_PARENT} for {HARDWARE_MODEL})")
    sys.exit(1)

action = sys.argv[1].lower()

if action == "status":
    show_status()

elif action == "apply-saved":
    persisted = "off"
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                persisted = f.read().strip().lower()
    except Exception:
        pass

    if persisted == "on":
        apply_lowpower_profile()
        print(f"[{HARDWARE_MODEL}] Restored low-power profile on boot (8W/12W, EPP=power, max_perf=50%).")
    else:
        apply_fullperf_profile()
        print(f"[{HARDWARE_MODEL}] Restored full-performance profile on boot (20W/28W, EPP=balance_performance).")

elif action == "on":
    print(f"Activating Whisper-Quiet Low-Power Mode ({HARDWARE_MODEL}, managed by {SCRIPT_PARENT})...")
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            f.write("on\n")
    except Exception as e:
        print(f"[WARNING] Could not write persistent state to {STATE_FILE}: {e}")

    apply_lowpower_profile()
    print("[SUCCESS] Power profile clamped:")
    print("  * Intel Turbo: Disabled")
    print("  * Speed Shift EPP: power")
    print("  * Max Perf: 50% (~2.5 GHz ceiling)")
    print("  * RAPL Powercap: 8W sustained (PL1) / 12W burst (PL2)")
    print("  * State saved: Low-power mode will persist across reboots.")

elif action == "off":
    print(f"Restoring Full Performance Mode ({HARDWARE_MODEL}, managed by {SCRIPT_PARENT})...")
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            f.write("off\n")
    except Exception as e:
        print(f"[WARNING] Could not write persistent state to {STATE_FILE}: {e}")

    apply_fullperf_profile()
    print("[SUCCESS] Full Performance profile active:")
    print("  * Intel Turbo: Enabled (up to 5.0 GHz)")
    print("  * Speed Shift EPP: balance_performance")
    print("  * Max Perf: 100%")
    print("  * RAPL Powercap: 20W sustained (PL1) / 28W burst (PL2)")
    print("  * State saved: Full performance mode will persist across reboots.")
"""

GPD_POCKET3_SERVICE = """# GPD Pocket 3 (Intel Core i7-1195G7) Power Profile Persistence Unit
# Installed and managed by gpd-pocket-3-i7-1195G7-governor.py
[Unit]
Description=GPD Pocket 3 (Intel Core i7-1195G7) Power Profile Persistence (managed by gpd-pocket-3-i7-1195G7-governor.py)
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/gpd-pocket-3-i7-1195G7-lowpower apply-saved
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""


# =====================================================================
# CONFIGURATION GUARD BLOCK HELPER (Debian-Safe)
# =====================================================================
def update_guarded_config(file_path, block_tag, lines_to_set):
    """Updates a guarded block in a shared config file without clobbering other lines."""
    begin_mark = f"### BEGIN {block_tag} (managed by {SCRIPT_NAME}) ###"
    end_mark = f"### END {block_tag} (managed by {SCRIPT_NAME}) ###"
    block_content = f"{begin_mark}\n" + "\n".join(lines_to_set) + f"\n{end_mark}\n"

    existing = ""
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            existing = f.read()

    pattern = re.compile(
        rf"### BEGIN {re.escape(block_tag)}.*?###\n.*?### END {re.escape(block_tag)}.*?###\n?",
        re.DOTALL,
    )
    if pattern.search(existing):
        updated = pattern.sub(block_content, existing)
    else:
        if existing and not existing.endswith("\n"):
            existing += "\n"
        updated = existing + block_content

    os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
    with open(file_path, "w") as f:
        f.write(updated)


def cleanup_obsolete_remnants():
    """Removes obsolete files, deprecated hook names, and stale artifacts from older versions."""
    obsolete_paths = [
        # Deprecated initramfs hook names
        "/etc/initramfs-tools/hooks/gpd-pocket-3-power",
        "/etc/initramfs-tools/hooks/gpd-pocket-3-governor",
        "/etc/initramfs-tools/hooks/early_power_watchdog",
        "/etc/initramfs-tools/scripts/init-top/gpd-pocket-3-power",
        "/etc/initramfs-tools/scripts/init-top/gpd-pocket-3-governor",
        "/etc/initramfs-tools/scripts/init-top/early_power_watchdog",
        "/etc/initramfs-tools/scripts/init-bottom/gpd-pocket-3-power",
        "/etc/initramfs-tools/scripts/init-bottom/gpd-pocket-3-governor",
        "/etc/initramfs-tools/scripts/init-bottom/early_power_watchdog",
        # Deprecated binary / source locations
        "/usr/local/bin/early_power_watchdog",
        "/usr/local/bin/gpd_pocket_3_power_watchdog",
        "/usr/local/src/early_power_watchdog.c",
        # Stale runtime pidfiles
        "/run/early_power_watchdog.pid",
        "/run/gpd_pocket_3_watchdog.pid",
    ]
    for path in obsolete_paths:
        try:
            if os.path.islink(path) or os.path.isfile(path):
                os.remove(path)
                print(f"  Removed obsolete artifact: {path}")
        except Exception:
            pass


def run_install():
    ensure_root()

    print("\n====================================================================")
    print(f" {MODEL_NAME} Power Watchdog & Low-Power Installer")
    print(f" Installer Script: {SCRIPT_NAME}")
    print("====================================================================\n")

    if not is_target_hardware():
        print(f"[WARNING] Host hardware does NOT match {MODEL_NAME}.")
        print("          Proceeding with installation for portable USB multi-boot.")
        print("          (Hardware locks will keep binaries dormant on other PCs).\n")

    # Step 1: Check Toolchain
    print("[1/7] Verifying build toolchain...")
    pkgs = ["build-essential", "gcc", "make", "pkg-config"]
    needs_apt = any(shutil.which(p) is None for p in ["gcc", "make", "pkg-config"])
    if needs_apt:
        print("  Installing missing build packages via apt...")
        subprocess.run(["apt-get", "update", "-qq"], check=True)
        subprocess.run(["apt-get", "install", "-y", "-qq"] + pkgs, check=True)
    else:
        print("  [OK] Build toolchain already present.")

    # Step 2: Clean up obsolete remnants from previous versions
    print("[2/7] Cleaning up obsolete remnants from previous versions...")
    cleanup_obsolete_remnants()

    # Step 3: Compile Early Power Watchdog C binary
    print("[3/7] Compiling namespaced early power watchdog C micro-daemon...")
    os.makedirs("/usr/local/src", exist_ok=True)
    c_src = "/usr/local/src/gpd-pocket-3-power-watchdog.c"
    with open(c_src, "w") as f:
        f.write(C_WATCHDOG_PAYLOAD)

    bin_target = "/usr/local/bin/gpd-pocket-3-power-watchdog"
    subprocess.run(["gcc", "-O2", c_src, "-o", bin_target], check=True)
    os.chmod(bin_target, 0o755)
    print(f"  [OK] {bin_target} compiled successfully.")

    # Step 4: Install Namespaced Initramfs Hooks
    print("[4/7] Installing Initramfs early power management hooks...")

    # 4a. Binary Hook
    hook_path = "/etc/initramfs-tools/hooks/gpd_pocket_3_power"
    with open(hook_path, "w") as f:
        f.write(
            f'#!/bin/sh\n# {MODEL_NAME} Initramfs Binary Hook\n# Installed and managed by {SCRIPT_NAME}\nPREREQ=""\nprereqs() {{ echo "$PREREQ"; }}\ncase "$1" in prereqs) prereqs; exit 0;; esac\n. /usr/share/initramfs-tools/hook-functions\nif [ -f /usr/local/bin/gpd-pocket-3-power-watchdog ]; then\n    copy_exec /usr/local/bin/gpd-pocket-3-power-watchdog /bin\nfi\nexit 0\n'
        )
    os.chmod(hook_path, 0o755)

    # 4b. Init-top Script
    top_path = "/etc/initramfs-tools/scripts/init-top/gpd_pocket_3_power"
    with open(top_path, "w") as f:
        f.write(
            f'#!/bin/sh\n# {MODEL_NAME} Early Watchdog Launcher (init-top)\n# Installed and managed by {SCRIPT_NAME}\nPREREQ="udev"\nprereqs() {{ echo "$PREREQ"; }}\ncase "$1" in prereqs) prereqs; exit 0;; esac\n. /scripts/functions\nif [ -x /bin/gpd-pocket-3-power-watchdog ]; then\n    /bin/gpd-pocket-3-power-watchdog &\n    echo "$!" > /run/gpd_pocket_3_watchdog.pid\nfi\n'
        )
    os.chmod(top_path, 0o755)

    # 4c. Init-bottom Script
    bottom_path = "/etc/initramfs-tools/scripts/init-bottom/gpd_pocket_3_power"
    with open(bottom_path, "w") as f:
        f.write(
            f'#!/bin/sh\n# {MODEL_NAME} Watchdog Handoff Script (init-bottom)\n# Installed and managed by {SCRIPT_NAME}\nPREREQ=""\nprereqs() {{ echo "$PREREQ"; }}\ncase "$1" in prereqs) prereqs; exit 0;; esac\n. /scripts/functions\nPIDFILE="/run/gpd_pocket_3_watchdog.pid"\nif [ -f "$PIDFILE" ]; then\n    PID=$(cat "$PIDFILE")\n    if [ -n "$PID" ]; then\n        kill -TERM "$PID" 2>/dev/null || true\n    fi\n    rm -f "$PIDFILE"\nfi\n'
        )
    os.chmod(bottom_path, 0o755)

    # Step 5: Enforce Initramfs Modules using Debian Guard Blocks
    print(
        "[5/7] Updating /etc/initramfs-tools/modules with Debian-safe guard blocks..."
    )
    required_modules = [
        "i915",
        "button",
        "i8042",
        "evdev",
        "intel_lpss_pci",
        "coretemp",
    ]
    update_guarded_config(
        "/etc/initramfs-tools/modules", f"{MODEL_NAME} MODULES", required_modules
    )
    print("  [OK] Preserved existing /etc/initramfs-tools/modules contents.")

    # Step 6: Rebuild Ramdisk
    print("[6/7] Rebuilding Initramfs...")
    subprocess.run(["update-initramfs", "-u"], check=True)

    # Step 7: Install Low-Power CLI Utility & Persistence Systemd Service
    print("[7/7] Installing CLI lowpower utility & systemd persistence unit...")
    lp_path = "/usr/local/bin/gpd-pocket-3-i7-1195G7-lowpower"
    with open(lp_path, "w") as f:
        f.write(LOWPOWER_PAYLOAD)
    os.chmod(lp_path, 0o755)

    svc_path = "/etc/systemd/system/gpd-pocket-3-power.service"
    with open(svc_path, "w") as f:
        f.write(GPD_POCKET3_SERVICE)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    if is_target_hardware():
        subprocess.run(
            ["systemctl", "enable", "gpd-pocket-3-power.service"], check=True
        )

    print(f"\n[SUCCESS] {MODEL_NAME} Power Stack Installed!")
    print(f"          (Managed by {SCRIPT_NAME})")
    print("--------------------------------------------------------------------")
    print(" * Watchdog Binary:   /usr/local/bin/gpd-pocket-3-power-watchdog")
    print(" * Initramfs Hook:    /etc/initramfs-tools/hooks/gpd_pocket_3_power")
    print(
        " * Initramfs Top:     /etc/initramfs-tools/scripts/init-top/gpd_pocket_3_power"
    )
    print(
        " * Initramfs Bottom:  /etc/initramfs-tools/scripts/init-bottom/gpd_pocket_3_power"
    )
    print(
        " * Low-Power Toggle:  /usr/local/bin/gpd-pocket-3-i7-1195G7-lowpower [on|off|status]"
    )
    print(" * Boot Persistence:  gpd-pocket-3-power.service (Restores on/off state)")
    print(" * Backlight Engine:  GNOME-matching 20-step dynamic curve (1% to 100%)")
    print(
        " * Prompt Gating:     Auto-dim/suspend only runs during active passphrase prompt"
    )
    print(
        " * fsck Safety:       Auto-defers poweroff until active disk repair finishes"
    )
    print("--------------------------------------------------------------------\n")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--install":
        run_install()
    else:
        print(__doc__)
        print(f"Usage: sudo ./{SCRIPT_NAME} --install (for {MODEL_NAME})")
