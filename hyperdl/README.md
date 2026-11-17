# HyperDL — Acelerador de Downloads Híbrido (IDM + AB Download Manager)

O **HyperDL** combina os pontos fortes da arquitetura do **IDM (`IDMan.exe`)** e do **AB Download Manager (`ABDownloadManager`)** numa aplicação moderna, multiplataforma (Windows, Linux, macOS) com motor assíncrono em Python (`aiohttp` + `SQLite WAL`), CLI, API HTTP/WebSocket, extensão de browser Manifest V3 e painel web reativo.

## O que foi aproveitado de cada projeto

| Origem | Ideia implementada no HyperDL |
|---|---|
| **IDM** | **Work-Stealing / Divisão Dinâmica de Segmentos**: quando uma conexão termina o seu bloco, divide ao meio o maior segmento ativo restante (`min_split_size`) para nenhuma conexão ficar ociosa |
| **IDM** | **Base de dados indexada em SQLite (WAL)** com recuperação automática após falhas e validação estrita de `ETag` / `Accept-Ranges` ao retomar |
| **IDM** | **Filas com concorrência configurável**, categorias automáticas por extensão/MIME e download/montagem de streams **HLS (`.m3u8`)** |
| **ABD** | **API HTTP local (`/api/ping`, `/api/queues`, `/api/downloads/add`)**, WebSocket em tempo real e **Native Messaging Host** para extensões de browser |
| **ABD** | **Importação direta de comandos `cURL`** (copiados do DevTools), **padrões Wildcard em lote** (`[01..20]`), **configurações por domínio (per-host)** e **verificação de Checksum** (`SHA-256`, `MD5`, etc.) |
| **ABD** | **CLI completo** (`hyperdl add`, `serve`, `import-curl`, `list`, `pause`, `resume`, `verify`) |

## Executar

```bash
pip install -e ./hyperdl
hyperdl serve --host 0.0.0.0 --port 8787
```

Depois abre `http://127.0.0.1:8787` (ou a pré-visualização ao vivo).
