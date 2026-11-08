#!/usr/bin/env python3
"""shellbeacon-control - the KJ5GBY station control panel service.

A deliberately small, standard-library-only HTTP service bound to
127.0.0.1:8891. nginx terminates HTTPS and Basic Auth on the public
side and proxies /kj5gby/control/ to it; nothing here is reachable
except through that authenticated location.

Actions (POST only):
  hold           minutes=30|60|120   hold voice announcements
  hold_until     until=HH:MM         hold until a clock time
  release                            release the hold now
  say_now                            speak one announcement now
  voice_on / voice_off               the voice.enabled flag
  rf_on / rf_off                     the rf.enabled flag

State lives in the beacon's own files (voice.enabled, rf.enabled,
voice.hold), so the panel and the beacon can never disagree. Every
action is logged to /var/aprs-beacon/control.log with its source.

Copyright (C) 2026 Robert McAtee, KJ5GBY. GPL v3 or later.
"""

import subprocess
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

BEACON = Path("/var/aprs-beacon")
SAYNOW = BEACON / "shellbeacon-saynow.sh"
LOG = BEACON / "control.log"
PORT = 8891


def log(action, source, detail=""):
    try:
        with LOG.open("a") as f:
            f.write("%s %s %s %s\n" % (datetime.now().isoformat(timespec="seconds"),
                                       source, action, detail))
    except OSError:
        pass


def hold_state():
    """(active, until_epoch) from the hold file; stale files ignored."""
    try:
        until = int((BEACON / "voice.hold").read_text().strip())
    except (OSError, ValueError):
        return False, 0
    return (time.time() < until), until


def fmt_time(epoch):
    return datetime.fromtimestamp(epoch).strftime("%H:%M")


PAGE = """<!DOCTYPE html>
<html lang="en" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KJ5GBY Station Control</title>
<style>
:root, html[data-theme="dark"]{
--bg:#002b36;--panel:#073642;--ink:#839496;--head:#eee8d5;--dim:#586e75;
--line:#586e75;--btn:#0a4a5c;--btnink:#eee8d5;--accent:#2aa198;--accent2:#268bd2;
--warn:#dc322f;--cardh:#b58900}
html[data-theme="light"]{
--bg:#fdf6e3;--panel:#eee8d5;--ink:#657b83;--head:#073642;--dim:#839496;
--line:#93a1a1;--btn:#eee8d5;--btnink:#073642;--accent:#2aa198;--accent2:#268bd2;
--warn:#dc322f;--cardh:#b58900}
body{background:var(--bg);color:var(--ink);font-family:ui-monospace,Menlo,monospace;
margin:0;padding:18px;font-size:16px}
header.top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
h1{color:var(--head);font-size:20px;margin:0 0 4px}
.sub{color:var(--dim);margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;
padding:14px;margin-bottom:14px}
.card h2{color:var(--cardh);font-size:13px;letter-spacing:.12em;margin:0 0 10px}
.state{color:var(--head)}
form{margin:0}
.row{display:flex;flex-wrap:wrap;gap:8px}
button{background:var(--btn);color:var(--btnink);border:1px solid var(--accent);
border-radius:6px;padding:12px 16px;font:inherit;font-size:15px;cursor:pointer}
button.warn{border-color:var(--warn)}
button:active{background:var(--accent);color:var(--bg)}
button#theme{background:none;border:1px solid var(--line);color:var(--ink);
border-radius:0;padding:5px 10px;font-size:12px;letter-spacing:.1em;flex:none}
input[type=time]{background:var(--bg);color:var(--head);border:1px solid var(--line);
border-radius:6px;padding:10px;font:inherit}
.msg{border-color:var(--accent);color:var(--head)}
a{color:var(--accent2)}
</style></head><body>
<header class="top"><div>
<h1>KJ5GBY &mdash; Station Control</h1>
<div class="sub">San Antonio, Texas &middot; 146.430 voice &middot; APRS 144.390</div>
</div>
<button id="theme" type="button" aria-label="Toggle light and dark">LIGHT</button>
</header>
{msg}
<div class="card"><h2>STATE</h2><div class="state">{state}</div></div>
<div class="card"><h2>VOICE HOLD &mdash; FOR A NET</h2>
<div class="row">
<form method="post" action="action"><input type="hidden" name="action" value="hold"><input type="hidden" name="minutes" value="30"><button>Hold 30 min</button></form>
<form method="post" action="action"><input type="hidden" name="action" value="hold"><input type="hidden" name="minutes" value="60"><button>Hold 1 hour</button></form>
<form method="post" action="action"><input type="hidden" name="action" value="hold"><input type="hidden" name="minutes" value="120"><button>Hold 2 hours</button></form>
<form method="post" action="action"><input type="hidden" name="action" value="release"><button class="warn">Release hold</button></form>
</div>
<form method="post" action="action" style="margin-top:10px">
<input type="hidden" name="action" value="hold_until">
<label>Hold until <input type="time" name="until" required></label>
<button>Set</button></form>
</div>
<div class="card"><h2>SPEAK</h2>
<form method="post" action="action"><input type="hidden" name="action" value="say_now"><button>Say announcement now</button></form>
</div>
<div class="card"><h2>SWITCHES</h2><div class="row">
<form method="post" action="action"><input type="hidden" name="action" value="voice_on"><button>Voice ON</button></form>
<form method="post" action="action"><input type="hidden" name="action" value="voice_off"><button class="warn">Voice OFF</button></form>
<form method="post" action="action"><input type="hidden" name="action" value="rf_on"><button>RF ON</button></form>
<form method="post" action="action"><input type="hidden" name="action" value="rf_off"><button class="warn">RF OFF</button></form>
</div></div>
<div class="sub">Every action is logged. Holds expire on their own.
<a href="/kj5gby/">Station page</a></div>
<script>
(function () {
  var root = document.documentElement, btn = document.getElementById('theme');
  try {
    var saved = localStorage.getItem('shellbeacon-theme');
    if (saved === 'light' || saved === 'dark') root.setAttribute('data-theme', saved);
  } catch (e) {}
  function label() { btn.textContent = root.getAttribute('data-theme') === 'dark' ? 'LIGHT' : 'DARK'; }
  label();
  btn.addEventListener('click', function () {
    var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem('shellbeacon-theme', next); } catch (e) {}
    label();
  });
})();
</script>
</body></html>"""


