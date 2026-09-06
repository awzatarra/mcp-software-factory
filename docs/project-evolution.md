# Evolución de MCP Software Factory

## Visión general

MCP Software Factory comenzó como un flujo educativo de generación de software
con un Host que coordinaba agentes y MCP Servers. Evolucionó hacia una Software
Factory gobernada capaz de planificar, implementar, probar, reparar, aprender,
observar costes y calidad, crear commits, ejecutar CI y promover código mediante
decisiones humanas y evidencia durable.

```text
Fase 1
  |
  v
Orquestación
  |
  v
MCP
  |
  v
Persistencia
  |
  v
Observabilidad / Evaluación
  |
  v
Git
  |
  v
CI
  |
  v
Despliegue local
  |
  v
GitHub Actions
```

La evolución no consistió en sustituir determinismo por LLMs. En cada etapa se
separaron mejor las decisiones estructuradas, los side effects y la evidencia:
las reglas deterministas conservan autoridad cuando es posible, los modelos
aportan propuestas o evaluación semántica, y las operaciones sensibles quedan
detrás de approvals y checkpoints.

## Fase 1 — Orquestación inicial y SoftwareFactoryGraph

### Objetivo

Convertir el flujo inicial del Host MCP en una orquestación explícita y
serializable, sin perder el runtime manual existente.

### Qué se agregó

- Host, clientes MCP y registro de tools para Software Factory, Filesystem y
  Testing.
- Flujo funcional de análisis, tareas, creación de proyecto, preparación del
  entorno, tests y reparación acotada.
- `SoftwareFactoryGraph`, `SoftwareFactoryState` y selección de runtime mediante
  `USE_LANGGRAPH`.
- Nodos y routing iniciales para intención, planning, workspace,
  implementation, testing y finalización.

### Arquitectura / impacto

El estado dejó de ser contexto implícito del bucle del Host y pasó a un contrato
serializable. Clientes OpenAI/MCP y otros objetos de runtime se mantuvieron fuera
del state mediante dependencias explícitas. El flujo manual siguió disponible
como compatibilidad.

```text
Solicitud del usuario
        |
        v
      host.py
        |
        +--> Runtime manual (compatibilidad)
        |
        `--> SoftwareFactoryGraph
                  |
                  v
          MCP Client Manager
            /      |       \
           v       v        v
      Planning  Filesystem  Testing
```

### Validación

Se validó el recorrido completo de un proyecto FastAPI con entorno aislado,
dependencias instaladas y pytest aprobado, además del flujo de revisión que
corrige un test cuando contradice el requerimiento original.

### Resultado

La fábrica obtuvo un grafo principal inspeccionable y una base estable para
persistencia y reanudación. Documentación histórica limitada en esta etapa: la
evolución temprana está conservada principalmente en código y pruebas.

## Fase 2 — Persistencia y checkpoints

### Objetivo

Hacer durable el workflow para que su progreso no dependiera de un único proceso
en memoria.

### Qué se agregó

- Checkpointer SQLite para estados LangGraph.
- Identidad durable por `thread_id`, checkpoint y rama.
- Recuperación del snapshot vigente después de reinicios.
- Estado terminal explícito y respuesta final desacoplada del state completo.

### Arquitectura / impacto

El grafo pasó de ejecución efímera a máquina de estados durable. Los comandos de
inspección pudieron leer el head actual sin reconstruir el workflow ni serializar
objetos de runtime.

```text
Antes                              Después
-----                              -------
Proceso en memoria                 SoftwareFactoryGraph
        |                                  |
        v                                  v
 Estado temporal                    Checkpoint SQLite
        |                                  |
        X  cierre/reinicio                 v
                                    Estado restaurado
                                           |
                                           v
                                      Continuación
