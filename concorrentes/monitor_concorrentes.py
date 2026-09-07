#!/usr/bin/env python3
"""
Monitor semanal dos canais concorrentes no YouTube.

Usa o feed RSS de cada canal (sem API key, sem cota, sem yt-dlp) para descobrir
o que foi publicado desde a ultima rodada. O RSS traz os 15 videos mais recentes
com data exata -- suficiente para uma checagem semanal, e o historico datado vai
crescendo a cada execucao.

Uso:
    python3 monitor_semanal.py                 # checa e imprime o relatorio
    python3 monitor_semanal.py --dias 30       # janela de destaque (padrao 7)
    python3 monitor_semanal.py --baixar        # baixa transcricao dos novos
    python3 monitor_semanal.py --resolver-ids  # refaz o cache de channel_id
    python3 monitor_semanal.py --quieto        # so escreve arquivos, sem stdout

Arquivos:
    canais.txt                 entrada: uma URL de canal por linha
    .canais_ids.json           cache handle -> channel_id (gerado)
    historico_videos.csv       todos os videos ja vistos, com data (gerado)
    relatorios/AAAA-MM-DD.md   relatorio da rodada (gerado)

Saida: exit code 0 sempre que rodou; 1 se nenhum feed pode ser lido.
"""

import argparse, csv, html, json, os, re, smtplib, subprocess, sys, urllib.request
import datetime as dt
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

BASE = os.path.dirname(os.path.abspath(__file__))
CANAIS = os.path.join(BASE, "canais.txt")
CACHE_IDS = os.path.join(BASE, ".canais_ids.json")
HISTORICO = os.path.join(BASE, "historico_videos.csv")
RELATORIOS = os.path.join(BASE, "relatorios")
YTDLP = os.path.expanduser("~/Library/Python/3.9/bin/yt-dlp")

NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"
CAMPOS = ["video_id", "canal", "handle", "titulo", "publicado_em", "url", "visto_em"]


def esc(t):
    """Titulo dentro de celula de tabela markdown: | quebra a coluna."""
    return (t or "").replace("|", "\\|").replace("\n", " ").strip()


