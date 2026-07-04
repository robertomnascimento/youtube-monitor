#!/usr/bin/env python3
"""
YouTube Channel Monitor
Monitora canais do YouTube, gera resumos com Claude e alimenta Google Sheets
"""

import os
import json
import requests
from datetime import datetime, timedelta, timezone
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.service_account import Credentials
from anthropic import Anthropic
import gspread


class YouTubeMonitor:
    def __init__(self):
        self.claude = Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))
        self.youtube = None
        self.gmail = None
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
            return {"channels": [], "email_to": "", "spreadsheet_id": ""}

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

    def get_channel_id(self, channel_handle):
        """Obtém o ID do canal a partir do handle (@usuario)"""
        try:
            response = (
                self.youtube.channels()
                .list(part="id", forHandle=channel_handle)
                .execute()
            )
            if response.get("items"):
                return response["items"][0]["id"]
        except Exception as e:
            print(f"❌ Erro ao buscar canal {channel_handle}: {e}")
        return None

    def get_recent_videos(self, channel_id, days=34):
        """Busca vídeos publicados nos últimos N dias (padrão: desde 01 junho)"""
        try:
            published_after = (
                datetime.now(timezone.utc) - timedelta(days=days)
            ).isoformat()

            response = (
                self.youtube.search()
                .list(
                    part="snippet",
                    channelId=channel_id,
                    publishedAfter=published_after,
                    order="date",
                    type="video",
                    maxResults=50,
                )
                .execute()
            )

            videos = []
            video_ids = [
                item["id"]["videoId"] for item in response.get("items", [])
            ]

            # A busca retorna descrições truncadas; videos().list traz a completa
            full_desc = {}
            if video_ids:
                details = (
                    self.youtube.videos()
                    .list(part="snippet", id=",".join(video_ids))
                    .execute()
                )
                for v in details.get("items", []):
                    full_desc[v["id"]] = v["snippet"]["description"]

            for item in response.get("items", []):
                vid = item["id"]["videoId"]
                videos.append(
                    {
                        "title": item["snippet"]["title"],
                        "description": full_desc.get(
                            vid, item["snippet"]["description"]
                        ),
                        "video_id": vid,
                        "published_at": item["snippet"]["publishedAt"],
                        "channel_title": item["snippet"]["channelTitle"],
                        "channel_handle": item["snippet"]["channelId"],
                    }
                )
            return videos
        except Exception as e:
            print(f"❌ Erro ao buscar vídeos: {e}")
            return []

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
            if response.status_code == 200 and response.json().get("results"):
                contact = response.json()["results"][0]
                return contact["id"], True
            return None, False
        except Exception as e:
            print(f"⚠️  Erro ao buscar Hubspot: {e}")
            return None, False

    def search_hubspot_company(self, company_name):
        """Busca empresa no Hubspot"""
        if not self.hubspot_api_key:
            return None, False

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
            if response.status_code == 200 and response.json().get("results"):
                company = response.json()["results"][0]
                owner_id = company.get("properties", {}).get("hubspot_owner_id")
                owner_name = self.get_hubspot_owner_name(owner_id) if owner_id else None
                return company["id"], True, owner_name
            return None, False, None
        except Exception as e:
            print(f"⚠️  Erro ao buscar empresa no Hubspot: {e}")
            return None, False, None

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

    def monitor_channels(self):
        """Monitora todos os canais e coleta vídeos novos"""
        all_videos = {}

        for channel_handle in self.config.get("channels", []):
            print(f"\n📺 Monitorando: @{channel_handle}")

            channel_id = self.get_channel_id(channel_handle)
            if not channel_id:
                print(f"⚠️  Canal não encontrado: {channel_handle}")
                continue

            videos = self.get_recent_videos(channel_id)
            if videos:
                print(f"✅ Encontrados {len(videos)} vídeos")
                all_videos[channel_handle] = videos
            else:
                print("ℹ️  Nenhum vídeo encontrado")

        return all_videos

    def add_to_sheets(self, videos_by_channel):
        """Adiciona vídeos à planilha Google Sheets"""
        if not self.sheets:
            print("⚠️  Google Sheets não configurado")
            return

        spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID") or self.config.get("spreadsheet_id")
        if not spreadsheet_id:
            print("⚠️  GOOGLE_SHEETS_ID não configurado")
            return

        try:
            spreadsheet = self.sheets.open_by_key(spreadsheet_id)
            worksheet = spreadsheet.sheet1

            # Adiciona cabeçalho se vazio
            if len(worksheet.get_all_values()) == 0:
                headers = [
                    "Data", "Canal Youtube", "Assunto", "Resumo", "Entrevistado",
                    "Contato no Hubspot", "Empresa Entrevistada", "Empresa no Hubspot",
                    "Observações", "Proprietário", "Link do Vídeo"
                ]
                worksheet.insert_row(headers, 1)

            # Adiciona vídeos
            for channel_handle, videos in videos_by_channel.items():
                for video in videos:
                    # Extrai informações
                    info = self.extract_video_info(video["title"], video["description"])

                    # Busca contato e empresa no Hubspot
                    contact_exists = False
                    company_exists = False
                    owner_name = None

                    if info.get("entrevistado"):
                        _, contact_exists = self.search_hubspot_contact(info["entrevistado"])

                    if info.get("empresa_mencionada"):
                        _, company_exists, owner_name = self.search_hubspot_company(info["empresa_mencionada"])

                    # Formata data
                    pub_date = video["published_at"].split("T")[0]

                    # Cria observação
                    observation = f"🎥 https://youtube.com/watch?v={video['video_id']}"
                    if owner_name:
                        observation += f" | @{owner_name}"

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
                        f"https://youtube.com/watch?v={video['video_id']}"
                    ]

                    # Adiciona à planilha
                    worksheet.append_row(row)
                    print(f"✅ Adicionado: {info.get('assunto', '')} - {channel_handle}")

            print(f"✅ Planilha atualizada: {spreadsheet_id}")

        except Exception as e:
            print(f"❌ Erro ao atualizar planilha: {e}")

    def run(self):
        """Executa o monitoramento completo"""
        print("🚀 Iniciando monitoramento de canais YouTube...")

        videos = self.monitor_channels()

        if not videos:
            print("ℹ️  Nenhum vídeo novo encontrado")
            return

        # Adiciona à planilha Google Sheets
        self.add_to_sheets(videos)

        print("\n✨ Monitoramento concluído!")


if __name__ == "__main__":
    monitor = YouTubeMonitor()
    monitor.run()
