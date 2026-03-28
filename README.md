# Brain-Sync

Web app em Flask para revisao ativa com Firestore, executando em Docker.

## Repositorio oficial
https://github.com/Rjj18/brain-sync-app

## Proposito do app
O Brain-Sync foi criado para apoiar estudos com revisao espacada, transformando trechos de conteudo em flashcards com Active Recall. O objetivo e priorizar automaticamente o que deve ser revisado primeiro e registrar feedback de dificuldade para recalcular a proxima revisao usando logica inspirada no SM-2.

## Stack
- Python 3.12 + Flask
- Firestore (Firebase Admin SDK)
- Docker + Docker Compose
- Gemini API (opcional para geracao automatica de card/trecho)

## Pre-requisitos
- Docker e Docker Compose instalados
- Credencial de Service Account em `secrets/service-account.json`

## Seguranca de credenciais
- O arquivo `secrets/service-account.json` **nao deve** ser versionado.
- O `.gitignore` ja protege a pasta `secrets/` e outros artefatos locais.
- O app usa `GOOGLE_APPLICATION_CREDENTIALS=/app/secrets/service-account.json` por padrao.

## Como rodar em desenvolvimento
```bash
docker compose up --build -d
```

Acesse:
- Home: http://localhost:5000/
- Health: http://localhost:5000/health

Logs:
```bash
docker compose logs -f web
```

## Como rodar em modo producao local
Use o compose de producao para validar comportamento proximo ao deploy externo.

```bash
docker compose -f docker-compose.prod.yml up --build -d
```

Acesse:
- Home: http://localhost:8000/
- Health: http://localhost:8000/health

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

## Variaveis de ambiente
Copie `.env.example` para `.env` e ajuste conforme seu ambiente.
Para producao, use `.env.production.example` como base.

Principais variaveis:
- `CORS_ORIGINS` (lista separada por virgula)
- `FIREBASE_PROJECT_ID`
- `GOOGLE_CLOUD_PROJECT`
- `GOOGLE_APPLICATION_CREDENTIALS`
- `FIREBASE_SERVICE_ACCOUNT_JSON` (alternativa ao arquivo de credencial)
- `GEMINI_API_KEY` (opcional)
- `GEMINI_MODEL` (opcional)

## Rotas principais
- `GET /` -> home
- `GET /upload-material` e `POST /upload-material` -> cadastro de material/card/trecho
- `GET /api/cards/review?topic=` -> proximo card para revisao
- `GET /api/cards/study?mode=topic|shuffle&topic=` -> estudo de cards
- `GET /api/readings/next?mode=topic|shuffle&topic=` -> revisao de leitura
- `POST /review/<id>` -> aplica feedback (`1`, `2`, `3`) com SM-2 adaptado
- `GET /health` -> health check simples

## Deploy inicial no Render
Opcao 1: criar o servico manualmente pelo painel.

1. Crie um novo Web Service no Render apontando para este repositorio.
2. Configure para usar `Dockerfile`.
3. Defina variaveis de ambiente no painel (`FIREBASE_PROJECT_ID`, `GOOGLE_CLOUD_PROJECT`, `CORS_ORIGINS`, `GEMINI_*`).
4. Para credencial Firebase, use **uma** opcao:
	- `GOOGLE_APPLICATION_CREDENTIALS` com arquivo montado em runtime.
	- `FIREBASE_SERVICE_ACCOUNT_JSON` com o JSON completo da service account como secret.
5. Apos o deploy, valide `GET /health` e as rotas principais.

Opcao 2: usar `render.yaml` deste repositorio para infra como codigo.

## Estrutura
```text
brain-sync/
├── app.py
├── Dockerfile
├── docker-compose.yml
├── docker-compose.prod.yml
├── requirements.txt
├── render.yaml
├── templates/
├── static/
├── secrets/              # local, ignorado no git
├── .env.production.example
├── .dockerignore
└── .gitignore
```

## Parar containers
```bash
docker compose down
```

## Licenca
Este projeto esta licenciado sob a licenca MIT. Veja o arquivo [LICENSE](LICENSE) para detalhes.
