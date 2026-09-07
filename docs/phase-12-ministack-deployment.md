# Fase 12.2B — MiniStack ECR y Docker Compose local

Target: `demo-aws-emulated`. Implementación preparada para validación E2E
por etapas; no declarar cerrado el E2E de plataforma por pasar tests mock.
Contrato: [spec revisada](../specs/phase-12-ministack-aws.md).

## Arquitectura

```text
GitHub Actions (workflow_dispatch)
             |
             v
Self-hosted runner Windows + Docker Desktop Linux
             |
             v
MiniStack ECR (127.0.0.1:4566, sin socket)
             |
             v
Backend:<SHA> + Frontend:<SHA> -> digests verificados
             |
             v
Docker Compose: sf-demo-aws
             |
       +-----+----------------+
       v                      v
Backend :18000           Frontend :15173
127.0.0.1                127.0.0.1
MCP privados
       |
       v
workspace / data propios
             |
             v
Docker inspect + HTTP health -> Journal current / previous
```

ECR emula el registry AWS; Compose es compute local y **no emula ECS**.
ECS sigue rechazado por la evidencia de
[12.2A](phase-12-2a-ministack-capabilities.md) y
[12.2A.1](phase-12-2a1-ministack-loopback.md). No se repiten pruebas ECS.

## Archivos

- `.github/workflows/software-factory-deploy-ministack.yml`: workflow manual separado.
- `scripts/deployment/deploy_ministack.py`: select, run y parada local explícita.
- `scripts/deployment/ministack_ecr.py`: cliente HTTP ECR local, sin discovery de credenciales.
- `scripts/deployment/deployment_metadata.py`: journal compartido parametrizado.
- `docker-compose.ministack.yml`: aplicación por digest, sin build durante deploy.

`demo-local` conserva su workflow, Compose, volúmenes y valores por defecto
del journal. El helper compartido de comandos admite un entorno opcional;
el comportamiento anterior sin ese parámetro no cambia.

## Prerrequisitos

Windows, Python >=3.12, Git, Docker Desktop con motor Linux, Docker Compose
v2 compatible con `env_file.required` y `config --no-env-resolution`.
El controlador utiliza stdlib; no exige AWS CLI ni boto3 en el runner.
Debe ejecutarse en el Docker context local, no DOCKER_HOST remoto.

Runner self-hosted autorizado, repositorio y rama master controlados.
El workflow no registra runners ni cambia visibilidad; permisos contents:read,
checkout fijado por SHA de action y persist-credentials=false. Solo dispatch,
concurrency propia y sin cancelación de otro deployment en progreso.

Los puertos 4566, 18000 y 15173 deben estar libres o pertenecer al target.
No detener procesos ajenos. Acceso saliente para GitHub y builds no equivale
a autorización para AWS real. Ninguna prueba health inicia LLM ni workflows.

## Home y datos

Configurar `SF_MINISTACK_DEPLOY_HOME=C:\software-factory-deploy-ministack`.
Debe ser absoluto y separado del checkout, controller y home demo-local.
No usar una carpeta padre de ellos ni una subcarpeta del otro home.

```text
C:\software-factory-deploy-ministack\
  deployment.json
  deployment.lock
  validated\<SHA>.json
  maintenance.json
  config\.env.production
  ministack-state\
```

`ministack-state/` reserva un lugar operativo del home; la persistencia real
elegida para el emulador es el volumen Docker dedicado, no ese directorio.
No guardar copias automáticas de secretos allí.

Volúmenes creados con label de propiedad `sf.deployment.target=demo-aws-emulated`:

| Volumen | Montaje |
| --- | --- |
| sf-demo-aws-workspace | Backend /app/workspace |
| sf-demo-aws-data | Backend /app/data |
| sf-demo-aws-ministack-state | MiniStack /state |

Un volumen existente sin propiedad esperada se rechaza; no se adopta ni
recrea silenciosamente. Nunca compartir los volúmenes `mcp-software-factory-*`
de Fase 11. No down -v, prune ni eliminación de datos durante deploy/stop.

## Evidencia y mantenimiento

Antes de un deployment, un operador debe validar el SHA real y crear su
evidencia en `validated/<SHA>.json`, con el mismo contrato de Fase 11:

```json
{
  "kind": "operator_platform_validation",
  "repository": "awzatarra/mcp-software-factory",
  "commit_sha": "<SHA completo validado>",
  "status": "passed",
  "validated_at": "<fecha UTC real de la validación>",
  "validated_by": "<operador>",
  "evidence_reference": "<referencia verificable>",
  "stores_compatible": true,
  "checks": [{"name": "<comprobación realmente ejecutada>", "result": "passed"}]
}
```

No copiar evidencia de demo-local automáticamente ni rellenar passed sin
ejecutar la comprobación. Los placeholders no son evidencia ejecutable.
La fecha debe incluir timezone y no ser futura.

`maintenance.json` contiene el mismo commit_sha, `quiescent=true` y expires_at
futuro con timezone. El operador detiene actividad del target antes de confirmar
la ventana. Se revalida antes de mutaciones, builds, pushes y sustitución de
contenedores; si expira, falla sin actualizar current/previous.

