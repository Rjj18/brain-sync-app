# Brain-Sync

Web app em Flask para revisão ativa com Firestore, executando em Docker.

## Repositório oficial
https://github.com/Rjj18/brain-sync-app

## Propósito do app
O Brain-Sync foi criado para apoiar estudos com revisão espaçada, transformando trechos de conteúdo em flashcards com Active Recall. O objetivo é priorizar automaticamente o que deve ser revisado primeiro e registrar feedback de dificuldade para recalcular a próxima revisão usando lógica inspirada no SM-2.

## Stack
- Python 3.12 + Flask
- Firestore (Firebase Admin SDK)
- Docker + Docker Compose
- Gemini API (opcional para geracao automatica de card/trecho)

## Pré-requisitos
- Docker e Docker Compose instalados
- Credencial de Service Account em `secrets/service-account.json`

## Segurança de credenciais
- O arquivo `secrets/service-account.json` **não deve** ser versionado.
- O `.gitignore` já protege a pasta `secrets/` e outros artefatos locais.
- O app usa `GOOGLE_APPLICATION_CREDENTIALS=/app/secrets/service-account.json` no compose.

## Como rodar
```bash
docker compose up --build -d
```

## Ativar geracao com Gemini (opcional)
Defina as variaveis de ambiente antes de subir o compose:

```bash
export GEMINI_API_KEY="sua-chave"
export GEMINI_MODEL="gemini-1.5-flash"
```

No Windows PowerShell:

```powershell
$env:GEMINI_API_KEY="sua-chave"
$env:GEMINI_MODEL="gemini-1.5-flash"
```

Sem chave, o app usa fallback local para geracao automatica.

Acesse:
- Home: http://localhost:5000/
- Health: http://localhost:5000/health

## Rotas principais
- `GET /` → carrega o próximo card (menor `metadata.next_review`)
- `POST /review/<id>` → aplica feedback (`1`, `2`, `3`) com SM-2 adaptado
- `GET /api/next-insight` → retorna próximo card em JSON (usado no fluxo sem reload)
- `GET /health` → health check simples

## Estrutura
```text
brain-sync/
├── app.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── templates/
│   └── index.html
├── static/
├── secrets/              # local, ignorado no git
├── .dockerignore
└── .gitignore
```

## Logs úteis
```bash
docker compose logs -f web
```

## Parar containers
```bash
docker compose down
```

## Licença
Este projeto está licenciado sob a licença MIT. Veja o arquivo [LICENSE](LICENSE) para detalhes.
