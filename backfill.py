#!/usr/bin/env python3
"""
Backfill pontual da janela em que a API da Anthropic ficou sem crédito.

Reprocessa linhas JÁ EXISTENTES na planilha (não cria linhas novas):
  - filtro: Data >= START_DATE, Entrevistado vazio E Empresa Entrevistada vazia
  - re-busca título/descrição no YouTube pelo video_id do link
  - roda extract_video_info() de novo (mesmo prompt da rotina diária)
  - preenche Assunto, Resumo, Entrevistado, Contato no Hubspot,
    Empresa Entrevistada, Empresa no Hubspot, Observações, Proprietário
  - cria a nota na empresa do Hubspot, DATADA NO DIA DE PUBLICAÇÃO do vídeo
    e marcada como registro retroativo

Uso:
  DRY_RUN=true  python backfill.py    # não escreve nada, só relata
  DRY_RUN=false python backfill.py    # escreve planilha + Hubspot
"""

import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone

import requests

from youtube_monitor import YouTubeMonitor

START_DATE = os.getenv("START_DATE", "2026-07-30")
END_DATE = os.getenv("END_DATE", "2026-09-01")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"
SKIP_HUBSPOT_NOTES = os.getenv("SKIP_HUBSPOT_NOTES", "false").lower() == "true"

# indices 0-based na planilha
C_DATA, C_CANAL, C_ASSUNTO, C_RESUMO, C_ENTREV = 0, 1, 2, 3, 4
C_CONTATO_HS, C_EMPRESA, C_EMPRESA_HS, C_OBS, C_OWNER, C_LINK = 5, 6, 7, 8, 9, 10


def cell(row, idx):
    return row[idx].strip() if len(row) > idx else ""


def video_id_from_link(link):
    if "watch?v=" not in link:
        return None
    return link.split("watch?v=")[1].split("&")[0].strip()



# ----------------------------------------------------------------------
# Validação de match no Hubspot
#
# O search do Hubspot usa `query` (substring em nome, domínio, telefone) com
# limit=1. Isso produz falsos positivos graves: "Google" casa CONSTRUTORA
# MASHIA (domínio business.google.com) e "CUB" casa cartórios de Cubatão.
# Só aceitamos o match se o nome extraído aparecer como token no nome do
# registro devolvido (ou vice-versa).
# ----------------------------------------------------------------------

NOMES_GENERICOS = {
    "google", "plaza", "grupo", "imobiliaria", "imoveis", "construtora",
    "brasil", "cub", "urbs", "youtube", "instagram", "whatsapp", "chatgpt",
}


def norm(txt):
    txt = unicodedata.normalize("NFKD", txt or "")
    txt = txt.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9 ]+", " ", txt.lower()).strip()


def match_confiavel(extraido, encontrado):
    """(ok, motivo) — extraido = o que a IA disse, encontrado = nome no Hubspot."""
    a, b = norm(extraido), norm(encontrado)
    if not a or not b:
        return False, "nome vazio"
    if len(a.replace(" ", "")) < 4:
        return False, f"sigla curta demais ('{extraido}')"
    if a in NOMES_GENERICOS:
        return False, f"nome genérico ('{extraido}')"
    if re.search(rf"\b{re.escape(a)}\b", b):
        return True, "ok"
    if re.search(rf"\b{re.escape(b)}\b", a):
        return True, "ok (nome do Hubspot contido no extraído)"
    tokens = [t for t in a.split() if len(t) >= 4]
    if tokens and all(re.search(rf"\b{re.escape(t)}\b", b) for t in tokens):
        return True, "ok (todos os tokens)"
    return False, f"divergente → Hubspot devolveu '{encontrado}'"


