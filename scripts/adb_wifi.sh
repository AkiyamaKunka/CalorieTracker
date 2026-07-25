#!/usr/bin/env bash
# Connect to the phone over Wi-Fi (wireless debugging), no cable.
#
# Usage:
#   scripts/adb_wifi.sh            # discover + connect, print the serial
#   scripts/adb_wifi.sh --pair     # first-time trust for a NEW Mac/phone pair
#
# How this works: Android's wireless debugging advertises the device over
# mDNS. PAIRING (once, with a 6-digit code) establishes trust; CONNECTING
# happens on a port that CHANGES every time wireless debugging restarts —
# hence the discovery step instead of a hardcoded address.
#
# GOTCHA worth remembering: the phone's pairing dialog shows the address of
# whichever interface it feels like — with a VPN active that is the tunnel
# address (172.19.x.x), which the Mac cannot reach. Use the PORT from the
# dialog with the phone's real LAN IP (this script's discovery finds it).
set -euo pipefail

export PATH="$HOME/Library/Android/sdk/platform-tools:$PATH"

discover() {
  # mDNS entries look like:  <name>  _adb-tls-connect._tcp  IP:PORT
  adb mdns services 2>/dev/null \
    | awk '/_adb-tls-connect/ {print $NF}' \
    | tail -1
}

if [[ "${1:-}" == "--pair" ]]; then
  echo "On the phone: Settings → Developer options → Wireless debugging"
  echo "  → 'Pair device with pairing code'. Keep that popup OPEN."
  read -r -p "6-digit pairing code: " code
  read -r -p "Port from that popup (the number after the colon): " port
  ip=$(discover | cut -d: -f1)
  if [[ -z "$ip" ]]; then
    read -r -p "Could not auto-detect the phone's Wi-Fi IP — enter it: " ip
  fi
  echo "==> pairing with $ip:$port"
  adb pair "$ip:$port" "$code"
fi

echo "==> discovering the phone over mDNS…"
target=""
for _ in 1 2 3 4 5; do
  target=$(discover)
  [[ -n "$target" ]] && break
  sleep 2
done

if [[ -z "$target" ]]; then
  cat >&2 <<'EOS'
No wireless-debugging service found. Check that:
  * the phone is on the SAME Wi-Fi as this Mac (a phone VPN often breaks
    LAN discovery — turn it off for the connect, then back on);
  * Developer options → Wireless debugging is ON (it silently switches
    itself off on some OEM builds after a reboot or a network change).
EOS
  exit 1
fi

# Stale transports from an earlier session answer as "offline" and shadow
# the fresh one — drop them all first.
adb disconnect >/dev/null 2>&1 || true
sleep 1
echo "==> connecting to $target"
adb connect "$target" >/dev/null
sleep 2

if adb -s "$target" shell true >/dev/null 2>&1; then
  echo "OK — connected: $target"
  echo "Use it with:  adb -s $target <command>"
else
  echo "Connected but the device is not answering; re-run, or re-pair with --pair." >&2
  exit 1
fi