```

### Validación

Las pruebas cubrieron creación, persistencia, restauración, continuidad y
finalización con `terminal_status` coherente.

### Resultado

Los workflows pudieron sobrevivir al proceso que los inició y conservar su
historial de checkpoints.

## Fase 3 — Human-in-the-loop durable

### Objetivo

Introducir control humano real antes de ejecutar operaciones sensibles.

### Qué se agregó

- Interrupts durables para creación de archivos, preparación de entornos y
  ejecución de tests.
- Approvals y rechazos con preview, motivo y evidencia histórica.
- Limpieza del estado `pending_*` después de un rechazo.
- Reanudación desde el checkpoint actual sin repetir nodos previos.

### Arquitectura / impacto

La approval dejó de ser una confirmación de consola y se convirtió en una
transición durable del grafo. El interrupt vigente pasó a ser fuente de verdad,
incluso para operaciones opcionales creadas después de `finalize`.

```text
Nodo prepara operación sensible
              |
              v
      Checkpoint + interrupt
              |
              v
       Approval pendiente
          /         \
         v           v
     aprobar       rechazar
         |           |
         v           v
  ejecutar tool   cancelar flujo
         |           |
         +-----+-----+
               v
       estado durable limpio
```

### Validación

Se probaron múltiples interrupts en un mismo workflow, aprobación sucesiva,
rechazo sin ejecutar la tool, bloqueo de approvals posteriores y recuperación
después de restart.

### Resultado

Los side effects críticos quedaron gobernados por decisiones humanas auditables.

## Fase 4 — Time Travel, Replay y Fork

### Objetivo

Permitir inspeccionar y continuar alternativas sin destruir el historial
original.

### Qué se agregó

- Replay desde checkpoints existentes.
- Fork con ramas alternativas y `lineage` explícito.
- Allowlists de campos editables por dominio.
- Revalidación de approvals en ramas nuevas.
- Historial con checkpoint de origen, campos actualizados y resultado de la
  alternativa.

### Arquitectura / impacto

El historial pasó a ser un árbol de ramas inmutable en lugar de una secuencia
sobrescrita. Planning e Implementation se preservan cuando un fork pertenece al
dominio TestingRepair, y un update inválido no crea checkpoint.

```text
Checkpoint original
        |
        +--------------------+
        |                    |
        v                    v
   rama original        Replay / Fork
        |                    |
        v                    v
    completed          update permitido
                             |
                       nueva approval
                             |
                        rama alternativa

El checkpoint original nunca se modifica.
```

### Validación

Se validaron replay de tests, fork previo a reparación, rechazo sin modificar
archivos, persistencia SQLite, checkpoint original intacto y ausencia de
ejecuciones duplicadas.

### Resultado

La fábrica pudo explorar decisiones alternativas con trazabilidad y aislamiento.

## Fase 5 — Parent Graph, subgrafos y Supervisor

### Objetivo

Separar responsabilidades del grafo principal y formalizar los contratos entre
especialistas.

### Qué se agregó

#### 5.1–5.4 — Subgrafos y estados privados

- `TestingRepairSubgraph` para testing, diagnóstico y reparación.
- `PlanningSubgraph` con salida estructurada y validación propia.
- `ImplementationSubgraph` para propuesta, dependencias, archivos y creación.
- Estados privados y adapters con allowlists explícitas de entrada/salida.
- Protección contra fuga de campos entre subgrafos y parent state.

#### 5.5 — Supervisor y handoffs

- Supervisor con handoffs explícitos entre Planning, workspace,
  Implementation, TestingRepair y finalización.
- Guards deterministas para destinos obligatorios o no ambiguos.
- Decisión estructurada del modelo solo entre targets permitidos.
- Fallbacks para timeout o salida inválida y detección de loops sin progreso.

### Arquitectura / impacto

`SoftwareFactoryGraph` se convirtió en Parent Graph. Cada subgrafo adquirió
estado privado, contratos y ownership propios; el Supervisor coordinó resultados
resumidos sin recibir archivos, stdout completo ni argumentos sensibles.

```text
                    Parent Graph
                         |
                         v
                    Supervisor
             +-----------+-----------+
             |           |           |
             v           v           v
        Planning     Implementation  TestingRepair
        Subgraph       Subgraph        Subgraph
             |           |           |
             v           v           v
          estado       estado       estado
          privado      privado      privado
             |           |           |
             +----- adapters --------+
                         |
                         v
                SoftwareFactoryState
