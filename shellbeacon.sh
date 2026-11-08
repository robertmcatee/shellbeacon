#!/bin/bash
###### shellbeacon 1.3 - a shell-script APRS beacon with telemetry,
###### weather, and optional RF transmission.
######
###### Fork of shellbeacon 1.0 by WA1GOV (a netcat APRS-IS beacon),
###### extended by Robert McAtee, KJ5GBY:
######   1.1  station comment; APRS telemetry of the host (CPU temp,
######        load, free memory, disk use, uptime, web-server status)
######        with PARM/UNIT/EQNS/BITS definitions.
######   1.2  live weather: an outdoor temperature reading maintained by
######        a companion collector (rtl_433 + an RTL-SDR receiver) is
######        sent in a complete APRS weather block whenever fresh.
######   1.3  the same frames are also transmitted on RF (144.390 MHz)
######        via Dire Wolf gen_packets + rpitx, when enabled.
######   1.4  a spoken voice announcement (Tidewater fragments; espeak-ng fallback) on a 2 m simplex
######        channel after each cycle's RF burst, when enabled:
######        station ID, time, outdoor temperature, station health.
######   1.7  the voice health report tells the truth about subsystems:
######        a stale/missing weather reading is announced ("Weather
######        system is down"), as is a failed site check; "All station
######        systems normal" is spoken only when every check passes.
######   1.8  the WX collector is paused (SIGSTOP) for the transmit
######        window and resumed (SIGCONT) right after: rtl_433
######        streaming/decoding on this single-core host audibly
######        corrupted the voice transmission (buzzing at the head of
######        the announcement; A/B-proven 2026-10-19).
######
###### All station-specific settings (callsign, APRS-IS passcode,
###### position, comment) live in shellbeacon.conf - see
###### shellbeacon.conf.example. The script looks for it, in order:
######   $SHELLBEACON_CONF, ./shellbeacon.conf (beside the script),
######   ~/.shellbeacon.conf, /etc/shellbeacon.conf
######
###### Copyright (C) 2026 Robert McAtee, KJ5GBY.
###### This program is free software: you can redistribute it and/or
###### modify it under the terms of the GNU General Public License
###### as published by the Free Software Foundation, either version
###### 3 of the License, or (at your option) any later version.
###### This program is distributed in the hope that it will be
###### useful, but WITHOUT ANY WARRANTY; without even the implied
###### warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR
###### PURPOSE. See the GNU General Public License for more details:
###### <https://www.gnu.org/licenses/>. The original shellbeacon 1.0
###### by WA1GOV was shared publicly without stated license terms
###### and is credited above.

scriptdir="$(cd "$(dirname "$0")" && pwd)"
for conf in "$SHELLBEACON_CONF" "$scriptdir/shellbeacon.conf" \
            "$HOME/.shellbeacon.conf" /etc/shellbeacon.conf; do
  if [ -n "$conf" ] && [ -r "$conf" ]; then . "$conf"; break; fi
done

: "${callsign:?callsign not set - copy shellbeacon.conf.example to shellbeacon.conf and edit it}"
: "${password:?password (APRS-IS passcode) not set in shellbeacon.conf}"
: "${latlon:?latlon not set in shellbeacon.conf (e.g. !3859.06N/07702.24W)}"
tocall="${tocall:-APZSHB}"          # experimental-range device ID
serverHost="${serverHost:-rotate.aprs2.net}"
serverPort="${serverPort:-14580}"
delay="${delay:-1200}"              # seconds between beacons (20 min)
comment="${comment:-}"
wxfile="${wxfile:-$HOME/wx/latest.json}"
wxmaxage="${wxmaxage:-2400}"        # ignore WX readings older than this
rpitx="${rpitx:-$HOME/src/rpitx/rpitx}"
rf_flag="${rf_flag:-$scriptdir/rf.enabled}"
seqfile="${seqfile:-$scriptdir/tele.seq}"
voice_flag="${voice_flag:-$scriptdir/voice.enabled}"
voice_freq="${voice_freq:-146430000}"
voice_id="${voice_id:-$callsign}"
voice_place="${voice_place:-}"

address="${callsign}>${tocall},TCPIP:"
login="user $callsign pass $password vers ShellBeacon 1.8"

buildpacket() {
  wx=""
  WXTEMPF=""
  if [ -r "$wxfile" ]; then
    wxtime=$(sed -nE 's/.*"time" : "([^"]+)".*/\1/p' "$wxfile")
    wxc=$(sed -nE 's/.*"temperature_C" : ([-0-9.]+).*/\1/p' "$wxfile")
    if [ -n "$wxtime" ] && [ -n "$wxc" ]; then
      epoch=$(date -d "$wxtime" +%s 2>/dev/null || echo 0)
      age=$(( $(date +%s) - epoch ))
      if [ "$age" -ge 0 ] && [ "$age" -le "$wxmaxage" ]; then
        tempf=$(awk -v c="$wxc" 'BEGIN{printf "%.0f", c*9/5+32}')
        WXTEMPF="$tempf"
        wx=$(printf '.../...g...t%03dr...p...P...h..b.....' "$tempf")
      fi
    fi
  fi
  if [ -n "$wx" ]; then
    # the weather-station symbol publishes the reading cleanly
    packet="${address}${latlon}_${wx} ${comment}"
  else
    packet="${address}${latlon}-${comment}"
  fi
}