## Configuración externa

Se requiere `config/.env.production` externo. Si falta, se devuelve
`external_backend_configuration_missing`. No se publica ni imprime su contenido.
Preparar solo valores de demo; no almacenar secretos reales dentro de MiniStack.
Backend conserva las rutas /app/data y /app/workspace y CORS del target.
Frontend se construye con `VITE_API_BASE_URL=http://127.0.0.1:18000`.

Las operaciones locales usan entorno AWS dummy: access key/secret test,
us-east-1 e IMDS deshabilitado. Perfiles, session token y otros AWS_* heredados
se descartan. El cliente ECR utiliza el protocolo JSON unsigned admitido por
la imagen fijada: no consulta credenciales del usuario ni llama a AWS.

El endpoint admitido es exactamente `http://127.0.0.1:4566`. Redirects se
rechazan; no proxy HTTP de entorno. Docker usa registry `localhost:4566`,
cuya resolución host se verifica loopback. repositoryUri con forma AWS se
trata solo como metadata del emulador, nunca como destino de Docker.

## MiniStack e imágenes

Imagen fijada:

```text
ministackorg/ministack@sha256:d865b1e43b0b1a7e6f246e1a46fb8a0a2fa04e2a94c64740c8ff454748040726
```

Nombre `sf-demo-aws-ministack`, binding 127.0.0.1:4566, SERVICES=ecr,
PERSIST_STATE=1 y STATE_DIR=/state. No Docker socket ni privileged. La imagen
declara dos volúmenes anónimos de inicialización; se reconocen únicamente
`/docker-entrypoint-initaws.d` y `/etc/localstack/init`, además de /state.
No se montan hooks del checkout.

Repositorios: `mcp-software-factory-demo-aws/backend` y `.../frontend`.
Ensure describe-before-create no elimina repositorios. Tags SHA inmutables;
builds locales `sf-demo-aws-backend:<SHA>` y `sf-demo-aws-frontend:<SHA>` con
label OCI de revisión. Push Docker real, digest ECR contrastado con RepoDigests,
pull por referencia local exacta y comprobación del label SHA.

Un SHA publicado sin un intento previo saludable se rechaza con
`unrecorded_image_exists`. Esto evita sobreescribir imágenes después de un
push parcial: requiere inspección del operador, no limpieza automática.
Se conservan imágenes current/previous y no hay garbage collection automático.

## Deploy manual

El SHA candidato debe estar committed, ser ancestro de origin/master y tener
checkout limpio, sin datos locales ni .env versionado. La definición Compose
del candidato debe coincidir con el contrato revisado del controller. También
se conserva el guard de la definición demo-local al reutilizar validate_source.
Un SHA anterior a este archivo no es un candidato válido para este target.

En GitHub Actions, seleccionar **Software Factory Deploy MiniStack** con
environment demo-aws-emulated, SHA completo y operation deploy. El workflow
selecciona target, hace checkout del SHA exacto y ejecuta el controller.

Equivalente local sobre un checkout limpio independiente:

```powershell
$env:CD_ENVIRONMENT = 'demo-aws-emulated'
$env:SF_MINISTACK_DEPLOY_HOME = 'C:\software-factory-deploy-ministack'
$env:CD_COMMIT_SHA = '<SHA completo validado>'
$env:CD_OPERATION = 'deploy'
$env:CD_ATTEMPT_ID = '<identificador nuevo alfanumérico>'
python scripts/deployment/deploy_ministack.py select
python scripts/deployment/deploy_ministack.py run --source '<checkout limpio del SHA>'
```

No ejecutar estos ejemplos sin sustituir los placeholders y preparar evidencia,
config y mantenimiento. El controller actual no debe estar dentro del home.

Pipeline: validate -> ministack -> registry -> build -> push -> pull -> deploy
-> inspect/health -> metadata. Pull pertenece a stage deploy para conservar
la lista cerrada de stages del contrato. Compose usa digests y --no-build,
--pull never tras pull verificado. Se detiene el escritor Backend anterior
antes de recrear la aplicación: downtime breve aceptado, no rolling HA.

## Identidad, health y diagnóstico

Antes y después de HTTP health se comprueban ambos contenedores: proyecto,
servicio, única instancia, running, Image ID asociado al digest y label SHA.
También se valida MiniStack y sus mounts. Todos los bindings esperados deben
ser exactamente 127.0.0.1, sin puertos extras ni wildcard. Un contenedor propio
que incumple bindings se detiene y el intento falla.

Backend: HTTP 200 + status=ok en :18000/health. Frontend: HTTP 200 en :15173/.
MiniStack: HTTP 200 en :4566/_ministack/health y ECR accesible. No redirects,
timeouts finitos ni éxito a partir de otro contenedor que ocupe el puerto.

