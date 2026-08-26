# Fase 9.2 - Despliegue con contenedores

## Servicios

- `frontend`: build multietapa de Vite/React servido por Nginx.
- `backend`: FastAPI, LangGraph, WorkflowRunner, Git, CI y gobernanza.
- MCP Servers: los servidores FastMCP existentes de planning, filesystem,
  testing, knowledge y Git se ejecutan como procesos hijo privados por `stdio`
  dentro de `backend`. Su transporte no abre listeners de red, por lo que
  Compose no publica ni emula puertos MCP.

## Red

Ambos servicios se unen a `mcp-software-factory-private`. El acceso desde el
host local se limita al Frontend en `127.0.0.1:5173` y la API del Backend en
`127.0.0.1:8000`. Los MCPs comparten el namespace del contenedor Backend y solo
son accesibles por sus clientes MCP. TLS y un reverse proxy externo quedan
fuera de esta fase.

## Volúmenes

- `mcp-software-factory-workspace` se monta en `/app/workspace`.
- `mcp-software-factory-data` se monta en `/app/data`.

Son volúmenes nombrados y sobreviven a `docker compose down` y al reemplazo del
Backend. `docker compose down -v` los elimina deliberadamente y no debe usarse
cuando el estado deba preservarse.

## Entorno

Crea un archivo local ignorado y configura el secreto real mediante el entorno
local o el mecanismo de gestión de secretos:

```powershell
Copy-Item .env.production.example .env.production
# Set OPENAI_API_KEY in .env.production without committing the file.
# For this local Compose deployment, also set:
# FRONTEND_BASE_URL=http://127.0.0.1:5173
# BACKEND_BASE_URL=http://127.0.0.1:8000
# APPLICATION_URL=http://127.0.0.1:5173
# VITE_API_BASE_URL=http://127.0.0.1:8000
# CORS_ALLOWED_ORIGINS=http://127.0.0.1:5173
# API_CORS_ORIGINS=http://127.0.0.1:5173
```

Compose mapea `WORKSPACE_ROOT`, el almacenamiento de checkpoints, eventos de
workflow, CORS y `VITE_API_BASE_URL` del build del Frontend a paths y URLs aptos
para contenedores. Usa `--env-file .env.production` para que los argumentos de
build compartan las mismas URLs públicas. Ningún secreto se copia a las
imágenes.

## Construcción

```powershell
docker compose --env-file .env.production config
docker compose --env-file .env.production build
```

## Inicio

```powershell
docker compose --env-file .env.production up -d
docker compose ps
```

Abre `http://127.0.0.1:5173`. El navegador usa la URL del Backend incorporada
durante el build, cuyo valor local predeterminado es
`http://127.0.0.1:8000`.

## Validación de salud

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-WebRequest http://127.0.0.1:5173/health -UseBasicParsing
docker compose ps
```

El inicio del Backend conecta todos los procesos hijo MCP críticos antes de que
Uvicorn esté ready. Un fallo aparece en `docker compose logs backend`. Las
comprobaciones no destructivas pueden usar las rutas existentes de inspección;
no requieren llamadas LLM pagadas, mutaciones Git ni CI.

## Validación de persistencia

```powershell
docker compose exec backend python -c "from pathlib import Path; Path('/app/workspace/.phase-9-persistence').write_text('workspace')"
docker compose exec backend python -c "from pathlib import Path; Path('/app/data/.phase-9-persistence').write_text('data')"
docker compose restart backend
docker compose exec backend python -c "from pathlib import Path; assert Path('/app/workspace/.phase-9-persistence').read_text() == 'workspace'; assert Path('/app/data/.phase-9-persistence').read_text() == 'data'"
docker compose down
docker compose --env-file .env.production up -d
docker compose exec backend python -c "from pathlib import Path; assert Path('/app/workspace/.phase-9-persistence').exists(); assert Path('/app/data/.phase-9-persistence').exists()"
```

Elimina los dos archivos de marca después de validar. Los volúmenes nombrados
permanecen intactos.

## Limitaciones conocidas

Este despliegue local no incluye HTTPS, despliegue remoto, load balancer,
autoscaling, Postgres, Redis, stack externo de observabilidad ni backup
automático. La configuración del Frontend se incorpora durante el build. La
instalación de dependencias de proyectos requiere acceso saliente y su approval
existente. La imagen Backend incluye Python y Git; workloads CI de Node o .NET
requieren una imagen derivada con esos toolchains.

## Siguiente fase 9.3

La Fase 9.3 valida build, salud, conectividad MCP, persistencia, recovery tras
reinicio y controles mínimos de seguridad, sin cambiar semánticas de MCP,
Planning, approvals, Git, CI ni Promotion.
