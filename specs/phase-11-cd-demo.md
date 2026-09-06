# Fase 11.1 — Especificación MiniStack CD Demo

Estado: especificación para implementación posterior. Esta fase define el
contrato y sus escenarios de aceptación; no activa deployments.

## Contexto

MCP Software Factory cuenta con FastAPI, LangGraph / SoftwareFactoryGraph,
MCP Servers privados, Git gobernado, CI, Promotion, Docker Compose y
persistencia local. La publicación de la propia plataforma es distinta de
Promotion de los proyectos generados: CD no ejecuta ni modifica ese flujo.

La Fase 10 validó GitHub Actions con un self-hosted runner Windows y consultas
read-only al Backend local. En el repositorio público esa integración está
inactiva: el runner fue desregistrado y el workflow está preservado en
`docs/examples/software-factory-local.yml`, según
[la documentación de Fase 10](../docs/phase-10-github-actions.md).
La implementación posterior requerirá habilitación explícita de Actions y
registro de un runner autorizado; esta especificación no los habilita.

Los artefactos existentes a reutilizar son `Dockerfile.backend`,
`frontend/Dockerfile` y `docker-compose.yml`. El Compose declara los servicios
Frontend y Backend; los MCP se ejecutan internamente bajo el Backend, sin
puertos públicos propios.

## Objetivo

Demostrar que una versión validada y versionada en GitHub puede identificarse
por SHA o tag, construirse, desplegarse, quedar accesible localmente, validarse
con healthchecks, revertirse manualmente y detenerse fácilmente.

El MiniStack mantiene una única instalación demo y un contrato pequeño de
publicación. CI conserva la responsabilidad de validar el código; CD consume
esa evidencia para el mismo SHA y no repite la suite completa de tests.

## Arquitectura

```text
GitHub: código versionado y validado
                 |
                 v
GitHub Actions: workflow_dispatch
                 |
                 v
Build / Package: identidad por commit SHA
                 |
                 v
Deploy Demo: Docker Compose existente
                 |
                 v
Health Validation --> Metadata y resultado
```

La implementación futura tendrá un workflow separado,
`.github/workflows/software-factory-deploy.yml`, dedicado al deployment manual.
`software-factory-local.yml` conserva su responsabilidad read-only. No se
modifica ni se reactiva su ejemplo como parte del nuevo contrato de CD.

## Target inicial

El único environment inicial será `demo-local`: máquina del usuario,
self-hosted runner Windows autorizado y Docker Compose local.

```text
GitHub Actions
      |
      v
Self-hosted runner Windows
      |
      v
Docker Compose local
      +--> Frontend: 127.0.0.1:5173
      +--> Backend:  127.0.0.1:8000 --> MCP privados
      `--> Volúmenes workspace y data
```

Se reutiliza Docker local, con soporte para los contenedores existentes, y su
configuración local. No se requiere AWS, endpoint público ni gasto cloud
adicional; se utiliza el equipo y los recursos ya disponibles del usuario.

El mecanismo futuro será equivalente a `docker compose build` y
`docker compose up -d`, ejecutado desde el checkout verificado, conservando
identidad del proyecto Compose, puertos, redes y volúmenes. No se crea una
segunda arquitectura Docker. La configuración local persistente debe quedar
fuera de cualquier checkout que Actions pueda limpiar.

Detener o retirar los contenedores debe conservar los volúmenes. La eliminación
de datos requiere una decisión manual separada; nunca forma parte de deploy,
rollback o apagado rutinario. No usar eliminación de volúmenes ni limpieza
global de Docker. No detener procesos ajenos si los puertos están ocupados:
reportar el conflicto y fallar antes de reemplazar el stack.

## Target cloud futuro

AWS es una evolución candidata del mismo contrato de SHA, metadata, health y
rollback; no es un target habilitado en 11.1 ni en el alcance inicial de 11.2.

```text
IP del operador
      |
      v
Security Group: IP permitida /32
      |
      v
