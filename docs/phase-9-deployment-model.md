# Fase 9.1 - Modelo de despliegue

## Arquitectura

```text
Internet
   |
   v
Frontend (público)
   |
   v
Backend API / LangGraph (API pública)
   |-- MCP Servers (privados)
   |-- Workspace persistente
   |-- Repositorios Git
   `-- Stores persistentes
```

Frontend y Backend son servicios separados. Backend, LangGraph y
`WorkflowRunner` forman el servicio principal de ejecución. Los MCP Servers se
ejecutan como procesos o servicios privados accesibles únicamente por Backend;
nunca se exponen a Internet.

## Componentes

- **Frontend:** aplicación Vite/React existente. Solo llama a la API del Backend
  y no accede directamente a MCPs, workspace, Git, stores ni secretos.
- **Backend:** FastAPI, LangGraph, ejecución de workflows, approvals, Git, CI,
  evaluación y gobernanza. Es el único componente autorizado para orquestar MCP
  Servers.
- **MCP Servers:** capacidades privadas existentes sobre transporte o red
  interna. La Fase 9.1 no agrega MCPs ni cambia contratos MCP.
- **Workspace:** almacenamiento persistente para proyectos generados,
  repositorios Git y entornos locales como `.venv` cuando corresponda.
- **Stores:** bases SQLite durables existentes sobre almacenamiento persistente.
  Postgres es una posible evolución futura, no un requisito de la Fase 9.1.

## Público y privado

| Componente | Exposición |
| --- | --- |
| Frontend | Servicio HTTP público |
| Backend | API pública gobernada |
| MCP Servers | Privados; acceso exclusivo del Backend |
| Workspace y Git | Privados; sin acceso HTTP ni shell directo |
| Stores SQLite | Almacenamiento persistente privado |

La frontera pública sigue siendo la API gobernada del Backend. Las mutaciones
de filesystem y Git solo están disponibles mediante sus flujos existentes de
validación, políticas y aprobación.

## Datos persistentes

Los siguientes datos deben sobrevivir reinicios o redespliegues del Backend:

- `workspace/`, proyectos generados, repositorios Git y entornos del proyecto;
- workflows y checkpoints LangGraph, incluidas approvals pendientes;
- eventos, ejecuciones CI, auditoría/estado Git y estado de Promotion;
- stores de recomendaciones, evaluación, gobernanza y políticas/runtime;
- datos durables de Knowledge, observabilidad, usage LLM, costes, pricing y
  budgets.

Los archivos SQLite deben residir fuera del filesystem efímero del proceso o
contenedor. Los volúmenes de workspace y stores tienen un ciclo de vida
independiente del Backend. Backup y restore son consideraciones operativas
necesarias, pero su automatización y la migración de stores quedan fuera de 9.1.

## Desarrollo y producción

| Aspecto | DEV | PROD |
| --- | --- | --- |
| Frontend | Servidor Vite local | Servicio público separado |
| Backend | Proceso FastAPI local | Servicio API/ejecución separado |
| MCP | Procesos privados locales | Procesos/servicios internos privados |
| Workspace | `workspace/` local | Almacenamiento persistente montado |
| Stores | Archivos SQLite locales | SQLite en almacenamiento persistente |
| Secretos | Entorno local | Inyección de entorno o secret manager |

Staging puede incorporarse más adelante con los mismos invariantes de
producción; no se define en esta fase.

## Configuración

`.env.production.example` es un template de producción sin secretos. Los
valores reales se inyectan durante el despliegue y los archivos `.env` reales no
se confirman. `OPENAI_API_KEY`, headers OTLP, tokens, passwords y credenciales
provienen de variables de entorno o un secret manager. El Frontend solo recibe
configuración pública como `VITE_API_BASE_URL`.

`FRONTEND_BASE_URL`, `BACKEND_BASE_URL`, `CORS_ALLOWED_ORIGINS`,
`WORKSPACE_ROOT` y `DATA_ROOT` describen el contrato de despliegue. El runtime
consume `APPLICATION_URL`, `VITE_API_BASE_URL`, `API_CORS_ORIGINS`,
`LANGGRAPH_CHECKPOINT_DB` y `WORKFLOW_EVENT_STORE_PATH`; el ejemplo mapea ambos
conjuntos explícitamente.

## Verificaciones de salud

- **Frontend:** su servicio HTTP devuelve una respuesta exitosa.
- **Backend:** la verificación esperada es `GET /health`.
- **MCPs críticos:** usan su mecanismo existente de proceso/readiness cuando
  está disponible.

Esta fase define las verificaciones esperadas sin agregar endpoints ni cambiar
el comportamiento runtime.

## Invariantes de seguridad

- Los MCP Servers permanecen privados y nunca se exponen directamente.
- No existe shell público ni interfaz pública directa al filesystem.
- Las mutaciones Git permanecen detrás de APIs gobernadas y approvals.
- Las semánticas de CI y Promotion no cambian.
- Los secretos no entran en source control ni bundles del Frontend.
- La validación del workspace rechaza paths absolutos y traversal.
- El Frontend nunca recibe credenciales del Backend.

## Limitaciones conocidas

La Fase 9.1 no incorpora imágenes de contenedor, orquestación, automatización de
despliegue, load balancing, autoscaling, backup automático, Postgres, Redis ni
operación multirregión. SQLite requiere almacenamiento con la durabilidad y
semántica de filesystem adecuadas.

## Siguiente fase 9.2

La Fase 9.2 selecciona un despliegue concreto y conecta volúmenes persistentes,
inicio de servicios, MCPs privados, verificaciones de salud e inyección de
secretos, preservando la frontera público/privado y los contratos de approvals,
Git, CI, Promotion y MCP.
