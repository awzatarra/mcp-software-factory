# Fase 12.1 — Especificación Deployment AWS Emulado con MiniStack

Estado: especificación revisada tras 12.2A.1; implementación de CD pendiente.
Revisión: 2026-09-06. Las pruebas aisladas de MiniStack 1.5.8 acreditaron
capacidades parciales y un bloqueo ECS de networking. Esta revisión aprueba
compute local Compose, no declara validado el deployment integrado.

## Contexto

Existen el runtime Docker Compose y el CD `demo-local` de
[Fase 11](phase-11-cd-demo.md), cuyo
[cierre E2E](../docs/phase-11-cd-demo.md) conserva sus evidencias y limitaciones.
Esta fase agrega un tercer target, `demo-aws-emulated`, sin reemplazarlos.

El nombre MiniStack en esta fase identifica el emulador AWS del proyecto
`ministackorg/ministack`, no solamente el nombre de la demo Docker de Fase 11.
El deployment publica la propia plataforma; no ejecuta ni modifica Git, CI o
Promotion de los proyectos generados.

## Objetivo

Especificar una arquitectura híbrida: registry AWS emulado mediante MiniStack
ECR y ejecución de Frontend/Backend mediante Docker Compose local. Conservar
imágenes por SHA/digest, persistencia, health, auditoría y rollback manual.
Docker Compose no emula ECS. No sustituir ECR por mocks ni un registry normal.

Costo AWS real: USD 0. Solo se usan recursos locales; no se crean cuentas,
recursos cloud, budgets ni billing. La demo no necesita evaluaciones LLM ni
llamadas pagadas para comprobar deployment y health.

## Arquitectura

```text
GitHub Actions: workflow_dispatch
                  |
                  v
Self-hosted runner Windows + Docker Desktop Linux
                  |
                  v
MiniStack ECR: 127.0.0.1:4566
                  |
                  v
      backend:<SHA> + frontend:<SHA>
          identidad fijada por digest
                  |
                  v
        Docker Compose local separado
                  |
          +-------+----------------+
          |                        |
          v                        v
 Backend 127.0.0.1:18000    Frontend 127.0.0.1:15173
 MCP privados             UI local
                   |
                   v
             workspace + data
             storage local durable

Runner ----> Health ----> Journal del target ----> Report
                             current / previous
MiniStack ----> Estado emulado durable separado
```

ECR demuestra registry AWS emulado; Compose proporciona compute local seguro,
no un servicio AWS emulado. Los Dockerfiles existentes siguen siendo fuente
del build. El Compose futuro del target será independiente de demo-local;
ninguna respuesta API reemplaza la verificación de contenedores reales.

### ADR: descartar ECS para el target actual

Decisión: reemplazar MiniStack ECS por Docker Compose local para ejecutar la
aplicación. ECS queda **Evaluated but rejected for current demo target**.

La [validación 12.2A](../docs/phase-12-2a-ministack-capabilities.md) demostró
ECR create/push/describe/pull, tareas ECS reales y persistencia limitada a las
pruebas descritas. La [investigación 12.2A.1](../docs/phase-12-2a1-ministack-loopback.md)
concluyó `MINISTACK_ECS_LOOPBACK_UNSUPPORTED`: los candidatos bridge probados
publicaron `0.0.0.0` y `::`, aun usando la red esperada. No se identificó una
opción soportada/documentada de HostIp exclusivo `127.0.0.1` para esta imagen.

No se relaja loopback. No se introducen proxies, reglas de firewall, parches
MiniStack ni cambios globales del daemon. La alternativa aprobada cambia
explícitamente el compute, sin afirmar equivalencia Compose/ECS. Los informes
históricos se conservan; el bloqueo del compute ECS no se declara resuelto.

## Target

`environment = target = demo-aws-emulated`, región `us-east-1`.
Una instalación local, un operador autorizado, sin autoscaling.