Una EC2 pequeña: Docker Compose
      +--> Frontend
      +--> Backend privado, con proxy desde Frontend si corresponde
      +--> MCP internos al Backend
      `--> EBS persistente: workspace y stores
```

La selección de instancia, región, conectividad, configuración del proxy y
credenciales cloud se resolverá en una fase posterior. AWS debe poder apagarse
y retirarse manualmente, con una decisión explícita sobre retener o eliminar
EBS. No se promete acceso público irrestricto.

## Trigger

El único trigger inicial será `workflow_dispatch`. No habrá deployment por
push, pull request, tag creado ni finalización automática de CI.

| Input conceptual | Contrato |
| --- | --- |
| `environment` | Obligatorio; solo `demo-local` inicialmente. |
| `commit_sha` o `version` | Selección explícita de un SHA completo o tag existente. Al menos uno obligatorio; si se envían ambos deben resolver al mismo commit. |

Resolver tags una sola vez y fijar el SHA del checkout antes de construir.
Rechazar una referencia inexistente, ambigua o no validada. No aceptar comandos
arbitrarios como inputs. Permisos GitHub mínimos: `contents: read`, sin PAT,
repository writes ni nuevos secrets de GitHub para `demo-local`.

## Pipeline

| Paso | Responsabilidad y condición de salida |
| --- | --- |
| 1. Checkout | Obtener la versión solicitada desde el repositorio GitHub y fijar su SHA. |
| 2. Validate source | Verificar identidad del checkout, procedencia remota, limpieza y evidencia de CI para ese SHA. Validar target, acceso Docker y configuración local requerida. |
| 3. Build | Construir Frontend y Backend con los Dockerfiles existentes desde el checkout validado. |
| 4. Package / tag | Identificar ambas imágenes locales por el mismo SHA y conservar referencias de la versión anterior. No se requiere registry. |
| 5. Deploy | Actualizar el proyecto Compose existente usando las imágenes del SHA solicitado y sus volúmenes persistentes. |
| 6. Health check | Validar Backend y Frontend mediante los endpoints existentes, con espera limitada. |
| 7. Record deployment metadata | Persistir el resultado y actualizar punteros de versión solo después de health satisfactorio. |
| 8. Report result | Informar environment, SHA, versión, estado y resultado de health; el job falla si el deployment falla. |

Registrar el intento como `pending` antes de sus efectos y como `deploying`
antes de reemplazar contenedores. El paso 7 cierra tanto éxito como fallo; un
fallo temprano salta los pasos restantes de despliegue, pero conserva resultado
diagnóstico seguro. No ejecutar evaluaciones LLM ni tests completos en CD.

## Versionado

`deployed_sha` es la identidad principal e inmutable: SHA completo del checkout.
`version` es el tag solicitado o el mismo SHA si no hay tag. `latest` no puede
ser la única fuente de verdad de código, imágenes ni rollback.

El código debe existir en GitHub y pertenecer al historial de `origin/master`.
Un working tree limpio por sí solo no prueba que el código esté publicado:
también se verifica su procedencia remota. Un SHA remoto tampoco autoriza
construir archivos dirty. La construcción siempre usa un checkout limpio de
ese SHA, sin incorporar cambios locales, datos ni secretos al paquete.

La evidencia de validación debe corresponder a la propia plataforma y al SHA
seleccionado. No confundir el CI de un proyecto generado con el CI del código
que se está desplegando. La fuente concreta de evidencia se conectará en 11.2
con el mecanismo existente, sin introducir otra política de gates.

## Persistencia

Se mantienen los volúmenes existentes `mcp-software-factory-workspace` y
`mcp-software-factory-data`, montados en `/app/workspace` y `/app/data`.
Cambiar el checkout o SHA no debe cambiar estos nombres ni crear volúmenes
vacíos que oculten los datos previos.

Deben sobrevivir workflows, checkpoints, approvals, eventos, repositorios Git
generados, CI state, governance, observability, FinOps y knowledge stores.
Los volúmenes tienen un ciclo de vida independiente de las imágenes.

