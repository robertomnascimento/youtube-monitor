#!/usr/bin/env python3
"""
YouTube Channel Monitor
Monitora canais do YouTube, gera resumos com Claude e alimenta Google Sheets.

- Canais em "full_history_channels" (config.json) sem registros na planilha
  têm o histórico completo importado na primeira execução.
- Demais canais: janela dos últimos 34 dias.
- Vídeos já presentes na planilha (coluna Link do Vídeo) são ignorados.
"""

import os
import json
import requests
from datetime import datetime, timedelta, timezone
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from anthropic import Anthropic
import gspread

SHEET_HEADERS = [
    "Data", "Canal Youtube", "Assunto", "Resumo", "Entrevistado",
    "Contato no Hubspot", "Empresa Entrevistada", "Empresa no Hubspot",
    "Observações", "Proprietário", "Link do Vídeo",
]


class YouTubeMonitor:
    def __init__(self):
        self.claude = Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))
        self.youtube = None
        self.drive = None
        self.sheets = None
        self.config = self.load_config()
        self.setup_google_apis()
        self.hubspot_api_key = os.getenv("HUBSPOT_API_KEY", "")

    def load_config(self):
        """Carrega configuração do arquivo JSON"""
        config_file = os.getenv("CONFIG_FILE", "config.json")
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"⚠️  Arquivo {config_file} não encontrado!")
            return {"channels": [], "spreadsheet_id": ""}

    def setup_google_apis(self):
        """Configura APIs do Google usando credenciais de conta de serviço"""
        credentials_path = os.getenv(
            "GOOGLE_CREDENTIALS_PATH", "credentials/google_credentials.json"
        )

        if not os.path.exists(credentials_path):
            print(f"⚠️  Credenciais não encontradas em {credentials_path}")
            return

        credentials = Credentials.from_service_account_file(
            credentials_path,
            scopes=[
                "https://www.googleapis.com/auth/youtube.readonly",
                "https://www.googleapis.com/auth/drive.file",
                "https://www.googleapis.com/auth/spreadsheets",
            ],
        )

        delegated_email = self.config.get("delegated_email")
        if delegated_email:
            credentials = credentials.with_subject(delegated_email)

        self.youtube = build("youtube", "v3", credentials=credentials)
        self.drive = build("drive", "v3", credentials=credentials)
        self.sheets = gspread.authorize(credentials)
        print("✅ APIs Google configuradas")

    # ------------------------------------------------------------------
    # YouTube
    # ------------------------------------------------------------------

    def get_channel_data(self, channel_handle):
        """Obtém ID do canal e a playlist de uploads a partir do handle"""
        try:
            response = (
                self.youtube.channels()
                .list(part="id,contentDetails", forHandle=channel_handle)
                .execute()
            )
            if response.get("items"):
                item = response["items"][0]
                return {
                    "channel_id": item["id"],
                    "uploads_playlist": item["contentDetails"][
                        "relatedPlaylists"
                    ]["uploads"],
                }
        except Exception as e:
            print(f"❌ Erro ao buscar canal {channel_handle}: {e}")
        return None

    def get_videos(self, uploads_playlist, since=None):
        """Lista vídeos da playlist de uploads (mais recentes primeiro).

        since=None busca o histórico completo do canal.
        A playlist de uploads custa 1 unidade de cota por página de 50
        vídeos (a busca antiga custava 100 unidades por chamada).
        """
        videos = []
        page_token = None
        try:
            while True:
                response = (
                    self.youtube.playlistItems()
                    .list(
                        part="snippet,contentDetails",
                        playlistId=uploads_playlist,
                        maxResults=50,
                        pageToken=page_token,
                    )
                    .execute()
                )

                reached_cutoff = False
                for item in response.get("items", []):
                    published = item["contentDetails"].get(
                        "videoPublishedAt", item["snippet"]["publishedAt"]
                    )
                    if since and published < since:
                        reached_cutoff = True
                        break
                    videos.append(
                        {
                            "title": item["snippet"]["title"],
                            "description": item["snippet"]["description"],
                            "video_id": item["contentDetails"]["videoId"],
                            "published_at": published,
                            "channel_title": item["snippet"]["channelTitle"],
                        }
                    )

                page_token = response.get("nextPageToken")
                if reached_cutoff or not page_token:
                    break

            return videos
        except Exception as e:
            print(f"❌ Erro ao buscar vídeos: {e}")
            return videos

    def monitor_channels(self, existing_channels):
        """Monitora todos os canais e coleta vídeos"""
        all_videos = {}
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=34)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        full_history = set(self.config.get("full_history_channels", []))

        for channel_handle in self.config.get("channels", []):
            print(f"\n📺 Monitorando: @{channel_handle}")

            data = self.get_channel_data(channel_handle)
            if not data:
                print(f"⚠️  Canal não encontrado: {channel_handle}")
                continue

            # Canal marcado para histórico completo e ainda sem registros
            is_new = (
                channel_handle in full_history
                and channel_handle not in existing_channels
            )
            if is_new:
                print("🆕 Canal novo — importando histórico completo")

            videos = self.get_videos(
                data["uploads_playlist"], since=None if is_new else cutoff
            )
            if videos:
                print(f"✅ Encontrados {len(videos)} vídeos")
                all_videos[channel_handle] = videos
            else:
                print("ℹ️  Nenhum vídeo encontrado no período")

        return all_videos

    # ------------------------------------------------------------------
    # Claude
    # ------------------------------------------------------------------

    @staticmethod
    def parse_json_response(text):
        """Extrai JSON da resposta do Claude, mesmo com cercas de código"""
        text = text.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            raise json.JSONDecodeError("JSON não encontrado", text, 0)
        return json.loads(text[start:end + 1])

    def extract_video_info(self, title, description):
        """Extrai informações do vídeo usando Claude"""
        fallback = {
            "assunto": title[:50],
            "resumo": description[:200],
            "entrevistado": None,
            "empresa_mencionada": None,
        }
        try:
            message = self.claude.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=500,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    f"Analise este vídeo de um canal do mercado imobiliário brasileiro "
                                    f"e extraia as informações em JSON:\n\n"
                                    f"Título: {title}\n"
                                    f"Descrição: {description}\n\n"
                                    f"Retorne um JSON com estes campos:\n"
                                    f'- "assunto": tema principal (máx 50 caracteres)\n'
                                    f'- "resumo": resumo curto (2-3 linhas)\n'
                                    f'- "entrevistado": nome completo da pessoa entrevistada ou que dá depoimento. '
                                    f"Procure com atenção no título e na descrição por nomes de pessoas "
                                    f"(convidados, especialistas, clientes, diretores). Se realmente não houver, use null\n"
                                    f'- "empresa_mencionada": nome da imobiliária/empresa entrevistada ou citada '
                                    f"(ignore a empresa dona do canal). Se não houver, use null\n\n"
                                    f"Responda somente com o JSON puro, sem cercas de código e sem explicações."
                                ),
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    }
                ],
            )

            try:
                return self.parse_json_response(message.content[0].text)
            except json.JSONDecodeError:
                print(f"⚠️  Resposta do Claude não era JSON válido para: {title[:60]}")
                return fallback
        except Exception as e:
            print(f"❌ Erro ao extrair informações: {e}")
            return fallback

    # ------------------------------------------------------------------
    # Hubspot
    # ------------------------------------------------------------------

    def search_hubspot_contact(self, email_or_name):
        """Busca contato no Hubspot"""
        if not self.hubspot_api_key:
            return None, False

        try:
            url = "https://api.hubapi.com/crm/v3/objects/contacts/search"
            # "query" faz busca livre em nome, sobrenome e e-mail
            payload = {"query": str(email_or_name), "limit": 1}

            headers = {
                "Authorization": f"Bearer {self.hubspot_api_key}",
                "Content-Type": "application/json"
            }

            response = requests.post(url, json=payload, headers=headers)
            if response.status_code == 200:
                if response.json().get("results"):
                    contact = response.json()["results"][0]
                    return contact["id"], True
            else:
                print(f"⚠️  Hubspot contatos retornou {response.status_code}: {response.text[:150]}")
            return None, False
        except Exception as e:
            print(f"⚠️  Erro ao buscar Hubspot: {e}")
            return None, False

    def search_hubspot_company(self, company_name):
        """Busca empresa no Hubspot"""
        if not self.hubspot_api_key:
            return None, False, None, None

        try:
            url = "https://api.hubapi.com/crm/v3/objects/companies/search"
            payload = {
                "query": str(company_name),
                "limit": 1,
                "properties": ["name", "hubspot_owner_id"],
            }

            headers = {
                "Authorization": f"Bearer {self.hubspot_api_key}",
                "Content-Type": "application/json"
            }

            response = requests.post(url, json=payload, headers=headers)
            if response.status_code == 200:
                if response.json().get("results"):
                    company = response.json()["results"][0]
                    owner_id = company.get("properties", {}).get("hubspot_owner_id")
                    owner_name = self.get_hubspot_owner_name(owner_id) if owner_id else None
                    return company["id"], True, owner_id, owner_name
            else:
                print(f"⚠️  Hubspot empresas retornou {response.status_code}: {response.text[:150]}")
            return None, False, None, None
        except Exception as e:
            print(f"⚠️  Erro ao buscar empresa no Hubspot: {e}")
            return None, False, None, None

    def create_hubspot_note(self, company_id, note_body, owner_id=None):
        """Cria uma observação (note) associada à empresa no Hubspot"""
        if not self.hubspot_api_key or not company_id:
            return False

        try:
            url = "https://api.hubapi.com/crm/v3/objects/notes"
            properties = {
                "hs_note_body": note_body,
                "hs_timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if owner_id:
                properties["hubspot_owner_id"] = owner_id

            payload = {
                "properties": properties,
                "associations": [
                    {
                        "to": {"id": company_id},
                        "types": [
                            {
                                "associationCategory": "HUBSPOT_DEFINED",
                                "associationTypeId": 190,  # note -> company
                            }
                        ],
                    }
                ],
            }

            headers = {
                "Authorization": f"Bearer {self.hubspot_api_key}",
                "Content-Type": "application/json"
            }

            response = requests.post(url, json=payload, headers=headers)
            if response.status_code == 201:
                print(f"✅ Observação criada no Hubspot (empresa {company_id})")
                return True
            print(f"⚠️  Falha ao criar observação ({response.status_code}): {response.text[:150]}")
            return False
        except Exception as e:
            print(f"⚠️  Erro ao criar observação no Hubspot: {e}")
            return False

    def get_hubspot_owner_name(self, owner_id):
        """Obtém nome do proprietário no Hubspot"""
        if not self.hubspot_api_key or not owner_id:
            return None

        try:
            url = f"https://api.hubapi.com/crm/v3/owners/{owner_id}"
            headers = {
                "Authorization": f"Bearer {self.hubspot_api_key}",
                "Content-Type": "application/json"
            }

            response = requests.get(url, headers=headers)
            if response.status_code == 200:
                owner = response.json()
                first = owner.get("firstName", "")
                last = owner.get("lastName", "")
                name = f"{first} {last}".strip()
                return name or None
            return None
        except Exception as e:
            print(f"⚠️  Erro ao buscar proprietário: {e}")
            return None

    # ------------------------------------------------------------------
    # Google Sheets
    # ------------------------------------------------------------------

    def get_sheet_state(self):
        """Abre a planilha e retorna (worksheet, links existentes, canais existentes)"""
        if not self.sheets:
            print("⚠️  Google Sheets não configurado")
            return None, set(), set()

        spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID") or self.config.get("spreadsheet_id")
        if not spreadsheet_id:
            print("⚠️  GOOGLE_SHEETS_ID não configurado")
            return None, set(), set()

        spreadsheet = self.sheets.open_by_key(spreadsheet_id)
        worksheet = spreadsheet.sheet1

        rows = worksheet.get_all_values()
        if len(rows) == 0:
            worksheet.insert_row(SHEET_HEADERS, 1)

        existing_links = {
            row[10].strip()
            for row in rows[1:]
            if len(row) > 10 and row[10].strip()
        }
        existing_channels = {
            row[1].strip()
            for row in rows[1:]
            if len(row) > 1 and row[1].strip()
        }
        return worksheet, existing_links, existing_channels

    def add_to_sheets(self, videos_by_channel, worksheet, existing_links):
        """Adiciona vídeos novos à planilha e registra observações no Hubspot"""
        new_count = 0
        skipped_count = 0

        for channel_handle, videos in videos_by_channel.items():
            for video in videos:
                video_link = f"https://youtube.com/watch?v={video['video_id']}"
                if video_link in existing_links:
                    skipped_count += 1
                    continue

                # Extrai informações
                info = self.extract_video_info(video["title"], video["description"])

                # Busca contato e empresa no Hubspot
                contact_exists = False
                company_exists = False
                company_id = None
                owner_id = None
                owner_name = None

                if info.get("entrevistado"):
                    _, contact_exists = self.search_hubspot_contact(info["entrevistado"])

                if info.get("empresa_mencionada"):
                    company_id, company_exists, owner_id, owner_name = (
                        self.search_hubspot_company(info["empresa_mencionada"])
                    )

                # Formata data
                pub_date = video["published_at"].split("T")[0]

                # Cria observação na planilha
                observation = f"🎥 {video_link}"
                if owner_name:
                    observation += f" | @{owner_name}"

                # Se a empresa está no Hubspot, registra observação lá também
                if company_exists and company_id:
                    note_body = (
                        f"<p>🎥 Empresa citada em vídeo no YouTube "
                        f"(canal @{channel_handle}):</p>"
                        f"<p><strong>{video['title']}</strong></p>"
                        f"<p>{info.get('resumo', '')}</p>"
                        f"<p><a href='{video_link}'>{video_link}</a></p>"
                    )
                    if owner_name:
                        note_body += f"<p>Responsável: @{owner_name}</p>"
                    self.create_hubspot_note(company_id, note_body, owner_id)

                # Prepara linha
                row = [
                    pub_date,
                    channel_handle,
                    info.get("assunto", "")[:50],
                    info.get("resumo", ""),
                    info.get("entrevistado") or "",
                    "Sim" if contact_exists else "Não",
                    info.get("empresa_mencionada") or "",
                    "Sim" if company_exists else "Não",
                    observation,
                    f"@{owner_name}" if owner_name else "",
                    video_link,
                ]

                # Adiciona à planilha
                worksheet.append_row(row)
                existing_links.add(video_link)
                new_count += 1
                print(f"✅ Adicionado: {info.get('assunto', '')} - {channel_handle}")

        print(
            f"✅ Planilha atualizada: {new_count} novos, "
            f"{skipped_count} já registrados (ignorados)"
        )

    # ------------------------------------------------------------------

    def run(self):
        """Executa o monitoramento completo"""
        print("🚀 Iniciando monitoramento de canais YouTube...")

        worksheet, existing_links, existing_channels = self.get_sheet_state()
        if worksheet is None:
            return

        videos = self.monitor_channels(existing_channels)

        if not videos:
            print("ℹ️  Nenhum vídeo novo encontrado")
            return

        self.add_to_sheets(videos, worksheet, existing_links)

        print("\n✨ Monitoramento concluído!")


if __name__ == "__main__":
    monitor = YouTubeMonitor()
    monitor.run()