def state_html():
    voice = (BEACON / "voice.enabled").exists()
    rf = (BEACON / "rf.enabled").exists()
    active, until = hold_state()
    parts = ["Voice announcements: <b>%s</b>" % ("ON" if voice else "OFF"),
             "RF (APRS + voice TX): <b>%s</b>" % ("ON" if rf else "OFF")]
    if active:
        parts.append("Voice hold: <b>HELD until %s</b>" % fmt_time(until))
    elif voice:
        parts.append("Voice hold: none")
    return " &middot; ".join(parts)


def render(msg=""):
    m = '<div class="card msg">%s</div>' % msg if msg else ""
    # str.replace, not str.format: the page's CSS is full of braces.
    return PAGE.replace("{msg}", m).replace("{state}", state_html())


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, body, code=200):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(render())

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        fields = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        action = fields.get("action", [""])[0]
        source = self.headers.get("X-Forwarded-For", self.client_address[0])
        msg = self.act(action, fields, source)
        log(action, source, msg)
        self._send(render(msg))

    def act(self, action, fields, source):
        if action == "hold":
            try:
                minutes = int(fields.get("minutes", ["0"])[0])
            except ValueError:
                return "Bad hold duration."
            if minutes not in (30, 60, 120):
                return "Bad hold duration."
            until = int(time.time()) + minutes * 60
            (BEACON / "voice.hold").write_text("%d\n" % until)
            return "Voice held for %d minutes, until %s." % (minutes, fmt_time(until))
        if action == "hold_until":
            raw = fields.get("until", [""])[0]
            try:
                hh, mm = (int(x) for x in raw.split(":"))
                now = datetime.now()
                target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
            except (ValueError, IndexError):
                return "Bad time."
            (BEACON / "voice.hold").write_text("%d\n" % int(target.timestamp()))
            return "Voice held until %s." % target.strftime("%H:%M")
        if action == "release":
            try:
                (BEACON / "voice.hold").unlink()
            except FileNotFoundError:
                pass
            return "Hold released. Voice announcements resume next cycle."
        if action == "say_now":
            try:
                r = subprocess.run([str(SAYNOW)], capture_output=True, text=True,
                                   timeout=200)
                out = (r.stdout or r.stderr).strip().splitlines()
                return out[-1] if out else "Say-now finished."
            except subprocess.TimeoutExpired:
                return "Say-now timed out - check the station."
            except OSError as e:
                return "Say-now could not run: %s" % e
        if action in ("voice_on", "voice_off", "rf_on", "rf_off"):
            flag = BEACON / ("voice.enabled" if action.startswith("voice")
                             else "rf.enabled")
            if action.endswith("_on"):
                flag.touch()
                return "%s is ON." % flag.name.split(".")[0].upper()
            try:
                flag.unlink()
            except FileNotFoundError:
                pass
            return "%s is OFF." % flag.name.split(".")[0].upper()
        return "Unknown action."

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