Los deployments se realizan en una ventana controlada sin operaciones mutantes
en curso. Se conservan los mecanismos actuales de apagado y recuperación.
No introducir migraciones nuevas de bases de datos: desplegar o revertir solo
versiones compatibles con los stores existentes. Ante incompatibilidad, fallar
antes de reemplazar la aplicación. Rollback de aplicación no restaura ni borra
datos históricos.

## Health

| Componente | Validación mínima |
| --- | --- |
| Backend | GET `http://127.0.0.1:8000/health`: HTTP 200 y JSON con `status=ok`. |
| Frontend | GET `http://127.0.0.1:5173/`: HTTP 200. |
| MCP | Cuando sea necesario, evidencia indirecta desde mecanismos existentes del Backend, sin endpoints MCP públicos ni ejecución mutante. |

Usar intentos acotados: ventana máxima inicial propuesta de 120 segundos,
timeout por petición de 5 segundos y pausa de 5 segundos entre rondas, sin
superar la ventana total. Un error transitorio durante arranque puede
reintentarse dentro de esa ventana; al agotarse, el resultado es `failed`.

La respuesta saludable debe pertenecer a los contenedores recién desplegados:
verificar servicio, referencia de imagen y SHA antes de aceptar el health.
Un proceso viejo que responda en el puerto no demuestra que la nueva versión
funcione. Health no sustituye CI ni garantiza disponibilidad individual de
todos los MCP. No se agregan endpoints.

## Metadata

Contrato mínimo de un intento durable; el formato físico se decidirá en 11.2
y residirá en almacenamiento local persistente. No se implementa store aquí.

| Campo | Semántica |
| --- | --- |
| `environment` | `demo-local` inicialmente. |
| `deployed_sha` | SHA completo verificado del candidato; en un intento fallido no implica versión activa saludable. |
| `version` | Tag resuelto o SHA explícito. |
| `deployed_at` | Fecha UTC ISO 8601 del éxito de deploy/health; null antes del éxito o si nunca fue saludable. |
| `status` | `pending`, `deploying`, `healthy`, `failed` o `rolled_back`. |
| `current_sha` | Último SHA confirmado saludable; null antes del primer éxito. |
| `previous_sha` | SHA saludable anterior distinto de current; null si no existe. |

Cada intento necesita identidad diferenciable, por ejemplo el ID y número de
intento del job, y hora de inicio para correlación. Se conserva el historial
de resultados; el puntero del environment se actualiza atómicamente con el
éxito. Un error al registrar el resultado impide reportar éxito completo.

Transiciones: `pending -> deploying -> healthy`, o `pending/deploying -> failed`.
Un rollback exitoso registra el intento como `rolled_back` y el environment
activo como saludable sobre el SHA restaurado. `rolled_back` exige los mismos
healthchecks que `healthy`; no significa solamente que se solicitó rollback.

Los punteros representan evidencia confirmada, no una garantía de qué proceso
está respondiendo después de un fallo parcial. El último estado `failed` debe
permanecer visible y el operador debe verificar el runtime antes de recuperarlo.

## Rollback

Rollback manual significa redeploy de `previous_sha` mediante el mismo pipeline,
con fuente verificada, referencias de imágenes y validación de health.
Si no existe `previous_sha`, rechazar con un resultado explícito.

Ejemplo: con `current_sha=B` y `previous_sha=A`, desplegar A y validar health.
Solo tras el éxito quedan `current_sha=A`, `previous_sha=B`; se preserva el
registro original de B y se añade el resultado `rolled_back` del intento.
Si falla, mantener los punteros confirmados y reportar `failed`.

Conservar las imágenes/referencias anteriores y los datos para facilitar la
recuperación. No hay rollback automático ni garantía de cero downtime. La
retención mínima cubre current y previous; su limpieza posterior es manual.

## Seguridad

