#!/bin/sh
# install.sh - install the ShellBeacon status page generator.
# Run as root from this directory. Idempotent: re-running upgrades the
# script and units in place and never overwrites an existing config.
set -e

PREFIX=/usr/local
CONFDIR=$PREFIX/etc

install -m 755 shellbeacon-page.py $PREFIX/sbin/shellbeacon-page.py
install -m 644 shellbeacon-page.service shellbeacon-page.timer \
    /etc/systemd/system/

if [ ! -f $CONFDIR/shellbeacon-page.conf ]; then
    install -m 644 shellbeacon-page.conf.example \
        $CONFDIR/shellbeacon-page.conf
    echo "Wrote $CONFDIR/shellbeacon-page.conf from the example."
    echo "Edit it now: callsign, place, lat, lon, webroot, wxfile."
fi
if [ -f map-features.json ]; then
    install -m 644 map-features.json $CONFDIR/map-features.json
    echo "Installed map-features.json - point map_features in the"
    echo "config at $CONFDIR/map-features.json to use it."
fi

systemctl daemon-reload
systemctl enable --now shellbeacon-page.timer
echo "Installed. First generation:"
systemctl start shellbeacon-page.service
echo "Done - the page refreshes every five minutes."