class Backfiller(YouTubeMonitor):
    def search_company_validated(self, nome_extraido):
        """Busca empresa e valida o match. Devolve (id, ok, owner_id, owner_name, motivo)."""
        if not self.hubspot_api_key:
            return None, False, None, None, "sem chave do Hubspot"
        try:
            r = requests.post(
                "https://api.hubapi.com/crm/v3/objects/companies/search",
                json={"query": str(nome_extraido), "limit": 1,
                      "properties": ["name", "hubspot_owner_id"]},
                headers={"Authorization": f"Bearer {self.hubspot_api_key}",
                         "Content-Type": "application/json"},
            )
            if r.status_code != 200:
                return None, False, None, None, f"Hubspot {r.status_code}"
            results = r.json().get("results") or []
            if not results:
                return None, False, None, None, "sem resultado"
            comp = results[0]
            nome_hs = comp.get("properties", {}).get("name", "")
            ok, motivo = match_confiavel(nome_extraido, nome_hs)
            if not ok:
                return None, False, None, None, motivo
            owner_id = comp.get("properties", {}).get("hubspot_owner_id")
            owner_name = self.get_hubspot_owner_name(owner_id) if owner_id else None
            return comp["id"], True, owner_id, owner_name, f"ok → '{nome_hs}'"
        except Exception as e:
            return None, False, None, None, f"erro: {e}"

    def fetch_snippets(self, video_ids):
        """Busca title/description/publishedAt em lotes de 50."""
        out = {}
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i:i + 50]
            try:
                resp = self.youtube.videos().list(
                    part="snippet", id=",".join(batch)
                ).execute()
            except Exception as e:
                print(f"❌ Erro ao buscar lote {i//50 + 1}: {e}")
                continue
            for item in resp.get("items", []):
                sn = item["snippet"]
                out[item["id"]] = {
                    "title": sn.get("title", ""),
                    "description": sn.get("description", ""),
                    "published_at": sn.get("publishedAt", ""),
                }
            print(f"  lote {i//50 + 1}: {len(resp.get('items', []))}/{len(batch)} vídeos retornados")
        return out

    def create_note_backdated(self, company_id, note_body, owner_id, iso_timestamp):
        """Igual a create_hubspot_note, mas com hs_timestamp na data do vídeo."""
        if not self.hubspot_api_key or not company_id:
            return False
        properties = {"hs_note_body": note_body, "hs_timestamp": iso_timestamp}
        if owner_id:
            properties["hubspot_owner_id"] = owner_id
        payload = {
            "properties": properties,
            "associations": [{
                "to": {"id": company_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED",
                           "associationTypeId": 190}],
            }],
        }
        try:
            r = requests.post(
                "https://api.hubapi.com/crm/v3/objects/notes",
                json=payload,
                headers={"Authorization": f"Bearer {self.hubspot_api_key}",
                         "Content-Type": "application/json"},
            )
            if r.status_code == 201:
                return True
            print(f"  ⚠️  nota falhou ({r.status_code}): {r.text[:120]}")
            return False
        except Exception as e:
            print(f"  ⚠️  erro na nota: {e}")
            return False

    def run_backfill(self):
        modo = "DRY-RUN (nada será escrito)" if DRY_RUN else "ESCRITA REAL"
        print(f"🔁 Backfill {START_DATE} → {END_DATE} | modo: {modo}")
        if SKIP_HUBSPOT_NOTES:
            print("   (notas no Hubspot desativadas por SKIP_HUBSPOT_NOTES)")

        worksheet, _, _ = self.get_sheet_state()
        if worksheet is None:
            print("❌ Planilha inacessível")
            return 1

        rows = worksheet.get_all_values()
        print(f"📄 Planilha: {len(rows) - 1} linhas de dados")

        alvos = []
        for n, row in enumerate(rows[1:], start=2):  # n = linha real na planilha
            data = cell(row, C_DATA)
            if not (START_DATE <= data <= END_DATE):
                continue
            if cell(row, C_ENTREV) or cell(row, C_EMPRESA):
                continue
            vid = video_id_from_link(cell(row, C_LINK))
            if not vid:
                print(f"  ⚠️  L{n}: link inválido, pulando")
                continue
            alvos.append({"linha": n, "vid": vid, "canal": cell(row, C_CANAL),
                          "data": data})

        print(f"🎯 {len(alvos)} linhas a reprocessar")
        if not alvos:
            return 0

        print("📥 Buscando títulos/descrições no YouTube...")
        snippets = self.fetch_snippets([a["vid"] for a in alvos])

        updates = []
        notas_previstas = []
        rejeitados = []
        stats = {"extraido": 0, "sem_video": 0, "com_entrev": 0,
                 "com_empresa": 0, "contato_sim": 0, "empresa_sim": 0,
                 "notas_criadas": 0, "match_rejeitado": 0}

        for a in alvos:
            sn = snippets.get(a["vid"])
            if not sn:
                stats["sem_video"] += 1
                print(f"  ⚠️  L{a['linha']}: vídeo {a['vid']} não retornado (removido/privado)")
                continue

            info = self.extract_video_info(sn["title"], sn["description"])
            stats["extraido"] += 1

            contact_exists = False
            company_exists = False
            company_id = owner_id = owner_name = None

            if info.get("entrevistado"):
                stats["com_entrev"] += 1
                _, contact_exists = self.search_hubspot_contact(info["entrevistado"])
                if contact_exists:
                    stats["contato_sim"] += 1
                time.sleep(0.3)

            if info.get("empresa_mencionada"):
                stats["com_empresa"] += 1
                company_id, company_exists, owner_id, owner_name, motivo = (
                    self.search_company_validated(info["empresa_mencionada"])
                )
                if company_exists:
                    stats["empresa_sim"] += 1
                elif not motivo.startswith("sem resultado"):
                    stats["match_rejeitado"] += 1
                    rejeitados.append((a["linha"], info["empresa_mencionada"], motivo))
                time.sleep(0.3)

            video_link = f"https://youtube.com/watch?v={a['vid']}"
            observation = f"🎥 {video_link}"
            if owner_name:
                observation += f" | @{owner_name}"

            pub_iso = sn["published_at"] or f"{a['data']}T12:00:00Z"
            pub_br = a["data"]
            try:
                pub_br = datetime.fromisoformat(
                    pub_iso.replace("Z", "+00:00")
                ).strftime("%d/%m/%Y")
            except Exception:
                pass

            if company_exists and company_id and not SKIP_HUBSPOT_NOTES:
                note_body = (
                    f"<p>🎥 Empresa citada em vídeo no YouTube "
                    f"(canal @{a['canal']}) — <strong>publicado em {pub_br}</strong>:</p>"
                    f"<p><strong>{sn['title']}</strong></p>"
                    f"<p>{info.get('resumo', '')}</p>"
                    f"<p><a href='{video_link}'>{video_link}</a></p>"
                )
                if owner_name:
                    note_body += f"<p>Responsável: @{owner_name}</p>"
                note_body += (
                    "<p><em>Registro retroativo: a automação de monitoramento do "
                    "YouTube ficou sem crédito de API entre 30/07/2026 e 01/09/2026 "
                    "e não registrou este vídeo na época.</em></p>"
                )
                notas_previstas.append(
                    (a["linha"], info["empresa_mencionada"], company_id, pub_br,
                     motivo.replace("ok → ", "").strip("'"))
                )
                if not DRY_RUN:
                    if self.create_note_backdated(company_id, note_body,
                                                  owner_id, pub_iso):
                        stats["notas_criadas"] += 1
                    time.sleep(0.3)

            updates.append({
                "range": f"C{a['linha']}:J{a['linha']}",
                "values": [[
                    info.get("assunto", "")[:50],
                    info.get("resumo", ""),
                    info.get("entrevistado") or "",
                    "Sim" if contact_exists else "Não",
                    info.get("empresa_mencionada") or "",
                    "Sim" if company_exists else "Não",
                    observation,
                    f"@{owner_name}" if owner_name else "",
                ]],
            })
            print(f"  L{a['linha']} {a['data']} @{a['canal']:22} | "
                  f"ent={(info.get('entrevistado') or '-')[:22]:22} | "
                  f"emp={(info.get('empresa_mencionada') or '-')[:20]:20} | "
                  f"HS contato={'Sim' if contact_exists else 'Não'} "
                  f"empresa={'Sim' if company_exists else 'Não'}")

        if not DRY_RUN and updates:
            print(f"\n💾 Gravando {len(updates)} linhas na planilha...")
            for i in range(0, len(updates), 20):
                worksheet.batch_update(updates[i:i + 20])
                print(f"  gravado lote {i//20 + 1}")
                time.sleep(1.5)

        print("\n" + "=" * 60)
        print(f"RESUMO ({modo})")
        print(f"  linhas alvo:                 {len(alvos)}")
        print(f"  vídeos indisponíveis:        {stats['sem_video']}")
        print(f"  extrações feitas:            {stats['extraido']}")
        print(f"  com entrevistado:            {stats['com_entrev']}")
        print(f"  └─ achado no Hubspot:        {stats['contato_sim']}")
        print(f"  com empresa:                 {stats['com_empresa']}")
        print(f"  └─ achada no Hubspot:        {stats['empresa_sim']}")
        print(f"  matches rejeitados:          {stats['match_rejeitado']}")
        print(f"  notas a criar no Hubspot:    {len(notas_previstas)}")
        if not DRY_RUN:
            print(f"  notas efetivamente criadas:  {stats['notas_criadas']}")
        print("=" * 60)

        if notas_previstas:
            print("\nEMPRESAS QUE RECEBERIAM NOTA (extraído → registro no Hubspot):")
            from collections import Counter
            pares = Counter((n[1], n[4]) for n in notas_previstas)
            for (emp, nome_hs), qtd in pares.most_common():
                print(f"  {qtd:3}x  {emp}  →  {nome_hs}")

        if rejeitados:
            print("\nMATCHES REJEITADOS (nota NÃO seria criada):")
            from collections import Counter
            for (emp, motivo), qtd in Counter(
                    (r[1], r[2]) for r in rejeitados).most_common():
                print(f"  {qtd:3}x  {emp}: {motivo}")
        return 0


if __name__ == "__main__":
    sys.exit(Backfiller().run_backfill())
