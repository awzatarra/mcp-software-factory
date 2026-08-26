# MCP Software Factory

## Descripción

Demo educativa y funcional de una Software Factory gobernada, construida con
Python, FastAPI, LangGraph, MCP, OpenAI Responses API y React. Convierte un
requerimiento en un proyecto verificable mediante planificación estructurada,
implementación, pruebas y reparación, Git, CI y Promotion, con aprobaciones
humanas, persistencia durable y trazabilidad de extremo a extremo.

El runtime LangGraph es la ruta principal para los flujos durables. La
orquestación manual se conserva como compatibilidad mediante configuración.

## Arquitectura

```text
Usuario / UI React / Host CLI
            |
            v
      FastAPI + LangGraph
            |
            +--> PlanningSubgraph
            +--> ImplementationSubgraph
            +--> TestingRepairSubgraph
            +--> Supervisor y approvals
            |
            +--> MCP Software Factory
            +--> MCP Filesystem
            +--> MCP Testing
            +--> MCP Knowledge
            +--> MCP Git
            |
            +--> SQLite durable + workspace + repositorios Git
```

Los MCP Servers se ejecutan como procesos privados por `stdio`. El Backend es
la única frontera autorizada para orquestarlos; el Frontend nunca accede
directamente al workspace, Git, stores, secretos ni MCPs.

## Capacidades principales

- Planning con contratos estructurados, validación de ejecutabilidad, riesgo,
  calidad y refinement adaptativo.
- Implementación segura con paths relativos, dependencias gobernadas y
  aislamiento por proyecto.
- Testing y Repair con entornos virtuales propios, clasificación de fallos,
  límites de reintentos y evidencia antes/después.
- Git gobernado con ramas por workflow, commits auditables y working tree
  protegido.
- CI durable con gates de entorno, build, test y lint, reparación controlada y
  vinculación exacta al commit.
- Promotion manual con aprobación, revalidación y merge controlado.
- Persistencia SQLite para checkpoints, eventos, approvals, CI, Git,
  observabilidad, evaluación, Knowledge y FinOps.
- Replay, Fork, interrupts y recovery durable después de reinicios.
- API HTTP/SSE y UI React para operación en tiempo real.

## Flujo principal

```text
Requerimiento
  -> Planning
  -> Implementation
  -> Testing / Repair
  -> Git
  -> CI
  -> Promotion
  -> Finalize
```

El Supervisor coordina traspasos explícitos entre especialistas. Las operaciones
sensibles se interrumpen de forma durable para solicitar aprobación y se
reanundan desde el checkpoint vigente, sin repetir etapas completadas.

## Gobernanza

- Aprobaciones humanas para escritura, preparación de entornos, tests y
  operaciones Git sensibles.
- Git gobernado mediante allowlists, provenance, fingerprints y políticas de
  dirty working tree compartidas.
- CI gates vinculados al SHA exacto; un resultado stale no habilita Promotion.
- Promotion manual y sin autoaprobación.
- Recomendaciones de evaluación y políticas en modo advisory: no existe
  auto-apply de cambios gobernados.
- Rutas, comandos, dependencias y argumentos de tools validados antes de
  ejecutar.

## Observabilidad y evaluación

La plataforma persiste traces, spans, eventos y relaciones entre workflows,
subgrafos, agentes, llamadas LLM, MCP tools, approvals, tests, CI y Git. La
política de exclusión evita que el polling de la UI contamine métricas locales;
los errores relevantes pueden conservarse de forma configurable.

FinOps registra usage reportado por el proveedor, pricing versionado, costes,
reservas y budgets. `usage_source` y `cost_source` son independientes, y un
`hard_limit` aplicable bloquea antes de invocar al proveedor.

La evaluación avanzada combina métricas deterministas, heurísticas y
LLM-as-a-Judge. Incluye rubrics versionadas, baselines, detección de regresiones,
RCA, desempeño de agentes y recomendaciones gobernadas. Las evaluaciones
históricas permanecen ligadas a su versión original y no se recalculan
automáticamente.

## Memoria y conocimiento

Knowledge MCP es la frontera exclusiva de RAG y aprendizaje cross-workflow.
Mantiene aislamiento por proyecto, provenance, sanitización, deduplicación y
estados durables de indexación. Planning, Implementation, QA y Repair consultan
contexto limitado y no confiable; un fallo de lectura degrada el contexto sin
bloquear el workflow, mientras las escrituras son fail-closed.

## Docker y despliegue local

