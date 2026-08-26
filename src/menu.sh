## SCRIPT FOUND BY BERRYY!! PLEASE DO NOT USE THIS ON A DEVICE YOU DO NOT OWN, OR YOU MAY FACE CONSEQUENCES WE'RE NOT LIABLE FOR!!!
## I'll be leaving comments for people who don't understand bash.
#!/bin/bash
set -e
BANNER=/etc/issue
## safe command, greps (pulls) board name through the chromeos built in command, crossystem hwid
get_board() {
    local b
    b="$(crossystem hwid 2>/dev/null | awk '{print tolower($1)}')"
    [ -z "$b" ] && b="$(grep CHROMEOS_RELEASE_BOARD /etc/lsb-release 2>/dev/null | cut -d= -f2)"
    echo "${b:-unknown}"
}
## safe command, anything that uses crossystem is directly pulling from chromeos, so far, there is no access to vpd where more dangerous changes are likely to be made.
## this gets kernel versoin btw
get_kernver() {
    local raw
    raw="$(crossystem tpm_kernver 2>/dev/null)"
    [ -z "$raw" ] && echo "unknown" && return
    printf "%d" "$((raw & 0xff))" 2>/dev/null || echo "unknown"
}
## this gets the version that the kernel will be able to rollback to by pulling from tpm_kernver. it's usually the hex of 1.1 (KV1)
get_rollback_version() {
    local raw
    raw="$(crossystem tpm_kernver 2>/dev/null)"
    [ -z "$raw" ] && echo "unknown" && return
    printf "%d" "$(((raw >> 8) & 0xff))" 2>/dev/null || echo "unknown"
}
## a chromeos milestone is sort of just the version, if you ever see people say M before a  chromeos version (like M150) it just means version in skiddy terms.
get_milestone() {
    grep CHROMEOS_RELEASE_MILESTONE /etc/lsb-release 2>/dev/null | cut -d= -f2
}
## get the bios version
get_bios_version() {
    crossystem ro_fwid 2>/dev/null || echo "unknown"
}
## find internal storagedrive, looks for emmc, sata and nvme's before complete failure.
setup_disk_vars() {
    intdis="$(get_fixed_dst_drive 2>/dev/null)"
    if [ -z "$intdis" ]; then
        for dev in /sys/block/mmcblk* /sys/block/sd* /sys/block/nvme*; do
            [ -d "$dev" ] || continue
            [ "$(cat "$dev/removable" 2>/dev/null)" = "1" ] && continue
            [ "$(cat "$dev/size" 2>/dev/null)" -lt 2097152 ] && continue
            intdis="/dev/$(basename "$dev")"
            break
        done
    fi
    if [ -z "$intdis" ]; then
        echo "  ERROR: cannot find internal disk" >&2
        return 1
    fi
    if echo "$intdis" | grep -q '[0-9]$'; then
        intdis_prefix="${intdis}p"
    else
        intdis_prefix="$intdis"
    fi
}
## resets tpm by clearing nvdata, device thinks it's good as new and starts at KV1 (in simple terms)
reset_tpm() {
    echo ""
    echo "  TPM Reset"
    echo "  =========="
    echo ""

    ## must be in recovery mode for tpm write access — shim boots as recovery so this should be set
    if ! crossystem "mainfw_type?recovery" 2>/dev/null; then
        echo "  [!] Not in recovery mode. Boot from the KRAFT USB in recovery mode"
        echo "      (hold Esc+Refresh then plug in) and try again."
        return
    fi

    mosys nvram clear 2>/dev/null && echo "  [+] NVData cleared successfully!" \
        || echo "  [-] NVData clear failed (non-fatal)"

    crossystem clear_tpm_owner_request=1 2>/dev/null && echo "  [+] TPM owner clear requested" || true

    ## nissa and other post-2019 Cr50 boards use tpm_manager instead of chromeos-tpm-recovery
    ## try tpm_manager first, fall back to the older chromeos-tpm-recovery for pre-2019 boards
    local tpm_ok=0
    if command -v tpm_manager_client >/dev/null 2>&1; then
        tpm_manager_client take_ownership 2>/dev/null \
            && echo "  [+] TPM ownership taken via tpm_manager" \
            && tpm_ok=1 \
            || echo "  [-] tpm_manager_client failed"
    fi

    if [ "$tpm_ok" -eq 0 ] && command -v chromeos-tpm-recovery >/dev/null 2>&1; then
        chromeos-tpm-recovery 2>/dev/null \
            && echo "  [+] TPM recovery OK (chromeos-tpm-recovery)" \
            && tpm_ok=1 \
            || echo "  [-] chromeos-tpm-recovery failed"
    fi

    ## last resort: trunks_send for Cr50/Ti50 (nissa uses Ti50 and I happen to be testing on nissa)
    if [ "$tpm_ok" -eq 0 ] && command -v trunks_send >/dev/null 2>&1; then
        ## send TPM2_CC_Clear command directly
        trunks_send --raw 80020000000c0000014100000107 2>/dev/null \
            && echo "  [+] TPM cleared via trunks_send" \
            && tpm_ok=1 \
            || echo "  [-] trunks_send failed"
    fi

    if [ "$tpm_ok" -eq 0 ]; then
        echo "  [-] All TPM reset methods failed. Try option 3 (Cr50 Reset) first,"
        echo "      then reboot and run TPM Reset again."
    fi

    ## report new kernel version — if reset worked this should now read 1
    echo ""
    local kernver
    kernver="$(get_kernver)"
    echo "  Reported TPM Kernel Rollback Version: $kernver"
    if [ "$kernver" = "1" ] || [ "$kernver" = "0" ]; then
        echo "  [+] Looks good — kernver is at minimum, downgrade should work."
    else
        echo "  [!] kernver is still $kernver — reboot and run TPM Reset again."
        echo "      If it stays high after two tries, do Cr50 Reset first."
    fi
    echo ""
    echo "  Done."
}
## google binary block flags editor, just a bonus to edit stuff. you probably only need to disable fwmp. honestly, this should better work in a shell and just use a ccd operation.
edit_gbb() {
    echo ""
    echo "  GBB Flags Editor"
    echo "  ================"
    echo "  Current: $(/usr/share/vboot/bin/get_gbb_flags.sh 2>/dev/null | grep -i flags || echo unknown)"
    echo ""
    echo "  1) Force dev mode on       (0x8)"
    echo "  2) Short dev delay         (0x1)"
    echo "  3) Dev + short delay       (0x9)"
    echo "  4) Disable FWMP            (0x40)"
    echo "  5) Dev + disable FWMP      (0x48)"
    echo "  6) All useful flags        (0x49)" ## check out binbashbanana's GBB-FLAGINATOR for more!
    echo "  7) Reset to factory        (0x0)"
    echo "  8) Custom hex"
    echo "  9) Back"
    echo ""
    read -p "  Choose: " g
    local flags
    case $g in
        1) flags=0x8  ;; 2) flags=0x1  ;; 3) flags=0x9  ;;
        4) flags=0x40 ;; 5) flags=0x48 ;; 6) flags=0x49 ;;
        7) flags=0x0  ;;
        8) read -p "  Hex value: " flags ;;
        9) return ;;
        *) echo "  Invalid."; return ;;
    esac
    flashrom -p host --wp-disable >/dev/null 2>&1 || true ## <-- this one line makes this super unlikely to work since we cannot manually turn of wp on modern devices, it's an spi flash that cannot have a svript conduct eletricity to change it's binary value to bypass anything.
    /usr/share/vboot/bin/set_gbb_flags.sh "$flags" \
        && echo "  [+] GBB flags set to $flags" \
        || echo "  [-] Failed - HW write protect may be on" ## you can bypass this by disconnected the battery. on most devices, it's the connector in the battery that says "batt" but if there's one that says "MB" pull that out instead since the battery cable is usually obstructed. MB cable connects the battery to the motherboard, as long as you wiggle it out carefully and a little firmly, you'll be able to safely put it back in.
}
## reset cr50, this is the least likely to work out of every script. (YES I KNOW THIS IS AUTOMATIC IN TPM RESET, I JUST ADDED IT AND I'M TOO LAZY TO DELETE TS)
reset_cr50() {
    echo ""
    echo "  Cr50 Reset"
    echo "  ==========="
    echo ""
    ## try cr50-reset.sh first, then fall back to gsctool for Ti50
    if /usr/share/cros/cr50-reset.sh 2>/dev/null; then
        echo "  [+] Cr50 reset complete"
    elif gsctool -a --ccd_open 2>/dev/null; then
        echo "  [+] CCD opened via gsctool (Ti50)"
    else
        echo "  [-] cr50/Ti50 reset failed — try running from crosh shell:"
        echo "      gsctool -a --ccd_open"
    fi
}
## device stats, doesn't write anything.
view_device_info() {
    echo ""
    echo "  Device Information"
    echo "  ==================="
    echo ""
    echo "  Board (HWID):      $(crossystem hwid 2>/dev/null || echo unknown)"
    echo "  BIOS Version:      $(get_bios_version)"
    echo "  Kernel Version:    $(get_kernver)"
    echo "  Rollback Counter:  $(get_rollback_version)"
    echo "  Chrome OS Build:   M$(get_milestone)"
    echo "  Dev Switch Boot:   $(crossystem devsw_boot 2>/dev/null || echo unknown)"
    echo "  WP Switch Boot:    $(crossystem wpsw_boot 2>/dev/null || echo unknown)"
    echo "  Firmware Type:     $(crossystem mainfw_type 2>/dev/null || echo unknown)"
    echo "  Cr50 RW Version:   $(gsctool -a -f -M 2>/dev/null | grep RW_FW_VER | cut -d= -f2 || echo unknown)"
    echo ""
    read -p "  Press Enter to continue..."
}
## full menu, shows you the banner and your options and a thingy to pick them. 
while true; do
    clear
    cat "$BANNER" 2>/dev/null || echo "  TPMstateDEM"
    echo ""
    KV="$(get_kernver)"
    MILE="$(get_milestone)"
    BOARD="$(get_board)"
    echo "  board: $BOARD   cros: M$MILE   kernver: $KV"
    echo ""
    echo "  1) TPM Reset"
    echo "  2) GBB Flags"
    echo "  3) Cr50 Reset"
    echo "  4) Device Info"
    echo "  5) Reboot"
    echo "  6) Shell"
    echo ""
    read -p "  Choose option: " choice
## links to above script so when you input an option, these scripts proceed to call the following functions.
    case "$(echo "$choice" | tr 'a-z' 'A-Z')" in
        1) reset_tpm;          read -p "  Press Enter to continue..." ;;
        2) edit_gbb;           read -p "  Press Enter to continue..." ;;
        3) reset_cr50;         read -p "  Press Enter to continue..." ;;
        4) view_device_info ;;
        5) reboot -f ;;
        6) echo "  Type 'exit' to return"; bash ;;
        *) echo "  Invalid."; sleep 1 ;;
    esac
done