def buscar(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def ler_canais():
    urls = []
    with open(CANAIS, encoding="utf-8") as f:
        for l in f:
            l = l.strip()
            if l and not l.startswith("#"):
                urls.append(l)
    return urls


def handle_de(url):
    m = re.search(r"/(@[^/]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/channel/([^/]+)", url)
    return m.group(1) if m else url


def resolver_ids(urls, forcar=False):
    """handle -> channel_id. O ID nunca muda, entao vale cache em disco."""
    cache = {}
    if os.path.exists(CACHE_IDS) and not forcar:
        cache = json.load(open(CACHE_IDS, encoding="utf-8"))
    novos = 0
    for u in urls:
        h = handle_de(u)
        if h in cache and cache[h]:
            continue
        m = re.search(r"/channel/(UC[\w-]{22})", u)
        if m:
            cache[h] = m.group(1)
        else:
            try:
                html = buscar(u)
                m = re.search(r'"(?:channelId|externalId)":"(UC[\w-]{22})"', html)
                cache[h] = m.group(1) if m else None
            except Exception as e:
                print(f"  ! nao resolvi {h}: {e}", file=sys.stderr)
                cache[h] = None
        novos += 1
    if novos:
        json.dump(cache, open(CACHE_IDS, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    return cache


def ler_feed(channel_id):
    """Retorna [{video_id,canal,titulo,publicado_em,url}] dos ~15 mais recentes."""
    xml = buscar(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
    raiz = ET.fromstring(xml)
    canal = raiz.findtext("a:title", namespaces=NS) or channel_id
    out = []
    for e in raiz.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", namespaces=NS)
        out.append({
            "video_id": vid,
            "canal": canal,
            "titulo": (e.findtext("a:title", namespaces=NS) or "").strip(),
            "publicado_em": e.findtext("a:published", namespaces=NS),
            "url": f"https://www.youtube.com/watch?v={vid}",
        })
    return out


def carregar_historico():
    if not os.path.exists(HISTORICO):
        return {}
    with open(HISTORICO, newline="", encoding="utf-8") as f:
        return {r["video_id"]: r for r in csv.DictReader(f) if r.get("video_id")}


def gravar_historico(hist):
    with open(HISTORICO, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        w.writeheader()
        for r in sorted(hist.values(), key=lambda x: x.get("publicado_em") or "", reverse=True):
            w.writerow({k: r.get(k, "") for k in CAMPOS})


def baixar_transcricoes(videos):
    script = os.path.join(BASE, "baixar_transcricoes.py")
    if not os.path.exists(script):
        print("  ! baixar_transcricoes.py nao encontrado; pulando", file=sys.stderr)
        return
    for v in videos:
        subprocess.run([sys.executable, script, "--url", v["url"]],
                       capture_output=True, text=True, timeout=300)


def montar_html(novos, recentes, cadencia, dias, agora):
    e = html.escape
    def linhas_novos():
        if not novos:
            return "<p style='color:#666'>Nenhum vídeo novo desde a última rodada.</p>"
        out = ["<table cellpadding='6' cellspacing='0' style='border-collapse:collapse;width:100%'>"]
        out.append("<tr style='background:#1F3864;color:#fff;text-align:left'>"
                   "<th>Data</th><th>Canal</th><th>Vídeo</th></tr>")
        for v in sorted(novos, key=lambda x: x["publicado_em"], reverse=True):
            d = dt.datetime.fromisoformat(v["publicado_em"])
            out.append(f"<tr style='border-bottom:1px solid #ddd'>"
                       f"<td style='white-space:nowrap'>{d:%d/%m}</td>"
                       f"<td>{e(v['canal'])}</td>"
                       f"<td><a href='{v['url']}'>{e(v['titulo'])}</a></td></tr>")
        out.append("</table>")
        return "".join(out)

    def tabela_cadencia():
        out = ["<table cellpadding='6' cellspacing='0' style='border-collapse:collapse;width:100%'>"]
        out.append("<tr style='background:#1F3864;color:#fff;text-align:left'>"
                   "<th>Canal</th><th>Último vídeo</th><th>Dias parado</th><th>30 dias</th></tr>")
        for parado, canal, n, ult, m30 in cadencia:
            cor = "#C00000" if parado > 180 else ("#B26B00" if parado > 30 else "#217346")
            out.append(f"<tr style='border-bottom:1px solid #ddd'>"
                       f"<td>{e(canal)}</td><td>{ult:%d/%m/%Y}</td>"
                       f"<td style='color:{cor};font-weight:bold'>{parado}</td><td>{m30}</td></tr>")
        out.append("</table>")
        return "".join(out)

    return f"""<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222;max-width:760px">
<h2 style="color:#1F3864;margin-bottom:2px">🔍 Concorrentes no YouTube — {agora:%d/%m/%Y}</h2>
<p style="color:#666;margin-top:0">{len(novos)} vídeo(s) novo(s) desde a última rodada ·
{len(recentes)} publicado(s) nos últimos {dias} dias</p>
<h3 style="color:#1F3864">Novidades</h3>
{linhas_novos()}
<h3 style="color:#1F3864;margin-top:26px">Cadência por canal</h3>
{tabela_cadencia()}
<p style="color:#888;font-size:12px;margin-top:26px">
Monitor de inteligência de produto. Fonte: feed RSS de cada canal.<br>
Repositório: robertomnascimento/youtube-monitor · pasta <code>concorrentes/</code>
</p></div>"""


def enviar_email(assunto, corpo_html, para):
    """Retorna None se enviou, ou a mensagem de erro. Nunca engole falha."""
    user = os.getenv("GMAIL_USER", "")
    senha = os.getenv("GMAIL_APP_PASSWORD", "")
    if not (user and senha and para):
        return "GMAIL_USER, GMAIL_APP_PASSWORD ou destinatario ausente"
    try:
        msg = MIMEMultipart("alternative")
        msg["subject"] = assunto
        msg["from"] = user
        msg["to"] = para
        msg.attach(MIMEText(corpo_html, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
            s.login(user, senha)
            s.sendmail(user, [p.strip() for p in para.split(",")], msg.as_string())
        return None
    except Exception as ex:
        return f"{type(ex).__name__}: {ex}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=7, help="janela de destaque no relatorio")
    ap.add_argument("--baixar", action="store_true", help="baixar transcricao dos videos novos")
    ap.add_argument("--resolver-ids", action="store_true", help="refazer o cache de channel_id")
    ap.add_argument("--quieto", action="store_true")
    ap.add_argument("--email", metavar="DEST", help="enviar o relatorio por e-mail (usa GMAIL_USER/GMAIL_APP_PASSWORD)")
    ap.add_argument("--email-sempre", action="store_true",
                    help="enviar mesmo sem videos novos (padrao: so envia quando ha novidade)")
    a = ap.parse_args()

    def diz(*x):
        if not a.quieto:
            print(*x)

    urls = ler_canais()
    ids = resolver_ids(urls, forcar=a.resolver_ids)
    hist = carregar_historico()
    primeira = not hist
    agora = dt.datetime.now(dt.timezone.utc)
    visto_em = agora.isoformat(timespec="seconds")

    novos, falhas, por_canal = [], [], {}
    for u in urls:
        h = handle_de(u)
        cid = ids.get(h)
        if not cid:
            falhas.append(h)
            continue
        try:
            itens = ler_feed(cid)
        except Exception as e:
            falhas.append(f"{h} ({type(e).__name__})")
            continue
        por_canal[h] = itens
        for it in itens:
            if it["video_id"] not in hist:
                it = dict(it, handle=h, visto_em=visto_em)
                hist[it["video_id"]] = it
                novos.append(it)

    if not por_canal:
        print("Nenhum feed pode ser lido. Sem alteracao no historico.", file=sys.stderr)
        return 1

    gravar_historico(hist)

    corte = agora - dt.timedelta(days=a.dias)
    def dtp(s):
        return dt.datetime.fromisoformat(s)
    recentes = sorted((v for v in hist.values() if v.get("publicado_em") and dtp(v["publicado_em"]) >= corte),
                      key=lambda v: v["publicado_em"], reverse=True)

    # ---------- relatorio ----------
    L = []
    L.append(f"# Monitor de concorrentes — {agora:%d/%m/%Y}\n")
    if primeira:
        L.append(f"> Primeira execucao: as {len(novos)} entradas dos feeds viraram a linha de base. "
                 f"A partir da proxima rodada, 'novos' significa realmente novos.\n")
    else:
        L.append(f"**{len(novos)} video(s) novo(s)** desde a ultima rodada.\n")

    if recentes:
        L.append(f"\n## Publicados nos ultimos {a.dias} dias ({len(recentes)})\n")
        L.append("| Data | Canal | Video |")
        L.append("|---|---|---|")
        for v in recentes:
            d = dtp(v["publicado_em"])
            marca = " 🆕" if v["video_id"] in {n["video_id"] for n in novos} and not primeira else ""
            L.append(f"| {d:%d/%m} | {esc(v['canal'])} | [{esc(v['titulo'])}]({v['url']}){marca} |")
    else:
        L.append(f"\n## Publicados nos ultimos {a.dias} dias\n\nNenhum.")

    L.append("\n## Cadencia por canal\n")
    L.append("| Canal | No feed | Ultimo video | Dias parado | 30 dias |")
    L.append("|---|---|---|---|---|")
    linhas = []
    for h, itens in por_canal.items():
        if not itens:
            continue
        canal = itens[0]["canal"]
        ult = max(dtp(i["publicado_em"]) for i in itens)
        parado = (agora - ult).days
        m30 = sum(1 for i in itens if (agora - dtp(i["publicado_em"])).days <= 30)
        linhas.append((parado, canal, len(itens), ult, m30))
    for parado, canal, n, ult, m30 in sorted(linhas):
        alerta = "" if parado <= 30 else (" ⚠️" if parado <= 180 else " 💤")
        linhas_ult = f"{ult:%d/%m/%Y}"
        L.append(f"| {esc(canal)} | {n} | {linhas_ult} | {parado}{alerta} | {m30} |")

    if falhas:
        L.append(f"\n## Falhas\n\n" + "\n".join(f"- {f}" for f in falhas))

    rel = "\n".join(L) + "\n"
    os.makedirs(RELATORIOS, exist_ok=True)
    caminho = os.path.join(RELATORIOS, f"{agora:%Y-%m-%d}.md")
    open(caminho, "w", encoding="utf-8").write(rel)

    diz(rel)
    diz(f"\nHistorico: {len(hist)} videos em {HISTORICO}")
    diz(f"Relatorio: {caminho}")

    if a.baixar and novos and not primeira:
        diz(f"\nBaixando transcricao de {len(novos)} video(s)...")
        baixar_transcricoes(novos)

    if a.email:
        if not novos and not a.email_sempre:
            diz("\nSem videos novos; e-mail nao enviado (use --email-sempre para forcar).")
        else:
            assunto = (f"🔍 Concorrentes: {len(novos)} vídeo(s) novo(s) — {agora:%d/%m/%Y}"
                       if novos else f"🔍 Concorrentes: sem novidades — {agora:%d/%m/%Y}")
            erro = enviar_email(assunto, montar_html(novos, recentes, sorted(linhas), a.dias, agora), a.email)
            if erro:
                # De proposito: falha de e-mail derruba o job. O monitor antigo
                # terminava "success" mesmo sem enviar, e a falha passava batida.
                print(f"ERRO ao enviar e-mail: {erro}", file=sys.stderr)
                return 2
            diz(f"\nE-mail enviado para {a.email}.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