Para `demo-local`, Backend escucha en `127.0.0.1:8000` y Frontend en
`127.0.0.1:5173`. MCP permanece privado. No abrir firewall, crear túneles ni
exponer servicios públicamente.

Actions tendrá `contents: read` y ninguna escritura en GitHub. La configuración
local del Backend se conserva fuera del repositorio y del checkout desechable.
No imprimir el environment completo, secretos, prompts, tokens ni credenciales;
no publicar `.env`, `.env.production`, workspace, data, SQLite o logs como
artefactos. Los nombres de archivos de configuración son referencias, nunca
sus valores reales. No se necesitan nuevos secrets de GitHub para este target.

El dispatch y el runner deben quedar limitados a operadores autorizados y código
verificado del repositorio. No ejecutar pull requests o forks no confiables en
la máquina del usuario. La existencia del repositorio público no habilita el CD.

AWS futuro: acceso solo desde IP permitida `/32`, máximo una instancia, MCP
privados y Backend sin exposición pública cuando Frontend pueda hacer proxy.
No abrir SSH público si se usa Session Manager. Secrets fuera del repositorio;
credenciales cloud y su gestión se especificarán posteriormente.

## Cost Guardrails

Para AWS futuro: una EC2 pequeña y EBS pequeño, dimensionados después de medir
el consumo del stack. Sin autoscaling, Load Balancer, NAT Gateway, RDS, EKS ni
ECS inicialmente. La instancia debe poder apagarse cuando no se use.

Se recomienda un AWS Budget bajo; es una alerta presupuestaria, no una garantía
de apagado. EBS y otros recursos retenidos pueden seguir generando cargos al
apagar la instancia. La retirada debe inventariarlos. No fijar precios concretos
sin verificación posterior de región, recursos y tarifas.

## Failure Behavior

| Fallo | Resultado requerido |
| --- | --- |
| Fuente, CI o configuración inválida | `failed`; no reemplazar contenedores. |
| Build o package | `failed`; conservar aplicación e imágenes anteriores. |
| Deploy | `failed`; conservar datos y referencias anteriores, informar posible actualización parcial. |
| Health | `failed`; no declarar saludable el candidato ni promover sus punteros. |
| Metadata | Job fallido y diagnóstico explícito; no declarar deployment exitoso sin registro coherente. |
| Interrupción del job | No inferir éxito; al reanudar verificar runtime y cerrar el intento incompleto antes de iniciar otro. |

Un health fallido tras reemplazar contenedores puede dejar el stack indisponible;
la recuperación es manual. No borrar ni sobrescribir automáticamente la versión
anterior. Los resultados reportan etapa y diagnóstico seguro, sin secrets.

## Idempotencia

Repetir el mismo SHA es seguro: puede reconstruir o reiniciar, pero debe preservar
volúmenes, identidad del proyecto y coherencia de metadata. Si A ya es current,
otro deployment saludable de A mantiene current=A y previous sin cambios; no
debe convertir previous en A ni perder el rollback disponible.

Cada ejecución tiene un resultado propio. Reintentar el registro del mismo
intento no duplica registros. Serializar deployments y rollback por environment;
una segunda solicitud espera o se rechaza explícitamente. No cancelar un
deployment en curso para comenzar otro sobre los mismos contenedores.

## Criterios de aceptación

Estos criterios se ejecutarán en 11.2; su definición no implica validación real
de deployment en 11.1.