Separar nombres, journal, locks, red, volúmenes, imágenes y puertos de
`demo-local`. Puertos obligatorios: Backend `127.0.0.1:18000`,
Frontend `127.0.0.1:15173`; MiniStack `127.0.0.1:4566`.
Si están ocupados por procesos ajenos, fallar sin detenerlos. No reutilizar
los volúmenes ni `C:\software-factory-deploy\deployment.json` de Fase 11.

## Servicios AWS emulados

| Candidato | Decisión | Motivo |
| --- | --- | --- |
| ECR | Seleccionado | Registro de las dos imágenes por SHA/digest. |
| ECS | Evaluado y rechazado para esta demo | Wildcard bindings en 12.2A.1; viola loopback exclusivo. |
| EC2 | Descartado | No se necesitan instancias ni gestión de hosts emulados. |
| EBS | Descartado | No se ha acreditado un volumen de bloques montable útil para este caso; usar storage local. |
| S3 | Descartado | No sustituye un filesystem SQLite; no hay requisito de objetos. |
| SSM | Descartado | Configuración local no sensible suficiente; no almacenar secretos en el emulador. |
| CloudWatch | Descartado | Health y journal cubren la demo; no se agrega una segunda observabilidad. |

ECR documenta repositorios, imágenes y carga de layers, pero no escaneo real;
su token de autorización es fijo y no tiene validez AWS.
[Referencia ECR](https://ministack.org/docs/services/ecr).

Como referencia histórica, ECS documenta `CreateCluster`, `RegisterTaskDefinition`, `CreateService`,
`UpdateService`, `DescribeServices`, `DescribeTasks` y `StopTask`, con tareas
ejecutadas por Docker. No reenvía `awslogs` a CloudWatch Logs ni emite eventos
EventBridge desde `SubmitTaskStateChange`.
[Referencia ECS](https://ministack.org/docs/services/ecs).

## MiniStack

Endpoint del cliente host: `http://127.0.0.1:4566`.
La configuración documenta `PERSIST_STATE=1`, snapshots en `STATE_DIR` y
`GET /_ministack/health`. Montar el directorio de estado fuera del checkout.
Los snapshots al apagar no prueban durabilidad frente a terminación abrupta.
[Configuración oficial](https://ministack.org/docs/configuration).

Credenciales exclusivamente dummy, limitadas al proceso futuro:

```text
AWS_ACCESS_KEY_ID=test
AWS_SECRET_ACCESS_KEY=test
AWS_DEFAULT_REGION=us-east-1
AWS_EC2_METADATA_DISABLED=true
```

No ejecutar `aws configure` ni modificar perfiles globales. Eliminar del
entorno del cliente herencias de session token, perfiles y proveedores de
credenciales reales; usar configuración aislada sin consultar IMDS.

12.2B usará la identidad validada en las pruebas aisladas, sin depender de latest:

```text
MiniStack 1.5.8
sha256:d865b1e43b0b1a7e6f246e1a46fb8a0a2fa04e2a94c64740c8ff454748040726
```

Los resultados previos no acreditan todavía el E2E de imágenes de la plataforma.

Lifecycle: start explícito, health con espera acotada, restore verificado,
stop ordenado. MiniStack ya no administra el compute de aplicación. Reiniciar
el registry no debe recrear ni duplicar servicios Compose. El stop completo
detendrá los contenedores propios de aplicación y el emulador, sin
eliminar storage, snapshots, imágenes retenidas ni metadata.

## Pipeline

```text
workflow_dispatch
  -> validate SHA/evidencia/target
  -> ministack health/capacidades
  -> registry ensure
  -> build imágenes
  -> push ECR + verificar digest
  -> pull/resolve imágenes exactas desde ECR
  -> deploy Docker Compose local
  -> health + identidad real
  -> metadata atómica
  -> report seguro
```

Usar AWS CLI con `--endpoint-url http://127.0.0.1:4566` explícito en cada
operación AWS, o SDK con endpoint explícito en cada cliente. Si falta el
override local, fallar cerrado; nunca usar AWS como fallback.

AWS CLI más controlador pequeño es la opción elegida. Implementar
describe-before-create y comparación del estado deseado; Terraform y CDK no
son necesarios. No incluir scripts ni workflows ejecutables en 12.1.

Mantener validación de SHA completo, checkout exacto/limpio, repositorio
autorizado, evidencia de plataforma vinculada al SHA y ventana de mantenimiento
de Fase 11. Validar el contrato del Compose separado de este target: imágenes
por digest, storage, red, puertos y contenedores. No reutilizar la identidad
Compose ni modificar la definición existente de demo-local.

## Registry

Repositorios lógicos: `mcp-software-factory-demo-aws/backend` y
`mcp-software-factory-demo-aws/frontend`. Tags obligatorios: `<SHA>` completo;
ejemplos abreviados de identidad: `backend:<SHA>` y `frontend:<SHA>`.

Registrar digest por componente y usarlo como identidad de despliegue.
No sobrescribir un SHA publicado ni depender de `latest`. Retener al menos
las imágenes current/previous y sus manifests/layers. No reconstruir
silenciosamente una versión durante rollback.

Antes de cerrar 12.2B, demostrar push y pull reales de layers, no solo
`PutImage`/`DescribeImages`. Verificar que el registry devuelto es local y que
el Docker host puede alcanzarlo. Una URI con forma de AWS no autoriza
tráfico a `amazonaws.com`. Resolver el mapeo local con mecanismos documentados
de la versión fijada; si no funciona, bloquear, no inventar un registry mock.
No modificar globalmente registries inseguros ni credenciales Docker del usuario.

## Compute

Compute aprobado: Docker Compose local, proyecto propio `sf-demo-aws`, con
servicios `backend` y `frontend`, una instancia de cada uno. No crear clusters,
task definitions ni servicios ECS en 12.2B. No simular desiredCount ni Fargate.

Resolver/pullar desde MiniStack ECR las dos imágenes exactas del SHA; Compose
debe referenciarlas por digest. Deploy no construye imágenes ni cambia tags
silenciosamente. Verificar imagen efectiva de cada contenedor, identidad del
proyecto y ausencia de duplicados. No usar el registry solo como decoración.

Mantener comandos, usuario y health de las imágenes actuales. Configurar las
rutas existentes `/app/workspace` y `/app/data`; no migrar stores. Frontend
debe usar el Backend loopback de este target y CORS debe corresponder al
origen Frontend, mediante configuración de deployment, no cambios de runtime.

Compose es operado por el runner con Docker Desktop Linux. El socket usado
en las pruebas ECS no forma parte del contrato mínimo de ECR: no montarlo
en MiniStack ni en la aplicación por herencia de aquel experimento. Si una
necesidad del registry lo exigiera, bloquear y revisar, no añadirlo en silencio.

Validar los mounts y permisos con las imágenes reales de aplicación, así como
bindings loopback efectivos y restart. Las pruebas BusyBox no acreditan
automáticamente el comportamiento del usuario no-root del Backend.

Evitar dos Backends simultáneos escribiendo el mismo SQLite. La actualización
debe detener de forma controlada el escritor anterior antes de iniciar el
nuevo, respetando mantenimiento. Downtime de demo aceptado; no prometer
rolling deployment sin interrupción.

## Persistencia

Elegir almacenamiento local persistente, no un servicio adicional AWS.
Volúmenes propios obligatorios: `sf-demo-aws-workspace` y `sf-demo-aws-data`,
montados mediante Compose en `/app/workspace` y `/app/data`. Estado MiniStack
separado en `sf-demo-aws-ministack-state`. Nunca compartir los volúmenes
`mcp-software-factory-workspace` o `mcp-software-factory-data` de demo-local.
Los binds auxiliares deben ser resolubles por el daemon Linux, no solo por
PowerShell; no inventar traducciones automáticas de paths Windows.

Separar cuatro dominios: workspace, stores de aplicación, estado del emulador
y journal externo. Los snapshots ECR no equivalen a datos de aplicación
ni garantizan que los bytes de imágenes sobrevivan. Verificar manifests,
layers y pull tras recrear el emulador, además de preservar storage Docker.

Preservar SQLite, checkpoints, eventos, approvals y demás stores sin cambios
de esquema; comprobar registros de muestra antes/después, no solo nombres
de volúmenes. No compartir bases activas entre targets. Usar datos de demo
no sensibles; ninguna copia de datos reales es implícita.

## Networking

Todo acceso publicado será loopback. Red Docker propia del target, MCP
internos al Backend. Dentro de un contenedor, `127.0.0.1` no identifica al
host: usar conectividad local del daemon verificada para registry/control.

Bindings Compose explícitos obligatorios: `127.0.0.1:18000:8000` para Backend,
`127.0.0.1:15173:80` para Frontend y `127.0.0.1:4566:4566` para MiniStack.
No firewall nuevo, túneles, exposición pública, Route53 ni ALB. Antes de marcar
healthy, Docker inspect debe comprobar todos los puertos publicados de todos
los contenedores del target: únicamente HostIp=127.0.0.1, nunca 0.0.0.0 ni ::.
Bindings ausentes para un endpoint obligatorio tampoco son éxito. Ante
incumplimiento, detener contenedores afectados propios y fallar cerrado;
no avanzar current/previous. No añadir proxies ni workarounds.

GitHub y descargas de dependencias/imágenes pueden necesitar Internet como
en Fase 11; eso no autoriza servicios públicos ni llamadas a AWS real.

## Health

| Componente | Contrato obligatorio |
| --- | --- |
| MiniStack | `GET http://127.0.0.1:4566/_ministack/health`, HTTP 200 y disponibilidad; comprobar ECR además del gateway. |
| Backend | `GET http://127.0.0.1:18000/health`, HTTP 200, `status=ok`. |
| Frontend | `GET http://127.0.0.1:15173/`, HTTP 200. |

Sin endpoints nuevos ni aceptación por redirección. Esperas acotadas y timeout
por petición. Mantener ventana máxima de health de aplicación de Fase 11;
definir timeout finito para cada operación del emulador en 12.2B. Correlacionar
health con contenedor Compose y digest esperados, no con otro servicio del puerto.

## Versionado

`deployed_sha` y `version` identifican el SHA completo solicitado; current y
previous contienen SHA completo o null. `deployed_at` solo se establece al
completar health e identidad. Ambos componentes pertenecen al mismo release.

Conservar los estados existentes: pending, deploying, healthy, failed;
un intento rollback exitoso registra rolled_back y el target queda healthy.
Same SHA no desplaza previous. Un fallo nunca avanza current/previous.

## Metadata

Reutilizar el contrato de `scripts/deployment/deployment_metadata.py`, no crear
otro modelo de consumo por la plataforma. Extensión conceptual mínima:
`target = demo-aws-emulated` y `environment = demo-aws-emulated`.
Conservar `deployed_sha`, `current_sha`, `previous_sha`, `version`,
`deployed_at`, `status`, attempts y diagnóstico seguro.

El Journal actual valida y escribe `demo-local` explícitamente. No puede
usarse sin adaptación: 12.2B deberá parametrizar ese contrato de forma
compatible, conservando el default y pruebas de Fase 11. Esta fase no lo edita.

Home propuesto externo: `C:\software-factory-deploy-ministack`; contiene su
propio `deployment.json` y lock. Rechazar home del otro target o dentro de
checkout. Añadir referencias mínimas por intento a digests e identidad del
proyecto Compose si se necesitan para identidad/rollback, sin duplicar stores.

Escritura atómica y lock por target durante todo el intento. Si falla guardar
tras health, registrar fallo de metadata si el medio lo permite; conservar
los punteros confirmados y reportar posible diferencia entre runtime y journal.
No proclamar deploy confirmado ni ocultar fallo de persistencia del diagnóstico.

## Rollback

Operación exclusivamente manual, destino igual a previous confirmado.
Validar evidencia, artefactos retenidos y compatibilidad de stores antes de
mutar servicios; pull/use digest A desde ECR emulado, desplegar A mediante
Compose, ejecutar health/inspect y recién
entonces intercambiar current/previous: current=B, previous=A pasa a
current=A, previous=B. Sin previous o imagen exacta, rechazar. No rebuild
silencioso ni resolución por un tag mutable durante rollback.

Rollback de aplicación no revierte datos ni ejecuta migraciones. Un fallo de
deploy no restaura automáticamente contenedores anteriores: conserva los
punteros, informa estado degradado y requiere decisión humana.

## Failure Behavior

Stages cerrados: `validate`, `ministack`, `registry`, `build`, `push`,
`deploy`, `health`, `metadata`. `reason = <stage>_failed` mantiene el contrato
genérico; `reason_code` conserva únicamente códigos conocidos seguros.

| Caso | reason_code propuesto |
| --- | --- |
| SHA inválido | `invalid_commit_sha` |
| Endpoint no local | `non_local_aws_endpoint` |
| MiniStack indisponible | `ministack_unavailable` |
| Capacidad requerida no demostrada | `ministack_capability_unverified` |
| Comando fallido | `deployment_command_failed` |
| Comando agotó timeout | `deployment_command_timeout` |
| Health inválido | `deployment_health_failed` |
| Excepción inesperada | `deployment_failed` |

Reutilizar propagación segura de DeploymentError. La futura allowlist de
`command_operation` podrá incluir `ecr_describe_repositories`,
`ecr_create_repository`, `docker_build`, `docker_push`, `docker_pull`,
`docker_compose_up` y `docker_inspect`. Son etiquetas, nunca comandos o argumentos arbitrarios.
Si no hay comando, omitir la operación. CLI muestra reason_code, stage y
operación disponible. No stdout/stderr completos, stack traces, env, tokens
ni configuración Compose con contenido sensible en journal/artifacts/logs.

## Idempotencia

Ensure recursos: describir, crear solo ausentes, comparar propiedad y contrato;
rechazar colisiones ajenas. No borrar/recrear datos para obtener estado limpio.
Same SHA saludable verifica identidad sin rebuild ni recreaciones
innecesarias; estado degradado se repara solo bajo intento explícito.

Mismo attempt ID exitoso no duplica efectos. Identidad incompatible se rechaza;
intento fallido necesita nuevo ID. Locks y concurrency impiden dos mutaciones
del target. Restart reconcilia contenedores Compose reales y metadata antes de desplegar;
no inferir salud solo por records restaurados.

## Seguridad

Solo dummy credentials y datos sintéticos. No account IDs reales, secretos
de aplicación en MiniStack, `.env` versionados, impresión de env ni sockets
públicos. No llamadas a proveedores como efecto colateral del health.
Allowlist de endpoint/registry local comprobada antes de cualquier operación;
sin perfiles, redirects ni fallback a AWS. No cambiar credenciales globales.

No confiar en emulación IAM como aislamiento: las limitaciones documentan
ausencia de validación SigV4 y networking VPC real, y validación más permisiva
que AWS. Esta demo no certifica seguridad cloud.
[Limitaciones oficiales](https://ministack.org/docs/limitations).

No habilitar runner para PRs no confiables. No publicar homes, snapshots,
workspace, SQLite ni logs sensibles. No registrar secrets reales en archivos
Compose versionados; una necesidad futura de secrets requiere decisión separada.

## GitHub Actions

Workflow futuro separado:
`.github/workflows/software-factory-deploy-ministack.yml`.
No editar ni reemplazar `software-factory-deploy.yml` de `demo-local`.
No crear ningún YAML en 12.1.

Solo `workflow_dispatch`; inputs `environment=demo-aws-emulated`,
`commit_sha`, `operation=deploy|rollback`. Self-hosted Windows autorizado,
Docker Desktop Linux; permisos mínimos contents:read, checkout sin persistir
credenciales y action fijada por commit. Concurrency propia del target,
cancel-in-progress false, timeout total finito. Sin push, PR ni schedule.

No ejecutar automáticamente stages del workflow de proyectos. AWS CLI con
entorno aislado; reportar únicamente SHA, target, estado, health y diagnóstico
seguro. Las políticas de autorización/publicación de Fase 11 no se relajan.

## Diferencias MiniStack vs AWS real

| Contrato | EMULATED: elegido aquí | REAL: evolución futura, no habilitada |
| --- | --- | --- |
| Registry | MiniStack ECR local | AWS ECR con autenticación IAM y registry TLS. |
| Compute | Docker Compose local, NO emulación ECS | Diseñar sustitución por ECS/Fargate/EC2, roles, capacidad y red. |
| Storage | Volúmenes/binds locales, SQLite | Diseñar storage durable compatible; no basta cambiar endpoint. |
| Control | Endpoint local y dummy credentials | Sin override, credenciales de mínimo privilegio autorizadas. |
| Red | Loopback y Docker | Red/ingress/TLS cloud requieren diseño explícito. |
| Health | HTTP local con identidad | Mismo contrato lógico, endpoints y seguridad por definir. |
| Auditoría | Journal local y errores seguros | Ubicación durable y acceso cloud por diseñar. |
| Rollback | Digests retenidos + previous | Mismo principio, validar stores y despliegue cloud. |

Conservar nombres lógicos, ECR, SHA, metadata y rollback facilita el handoff,
pero quitar `--endpoint-url` NO convierte Compose en ECS ni la demo en producción.
No se certifican paridad IAM, disponibilidad, aislamiento, crash recovery,
escaneo de imágenes ni entrega de logs. Ningún cambio a AWS real queda
autorizado por esta especificación.

## Criterios de aceptación

Todos son pruebas futuras; ninguno se marca cumplido por escribir esta spec.

| AC | Evidencia requerida en 12.2B/E2E |
| --- | --- |
| AC1 | MiniStack inicia localmente con imagen/digest registrados. |
| AC2 | Health del gateway y operaciones de lectura ECR disponibles. |
| AC3 | Entorno aislado usa solo credenciales dummy; no proveedores reales. |
| AC4 | demo-aws-emulated separado en recursos, puertos y journal. |
| AC5 | Checkout y deployment corresponden al SHA explícito verificado. |
| AC6 | Ambas imágenes identificadas por SHA y digests persistidos. |
| AC7 | Push/pull de manifests y layers mediante registry emulado local. |
| AC8 | Imágenes recuperadas desde ECR emulado ejecutadas mediante Compose local aprobado; contenedores reales corresponden a los digests del SHA, una instancia por servicio. |
| AC9 | Backend correcto devuelve 200 y status=ok; Docker inspect confirma todos los bindings obligatorios exclusivamente 127.0.0.1 antes de healthy, sin wildcard. |
| AC10 | Frontend correcto responde 200 y apunta al Backend del target. |
| AC11 | Workspace y registros de stores conservados, sin migración. |
| AC12 | Same SHA conserva previous, identidad y ausencia de duplicados. |
| AC13 | Deploy B saludable deja current=B, previous=A. |
| AC14 | Rollback manual A deja current=A, previous=B; intento rolled_back. |
| AC15 | Restart preserva estado ECR, imágenes utilizables y contenedores Compose coherentes sin duplicación. |
| AC16 | Stop conserva datos críticos, imágenes retenidas y journal. |
| AC17 | Fallos/timeout preservan stage, reason, reason_code y operación segura. |
| AC18 | demo-local conserva archivos, datos y comportamiento sin cambios. |
| AC19 | Ningún endpoint/recurso/credencial AWS real utilizado. |
| AC20 | Handoff cloud y diferencias inevitables documentados explícitamente. |

## Escenarios

Usar SHA completos A/B realmente validados en ejecución futura; no inventar
commits ni evidencia. Capturar resúmenes sanitizados antes/después.

| Escenario | Acción futura | Resultado esperado |
| --- | --- | --- |
| A — MiniStack Start | Start aislado y health | Endpoint loopback disponible, sin mutar demo-local. |
| B — First Deploy A | Validate/build/push ECR/pull digest/Compose/health | current=A, previous=null, healthy, dos contenedores reales con loopback verificado. |
| C — Redeploy Same SHA A | Resolver identidad ECR y verificar Compose | Identidad estable, previous=null, sin rebuild ni duplicación. |
| D — New Version B | Publicar/verificar B en ECR y desplegar digests con Compose | current=B, previous=A solo después de health/inspect. |
| E — Rollback A | Pull/use digests originales ECR y Compose deploy A manual | current=A, previous=B, sin rebuild. |
| F — MiniStack Restart | Stop/start ordenado y recreación controlada | Estado ECR restaurado, pull disponible; compute Compose no se recrea por reiniciar registry. |
| G — Application Restart | Reiniciar contenedores Compose del target | Registros/workspace intactos, health, bindings e identidad recuperados. |
| H — Stop | Parada controlada de Compose y emulador | Sin servicios publicados; storage/journal/imágenes conservados, sin eliminar volúmenes. |
| I — Registry Failure | Fallo controlado en ensure/push | registry_failed o push_failed, razón específica, punteros intactos. |
| J — Compute Failure | Compose no inicia/actualiza contenedor | deploy_failed, no éxito ficticio ni rollback automático. |
| K — Health Failure | Candidato no cumple health | health_failed, punteros confirmados intactos, degradación explícita. |
| L — Invalid SHA | SHA malformado/no autorizado | validate_failed antes de build o mutación de recursos. |

Añadir pruebas negativas de binding no loopback, mount inválido, endpoint AWS,
imagen previous ausente, intento concurrente, error de metadata y excepción
inesperada. No usar datos reales ni eliminar volúmenes para simular fallos.

## Fuera de alcance

AWS real, cuentas/billing nuevos, Terraform sin necesidad, CDK, Kubernetes,
EKS, migración RDS/Redis, Lambda, API Gateway, CloudFront, Route53, ALB,
autoscaling, multi-region, production, staging, rollback automático,
auto-deploy, ECS emulado como compute de aplicación y GitHub-hosted runners.
Sin cambios a runtime, MCP, CI, Promotion
ni demo-local en esta fase. Sin modelos nuevos de stores o evaluación.

## Handoff a 12.2B

12.2B podrá implementar únicamente MiniStack lifecycle, repositorios ECR,
build/push/pull por SHA y digest, Docker Compose compute separado, metadata
compatible, health, rollback manual y workflow GitHub Actions separado.
NO implementar ECS. ECR sigue obligatorio; no sustituirlo por registry normal.

Usar la imagen fijada y evidencias 12.2A/12.2A.1 sin reinterpretar el fallo
de ECS como éxito. Validar con imágenes de plataforma push/pull real,
mounts/permisos, bindings Compose loopback y persistencia ECR/almacenamiento.
Restart previo fue ordenado y pull podía usar caché: no acredita crash recovery
ni recuperación de blobs en un daemon vacío. Si una prueba obligatoria falla,
bloquear y revisar, no abrir puertos, migrar stores o añadir servicios.

Después, implementar el adaptador mínimo con pruebas de compatibilidad de
Journal para demo-local, diagnósticos seguros, idempotencia y escenarios A-L.
Guardar evidencia sanitizada por AC y separar validación documental de E2E.
No afirmar completados AC de persistencia solo por ver volúmenes existentes.

Validación de esta revisión: `git diff --check` y revisión de alcance del único
archivo modificado. Sin nuevos scripts, workflows, cambios Docker/runtime,
recursos AWS ni ejecución de deployments. No pytest para esta revisión documental.