```

### Validación

Las pruebas cubrieron contratos de state, filtrado de outputs, checkpoints entre
nodos, reanudación dentro del especialista, routing, fallbacks y detección de
loops reales sin falsos positivos.

### Resultado

La orquestación quedó modular, gobernable y preparada para crecer sin convertir
el state global en un contenedor de datos sin dueño.

## Fase 6 — Operación, observabilidad, FinOps y Git gobernado

### Objetivo

Transformar el workflow durable en una plataforma operable en tiempo real y
añadir control de calidad, costes y cambios de código.

### Qué se agregó

#### 6.1–6.7 — Streaming, API y UI durable

- Eventos estructurados con secuencia por rama.
- CLI capaz de iniciar y reanudar threads sin reemitir historia.
- API FastAPI con SSE, historial SQLite y recovery.
- UI React en tiempo real para workflows, approvals y timeline.
- Gestión durable, explorador seguro de proyectos y vista de ejecución por
  Planning, Implementation y TestingRepair.

#### 6.8–6.11 — Calidad operativa, dashboard y notificaciones

- Evaluación determinista y explicable de workflows.
- Dashboard global con métricas durables.
- Alertas operativas, incidentes y lifecycle de auto-resolve.
- Canales de notificación, quiet hours, retries, dead letters y políticas de
  escalamiento.

#### 6.15 — Observabilidad de extremo a extremo

- Traces y spans para API, workflow, subgrafos, agentes, LLM, MCP, tests y Git.
- Jerarquía validable, cierre terminal e idempotencia de reconciliación.
- Redacción de secretos y exclusión previa a persistencia para polling y rutas
  de observabilidad.
- Scripts seguros de backfill, cleanup y reconciliación.

#### 6.16 — FinOps y budgets LLM

- Usage normalizado con evidencia de origen del proveedor.
- Pricing versionado y snapshots de coste.
- Budgets, reservas, precedence de enforcement y bloqueo `hard_limit` antes del
  provider.
- Auditoría y alertas FinOps sin confundir disponibilidad de usage con pricing.

#### 6.17 — Evaluación avanzada durable

- Runs, métricas y rubrics versionadas.
- Baselines y detección de regresiones con dirección por métrica.
- Evaluación por agente y UI para métricas, regressions y versiones históricas.
- Vínculo durable entre Evaluation Run y Rubric Version.

#### 6.19 — Git MCP e integración con workflows

- Git MCP privado con allowlist, paths seguros y subprocesses aislados.
- Repositorio y rama por workflow, commits con provenance y approval preview.
- Atribución de archivos, política compartida de dirty paths y manejo de repos
  unborn.
- Promotion controlada con fingerprint, revalidación y merge aprobado.

### Arquitectura / impacto

La plataforma ganó una capa operativa alrededor del grafo: API, UI, eventos,
telemetría y stores especializados. Git se incorporó como side effect gobernado,
no como acceso shell libre. Observabilidad y FinOps pasaron a ser capacidades de
primera clase.

```text
                     UI React
                        |
                 HTTP + SSE durable
                        |
                        v
                 FastAPI / Runner
                        |
                        v
              SoftwareFactoryGraph
                 /      |       \
                v       v        v
          Event Store  Traces   Evaluations
                |       |        |
                v       v        v
             Timeline  Metrics  Dashboard
                        |
                        v
                 FinOps / Budgets
