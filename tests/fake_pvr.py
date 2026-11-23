"""A local stand-in for the PVR pages the adapter uses, driven by a real browser in tests.

Markup is the real captured markup. The Doomsday ScreenX show is SYNTHETIC: it is made by editing the
captured Ramayana ScreenX listing, because no real one existed on 22 Nov 2026.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.conftest import fixture_text

PAGE = """<!doctype html><title>%(title)s</title><body><div id=root></div><script>
const root=document.getElementById('root');
if(!localStorage.getItem('city')){
  root.innerHTML='<div>Select City</div><div id=r>Delhi-NCR</div><div id=sub></div>';
  document.getElementById('r').onclick=()=>{const a=document.createElement('div');a.textContent='All';
    a.onclick=()=>{localStorage.setItem('city','Delhi-NCR');location.href='/'};document.getElementById('sub').replaceChildren(a)};
}else{load('')}
async function load(day){
  const r=await fetch('/api/v1/booking/content/msessions',{method:'POST',body:day});
  if(!r.ok)return;
  root.innerHTML=await r.text();
  document.querySelectorAll('.calnder-current li').forEach(li=>li.querySelector('div').onclick=()=>load(li.querySelector('h5').textContent));
  document.querySelectorAll('.box-slot-moviesession').forEach(b=>b.querySelector('.time-select').onclick=()=>click(b));
}
async function click(b){
  const r=await fetch('/click',{method:'POST',body:b.querySelector('h5').textContent});const j=await r.json();
  if(j.url){location.href=j.url}
  else if(j.notice){const d=document.createElement('div');d.className='p-dialog popup_rate';
    d.innerHTML='<p></p><button>Close</button><button>Accept</button>';d.querySelector('p').textContent=j.notice;
    if(j.buttons)d.querySelectorAll('button').forEach((x,i)=>x.textContent=j.buttons[i]);
    d.querySelectorAll('button')[1].onclick=()=>fetch('/accepted',{method:'POST'});document.body.append(d)}
}
</script>"""


def screenx_listing(time_text: str = "03:30 PM", colour: str = "", language: str = "English", header: str = "SCREEN X") -> str:
    html = fixture_text("pvr_screenx_control_trimmed.html").replace("RAMAYANA", "AVENGERS: DOOMSDAY")
    start = html.index("<h2>SCREEN X</h2>")
    stop = html.index("p-accordion-tab", start)
    group = html[start:stop].replace("Hindi", language).replace("03:25 PM", time_text)
    group = group.replace("seat-color-disable", colour, 1).replace("SCREEN X", header, 1)
    return html[:start] + group + html[stop:]


class FakePvr:
    """`mode` selects behaviour; `hits` records what the adapter asked for."""

    def __init__(self) -> None:
        self.mode = "absent"
        self.notice = "We accept only digital payments for all food and beverage purchases at the cinema."
        self.notice_buttons: list[str] | None = None
        self.listing = screenx_listing()
        self.seat_html = (
            fixture_text("pvr_seatlayout_trimmed.html")
            .replace("RAMAYANA", "AVENGERS: DOOMSDAY").replace("</span>Hindi", "</span>English")
            .replace("Sun, 22 Nov, 11:15 PM - 3:10 AM", "Sat, 19 Dec, 03:30 PM - 6:05 PM")
        )
        # PVR builds rows with React outside a <table>; an HTML parser would drop them, so serve them as divs.
        for tag in ("tr", "td"):
            self.seat_html = self.seat_html.replace(f"<{tag} ", "<div ").replace(f"</{tag}>", "</div>")
        self.dates = [("17", "Thu"), ("18", "Fri"), ("19", "Sat"), ("20", "Sun")]
        self.hits: list[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def _send(self, status: int, body: str, kind: str = "text/html", headers: dict[str, str] | None = None) -> None:
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", f"{kind}; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                fake.hits.append(f"GET {self.path}")
                if self.path.startswith("/seatlayout/"):
                    return self._send(200, fake.seat_html)
                if fake.mode == "403":
                    return self._send(403, "Access Denied")
                if fake.mode == "429":
                    return self._send(429, "slow down", headers={"Retry-After": "77"})
                if fake.mode == "500":
                    return self._send(500, "oops")
                if fake.mode == "hang":
                    time.sleep(8)
                if fake.mode == "challenge":
                    return self._send(200, "<title>Access Denied</title><body>You have been blocked")
                if fake.mode == "blank":
                    return self._send(200, "<title>PVR</title><body></body>")
                self._send(200, PAGE % {"title": "PVR"})

            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                fake.hits.append(f"POST {self.path} {body}")
                if self.path.endswith("/msessions"):
                    if fake.mode == "api500":
                        return self._send(500, "oops")
                    return self._send(200, fake.render(body))
                if self.path == "/click":
                    if fake.mode == "notice":
                        answer = {"notice": fake.notice, "buttons": fake.notice_buttons}
                    elif fake.mode == "deadclick":
                        answer = {}
                    else:
                        answer = {"url": "/seatlayout/tokenABC=="}
                    return self._send(200, json.dumps(answer), "application/json")
                self._send(200, "{}", "application/json")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.url = f"{self.base}/moviesessions/avengers--doomsday/35312"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def render(self, day: str) -> str:
        active = day or self.dates[0][0]
        source = fixture_text("pvr_doomsday_19dec_trimmed.html") if self.mode == "absent" or active != "19" else self.listing
        if self.mode == "wrongfilm":
            source = source.replace("AVENGERS: DOOMSDAY", "DUNE PART THREE")
        tabs = "".join(
            f'<li><div class="{"dates-pvr-active" if d == active else "dates-inactive"}"><span class="month">Dec</span>'
            f'<h5>{d}</h5><span class="day">{w}</span></div></li>'
            for d, w in self.dates
        )
        body = re.search(r"<body>(.*)</body>", source, re.DOTALL)
        assert body
        html = re.sub(r"<ul>.*?</ul>", f"<ul>{tabs}</ul>", body[1], count=1, flags=re.DOTALL)
        if self.mode == "empty":
            html = re.sub(r'<div class="p-accordion-tab.*', "", html, flags=re.DOTALL)
        return html

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