| ID | Criterio verificable |
| --- | --- |
| AC1 | Solo un dispatch manual autorizado inicia deployment. |
| AC2 | El deployment identifica un SHA completo y verificable. |
| AC3 | El build usa código limpio, versionado en GitHub y validado para ese SHA. |
| AC4 | Reutiliza Dockerfiles y Compose existentes. |
| AC5 | Backend devuelve HTTP 200 y `status=ok` dentro del plazo. |
| AC6 | Frontend devuelve HTTP 200. |
| AC7 | MCP permanece privado y sin nuevos puertos publicados. |
| AC8 | Workspace y todos los stores sobreviven a redeploy y restart. |
| AC9 | Redesplegar el mismo SHA preserva estado y previous_sha. |
| AC10 | Rollback manual a previous_sha aplica los mismos checks y registra resultado. |
| AC11 | No hay secrets hardcodeados, en outputs ni en artefactos. |
| AC12 | demo-local funciona exclusivamente con exposición loopback. |
| AC13 | CD está separado del workflow read-only histórico. |
| AC14 | Detener/retirar contenedores conserva datos persistentes. |
| AC15 | Build, deploy o health fallidos nunca se reportan como éxito. |
| AC16 | Actualización de metadata y punteros es coherente y durable. |
| AC17 | Requests concurrentes no operan simultáneamente sobre el environment. |
| AC18 | El health se atribuye a las imágenes del SHA seleccionado. |

## Escenarios

Los símbolos A, B y C representan SHAs completos distintos publicados y
validados; no son valores literales aceptables para `deployed_sha`.

| Escenario | Preparación y acción | Resultado esperado |
| --- | --- | --- |
| A — First Deploy | Sin deployment previo; desplegar A. | Health OK; current=A, previous=null, status=healthy. |
| B — Redeploy Same SHA | current=A; desplegar A otra vez. | Health OK; current=A; previous conservado; datos intactos. |
| C — New Version | current=A; desplegar B. | Health OK; previous=A, current=B. |
| D — Failed Health | current=B, previous=A; desplegar C y hacer fallar health. | Intento failed; C nunca healthy; punteros B/A conservados; reportar runtime potencialmente parcial. |
| E — Manual Rollback | current=B, previous=A; solicitar rollback a A. | Health OK; current=A, previous=B; intento rolled_back; datos intactos. |
| F — Restart | Deployment healthy; reiniciar contenedores. | Workspace/data y metadata permanecen; verificar health nuevamente. |
| G — Stop | Detener o retirar contenedores de la demo. | Servicios detenidos; volúmenes presentes; redeploy puede recuperar los datos. |
| H — Invalid Source | SHA no remoto, dirty checkout o CI ausente para el SHA. | Rechazo antes de build/deploy; runtime actual intacto. |
| I — Build Failure | Fallar construcción de un servicio. | Intento failed; no desplegar mezcla de imágenes; current intacto. |
| J — Concurrent Requests | Solicitar deploy y rollback al mismo environment. | Ejecución serializada o rechazo explícito; sin carrera de punteros. |

La evidencia futura incluye metadata antes/después, SHA de checkout e imágenes,
respuestas health y comprobación de registros persistentes representativos.
No copiar stores o secretos a GitHub para demostrar persistencia.

## Fuera de alcance

- Auto deploy on push, pull_request deploy, producción y staging.
- Kubernetes, EKS, ECS complejo, Terraform, Helm y ArgoCD.
- Load balancer, autoscaling, multi-region, blue/green, canary deployment y traffic shifting.
- RDS, Redis, CDN, TLS/domain y external secret manager.
- Automatic rollback y nuevas database migrations.
- Recursos AWS, credenciales cloud o implementación de AWS en 11.1.
- Cambios al runtime, MCP, gates CI, Promotion, Docker o Actions durante esta especificación.

## Handoff a 11.2

Fase 11.2 implementará únicamente el workflow separado
`software-factory-deploy.yml`, deployment `demo-local`, metadata mínima,
health validation y rollback manual conforme a este contrato.

Antes de implementarlo se concretarán el registro autorizado del runner, el
acceso a Docker y configuración local, la evidencia CI del SHA seleccionado,
el almacenamiento mínimo de metadata y la selección de imágenes por SHA usando
el Compose existente. Los escenarios y AC de este documento serán su checklist
de validación.

AWS no se implementará en 11.2 salvo decisión explícita posterior. La Fase 11.1
entrega exclusivamente esta especificación; no crea workflows, recursos,
scripts de deployment, stores, endpoints ni cambios de runtime.