```

### Validación

Se probaron streams reanudables, concurrencia, restart, reconciliación de traces,
budgets antes del provider, seguridad de rutas, commits, repos unborn, working
trees con artefactos ignorados y Promotion sin auto-merge.

### Resultado

La fábrica dejó de ser solo un generador y se convirtió en un sistema operable,
auditable y consciente de coste.

## Fase 6.21 — Pipeline CI y cierre CI/CD local

### Objetivo

Validar el código committed con una pipeline durable y usar su evidencia como
gate previo a Promotion.

### Qué se agregó

#### 6.21.1–6.21.3 — Pipeline, gates y vínculo Git ↔ CI

- Modelo y ejecución local de pipeline.
- Gates deterministas de environment, build, test, lint y package.
- Persistencia de runs, steps, decisiones y policy snapshots.
- Vínculo entre `source_commit`, `ci_validated_commit` y HEAD actual.

#### 6.21.4–6.21.5 — CI Repair, observabilidad y auditoría

- Entrada acotada a Repair solo para fallos reparables.
- Exigencia de un SHA nuevo después de una reparación.
- Métricas operativas, historial y evidencia consultable sin reejecutar CI.
- Reconciliación de reruns manuales con el state LangGraph.

#### 6.21.6 — Hardening y cierre

- Reanudación post-approval y post-rerun sin repetir Planning, Implementation,
  Testing, Git ni CI ya completados.
- Ejecución CI desde el repository root real y clasificación de errores de
  contexto como configuración, no como tests reparables.
- Invariantes de Promotion basados en evidencia exacta del commit.

### Arquitectura / impacto

Git produce la revisión fuente; CI la ejecuta y materializa una decisión durable;
Promotion consume únicamente evidencia vigente del mismo SHA. Un resultado
aceptado para el commit A nunca autoriza el commit B.

```text
Implementation / Repair
          |
          v
  Git commit (SHA A)
          |
          v
    CI sobre SHA A
      /    |     \
     v     v      v
 build   test    lint
      \    |     /
       v   v    v
      decisión durable
          |
          v
 ci_validated_commit == HEAD ?
       /              \
      no              sí
      |                |
   bloquear      Promotion elegible
```

### Validación

La suite de cierre cubrió pipeline, gates, SHA binding, repair, restart,
concurrencia, auditoría y coherencia entre endpoints CI, Git y workflow.

### Resultado

La Fase 6.21 quedó cerrada como capa local de gobernanza CI/CD, manteniendo
Promotion manual.

## Fase 7 — Knowledge, Planner governance y experimentación

### Objetivo

Incorporar memoria cross-workflow y elevar Planning desde una salida estructurada
hasta una capacidad evaluada, calibrada y gobernada.

### Qué se agregó

#### 7.0 — Knowledge MCP, RAG y aprendizaje

- Knowledge MCP como única frontera para retrieval y aprendizaje durable.
- Fuentes, records, chunks, embeddings e índice reconstruible con provenance.
- RAG aislado por proyecto para Planner, Developer, QA y Repair.
- Workflow learning con sanitización, deduplicación, estados de validación y
  política fail-closed para escrituras.

#### 7.1–7.6 — Contrato, riesgo y calidad del Planner

- Validación del contrato de Planning y normalización de tipos soportados.
- Ejecutabilidad, dependencias y orden de tareas deterministas.
- Análisis de riesgo e impacto.
- Approval basada en riesgo.
- Quality score, decision confidence, quality gate y refinement adaptativo.

#### 7.7–7.10 — Calibración y propuestas

- Registros de evaluación y calibración del Planner.
- Analytics y recomendaciones de política.
- Review humano de recomendaciones.
- Propuestas de cambio separadas de la aplicación efectiva.

#### 7.11–7.16 — Aplicación, rollback y experimentos

- Aplicación controlada y rollback de policies.
- Rollout canary y monitoreo de impacto.
- Experimentos multivariante con snapshots por workflow.
- Confianza estadística y calidad de decisión.
- Promotion readiness gobernada.
- Portfolio y detección de conflictos entre rollouts y experimentos.

#### 7.17 — Cierre

- Hardening de fingerprints, idempotencia, concurrencia y restart.
- Confirmación de que recomendación, proposal, rollout o ganador no implican
  aplicación automática.

### Arquitectura / impacto

Planning quedó respaldado por Knowledge y por un ciclo durable de evaluación y
gobernanza. Los stores de recomendaciones, proposals, runtime policies,
rollouts y experiments se mantuvieron separados del state de ejecución y
preservaron snapshots históricos.

```text
                 Knowledge MCP
                /      |       \
               v       v        v
           records   chunks    index
               |
               v
        contexto con provenance
               |
               v
        PlanningSubgraph
               |
               v
     evaluación y calibración
               |
               v
         recomendación
               |
          review humano
               |
               v
 proposal -> aplicación -> rollout -> experimento
               ^                         |
               `-------- rollback <------+
```

