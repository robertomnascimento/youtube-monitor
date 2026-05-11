#!/usr/bin/env python3
"""
YouTube Channel Monitor
Monitora canais do YouTube, gera resumos com Claude e envia por e-mail
"""

import os
import json
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.service_account import Credentials
from anthropic import Anthropic


class YouTubeMonitor:
    def __init__(self):
        self.claude = Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))
        self.youtube = None
        self.gmail = None
        self.drive = None
        self.config = self.load_config()
        self.setup_google_apis()

    def load_config(self):
        """Carrega configuração do arquivo JSON"""
        config_file = os.getenv("CONFIG_FILE", "config.json")
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"⚠️  Arquivo {config_file} não encontrado!")
            return {"channels": [], "email_to": ""}

    def setup_google_apis(self):
        """Configura APIs do Google usando credenciais de conta de serviço.

        IMPORTANTE: Para envio de e-mail via Gmail com service account é necessário
        configurar delegação de domínio (Google Workspace) e informar 'delegated_email'
        no config.json. Para contas Gmail pessoais, utilize OAuth 2.0.
        """
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
            ],
        )

        # Delegação de domínio — necessária para Gmail via service account
        delegated_email = self.config.get("delegated_email")
        if delegated_email:
            credentials = credentials.with_subject(delegated_email)

        self.youtube = build("youtube", "v3", credentials=credentials)
        self.drive = build("drive", "v3", credentials=credentials)
        print("✅ APIs Google configuradas")

    def get_channel_id(self, channel_handle):
        """Obtém o ID do canal a partir do handle (@usuario)"""
        try:
            # forHandle é a forma correta e precisa de buscar por handle
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

    def get_recent_videos(self, channel_id, hours=24):
        """Busca vídeos publicados nas últimas N horas"""
        try:
            published_after = (
                datetime.now(timezone.utc) - timedelta(hours=hours)
            ).isoformat()

            response = (
                self.youtube.search()
                .list(
                    part="snippet",
                    channelId=channel_id,
                    publishedAfter=published_after,
                    order="date",
                    type="video",
                    maxResults=10,
                )
                .execute()
            )

            videos = []
            for item in response.get("items", []):
                videos.append(
                    {
                        "title": item["snippet"]["title"],
                        "description": item["snippet"]["description"],
                        "video_id": item["id"]["videoId"],
                        "published_at": item["snippet"]["publishedAt"],
                        "channel_title": item["snippet"]["channelTitle"],
                    }
                )
            return videos
        except Exception as e:
            print(f"❌ Erro ao buscar vídeos: {e}")
            return []

    def summarize_video(self, title, description):
        """Gera resumo do vídeo usando Claude com prompt caching"""
        try:
            message = self.claude.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=300,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    f"Faça um resumo conciso (2-3 linhas) deste vídeo "
                                    f"do YouTube em português:\n\n"
                                    f"Título: {title}\n"
                                    f"Descrição: {description}\n\n"
                                    f"Resumo:"
                                ),
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    }
                ],
            )
            return message.content[0].text.strip()
        except Exception as e:
            print(f"❌ Erro ao resumir: {e}")
            return "Não foi possível gerar resumo"

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
                print(f"✅ Encontrados {len(videos)} vídeos novos")
                all_videos[channel_handle] = videos
            else:
                print("ℹ️  Nenhum vídeo novo nas últimas 24h")

        return all_videos

    def create_email_body(self, videos_by_channel):
        """Cria o corpo do e-mail com resumos em HTML"""
        if not videos_by_channel:
            return "<p>Nenhum vídeo novo nos canais monitorados.</p>"

        html = "<html><body style='font-family: Arial, sans-serif;'>"
        html += (
            f"<h2>📺 Resumo de Vídeos - "
            f"{datetime.now().strftime('%d/%m/%Y')}</h2>"
        )

        for channel, videos in videos_by_channel.items():
            html += f"<h3>@{channel}</h3>"
            for video in videos:
                summary = self.summarize_video(video["title"], video["description"])
                html += f"""
                <div style='border-left: 3px solid #1f1f1f; padding-left: 10px; margin: 15px 0;'>
                    <p><strong>🎬 {video['title']}</strong></p>
                    <p style='color: #666;'>{summary}</p>
                    <p><small>📅 {video['published_at'][:10]}</small></p>
                    <p><a href='https://youtube.com/watch?v={video["video_id"]}'>
                        Assistir no YouTube →
                    </a></p>
                </div>
                """

        html += "</body></html>"
        return html

    def send_email(self, to_email, subject, html_body):
        """Envia e-mail via SMTP do Gmail com senha de app"""
        smtp_user = os.getenv("GMAIL_USER") or self.config.get("gmail_user", "")
        smtp_password = os.getenv("GMAIL_APP_PASSWORD") or self.config.get("gmail_app_password", "")

        if not smtp_user or not smtp_password:
            print("⚠️  GMAIL_USER ou GMAIL_APP_PASSWORD não configurados")
            return False

        try:
            message = MIMEMultipart("alternative")
            message["to"] = to_email
            message["from"] = smtp_user
            message["subject"] = subject
            message.attach(MIMEText(html_body, "html"))

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(smtp_user, smtp_password)
                server.sendmail(smtp_user, to_email, message.as_string())

            print(f"✅ E-mail enviado para {to_email}")
            return True
        except Exception as e:
            print(f"❌ Erro ao enviar e-mail: {e}")
            return False

    def save_to_drive(self, filename, filepath):
        """Salva o resumo no Google Drive"""
        if not self.drive:
            print("⚠️  Google Drive não configurado")
            return False

        try:
            folder_id = self.config.get("google_drive_folder_id")
            if not folder_id:
                print("⚠️  Google Drive Folder ID não configurado")
                return False

            file_metadata = {"name": filename, "parents": [folder_id]}

            # MediaFileUpload é obrigatório para envio de arquivos via Drive API
            media = MediaFileUpload(filepath, mimetype="text/html")

            file = (
                self.drive.files()
                .create(body=file_metadata, media_body=media, fields="id")
                .execute()
            )

            print(f"✅ Arquivo salvo no Google Drive: {filename} (id: {file.get('id')})")
            return True
        except Exception as e:
            print(f"❌ Erro ao salvar no Drive: {e}")
            return False

    def run(self):
        """Executa o monitoramento completo"""
        print("🚀 Iniciando monitoramento de canais YouTube...")

        videos = self.monitor_channels()

        if not videos:
            print("ℹ️  Nenhum vídeo novo encontrado")
            return

        email_body = self.create_email_body(videos)

        # Envia e-mail
        email = self.config.get("email_to")
        subject = self.config.get("email_subject", "Resumo de Vídeos").format(
            data=datetime.now().strftime("%d/%m/%Y")
        )
        if email:
            self.send_email(email, subject, email_body)

        # Salva localmente
        os.makedirs("summaries", exist_ok=True)
        filename = f"resumo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        filepath = f"summaries/{filename}"

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(email_body)
        print(f"✅ Resumo salvo localmente: {filepath}")

        # Salva no Drive (sem commitar no repositório)
        self.save_to_drive(filename, filepath)

        print("\n✨ Monitoramento concluído!")


if __name__ == "__main__":
    monitor = YouTubeMonitor()
    monitor.run()
