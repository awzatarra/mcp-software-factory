# Fase 12.2A — MiniStack Capability Validation

Fecha: 2026-09-06. Resultado: **capacidad parcial; integración bloqueada por
publicación de puertos fuera de loopback**. No se implementó CD.

## Entorno y alcance

Prueba real en Docker Desktop Linux, iniciando Docker Desktop porque estaba
detenido. No había contenedores ejecutándose al comenzar. Se utilizó MiniStack
1.5.8 fijado por digest:

```text
sha256:d865b1e43b0b1a7e6f246e1a46fb8a0a2fa04e2a94c64740c8ff454748040726
```

API local `http://127.0.0.1:4566`, llamadas JSON unsigned aceptadas por el
emulador, sin AWS CLI/SDK ni cadena de credenciales. No se usaron credenciales
reales ni se crearon recursos AWS. Imagen sintética final: `busybox:1.37.0`,
push al registry local y tareas ECS reales; no Frontend/Backend de plataforma.

## Resultados

| Capacidad | Resultado observado |
| --- | --- |
| Start y `/_ministack/health` | HTTP 200. |
| Docker socket | `docker.from_env().ping()` desde MiniStack devuelve True. |
| ECR CreateRepository | Repositorio local creado. |
| ECR push | Imagen real enviada con Docker al registry local. |
| ECR DescribeImages | Digest devuelto coincide con la identidad registrada. |
| ECR pull | Tag local eliminado y descargado nuevamente con Docker. |
| ECS CreateCluster | Cluster creado mediante API. |
| ECS RegisterTaskDefinition | Definición registrada mediante API. |
| ECS CreateService | Servicio desiredCount=1 creado mediante API. |
| Contenedor Docker real | Detectado con labels ECS y State.Running=true. |
| Loopback de tarea publicada | **Falló**: bindings `0.0.0.0` y `::`. |
| Tarea sin publicar puertos | Sin bindings; usada para continuar storage/restart. |
| Storage | Dos named volumes montados read-write en `/app/workspace` y `/app/data`. |
| Restart ECR | Metadata/digest restaurados; pull posterior exitoso. |
| Restart ECS | Cluster, task definition y service restaurados. |
| Restart ejecución | Una tarea real ejecutándose, sin duplicación observada. |
| Restart storage | Hashes de ambos archivos sintéticos sin cambios. |

## Networking: bloqueo confirmado

Intento `sf-cap-040a0e9c03`: red Docker propia creada con
`com.docker.network.bridge.host_binding_ipv4=127.0.0.1`; MiniStack publicado
explícitamente como `127.0.0.1:4566:4566`. Task definition en modo bridge con
`containerPort=8080`, `hostPort=18081`.

Docker inspect de la tarea mostró:

```json
[
  {"HostIp": "0.0.0.0", "HostPort": "18081"},
  {"HostIp": "::", "HostPort": "18081"}
]
```

La prueba detectó la publicación wildcard y detuvo los contenedores. El puerto
existió durante esa comprobación; no se afirma que siempre estuvo aislado a
loopback. No se abrieron reglas de firewall ni túneles. La configuración de
red probada no basta para cumplir el contrato; tampoco demuestra que no exista
otra configuración compatible. No se modificó MiniStack ni el daemon global.

El verificador final deja `portMappings=[]` para no repetir esa exposición.
Reporta el bloqueo conocido y devuelve código 1 aunque las capacidades
restantes pasen. No introducir ALB, proxy ni parche del emulador sin decisión
separada y revisión del contrato de 12.1.

## Storage y persistencia observada

Intento final `sf-cap-00ddb4a4fc`: las entradas `volumes[].host.sourcePath`
con nombres de volúmenes Docker fueron traducidas a mounts tipo `volume`.
Es una compatibilidad del emulador, no una afirmación de equivalencia portable
con paths host de ECS real.

Se escribieron archivos sintéticos en ambos mounts; hashes iguales antes y
después de `docker stop` / `docker start` de MiniStack. Mientras MiniStack
estaba detenido se observaron cero contenedores de tarea en ejecución. Tras
arranque y reconciliación se observó uno, con storage conservado.

Configuración del emulador: `PERSIST_STATE=1`, `STATE_DIR=/state`, volumen
propio para snapshots, socket Linux local montado únicamente en MiniStack.
El socket otorga control del daemon; esto es una demo local confiable.

Digest ECR observado en la ejecución final:

```text
sha256:40baa8cf363d964fe2a44bfc560923d008a6e284fde13d8984ad3bafe9c55f3c
```

Límites: restart ordenado del mismo contenedor, no crash ni recreación completa.
Docker pudo reutilizar layers en caché al hacer pull; no se acredita recuperación
de todos los blobs en un daemon vacío. Se probaron archivos y permisos de la
imagen sintética, no SQLite concurrente ni el usuario no-root del Backend.
No afirmar completos esos criterios más amplios de 12.1.

## Evidencia y recursos retenidos

Reportes locales ignorados por Git:

- `data/ministack-capability-report.json`: intento con fallo de networking.
- `data/ministack-storage-restart-report.json`: 19 comprobaciones positivas,
  bloqueo de networking registrado, cleanup sin errores, salida 1 intencional.

Se conservaron contenedores MiniStack detenidos y sus volúmenes/redes de prueba:
prefijos `sf-cap-c3d8ecb2a8`, `sf-cap-040a0e9c03`, `sf-cap-00ddb4a4fc`.
El primer intento usó Alpine sin httpd y se descartó como error del fixture;
no se atribuye ese fallo a ECS. No se eliminaron datos ni recursos ajenos.
Las imágenes de prueba también quedaron en caché. Docker Desktop permanece
iniciado; los contenedores de prueba no quedan en segundo plano ejecutándose.

## Reproducibilidad y siguiente decisión

El script independiente `scripts/deployment/validate_ministack_capabilities.py`
requiere `--execute` y `--report`, usa recursos únicos y detiene sus contenedores
en finally. No es workflow, controlador CD ni mecanismo de rollback.

```powershell
.venv\Scripts\python.exe scripts/deployment/validate_ministack_capabilities.py `
  --execute --report data/ministack-storage-restart-report.json
```

Próximo paso previo a integración: resolver y validar publicación loopback
para tareas ECS con la versión fijada. Mantener el target como no aprobado
para deployment completo hasta contar con esa evidencia. No hubo cambios a
GitHub Actions, metadata CD, rollback, runtime, MCP ni demo-local.
