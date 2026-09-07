# Fase 12 — AWS Emulado con MiniStack

## Objetivo

Cerrar el alcance de demo local `demo-aws-emulated`: Continuous Deployment
manual con registry AWS emulado, identidad por SHA/digest y compute local
seguro. No es un despliegue en AWS ni certifica preparación para producción cloud.

Este cierre utiliza la [especificación revisada](../specs/phase-12-ministack-aws.md),
la [validación de capacidades](phase-12-2a-ministack-capabilities.md), la
[investigación de loopback](phase-12-2a1-ministack-loopback.md) y la
[guía de implementación y operación](phase-12-ministack-deployment.md).
Los resultados A–H fueron aportados por el operador para este cierre; no se
repitieron deployments ni pruebas en esta tarea documental. No se adjuntan
logs completos ni se atribuyen IDs de Actions no proporcionados.

## Arquitectura final

```text
GitHub Actions (workflow_dispatch manual, SHA explícito)
                         |
                         v
             Self-hosted runner Windows
                         |
                         v
                Build + MiniStack ECR
                         |
                         v
          backend:<SHA> / frontend:<SHA>
                         |
                         v
             Docker images por digest
                         |
                         v
              Docker Compose local
                         |
                         v
               Frontend + Backend
                         |
                         v
                 Health / inspect
                         |
                         v
             Metadata current/previous
                         |
                         v
                  Rollback manual
```

MiniStack ECR es el componente AWS emulado. Docker Compose es compute local
seguro; **Compose no emula ECS**. El target mantiene recursos separados de
`demo-local`, journal externo y volúmenes propios. La configuración final
ECR-only no requiere montar el socket Docker en MiniStack.

MiniStack 1.5.8 está fijado a:

```text
sha256:d865b1e43b0b1a7e6f246e1a46fb8a0a2fa04e2a94c64740c8ff454748040726
```

## Decisión sobre ECS

MiniStack ECS fue evaluado y sí creó contenedores Docker reales. Sin embargo,
las variantes bridge investigadas publicaron puertos en `0.0.0.0` y `::`,
incluso con una red configurada para binding predeterminado loopback.
El resultado de 12.2A.1 fue `MINISTACK_ECS_LOOPBACK_UNSUPPORTED` para la
versión fijada y Docker Desktop Linux de este equipo.

Se decidió no relajar seguridad, no parchear MiniStack y no añadir proxies
ni cambiar el daemon. La revisión explícita de la spec conservó ECR y adoptó
Compose como compute. ECS no se utiliza en el deployment final; su evaluación
previa sigue documentada como evidencia histórica, no como capacidad aprobada.

## Qué se validó realmente

- Registry ECR emulado con repositories, push, describe y pull de imágenes reales.
- Identidad de imágenes por commit SHA y digest, utilizada por Compose.
- Deploy inicial, redeploy idempotente, nueva versión y rollback manual.
- Health y publicación local en loopback del target.
- Restart ordenado de registry y aplicación, sin duplicación de servicios.
- Conservación reportada de metadata y volúmenes al detener el target.
- Diagnósticos seguros y comportamiento de fallos cubiertos por las pruebas
  de implementación ya registradas; no se presenta esa cobertura como una
  nueva inyección E2E de fallos I–L.

La guía conserva los resultados históricos de tests. Este cierre no ejecuta
pytest, Actions, Docker ni llamadas a AWS.

## Resultados E2E

Identidades suministradas por el operador, conservadas literalmente:

| Versión | SHA |
| --- | --- |
| A | `4925a92baf0765fb62c8cb702f2a1fb9be79c838` |
| B | `276c6e51fea359854dae96bc780ed886d277e149` |

| Versión | Imagen | Digest |
| --- | --- | --- |
| A | backend | `sha256:df883cc3f26f9b1cde73da5e2b981293aa143af88d3d046476f1a86fb8834d9c` |
| A | frontend | `sha256:ea903de159a3e355e30b8b7276ccc913a53a76187d51df769f6a1bcb40694769` |
| B | backend | `sha256:d545895d1c42ea672c46163012e146ac4924edfb6421d374375b57b368eb78ec` |
| B | frontend | `sha256:9a722dd6a674e68778fc9f0d72e7ff59476668c948f043151ad03901d4cd41eb` |

