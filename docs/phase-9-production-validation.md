# Fase 9.3 — Validación de producción

## Validación del stack

`docker compose config` y el build sin caché terminaron correctamente. Frontend
y Backend iniciaron saludables en `127.0.0.1:5173` y `127.0.0.1:8000`. Backend
conectó los cinco MCP Servers privados existentes por `stdio`: Software Factory,
Filesystem, Testing, Knowledge y Git.

## Validación E2E

Un workflow controlado de revisión entró por `POST /api/workflows`, alcanzó
LangGraph, seleccionó `inspect_workspace` y completó
`filesystem__list_files` contra el workspace montado. El hook de desarrollo
existente detuvo la ejecución inmediatamente después, antes de Planning,
Implementation, Git, CI o una llamada al proveedor. Evidencia durable:

- thread: `6e70f173-3f02-49a8-8b5c-c1fdb70ddd36`;
- checkpoint: `1f19fe7b-1b50-63e6-8003-dd1390b41408`;
- 15 eventos durables, incluido `workspace_inspection_completed`;
- llamadas LLM: 0.

## Persistencia

Los volúmenes nombrados de `workspace` y `data` conservaron los datos de marca
después de `docker compose restart backend` y de `docker compose down` /
`up -d`. El mismo checkpoint y los 15 eventos siguieron accesibles por API.

## Recuperación tras reinicio

Backend volvió a estar saludable tras el reinicio, reconectó los cinco MCP
Servers, cargó el checkpointer SQLite desde `/app/data` y recuperó el registro,
snapshot, eventos, archivo del workspace y marca del store.

## Comprobaciones de seguridad

- Backend se ejecuta como usuario non-root `app`.
- Ningún contenedor es privilegiado y no se monta el socket Docker.
- Solo `/app/workspace` y `/app/data` son mounts persistentes del Backend.
- Los MCP Servers no publican puertos; Frontend y Backend usan loopback.
- ENV de imágenes, contexto de build y bundle del Frontend no contienen la key
  de validación ni `OPENAI_API_KEY`.
- `.env.production` está ignorado y se eliminó después de validar.

## Limitaciones conocidas

El despliegue sigue siendo Compose local, sin proveedor cloud, TLS/dominio,
autoscaling, Kubernetes, CD remoto, Postgres, Redis ni secret manager externo.
SQLite permanece como store durable inicial y observabilidad conserva su
implementación actual.

## Resultado

**PASS.** Se validaron build de contenedores, salud, conectividad MCP, acceso E2E
al workspace, persistencia, recovery tras reinicio e invariantes mínimos de
seguridad.
