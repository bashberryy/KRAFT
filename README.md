# KRAFT - Kernel Rollback Adjustment & Factory Tool

<pre>
██╗  ██╗ ██████╗   █████╗  ███████╗ ████████╗
██║ ██╔╝ ██╔══██╗ ██╔══██╗ ██╔════╝ ╚══██╔══╝
█████╔╝  ██████╔╝ ███████║ █████╗      ██║
██╔═██╗  ██╔══██╗ ██╔══██║ ██╔══╝      ██║
██║  ██╗ ██║  ██║ ██║  ██║ ██║         ██║
╚═╝  ╚═╝ ╚═╝  ╚═╝ ╚═╝  ╚═╝ ╚═╝         ╚═╝
__________________________________________

  board: <board>   cros: M<version>   kernver: <version>

  1) TPM Reset
  2) GBB Flags
  3) Cr50 Reset
  4) Device Info
  5) Reboot
  6) Shell

  Choose option: _
  
__________________________________________
</pre>

KRAFT is a research and experimentation project for interacting with low-level ChromeOS recovery, firmware, TPM, and device configuration components.

## ⚠️ IMPORTANT!! READ BEFORE USAGE

>
> **KRAFT is intended only for devices that you own or are explicitly authorized to modify!!**
>

Do **not** use KRAFT on a Chromebook, computer, account, or other device belonging to another person, school, organization, business, or other entity unless you have explicit permission to do so.

### You are responsible for what you do with this software.

KRAFT performs low-level operations that can affect device state. Depending on the device, firmware, configuration, or environment, using these tools may result in:

* Data loss
* Device configuration changes
* TPM state changes
* Firmware or boot-related problems
* Recovery or boot failures
* Loss of functionality
* Unexpected behavior
* A device requiring recovery or reinstallation
* Other consequences that may not be immediately obvious

**Do not run a command or option simply because it is available in the menu. Understand what it does before using it.**

The developers and contributors of KRAFT are **not responsible for damage, data loss, configuration changes, loss of access, device malfunction, or other consequences resulting from your use or misuse of this software, to the extent permitted by applicable law.**

You assume responsibility for:

1. Confirming that you own the device or have authorization to modify it.
2. Understanding what the software is going to do before running it.
3. Backing up anything important before performing potentially destructive operations.
4. Understanding the risks associated with modifying TPM, firmware, boot, or other low-level device state.
5. Complying with all applicable laws, policies, agreements, and organizational rules.
6. Recovering or restoring your device if your actions cause it to stop functioning normally.

### 🚫 Do not use this to bypass someone else's controls

KRAFT is **not permission** to circumvent security controls, administrative restrictions, organizational policies, enrollment, ownership protections, or other restrictions on a device you do not own or are not authorized to modify.

For example, if a Chromebook belongs to a school, employer, organization, or another person, **do not use KRAFT to alter it unless the owner or authorized administrator has explicitly given you permission.**

If you use this software on somebody else's device without authorization, **that is your decision and your responsibility, NOT ours.**

### 🔬 Research / educational use

This project exists for research, experimentation, education, and understanding how ChromeOS and related low-level components work.

The existence of a feature or script in this repository does **not** mean that it is appropriate to use it on every device or in every situation.

Before using KRAFT, we strongly recommend reading the source code and understanding the operation you intend to perform. Even if you do not understand the language, we've wrote comments for you explaining, OR you could just as someone else or AI.

## No Warranty

KRAFT is provided **as-is and without warranty**, to the maximum extent permitted by applicable law.

The authors and contributors make no guarantee that:

* KRAFT will work on your particular device;
* KRAFT will behave exactly as expected;
* a device will remain bootable after use;
* data will remain intact;
* a device will remain in its previous configuration; or
* recovery will always be possible.

**Use it at your own risk.**

Nothing in this README is intended to provide legal advice or to override rights or protections that cannot legally be disclaimed in your jurisdiction.

## Responsible Use

By choosing to use KRAFT, you acknowledge that you are responsible for determining whether you are authorized to perform the operation you are attempting and for understanding the potential consequences.

**If you do not own the device and do not have explicit authorization to modify it, don't use KRAFT on it.**

## 🔧 What KRAFT Does

KRAFT is a low-level ChromeOS research and experimentation tool designed to reset the device's kernel version to the absolute lowest possible by editing TPM.

Depending on the device and environment, KRAFT provides functionality for:

* **Device information** - view information about the current ChromeOS device and environment.
* **Board identification** - identify the board/platform the device is running.
* **Kernel version information** - inspect the device's stored kernel version state.
* **TPM-related operations** - perform supported TPM operations provided by the project.
* **Device-state operations** - interact with supported low-level device state and configuration.
* **Rebooting** - restart the device after completing an operation.

> **Availability and behavior may vary by board, ChromeOS version, firmware, and device state.**