| Paso | Resultado registrado |
| --- | --- |
| A — MiniStack Start | Health 200; binding `127.0.0.1:4566`. |
| B — First Deploy A | `healthy`, `previous=null`; digests A; backend `127.0.0.1:18000`, frontend `127.0.0.1:15173`. |
| C — Redeploy Same SHA | `healthy`, `current=A`, `previous=null`; mismos digests, sin duplicación. |
| D — New Version B | `healthy`, `current=B`, `previous=A`; digests B distintos. |
| E — Rollback A | `operation=rollback`, intento `status=rolled_back`, `current=A`, `previous=B`; digests originales A recuperados. |
| F — MiniStack Restart | Health 200, loopback intacto; ECR conservó digests A y ambas imágenes consultables por SHA. |
| G — Application Restart | `healthy`, `current=A`, `previous=B`; una instancia backend y una frontend, sin duplicación. |
| H — Stop | Contenedores detenidos, metadata y volúmenes preservados. |

Volúmenes confirmados en H: `sf-demo-aws-data`, `sf-demo-aws-workspace` y
`sf-demo-aws-ministack-state`. La existencia de esos volúmenes no sustituye
una comparación antes/después de cada registro de los stores.

## Criterios de aceptación

El [mapeo AC1–AC20](../specs/phase-12-ministack-aws.md#resultado-de-implementación)
conserva los criterios originales. Se registran **13 validados E2E**, **4
validados por capacidad/documentación** y **3 parciales**. No se etiqueta
ningún criterio revisado como no aplicable para ocultar evidencia faltante.

Los parciales son AC11 (contenido de todos los stores), AC16 (retención de
imágenes después de stop) y AC18 (auditoría antes/después de datos de
`demo-local`). La aprobación de ECS no forma parte de los AC revisados:
AC8 exige Compose. El cierre de la demo no declara los veinte AC íntegramente
verificados ni elimina estas verificaciones pendientes.

## Cercanía con AWS real

### Registry

MiniStack ECR utiliza repositories, tags por SHA, image digests, push,
describe y pull. La identidad del deployment queda fijada al digest, no a
un tag mutable. AWS ECR es el equivalente futuro: cercanía conceptual y
operacional alta para este contrato, sin afirmar paridad completa del servicio.

### Compute

Compose tiene cercanía parcial con un futuro ECS/Fargate o ECS/EC2. Se
conservan una versión explícita, imagen por digest, una instancia por servicio,
health, rollback y metadata current/previous. La persistencia sigue siendo
un requisito, pero scheduler, recuperación y almacenamiento cloud necesitan
otro diseño. Compose no equivale a ECS.

### Versionado y CD

```text
commit SHA -> Docker image -> ECR tag -> image digest -> deployment
```

Esta cadena es una de las partes más transferibles a un pipeline cloud.
El flujo manual Actions -> SHA -> build -> push ECR -> deploy -> health ->
metadata puede conservarse conceptualmente. Cambian endpoint, autenticación,
compute, networking y storage; no basta con quitar el endpoint override.

### Seguridad y persistencia

Loopback exclusivo, credenciales dummy, ausencia de fallback AWS, deployment
manual y rollback humano expresan principios de acceso restringido y cambios
autorizados. En AWS se traducirían en ingress restringido, IAM de mínimo
privilegio, red privada y autorización explícita. La prueba local **no valida
IAM ni VPC reales**.

Docker volumes y SQLite/stores actuales conservan semántica de persistencia;
rollback cambia versión de aplicación, no revierte datos. El soporte físico
cloud requiere diseño propio. EBS, EFS o RDS son alternativas a evaluar,
no reemplazos automáticos ni intercambiables para SQLite.

| Área | Emulado | AWS real | Cercanía |
| --- | --- | --- | --- |
| Registry | MiniStack ECR | AWS ECR | Alta |
| Image SHA/digest | Sí | Sí | Alta |
| GitHub Actions CD | Sí | Sí | Alta |
| Manual deployment | Sí | Sí | Alta |
| Rollback por versión | Sí | Sí | Alta |
| Metadata current/previous | Sí | Implementable | Alta |
| Compute | Docker Compose | ECS/Fargate o ECS/EC2 | Parcial |
| Networking | Loopback | VPC/Security Groups | Parcial |
| Storage | Docker volumes | Diseño explícito: EBS/EFS/RDS/etc. | Parcial |
| IAM | Credenciales dummy | IAM | Baja |
| HA/autoscaling | No | ECS/ASG | No implementado |

## Diferencias con AWS real

No se utilizaron recursos ni billing AWS. No se validaron IAM, VPC, compute
ECS aprobado, autoscaling, HA ni cloud storage. MiniStack no prueba paridad
total AWS, aislamiento multi-tenant ni disponibilidad regional. Este resultado
no acredita production readiness cloud.

## Riesgos y limitaciones

- Restart observado fue ordenado; no certifica crash recovery ni restauración
  de todos los blobs en un daemon vacío.
- Metadata y volúmenes conservados no prueban por sí solos integridad de todos
  los registros, backups o compatibilidad de futuras migraciones.
- El target depende del host Windows, Docker Desktop y el runner local.
- Rollback requiere imágenes previas retenidas y no revierte el estado de datos.
- No hay evidencia nueva de fallos E2E I–L; la cobertura automatizada previa
  permanece identificada como tal.

Checklist antes de publicar o reactivar automatización del repositorio:

- Finalizar jobs y detener el runner.
- Desregistrar el runner self-hosted.
- Mover o deshabilitar workflows ejecutables y preservar ejemplos documentales.
- Revisar secrets, assets y el historial Git, no solo el árbol actual.
- Mantener homes de deployment externos al repositorio.
- Confirmar que data, workspace, SQLite, logs y entornos privados están ignorados.
- Confirmar `git status` limpio después de los commits previstos.

Este checklist no afirma que esas acciones ya se realizaron. La tarea no
cambia visibilidad, registro del runner, secrets ni workflows.

## Handoff hacia AWS real

Mantener GitHub Actions, SHA, Dockerfiles, nombres lógicos de repositories,
digests, health, metadata y rollback manual. El delta conceptual mínimo es:

| Cambiar | Por |
| --- | --- |
| MiniStack ECR y endpoints localhost | AWS ECR y endpoints AWS explícitos |
| Credenciales dummy | OIDC/IAM con mínimo privilegio |
| Docker Compose | ECS/Fargate o ECS/EC2 |
| Loopback | VPC y Security Groups con acceso restringido |
| Docker volumes | Storage cloud diseñado y validado explícitamente |

**Fase 13 — AWS Real Demo Deployment** queda como propuesta opcional, no
implementada ni autorizada por este cierre:

```text
GitHub Actions -> OIDC -> AWS ECR -> 1 compute target pequeño -> health
```

Su alcance inicial sugerido sería demo de bajo costo, acceso restringido,
sin autoscaling, sin RDS inicialmente, sin ALB si puede evitarse y con
apagado/eliminación sencillos. No se diseña aquí esa fase completa.

## Estado final

**Fase 12 cerrada documentalmente para la demo AWS emulada**, con A–H
registrados y las reservas AC11/AC16/AC18 explícitas. ECS no se utiliza en
el deployment final y AWS real no fue utilizado. Solo se modifica documentación.

Resumen para portfolio: se implementó un pipeline de Continuous Deployment
sobre un entorno AWS emulado con MiniStack, utilizando ECR para almacenar
imágenes versionadas por commit SHA y digest, GitHub Actions como orquestador
y Docker Compose como compute local seguro. Se validaron deployment,
redeployment idempotente, nueva versión, rollback, persistencia de metadata
y volúmenes y restart, con los límites de evidencia indicados en este cierre.