### Validación

Se validaron contratos, riesgo, calidad, calibration, approvals, rollback,
canary, experimentos, estadísticas, conflictos e invariantes de no auto-apply.

### Resultado

La Fase 7 cerró la inteligencia gobernada del Planner y la memoria reutilizable
sin introducir optimización automática de políticas.

## Fase 8 — LLM-as-Judge y evaluación de agentes

### Objetivo

Agregar evaluación semántica y análisis de desempeño sin desplazar la autoridad
determinista del workflow.

### Qué se agregó

#### 8.1–8.2 — Judge y evaluación híbrida

- LLM-as-Judge con Structured Output para revisar planes.
- Sanitización y tratamiento de requerimiento/plan como datos no confiables.
- Evaluación híbrida que combina score determinista y Judge cuando está
  disponible.
- Degradación a evaluación determinista si el Judge está deshabilitado o falla.

#### 8.3–8.5 — Desempeño, RCA y recomendaciones

- Métricas de desempeño para Planner, Developer, QA y Repair.
- Failure Attribution y RCA con causas de agente, infraestructura, provider,
  policy y usuario.
- Exclusión de agentes de blame ante fallos externos o de infraestructura.
- Recomendaciones durables para agentes, solo para review y nunca aplicadas.

#### 8.6 — Hardening y cierre

- Separación entre recomendaciones de agentes y proposals de policy del
  Planner.
- Fingerprints estables, idempotencia y protección de datos sensibles.
- Validación conjunta con la autoridad determinista de Planning, MCP, Git,
  Promotion y FinOps.

### Arquitectura / impacto

La evaluación LLM se incorporó como evidencia advisory. No obtuvo acceso a MCP,
filesystem, Git, shell ni ejecución de tools, y no puede cambiar prompts,
modelos, routing o políticas.

```text
Plan estructurado
      |
      +-------------------+
      |                   |
      v                   v
Evaluación            LLM-as-Judge
determinista           (advisory)
      |                   |
      +---------+---------+
                v
       evaluación híbrida
                |
        +-------+-------+
        |               |
        v               v
     métricas           RCA
        |               |
        +-------+-------+
                v
       recomendación para review

Autoridad de ejecución: reglas deterministas.
```

### Validación

Las pruebas de cierre cubrieron Judge, evaluación híbrida, desempeño, RCA,
recomendaciones y rechazo de cruces inválidos entre dominios de gobernanza.

### Resultado

La Fase 8 quedó cerrada con evaluación avanzada y recomendaciones explicables,
sin automatizar cambios de agentes.

## Fase 9 — Production Readiness y despliegue local

### Objetivo

Definir y validar un modelo de ejecución reproducible que preservara las
fronteras de seguridad y los stores durables.

### Qué se agregó

#### 9.1 — Modelo de despliegue

- Separación explícita entre Frontend público, Backend gobernado y MCPs privados.
- Contrato para workspace, data, configuración y secretos persistentes.

#### 9.2 — Docker Compose

- Imágenes separadas para Frontend y Backend.
- Red Compose privada y puertos host limitados a loopback.
- Backend non-root y MCP Servers privados por `stdio`.
- Volúmenes nombrados para `/app/workspace` y `/app/data`.

#### 9.3 — Validación y cierre

- Build, healthchecks, conectividad de los cinco MCPs y workflow E2E sin coste.
- Persistencia después de restart y `down`/`up`.
- Verificación de que imágenes y bundle no contienen secretos.

### Arquitectura / impacto

El mismo runtime local se empaquetó sin exponer los MCPs ni convertir SQLite o
workspace en interfaces públicas. Frontend y Backend adquirieron ciclos de vida
separados y los stores sobrevivieron al contenedor.

```text
Host local
  |
  +--> 127.0.0.1:5173
  |          |
  |          v
  |     Frontend / Nginx
  |          |
  +--> 127.0.0.1:8000
             |
             v
       Backend non-root
         /           \
        v             v
 MCPs privados     LangGraph
 por stdio            |
                  +---+---+
                  |       |
                  v       v
              workspace  data
               volume   volume
```

