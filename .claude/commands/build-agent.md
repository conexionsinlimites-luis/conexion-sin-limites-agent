Lee el archivo CLAUDE.md completo. Contiene todas las instrucciones detalladas.

Ejecuta el flujo de onboarding de AgentKit siguiendo las 5 fases EN ORDEN:

FASE 1 — Bienvenida y verificación del entorno
- Muestra el mensaje de bienvenida
- Verifica Python >= 3.11
- Crea las carpetas necesarias (agent/, config/, knowledge/, tests/)
- Genera requirements.txt e instala dependencias
- Crea .env base

FASE 2 — Entrevista del negocio
- Haz las 9 preguntas UNA POR UNA
- Espera respuesta antes de continuar a la siguiente
- Guarda todas las respuestas para la Fase 3

FASE 3 — Generación del agente
- Genera config/business.yaml con datos del negocio
- Genera config/prompts.yaml con system prompt poderoso y específico
- Si hay archivos en /knowledge, léelos e incorpóralos al prompt
- Genera agent/main.py (FastAPI + webhook Whapi)
- Genera agent/brain.py (Claude API)
- Genera agent/memory.py (SQLite + historial)
- Genera agent/tools.py (herramientas según caso de uso)
- Genera tests/test_local.py (simulador de chat)
- Genera Dockerfile y docker-compose.yml
- Configura .env con las API keys del usuario

FASE 4 — Testing local
- Ejecuta python tests/test_local.py
- El usuario chatea con su agente en la terminal
- Si hay ajustes, modifica prompts.yaml y repite
- No avanza sin aprobación del usuario

FASE 5 — Deploy a Railway
- Solo si el usuario quiere
- Build Docker + instrucciones de Railway + webhook en Whapi

REGLAS:
- Habla siempre en español
- Una pregunta a la vez
- Nunca hardcodees API keys
- No avances de fase sin confirmación
- El agente debe funcionar antes de hablar de deploy
