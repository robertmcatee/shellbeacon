#!/bin/bash
###### shellbeacon-saynow.sh - speak one station announcement NOW.
######
###### The on-demand twin of shellbeacon.sh's txvoice(): same conf,
###### same fragment voice, same transmit chain, run once and exit.
###### The control panel (shellbeacon-control.py) invokes it; it can
###### also be run by hand. Honours rf.enabled (inside pipetx) and
###### voice.enabled, but NOT voice.hold - a deliberate "say now" is
###### the control operator speaking, hold or no hold.
######
###### Copyright (C) 2026 Robert McAtee, KJ5GBY. GPL v3 or later.

scriptdir="$(cd "$(dirname "$0")" && pwd)"
for conf in "$SHELLBEACON_CONF" "$scriptdir/shellbeacon.conf" \
            "$HOME/.shellbeacon.conf" /etc/shellbeacon.conf; do
  if [ -n "$conf" ] && [ -r "$conf" ]; then . "$conf"; break; fi
done

: "${callsign:?callsign not set}"
rpitx="${rpitx:-$HOME/src/rpitx/rpitx}"
csdr=/home/sagamore/src/rpitx/csdr/csdr
wxfile="${wxfile:-$HOME/wx/latest.json}"
wxmaxage="${wxmaxage:-2400}"
rf_flag="${rf_flag:-$scriptdir/rf.enabled}"
voice_flag="${voice_flag:-$scriptdir/voice.enabled}"
voice_freq="${voice_freq:-146430000}"
voice_place="${voice_place:-}"

[ -e "$voice_flag" ] || { echo "saynow: voice is OFF (voice.enabled absent)"; exit 3; }
[ -e "$rf_flag" ] || { echo "saynow: RF is OFF (rf.enabled absent)"; exit 3; }
[ -x "$rpitx" ] || { echo "saynow: rpitx not executable: $rpitx"; exit 3; }

wxpause() {
  pkill -CONT -x rtl_433 2>/dev/null
  pkill -STOP -x rtl_433 2>/dev/null
}
wxresume() { pkill -CONT -x rtl_433 2>/dev/null; }
trap wxresume EXIT

pipetx() { # $1 = 48 kHz wav, $2 = frequency Hz
  "$csdr" convert_i16_f < "$1" | "$csdr" gain_ff 7000 | \
    "$csdr" convert_f_samplerf 20833 | \
    timeout 150 "$rpitx" -i- -m RF -f "$(( $2 / 1000 ))" >/dev/null 2>&1
}

# Outdoor temperature - the beacon's own derivation, copied from
# buildpacket(): sed the string time + temperature_C out of the wx
# file, convert, and round to whole degrees Fahrenheit.
WXTEMPF=""
if [ -r "$wxfile" ]; then
  wxtime=$(sed -nE 's/.*"time" : "([^"]+)".*/\1/p' "$wxfile")
  wxc=$(sed -nE 's/.*"temperature_C" : ([-0-9.]+).*/\1/p' "$wxfile")
  if [ -n "$wxtime" ] && [ -n "$wxc" ]; then
    epoch=$(date -d "$wxtime" +%s 2>/dev/null || echo 0)
    age=$(( $(date +%s) - epoch ))
    if [ "$age" -ge 0 ] && [ "$age" -le "$wxmaxage" ]; then
      WXTEMPF=$(awk -v c="$wxc" 'BEGIN{printf "%.0f", c*9/5+32}')
    fi
  fi
fi

# Web-server health, same check as the beacon.
SITEV=0
if [ -n "$sitecheck_host" ]; then
  [ "$(curl -sk -o /dev/null -w '%{http_code}' --max-time 8 https://127.0.0.1/ -H "Host: $sitecheck_host")" = "200" ] && SITEV=1
fi

vwav=$(mktemp /tmp/voice.XXXXXX.wav) || exit 1
trap 'wxresume; rm -f "$vwav"' EXIT
vsay_args=(--dir "$scriptdir" --prefix voice-tidewater2 --out "$vwav"
           --hour "$(date '+%-I')" --minute "$(date '+%-M')" --ampm "$(date '+%p')")
[ -n "$WXTEMPF" ] && vsay_args+=(--temp "$WXTEMPF" --wx-ok)
if [ -z "$sitecheck_host" ] || [ "$SITEV" = "1" ]; then vsay_args+=(--web-ok); fi
if [ "$voice_place" = "San Antonio, Texas" ]; then
  vsay_args+=(--place-token san-antonio-texas)
fi

if vsentence=$(python3 "$scriptdir/voice-say.py" "${vsay_args[@]}" 2>/dev/null); then
  wxpause
  pipetx "$vwav" "$voice_freq"
  rc=$?
  wxresume
  echo "saynow: transmitted (rc=$rc): $vsentence"
  exit 0
fi
echo "saynow: fragment build failed"
exit 1