clamp() { v=$1; [ "$v" -lt 0 ] 2>/dev/null && v=0; [ "$v" -gt 255 ] 2>/dev/null && v=255; printf '%s' "$v"; }

seq=0
[ -r "$seqfile" ] && seq=$(cat "$seqfile" 2>/dev/null || echo 0)
case "$seq" in *[!0-9]*|'') seq=0 ;; esac
TELE=""
telemetry() {
  tempc=$(vcgencmd measure_temp 2>/dev/null | sed -E 's/[^0-9.]//g')
  tempf=$(awk -v c="${tempc:-0}" 'BEGIN{printf "%d", c*9/5+32}')
  load=$(awk '{printf "%d", $1*10}' /proc/loadavg)
  memfree=$(free | awk '/^Mem:/{printf "%d", $7/$2*100}')
  diskused=$(df / | awk 'NR==2{gsub(/%/,"",$5); print $5}')
  updays=$(awk '{printf "%d", $1/86400}' /proc/uptime)
  site=0
  if [ -n "$sitecheck_host" ]; then
    [ "$(curl -sk -o /dev/null -w '%{http_code}' --max-time 8 https://127.0.0.1/ -H "Host: $sitecheck_host")" = "200" ] && site=1
  fi
  SITEV=$site
  seq=$((seq % 999 + 1))
  printf '%s' "$seq" > "$seqfile" 2>/dev/null || true
  TELE=$(printf '%sT#%03d,%s,%s,%s,%s,%s,%d0000000' "$address" "$seq" "$(clamp "$tempf")" "$(clamp "$load")" "$(clamp "$memfree")" "$(clamp "$diskused")" "$(clamp "$updays")" "$site")
}

DEFS=""
makedefs() {
  to=$(printf '%-9s' "$callsign")
  DEFS=$(printf '%s:%s:PARM.CPUtemp,Load,FreeMem,DiskUsed,Uptime\n%s:%s:UNIT.degF,x0.1,%%,%%,days\n%s:%s:EQNS.0,1,0,0,0.1,0,0,1,0,0,1,0,0,1,0\n%s:%s:BITS.10000000,WebUp,,,,,,,' "$address" "$to" "$address" "$to" "$address" "$to" "$address" "$to")
}

###### RF: transmit the same frames on 144.390 MHz (AX.25 AFSK via
###### Dire Wolf's gen_packets, FM via rpitx on GPIO 4) - only while the
###### rf.enabled flag file exists. The internet-only TCPIP path marker
###### is replaced with a digipeater path for RF.
###### Narrow-band FM the way this rpitx build actually works: 48 kHz
###### audio -> csdr -> rpitx -m RF (this binary has no FM mode; every
###### -m FM transmission before rev 1.5 modulated nothing).
###### NOTE: this build's -f is in kHz, so pipetx divides Hz by 1000
###### (proven on-air 2026-10-18: kHz scaling is the audible one).
csdr=/home/sagamore/src/rpitx/csdr/csdr

###### WX collector pause (rev 1.8, see header): freeze the collector's
###### rtl_433 only while transmitting. wxpause first releases any
###### freeze a crashed cycle may have left behind, then stops the
###### decoder; the main loop also resumes on exit (trap), so the
###### collector can never be left frozen. If the USB stream does not
###### survive a freeze, the collector's own loop restarts rtl_433.
wxpause() {
  pkill -CONT -x rtl_433 2>/dev/null
  pkill -STOP -x rtl_433 2>/dev/null && echo "WX collector paused for TX"
}
wxresume() {
  pkill -CONT -x rtl_433 2>/dev/null && echo "WX collector resumed"
}

pipetx() { # $1 = 48 kHz wav, $2 = frequency Hz
  "$csdr" convert_i16_f < "$1" | "$csdr" gain_ff 7000 | "$csdr" convert_f_samplerf 20833 | timeout 150 "$rpitx" -i- -m RF -f "$(( $2 / 1000 ))" >/dev/null 2>&1
}

txrf() {
  [ -e "$rf_flag" ] || return 0
  command -v gen_packets >/dev/null 2>&1 || return 0
  [ -x "$rpitx" ] || return 0
  rftxt=$(mktemp /tmp/aprs-rf.XXXXXX) || return 0
  rfwav="${rftxt}.wav"
  printf '%s\n' "${packet//TCPIP/WIDE1-1,WIDE2-1}" >> "$rftxt"
  printf '%s\n' "${TELE//TCPIP/WIDE1-1,WIDE2-1}" >> "$rftxt"
  [ -n "$DEFS" ] && printf '%s\n' "${DEFS//TCPIP/WIDE1-1,WIDE2-1}" >> "$rftxt"
  if gen_packets -r 48000 -a 50 -o "$rfwav" "$rftxt" >/dev/null 2>&1; then
    pipetx "$rfwav" 144390000
    echo "RF TX done (rc=$?) frames: $(grep -c . "$rftxt")"
  fi
  rm -f "$rftxt" "$rfwav"
}