### Validación

Se validaron build sin caché, salud en `127.0.0.1`, acceso al workspace,
checkpoints/eventos después de restart, ejecución non-root y mounts mínimos.

### Resultado

La Fase 9 quedó cerrada con un despliegue local reproducible y durable mediante
Docker Compose.

## Fase 10 — Validación histórica con GitHub Actions

### Objetivo

Conectar GitHub Actions con la Software Factory local sin publicar el Backend ni
añadir operaciones remotas de escritura.

### Qué se agregó

#### 10.1 — Workflow y runner local

- Workflow manual `workflow_dispatch` con `contents: read`.
- self-hosted runner Windows instalado fuera del repositorio.
- Verificación de `GET /health` contra el Backend loopback.

#### 10.2 — Inspección read-only

- Input opcional `thread_id`.
- Consulta de un workflow existente y salida limitada a seis campos seguros.
- Fallo explícito para un thread inexistente.

#### 10.3 — Validación E2E y cierre

- Ejecución health-only exitosa.
- Consulta exitosa de un workflow durable real.
- Fallo controlado con `Software Factory workflow not found.`.
- Confirmación de ausencia de secretos, writes y exposición pública.

### Arquitectura / impacto

GitHub despacha el job al runner local; el runner consulta al Backend en
`127.0.0.1:8000`. GitHub Actions no controla Docker, no ejecuta approvals y no
realiza Git, CI interno, Promotion ni llamadas LLM.

```text
GitHub
  |
  v
GitHub Actions
  |
  | workflow_dispatch
  v
self-hosted runner Windows
  |
  | GET /health
  | GET /api/workflows/{thread_id}
  v
Backend local 127.0.0.1:8000
  |
  `--> inspección read-only

Sin túnel, secretos, writes ni exposición pública.
```

### Validación

Los tres escenarios reales terminaron con los resultados esperados. El runner y
el Backend temporal se detuvieron después de validar.

### Resultado

La Fase 10 quedó cerrada con una integración GitHub Actions read-only, local y
sin capacidades runtime adicionales. Para publicar el repositorio como
portafolio, el runner fue desregistrado y el workflow activo se convirtió en
un [ejemplo documental inerte](examples/software-factory-local.yml).

## Fase 11 — MiniStack CD Demo Local

La publicación de la plataforma se definió mediante Spec-Driven Development
en 11.1, se implementó en 11.2 y se documentó su cierre E2E en 11.3.
GitHub Actions ejecuta un deployment manual por SHA mediante un self-hosted
runner Windows y el Docker Compose existente.

```text
Spec -> deployment por SHA -> health -> metadata current/previous
                                     -> rollback manual
                                     -> restart / stop con persistencia
```

Se validaron primer deploy, redeploy del mismo SHA, nueva versión, rollback
manual, restart y stop. La metadata y los volúmenes persistieron. El target es
`demo-local`, sin AWS ni auto-deploy. La evidencia de plataforma y la ventana
de mantenimiento siguen siendo locales y manuales; rollback requiere imágenes
retenidas. El CD de la plataforma permanece separado del CI y Promotion de los
proyectos generados.

El [cierre E2E y checklist de publicación](phase-11-cd-demo.md) y la
[matriz de aceptación](../specs/phase-11-cd-demo.md#resultado-de-implementación)
delimitan la evidencia y las verificaciones pendientes. El cierre documental
no cambia la visibilidad del repositorio ni retira el workflow CD.

# Arquitectura actual

```text
Usuario / Frontend -----------+
HTTP + SSE                    |
                              |
GitHub Actions (histórico)    |
  | ejemplo inactivo          |
  v                           |
