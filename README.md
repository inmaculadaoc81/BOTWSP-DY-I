# WhatsApp Bot con IA - Kelatos

Bot de WhatsApp con inteligencia artificial usando FastAPI + OpenAI + Docker.

## Arquitectura

- **FastAPI** — Backend con webhook para recibir mensajes de WhatsApp
- **OpenAI** — GPT-4o-mini para generar respuestas inteligentes
- **SQLite** — Historial de conversaciones y estado
- **Docker** — Contenedores para app + Nginx
- **GitHub Actions** — CI/CD automático
- **Nginx + Let's Encrypt** — HTTPS obligatorio para Meta

## Configuración rápida

### 1. Clonar el repositorio
```bash
git clone https://github.com/TU_USUARIO/whatsapp-bot.git
cd whatsapp-bot
```

### 2. Crear archivo .env
```bash
cp .env.example .env
nano .env  # Editar con tus valores reales
```

### 3. Desarrollo local (sin Docker)
```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

### 4. Producción (con Docker)
```bash
# Reemplazar YOUR_DOMAIN en nginx/conf.d/default.conf
docker compose up -d
```

### 5. Configurar SSL
```bash
# Primera vez - obtener certificado
docker compose run --rm certbot certonly --webroot -w /var/www/certbot -d tu-dominio.com
# Reiniciar nginx
docker compose restart nginx
```

### 6. Configurar webhook en Meta
- URL: `https://tu-dominio.com/webhook`
- Verify token: el valor de VERIFY_TOKEN en tu .env
- Suscribirse a: messages

## Variables de entorno

| Variable | Descripción |
|---|---|
| `WHATSAPP_TOKEN` | Token permanente de Meta |
| `WHATSAPP_PHONE_NUMBER_ID` | ID del número de teléfono |
| `VERIFY_TOKEN` | Token para verificar webhook |
| `OPENAI_API_KEY` | API key de OpenAI |
| `OPENAI_MODEL` | Modelo a usar (default: gpt-4o-mini) |
| `SYSTEM_PROMPT` | Personalidad del bot |

## GitHub Secrets (para CI/CD)

| Secret | Descripción |
|---|---|
| `VPS_HOST` | IP de tu VPS |
| `VPS_USER` | Usuario SSH (root o similar) |
| `VPS_SSH_KEY` | Clave privada SSH |

## Estructura del proyecto

```
whatsapp-bot/
├── main.py                 # FastAPI app + webhook endpoints
├── config.py               # Configuración con variables de entorno
├── database.py             # SQLite - historial de chats
├── openai_service.py       # Conexión con OpenAI
├── whatsapp_service.py     # Envío de mensajes por WhatsApp API
├── requirements.txt        # Dependencias Python
├── Dockerfile              # Imagen Docker
├── docker-compose.yml      # Orquestación de contenedores
├── .env.example            # Plantilla de variables de entorno
├── .gitignore
├── nginx/
│   └── conf.d/
│       └── default.conf    # Configuración Nginx
└── .github/
    └── workflows/
        └── deploy.yml      # CI/CD con GitHub Actions
```