El despliegue de producción local separa Frontend y Backend con Docker Compose.
Los MCPs permanecen privados dentro del Backend, que se ejecuta como usuario
non-root. Los volúmenes nombrados conservan `workspace/` y `data/` después de
`restart` y `down`/`up`; `docker compose down -v` los elimina deliberadamente.

```powershell
Copy-Item .env.production.example .env.production
docker compose --env-file .env.production config
docker compose --env-file .env.production build
docker compose --env-file .env.production up -d
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-WebRequest http://127.0.0.1:5173/health -UseBasicParsing
```

Documentación de despliegue:

- [Modelo de despliegue](docs/phase-9-deployment-model.md)
- [Despliegue con contenedores](docs/phase-9-container-deployment.md)
- [Validación de producción](docs/phase-9-production-validation.md)
- [Resumen de Fase 9](docs/phase-9-summary.md)

## GitHub Actions

La integración usa un self-hosted runner Windows instalado fuera del repositorio
y un workflow manual `workflow_dispatch`. GitHub Actions conecta el runner con
el Backend local en `127.0.0.1:8000`; `thread_id` es opcional y permite consultar
un workflow existente.

La integración actual es read-only: usa `contents: read`, no contiene secretos,
no realiza writes ni expone públicamente el Backend. Su instalación, ciclo de
vida y validación E2E están en
[GitHub Actions con runner local](docs/phase-10-github-actions.md).

## Cómo ejecutar localmente

Requiere Python 3.12+ y Node.js para el Frontend.

```powershell
uv sync --dev
Copy-Item .env.example .env
uv run python host.py
```

API:

```powershell
.\.venv\Scripts\uvicorn.exe api.app:app --host 127.0.0.1 --port 8000
```

Frontend:

```powershell
cd frontend
Copy-Item .env.example .env
npm install
npm run dev
```

La UI queda en `http://127.0.0.1:5173` y la API en
`http://127.0.0.1:8000`.

## Variables de entorno

Usa `.env.example` para desarrollo y `.env.production.example` para Docker
Compose. No confirmes `.env`, `.env.production`, API keys, tokens ni
credenciales.

Variables principales:

```env
OPENAI_API_KEY=
OPENAI_MODEL=
USE_LANGGRAPH=true
MCP_FACTORY_DEBUG=false
LANGGRAPH_CHECKPOINT_DB=
WORKFLOW_EVENT_STORE_PATH=
WORKSPACE_ROOT=
API_CORS_ORIGINS=http://127.0.0.1:5173
```

Los templates contienen la configuración completa para MCP timeouts,
Knowledge, observabilidad, notificaciones, FinOps, CI y despliegue.

## Pruebas

Backend:

```powershell
uv run pytest
```

Frontend:

```powershell
cd frontend
npm test
npm run typecheck
npm run lint
npm run build
```

Las pruebas usan workspaces temporales y mocks; no requieren una API key ni
deben efectuar llamadas pagadas.

## Seguridad

- El workspace rechaza paths absolutos, traversal y acceso fuera de su raíz.
- Los Testing y Git MCP no aceptan comandos arbitrarios y aíslan subprocesses
  del transporte MCP `stdio`.
- MCP Servers, SQLite, workspace y repositorios Git no se exponen directamente.
- Prompts, respuestas completas, archivos, credenciales y headers sensibles se
  redactan o no se persisten en observabilidad.
- El Frontend recibe únicamente configuración pública.

## Estado del roadmap

| Fase | Estado |
| --- | --- |
| Fase 6.21 — CI/CD | Cerrada ✅ |
| Fase 7 — Memoria y conocimiento | Cerrada ✅ |
| Fase 8 — Evaluación avanzada | Cerrada ✅ |
| Fase 9 — Production Readiness & Deployment | Cerrada ✅ |
| Fase 10 — GitHub Actions Integration | Cerrada ✅ |

Documentación de cierre:

- [Fase 6.21](docs/phase-6-21-summary.md)
- [Fase 7](docs/phase-7-summary.md)
- [Fase 8](docs/phase-8-summary.md)
- [Fase 9](docs/phase-9-summary.md)
- [Fase 10](docs/phase-10-github-actions.md)

## Limitaciones conocidas

- El despliegue validado es Docker Compose local, sin cloud, TLS, autoscaling,
  Kubernetes, Postgres, Redis ni alta disponibilidad.
- El self-hosted runner es Windows, interactivo y depende de que el Backend
  local ya esté disponible.
- SQLite es la fuente durable inicial y requiere una política operativa externa
  de backup y restore.
- La calidad de generación y evaluación LLM depende del modelo y pricing
  configurados.
- Los canales externos, instalaciones de dependencias y llamadas al proveedor
  requieren conectividad saliente y conservan sus gates existentes.