###### Voice: speak the station report (ID, time, outdoor temperature,
###### health) on the voice channel - only while voice.enabled exists.
###### Runs in this (root) context because rpitx needs /dev/mem.
txvoice() {
  [ -e "$voice_flag" ] || return 0
  # Control-panel hold: stay off the air while a net is on. The hold
  # file holds an epoch deadline and expires on its own; a stale or
  # malformed file is removed, never obeyed forever.
  if [ -f "$scriptdir/voice.hold" ]; then
    hold_until=$(cat "$scriptdir/voice.hold" 2>/dev/null || echo 0)
    case "$hold_until" in ''|*[!0-9]*) hold_until=0 ;; esac
    if [ "$(date +%s)" -lt "$hold_until" ]; then
      echo "Voice held until $(date -d "@$hold_until" '+%H:%M') (control panel)"
      return 0
    fi
    rm -f "$scriptdir/voice.hold"
  fi
  [ -x "$rpitx" ] || return 0
  vwav=$(mktemp /tmp/voice.XXXXXX.wav) || return 0
  # Tidewater fragments first (already 48 kHz, no resampling needed);
  # espeak stays as the fallback for place names and build failures.
  vsay_args=(--dir "$scriptdir" --prefix voice-tidewater2 --out "$vwav" --hour "$(date '+%-I')" --minute "$(date '+%-M')" --ampm "$(date '+%p')")
  [ -n "$WXTEMPF" ] && vsay_args+=(--temp "$WXTEMPF" --wx-ok)
  if [ -z "$sitecheck_host" ] || [ "$SITEV" = "1" ]; then vsay_args+=(--web-ok); fi
  # The one known place has a fragment; any other place falls back to
  # espeak (voice-say.py also exits 2 on --place, as a backstop).
  frag_ok=1
  if [ -n "$voice_place" ]; then
    if [ "$voice_place" = "San Antonio, Texas" ]; then
      vsay_args+=(--place-token san-antonio-texas)
    else
      frag_ok=0
    fi
  fi
  if [ "$frag_ok" = "1" ] && vsentence=$(python3 "$scriptdir/voice-say.py" "${vsay_args[@]}" 2>/dev/null); then
    pipetx "$vwav" "$voice_freq"
    echo "Voice TX done (rc=$?): $vsentence"
    rm -f "$vwav"
    return 0
  fi
  # Fallback: the old espeak path (22050 Hz, resampled up to 48 kHz).
  command -v espeak-ng >/dev/null 2>&1 || { rm -f "$vwav"; return 0; }
  vmsg="This is $voice_id"
  [ -n "$voice_place" ] && vmsg="$vmsg, $voice_place"
  vmsg="$vmsg. The time is $(date '+%-I:%M %p')."
  [ -n "$WXTEMPF" ] && vmsg="$vmsg Outdoor temperature $WXTEMPF degrees."
  health=""
  [ -z "$WXTEMPF" ] && health="$health Weather system is down."
  if [ -n "$sitecheck_host" ] && [ "$SITEV" != "1" ]; then health="$health Web server is down."; fi
  if [ -z "$health" ]; then vmsg="$vmsg All station systems normal."; else vmsg="$vmsg$health"; fi
  if espeak-ng -s 135 -v en-us -w "$vwav" "$vmsg" 2>/dev/null; then
    # the NFM pipeline wants 48 kHz audio - resample the 22050 Hz speech up
    python3 - "$vwav" <<'PYEOF' 2>/dev/null
import sys, wave, audioop
f = sys.argv[1]
w = wave.open(f); p = w.getparams(); frames = w.readframes(p.nframes); w.close()
out, _ = audioop.ratecv(frames, p.sampwidth, p.nchannels, p.framerate, 48000, None)
o = wave.open(f, "wb"); o.setnchannels(1); o.setsampwidth(2)
o.setframerate(48000); o.writeframes(out); o.close()
PYEOF
    pipetx "$vwav" "$voice_freq"
    echo "Voice TX done (rc=$?): $vmsg"
  fi
  rm -f "$vwav"
}

trap wxresume EXIT
cycle=0
while true
do
  buildpacket
  telemetry
  if [ $((cycle % 6)) -eq 0 ]; then makedefs; else DEFS=""; fi
  nc -C $serverHost $serverPort -q 10 <<-END
	$login
	$packet
	$TELE
	$DEFS
	END
  echo "$packet"
  echo "$TELE"
  [ -n "$DEFS" ] && echo "$DEFS"
  wxpause
  txrf
  txvoice
  wxresume
  cycle=$((cycle+1))
  if [ "$1" = "1" ]
  then
      exit
  fi
  sleep $delay
done
