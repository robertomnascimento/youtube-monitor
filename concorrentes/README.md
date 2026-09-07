# Monitor de concorrentes (semanal)

Inteligência de produto: acompanha os canais dos CRMs concorrentes no YouTube e
avisa por e-mail o que foi publicado desde a última rodada.

**Não confundir com o `youtube_monitor.py` da raiz.** São dois monitores com
objetivos diferentes, que só compartilham os secrets:

| | `youtube_monitor.py` (raiz) | `concorrentes/` |
|---|---|---|
| Objetivo | achar entrevistados e cruzar com HubSpot | acompanhar produto de concorrente |
| Cadência | diária, 8h | semanal, segunda 8h |
| Canais | mídia e conteúdo do setor | os 10 CRMs concorrentes + Pipeimob |
| Estado | Google Sheets | `historico_videos.csv` no próprio repo |
| Dependências | Claude, Google, gspread, HubSpot | nenhuma (só a biblioteca padrão) |
| Assunto do e-mail | `📺 YouTube Monitor:` | `🔍 Concorrentes:` |

## Como funciona

Lê o feed RSS de cada canal (`youtube.com/feeds/videos.xml?channel_id=UC…`), que
devolve os ~15 vídeos mais recentes com data exata. Sem API key, sem cota, sem yt-dlp.
Compara com `historico_videos.csv` e reporta a diferença.

O histórico é o estado: o workflow commita ele de volta ao repo depois de cada rodada.
Sem isso, todo vídeo pareceria novo a cada execução.

## Uso local

```bash
python3 monitor_concorrentes.py                       # relatório no terminal
python3 monitor_concorrentes.py --dias 30             # janela maior
python3 monitor_concorrentes.py --email a@b.com       # envia só se houver novidade
python3 monitor_concorrentes.py --email a@b.com --email-sempre
```

Para adicionar um concorrente, acrescente a URL do canal em `canais.txt`. O
`channel_id` é resolvido e cacheado sozinho em `.canais_ids.json`.

## Diferença deliberada em relação ao monitor antigo

**Falha de e-mail derruba o job.** O `youtube_monitor.py` termina `success` mesmo
quando o envio falha, e o problema passa batido por dias. Aqui, se `--email` foi
pedido e o envio falhou, o script sai com código 2 e o Actions marca vermelho.

## Secrets usados

Só `GMAIL_USER` e `GMAIL_APP_PASSWORD` — os mesmos que o monitor diário já usa.
Nenhum secret novo precisa ser criado.
