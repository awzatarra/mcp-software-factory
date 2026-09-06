# Fase 12.2A.1 — Investigación bloqueante de networking

Fecha: 2026-09-06. Resultado de la matriz real:

```text
MINISTACK_ECS_LOOPBACK_UNSUPPORTED
```

Conclusión limitada a MiniStack 1.5.8, el digest indicado y Docker Desktop
Linux de este equipo: ninguna configuración soportada investigada cumple
el criterio de publicación exclusivo en `127.0.0.1`. Fase 12 sigue bloqueada
para integración completa. No es una afirmación sobre todas las versiones
futuras ni todos los motores Docker.

## Identidad y fuentes

Imagen ejecutada, sin parches:

```text
ministackorg/ministack@sha256:d865b1e43b0b1a7e6f246e1a46fb8a0a2fa04e2a94c64740c8ff454748040726
```

Se inspeccionó `ministack.services.ecs` dentro de esa imagen con Python, no
se asumió que la rama main actual fuera idéntica. SHA256 del texto obtenido
con `inspect.getsource`:

```text
0daae16379da7a1b97a43a9b2fc67b67b929d24d5bb625bd269a1be3b2e5ed04
```

Fuentes documentales contrastadas:

- [MiniStack ECS](https://ministack.org/docs/services/ecs): tareas ejecutadas
  mediante Docker y operaciones ECS disponibles; no documenta parámetro bind IP.
- [Docker bridge](https://docs.docker.com/engine/network/drivers/bridge/):
  `com.docker.network.bridge.host_binding_ipv4` configura la dirección de
  publicación predeterminada de una red bridge propia.

La documentación general no sustituye el Docker inspect de cada tarea.

## Hallazgos del código fijado

| Superficie | Comportamiento observado en el código de la imagen |
| --- | --- |
| bridge | Construye diccionario de puertos y llama `containers.run`. |
| `portMappings` / `hostPort` | Líneas 1498-1499: asigna hostPort a la clave containerPort/tcp. No añade HostIp. |
| Detección de red | Líneas 1442-1451: busca el contenedor por `os.environ['HOSTNAME']`, toma su primera red. |
| `MINISTACK_HOSTNAME` | No sustituye a HOSTNAME en esa búsqueda de red. |
| host | Línea 1296: `network_mode='host'`; no satisface el requisito de un binding Docker loopback explícito. |
| awsvpc | Omite publicación de puertos en el host; no equivale a HostIp=127.0.0.1. |
| Docker SDK instalado | Binding numérico se transforma en HostIp vacío y HostPort string; una IP explícita requiere estructura distinta. |
| HostIp / bind IP | No se encontró opción ECS dedicada en este código ni documentación consultada. |

No se intentó pasar un dict/tuple inventado como hostPort: que el Docker SDK
interno acepte esa estructura no lo convierte en contrato ECS soportado.
Tampoco se inyectaron variables supuestas o configuraciones del daemon.

## Matriz investigada

| Candidato | Decisión de ejecución | Motivo |
| --- | --- | --- |
| A: bridge, hostPort fijo, red preexistente, HOSTNAME automático | Ejecutado | Control que reproduce el caso previo y comprueba red efectiva. |
| B: bridge, hostPort fijo, HOSTNAME explícito | Ejecutado | Comprobar la hipótesis de autodetección del contenedor MiniStack. |
| C: bridge, hostPort=0, HOSTNAME explícito | Ejecutado | Puerto dinámico traducido por el código existente. |
| D: host | No ejecutado | No genera el binding requerido; compartir red host no mejora el aislamiento exigido. |
| E: awsvpc | No ejecutado | Código omite publicación host; lista vacía de bindings no es éxito. |
| Red Docker preexistente | Incluida en A/B/C | Red propia con default binding 127.0.0.1, sin cambios globales. |
| Opción específica MiniStack para IP | No ejecutada | No se encontró opción soportada/documentada; no inventar flags. |

Cada candidato creó su propia red e instancia MiniStack, tareas BusyBox
sintéticas y un cluster/service con desiredCount=1. No hubo proxy ni otros
servicios. Control MiniStack publicado solo en loopback con puerto temporal;
la tarea utilizó puerto fijo temporal o asignación dinámica del daemon.

## Evidencia real

| Prueba | Identidad | Puerto API local | HostPort solicitado | Bindings efectivos de tarea | Resultado |
| --- | --- | --- | --- | --- | --- |
| A | `sf-loop-35050eb803` | 59236 | 59237 | `0.0.0.0:59237`, `[::]:59237` | Rechazada |
| B | `sf-loop-aba602e840` | 59251 | 59252 | `0.0.0.0:59252`, `[::]:59252` | Rechazada |
| C | `sf-loop-2f8defb343` | 59267 | 0 | `0.0.0.0:32768`, `[::]:32768` | Rechazada |

Ejemplo Docker inspect de B:

```json
[
  {"HostIp": "0.0.0.0", "HostPort": "59252"},
  {"HostIp": "::", "HostPort": "59252"}
]
```

Las tres tareas usaron la red esperada `sf-loop-<id>-net`. En A, Python vio
HOSTNAME `e970f5427d48`; en B/C vio el nombre explícito del contenedor. Por
tanto, la hipótesis de una red equivocada por HOSTNAME **no explica** el
fallo observado. El binding predeterminado de esta red no produjo loopback
en las llamadas ECS/Docker SDK probadas. No se atribuye una causa interna
adicional al daemon sin evidencia.

No se probó HTTP después de detectar wildcard: seguridad rechaza el candidato
antes. El harness solo considera éxito si pasa inspect exclusivo loopback,
HTTP real con marcador sintético, pertenencia de task ARN a ECS y
desiredCount=1. Ningún candidato llegó a esa aprobación.

## Seguridad y cleanup

Un monitor empieza antes de CreateService e inspecciona contenedores por el
label de familia único. Ante binding distinto de 127.0.0.1 ejecuta kill;
el hilo principal también comprueba bindings inmediatamente al retornar la
creación. El polling no elimina la breve ventana entre publicación y detección:
hubo exposición wildcard transitoria durante estas pruebas autorizadas.

Finally detiene MiniStack para impedir recreación, termina el monitor, elimina
solo tareas con la familia del intento, su contenedor MiniStack y su red.
Los tres intentos reportaron cleanup_errors=[] y guard_errors=[]. Al terminar,
`docker ps` no mostró contenedores activos. Recursos de 12.2A y otros targets
no se eliminaron ni modificaron. No hay volúmenes nuevos en esta matriz.

No se cambió Docker daemon, runtime, MiniStack, firewall, workflows, CD,
Promotion ni metadata. No se usó AWS real. No socat, portproxy, reverse proxy,
ALB ni Compose para desplegar la aplicación.

## Verificador y tests

Archivo: `scripts/deployment/validate_ministack_loopback.py`.
Requiere imagen fijada y BusyBox disponibles, Docker Linux y ejecución
explícita. Reporte local ignorado: `data/ministack-loopback-report.json`.

```powershell
.venv\Scripts\python.exe scripts/deployment/validate_ministack_loopback.py `
  --execute --report data/ministack-loopback-report.json
```

La ejecución real terminó con código 1 y la conclusión bloqueante. Pruebas
unitarias limitadas al harness: IPv4 exacto, wildcard IPv4/IPv6, mezcla de
bindings, campo ausente, puertos sin publicar, cleanup ante fallo y rechazo
de redirects. No se ejecuta pytest completo.

## Recomendación

No seguir con CD completo en esta configuración. Mantener el requisito de
loopback; revisar mediante decisión explícita la versión/configuración del
emulador o el target permitido, y repetir la validación antes de integrar.
Si se propone otro mecanismo de publicación, necesita revisión de alcance
y seguridad; no relajar automáticamente la spec a wildcard.

`specs/phase-12-ministack-aws.md` no se modificó. Cualquier cambio de spec
permanece sujeto a revisión; aquí solo se registra evidencia negativa.
