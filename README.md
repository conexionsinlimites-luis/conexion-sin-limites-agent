# AgentKit — WhatsApp AI Agent Builder

Construye tu agente de WhatsApp con IA en menos de 30 minutos. Sin saber codigo. Claude Code hace todo por ti.

<!-- ![AgentKit Demo](demo.gif) -->

---

## Que hace esto?

Clonas este repo, corres un comando, y **Claude Code te guia paso a paso** para construir un agente de WhatsApp personalizado para tu negocio:

- **Powered by Claude AI** — respuestas inteligentes y naturales
- **Memoria por cliente** — recuerda el contexto de cada conversacion
- **Conocimiento de tu negocio** — sube tus archivos y el agente los aprende
- **Deploy a Railway** — de tu computadora al servidor en minutos
- **100% personalizado** — Claude Code adapta todo a tu negocio especifico

---

## Requisitos

- **Python 3.11+** — [python.org/downloads](https://python.org/downloads)
- **Claude Code** — `npm install -g @anthropic-ai/claude-code`
- **API Key de Anthropic** — [platform.anthropic.com](https://platform.anthropic.com/settings/api-keys)
- **Cuenta Whapi.cloud** — [whapi.cloud](https://whapi.cloud) (plan gratis disponible)

---

## Inicio rapido

```bash
# 1. Clona el repo
git clone https://github.com/Hainrixz/claude-agentkit.git
cd claude-agentkit

# 2. Prepara el entorno
bash start.sh

# 3. Abre Claude Code y construye tu agente
claude
# Escribe: /build-agent
```

Claude Code te hara preguntas sobre tu negocio y construira todo automaticamente.

---

## Que construye Claude Code?

Cuando ejecutas `/build-agent`, Claude Code genera:

```
agentkit/
├── agent/
│   ├── main.py            ← Servidor FastAPI + webhook WhatsApp
│   ├── brain.py           ← Conexion con Claude AI
│   ├── memory.py          ← Historial de conversaciones por cliente
│   └── tools.py           ← Herramientas especificas de tu negocio
├── config/
│   ├── business.yaml      ← Datos de tu negocio
│   └── prompts.yaml       ← System prompt personalizado
├── knowledge/             ← Tus archivos (menu, precios, FAQ, etc.)
├── tests/
│   └── test_local.py      ← Prueba tu agente sin WhatsApp
├── Dockerfile
├── docker-compose.yml
└── .env                   ← Tus API keys (seguro, no se sube)
```

---

## Flujo

```
bash start.sh              →  Verifica tu entorno
claude → /build-agent      →  Claude Code te entrevista
                           →  Genera el agente personalizado
                           →  Prueba local en tu terminal
                           →  Deploy a Railway (opcional)
```

---

## Casos de uso

| Tipo | Ejemplo |
|------|---------|
| FAQ | Restaurante que responde sobre menu y horarios |
| Citas | Clinica dental que agenda consultas |
| Ventas | Inmobiliaria que califica leads |
| Pedidos | Pasteleria que toma pedidos por WhatsApp |
| Soporte | SaaS que resuelve dudas post-venta |

---

## Comandos utiles (despues del setup)

```bash
# Probar el agente sin WhatsApp
python tests/test_local.py

# Arrancar servidor local
uvicorn agent.main:app --reload --port 8000

# Build para produccion
docker compose up --build
```

---

## Stack tecnico

| Componente | Tecnologia |
|-----------|-----------|
| IA | Claude AI (claude-sonnet-4-6) |
| Servidor | FastAPI + Uvicorn |
| WhatsApp | Whapi.cloud |
| Base de datos | SQLite (local) / PostgreSQL (prod) |
| Deploy | Docker + Railway |

---

## Personalizar despues del setup

```bash
# Ajustar respuestas del agente
claude "Mejora el system prompt para que el agente sea mas detallado con los precios"

# Agregar herramientas
claude "Agrega una herramienta para consultar disponibilidad de citas"

# Actualizar info del negocio
claude "Actualiza el menu con estos nuevos platos: ..."
```

---

## Creditos

Creado por **Todo de IA** — [@soyenriquerocha](https://instagram.com/soyenriquerocha)

Construido con [Claude Code](https://claude.ai/claude-code) para builders de LATAM.

---

## Licencia

MIT