El journal conserva reason=<stage>_failed, reason_code seguro, stage y
command_operation allowlisted. Sin stdout/stderr, env, tokens ni stack traces.
Un fallo no mueve punteros; si falla el almacenamiento del diagnóstico, CLI
lo indica sin inventar persistencia. No rollback automático ni fallback.

## Same SHA, versión y rollback

Same SHA con evidencia saludable retenida verifica ECR y digests, vuelve a
pull/health sin rebuild y conserva previous. No duplica contenedores. El
mismo attempt_id exitoso no repite efectos; otro intento necesita ID nuevo.

Deploy B solo confirma current=B, previous=A después de todos los checks.
Rollback exige operation=rollback y commit_sha igual a previous; utiliza
los digests originales desde journal/ECR, sin rebuild. Tras health pasa a
current=A, previous=B y el intento queda rolled_back. El target queda healthy.
No rollback de datos ni migraciones de SQLite.

El journal mantiene default demo-local; environment/target demo-aws-emulated
están separados y se rechaza abrir un journal de otro entorno. Ambos targets
usan atomic write y lock; no existe un nuevo modelo de stores.

## Restart y stop

Ejecutar restart solamente en ventana de mantenimiento y un paso por vez:

```powershell
docker stop -t 30 sf-demo-aws-ministack
docker start sf-demo-aws-ministack
```

Después comprobar health ECR y pull de ambos digests guardados. Restart
ordenado no certifica crash recovery. No resetear el estado del emulador.

Para reiniciar aplicación, usar un nuevo intento deploy del SHA current:
reutiliza imágenes, detiene/inicia Compose, verifica health/identidad/bindings
y conserva previous. Comprobar registros y archivos de muestra antes/después.
No declarar preservación de todos los stores solamente por ver volúmenes.

Parada local explícita, sin cambiar inputs de Actions:

```powershell
$env:CD_ENVIRONMENT = 'demo-aws-emulated'
$env:SF_MINISTACK_DEPLOY_HOME = 'C:\software-factory-deploy-ministack'
python scripts/deployment/deploy_ministack.py stop
```

Detiene contenedores Compose sf-demo-aws y MiniStack propio. Conserva journal,
volúmenes, snapshots ECR e imágenes. No reescribe el último resultado de deploy
como si stop fuera otro deployment; el runtime queda detenido intencionalmente.

## Validación y E2E pendiente

Pruebas específicas cubren target/home, env, endpoints, ECR ensure, digests,
bindings, metadata, first/same/new/rollback, fallos e idempotencia. Fase 11
conserva sus pruebas. YAML y Compose se validan sin iniciar la aplicación.

Resultados de esta entrega (2026-09-06): específicos + Fase 11,
`93 passed, 1 deselected in 4.05s`; suite general,
`1921 passed, 9 deselected, 1 warning in 185.69s`. El warning es la deprecación
Starlette TestClient/httpx, no un fallo de deployment. Python AST, YAML,
`docker compose config --quiet`, `git diff --check` y scan básico de patrones
de secretos pasaron. El scan básico no sustituye una auditoría del historial.

Se ejecutó una prueba real **aislada ECR-only**: sin socket, con capa sintética
única, build/tag/push, eliminación de referencias locales de prueba, pull por
digest, restart ordenado, pull y lectura del archivo marcador. Pasó y limpió
sus recursos. Esto no es el E2E de plataforma ni vuelve a abrir ECS.
Resultado exacto: `1 passed in 11.10s` para el test de integración ECR-only.

Ejecutar A-H uno por uno, revisando evidencia antes de continuar:

| Paso | Estado de entrega |
| --- | --- |
| A MiniStack Start del target definitivo | Preparado; pendiente con home definitivo. |
| B First Deploy A | Pendiente: SHA committed, config/evidencia/mantenimiento reales. |
| C Same SHA A | Preparado; pendiente tras B. |
| D New Version B | Preparado; pendiente segundo SHA validado. |
| E Rollback A | Preparado; pendiente tras D. |
| F MiniStack Restart | Prueba sintética pasó; pendiente ECR de plataforma. |
| G Application Restart | Preparado; pendiente comprobar stores de plataforma. |
| H Stop | Implementado; pendiente validación del target definitivo. |

No se disparó Actions ni se ejecutó A-H automáticamente. No afirmar cierre
E2E mientras estas evidencias estén pendientes.

## Límites y publicación segura

No AWS real, ECS, EC2, EBS, S3, SSM, CloudWatch, Terraform/CDK, proxies,
ALB, Kubernetes, migraciones de stores, auto-deploy ni rollback automático.
AWS real requerirá diseñar compute/red/roles/storage; quitar endpoint override
no convierte Compose en ECS. Costo AWS de estas pruebas: cero.

Antes de volver público el repositorio: finalizar jobs, detener y desregistrar
runner, mover/deshabilitar workflows ejecutables conservando ejemplos si
conviene, revisar secrets/historial/assets, homes externos y gitignore, y
confirmar git status limpio. No se ejecutan esos cambios ni se modifica la
visibilidad automáticamente como parte de 12.2B.

Validación E2E progresiva del target demo-aws-emulated en curso.