self-hosted runner -----------+
Windows / GET read-only       |
                              v
                       FastAPI Backend
                       127.0.0.1:8000
                              |
                              v
                          LangGraph
                    SoftwareFactoryGraph
                              |
                              +--> Stores durables
                              |    - workflows y checkpoints
                              |    - eventos y approvals
                              |    - observabilidad y FinOps
                              |    - evaluation y metrics
                              |    - policies y recommendations
                              |    - Knowledge y workflow learning
                              |
                              v
                         Supervisor
                              |
                              +--> PlanningSubgraph
                              +--> ImplementationSubgraph
                              `--> TestingRepairSubgraph
                              |
                              v
                    MCP Client / Manager
                              |
                              v
                    MCP Servers privados
                              |
                              +--> Software Factory
                              +--> Filesystem
                              +--> Testing
                              +--> Knowledge
                              `--> Git
                              |
                              v
                             Git
                              |
                              v
                             CI
                              |
                              v
                          Promotion
                              |
                              v
                          Finalize
```

# Decisiones arquitectónicas importantes

- **Determinismo antes que LLM:** contratos, routing obligatorio, gates,
  seguridad, budgets y elegibilidad se calculan de forma determinista cuando es
  posible.
- **LLM como evidencia o propuesta:** Planning genera estructuras validadas y
  LLM-as-Judge aporta señales advisory; ningún output del modelo obtiene
  autoridad irrestricta.
- **Human-in-the-loop:** los side effects sensibles requieren approval durable.
- **Sin auto-apply:** recomendaciones, proposals y resultados experimentales no
  mutan policies automáticamente.
- **Sin auto-promote:** CI aceptada habilita Promotion, pero no ejecuta el merge
  sin el flujo gobernado correspondiente.
- **Git y CI ligados a SHA exacto:** la evidencia stale nunca autoriza otro
  commit.
- **Side effects gobernados:** filesystem, testing, Git y provider calls pasan
  por allowlists, políticas, límites y auditoría.
- **Recovery durable:** checkpoints, interrupts, eventos y stores permiten
  continuar después de restart sin repetir trabajo completado.
- **Persistencia explícita:** SQLite y volúmenes persistentes son la fuente de
  verdad; los caches o índices derivados se pueden reconstruir.
- **MCP privado:** solo el Backend orquesta MCP Servers por transporte privado.
- **Separación por subgrafos:** Planning, Implementation y TestingRepair tienen
  estados y contratos propios.
- **CI y Promotion explícitos:** validar código y promoverlo son etapas distintas
  con evidencia y approvals separadas.
- **Observabilidad y FinOps de primera clase:** traces, métricas, usage, costes y
  budgets forman parte del diseño, no son añadidos externos posteriores.

# Hitos principales

- Primer flujo completo de análisis, generación, testing y reparación.
- Migración del estado a `SoftwareFactoryGraph`.
- Persistencia durable con checkpoints e interrupts humanos.
- Parent Graph con subgrafos privados y Supervisor.
- Streaming SSE, API y UI operativa en tiempo real.
- Observabilidad completa y FinOps con budgets.
- Knowledge MCP, RAG y workflow learning.
- Planner calibration y governance de policies.
- LLM-as-Judge, evaluación híbrida, desempeño de agentes y RCA.
- Git gobernado con commits y Promotion.
- CI end-to-end ligado al SHA.
- Promotion controlada a la rama objetivo.
- Docker Compose con workspace y data persistentes.
- GitHub Actions validado con self-hosted runner Windows; actualmente inactivo.

# Estado actual

- Fase 6.21 — CI/CD: cerrada.
- Fase 7 — Knowledge y Planner governance: cerrada.
- Fase 8 — Evaluación avanzada: cerrada.
- Fase 9 — Production Readiness y despliegue: cerrada.
- Fase 10 — Integración GitHub Actions: cerrada y archivada como ejemplo.
- Fase 11 — MiniStack CD Demo Local: cierre E2E documentado, con límites y verificaciones pendientes explícitos.

La plataforma actual puede recibir un requerimiento, planificar, implementar,
validar, reparar, usar conocimiento durable, persistir y reanudar estado,
observar ejecuciones, medir costes, gobernar cambios, crear commits, ejecutar CI,
promover código y operar con Docker Compose. La integración read-only con
GitHub Actions permanece documentada como evidencia histórica, sin
automatización activa en el repositorio público.
