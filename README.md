# mcp-software-factory

Demo educativa y funcional de una Software Factory mínima en Python usando la arquitectura MCP Host-Client-Server y OpenAI Responses API.

LangGraph está disponible como runtime durable de Fase 2; la orquestación manual continúa activa por defecto.

## Arquitectura

```text
Usuario
  |
  v
host.py
  |  selecciona runtime manual o LangGraph
  |  descubre tools, resources y prompts
  v
clients/MCPClientManager
  |
  +--> MCPClient STDIO --> servers/software_factory_server.py
  +--> MCPClient STDIO --> servers/filesystem_server.py
  +--> MCPClient STDIO --> servers/testing_server.py
  +--> MCPClient STDIO --> servers/knowledge_server.py
  |
  v
OpenAI Responses API decide function calls
```

## Conceptos MCP

Host: la aplicación de consola que conversa con el usuario, llama a OpenAI y orquesta tool calling.

Client: adaptador STDIO que abre un MCP Server, lista tools/resources/prompts y ejecuta tools.

Server: proceso MCP independiente que publica capacidades. Esta demo tiene cuatro servidores.

## Servidores

`software_factory_server.py`: planificación determinista. Publica `analyze_requirement`, `create_tasks`, el resource `standards://backend` y el prompt `plan_project`.

`filesystem_server.py`: operaciones de archivos restringidas a `workspace/`. Permite listar, leer, crear directorios, escribir archivos, crear estructuras de proyecto y actualizar archivos existentes.

`testing_server.py`: ejecuta pruebas con comandos cerrados para `pytest`, `npm` o `dotnet`. No acepta comandos arbitrarios.

`knowledge_server.py`: frontera exclusiva de RAG y aprendizaje durable. Publica
tools de lectura con aislamiento por proyecto y `submit_learning` como unica
entrada de escritura. Los agentes no reciben acceso al proveedor de embeddings,
al chunker ni al indice vectorial.

## Knowledge MCP

El read path expone `search_knowledge`, `get_relevant_context`,
`get_project_decisions`, `get_similar_implementations` y
`get_knowledge_source`. Todas las respuestas incluyen provenance y el filtro
`project_id` no puede desactivarse.

El write path expone `submit_learning` y `get_learning_status`. Una entrega pasa
por sanitizacion, validacion, clasificacion, deduplicacion, politica, persistencia
canonica, chunking, embeddings e indexacion. Los tipos de bajo riesgo configurados
en `KNOWLEDGE_AUTO_VALIDATE_TYPES` pueden validarse automaticamente; los demas
permanecen como `candidate` hasta una decision administrativa.
Las lecturas usan `KNOWLEDGE_READ_FAIL_MODE=open` por defecto: una falla de
infraestructura devuelve contexto vacio y marcado como degradado. Las escrituras
siempre son fail-closed y nunca informan indexacion si el pipeline no termino.

`knowledge_records` es la fuente de verdad. `knowledge_chunks`,
`knowledge_embeddings` y `knowledge_vector_index` son representaciones
reconstruibles. Solo registros `indexed` participan en retrieval. No existen
tools MCP para insertar embeddings, escribir chunks, buscar vectores crudos o
eliminar entradas del indice.

Cada busqueda crea un `knowledge_retrievals` independiente y persiste query
sanitizada, filtros, top-k, latencia y correlacion opcional con workflow,
observabilidad y FinOps. `knowledge_retrieval_results` conserva el ranking,
score y provenance canonica de cada chunk en la misma transaccion. Los misses
tambien se registran con cero resultados. Retrievals historicos sin IDs
durables permanecen `summary_only`; nunca se reconstruyen relaciones por
suposicion. El detalle se consulta en `GET /api/knowledge/retrievals/{id}` y en
`/knowledge/retrievals/{id}` sin exponer embeddings ni vectores.

Antes de generar un proyecto, `ImplementationSubgraph` consulta exclusivamente
`knowledge__get_relevant_context` con el `project_id` actual. El contexto del
Developer se limita con `DEVELOPER_KNOWLEDGE_TOP_K` (5) y
`DEVELOPER_KNOWLEDGE_MAX_TOKENS` (4000), conserva provenance y se trata como
contenido no confiable. Un error o un miss no bloquea la implementacion.

Despues del analisis de requerimientos y antes de crear tareas,
`PlanningSubgraph` consulta la misma tool MCP con el proyecto actual. El
contexto del Planner se limita con `PLANNER_KNOWLEDGE_TOP_K` (5) y
`PLANNER_KNOWLEDGE_MAX_TOKENS` (3000). Los retries reutilizan una consulta
identica; una query modificada puede generar un retrieval nuevo. La API de
workflow publica solo estado, IDs, conteos y provenance, no el contexto RAG.

Antes de preparar una ejecucion de tests, `TestingRepairSubgraph` consulta
`knowledge__get_relevant_context` como agente QA. El contexto se limita con
`QA_KNOWLEDGE_TOP_K` (5) y `QA_KNOWLEDGE_MAX_TOKENS` (3000). Un rerun posterior
a una reparacion reutiliza el retrieval si la evidencia no cambio y crea uno
nuevo cuando cambian archivos o el resumen de fallo.

Cuando los tests fallan y Repair participa, el subgrafo consulta Knowledge MCP
despues de leer los archivos implicados y antes de preparar el fix. Prioriza
incidentes y soluciones del proyecto, limita el contexto con
`REPAIR_KNOWLEDGE_TOP_K` (5) y `REPAIR_KNOWLEDGE_MAX_TOKENS` (3000), y reutiliza
el retrieval mientras el contexto de fallo sea identico. Un error o un miss de
Knowledge no bloquea la reparacion existente.

## Instalación con uv

```bash
uv sync --dev
```

También puedes usar un entorno Python 3.12+ equivalente e instalar las dependencias declaradas en `pyproject.toml`.

## Configuración

Copia `.env.example` a `.env` y completa:

```env
OPENAI_API_KEY=
OPENAI_MODEL=
USE_LANGGRAPH=false
MCP_FACTORY_DEBUG=false
LANGGRAPH_CHECKPOINT_DB=
LANGGRAPH_DEVELOPMENT=false
LANGGRAPH_FAIL_AFTER_NODE=
```

Si `OPENAI_MODEL` queda vacío, el Host usa `gpt-4.1-mini`. Cambia `USE_LANGGRAPH=true` para activar el grafo; con `false` se conserva el workflow manual. `MCP_FACTORY_DEBUG=true` agrega el state final serializado y truncado después del resumen humano.

Nunca escribas una API key real en el repositorio.

El modelo base de produccion, limites de exposicion y requisitos de persistencia
se documentan en [Phase 9.1 - Deployment Model](docs/phase-9-deployment-model.md).

## Ejecutar el Host

```bash
uv run python host.py
```

En Windows, si la consola muestra caracteres dañados, fuerza UTF-8 antes de ejecutar:

```powershell
$env:PYTHONUTF8 = "1"
python host.py
```

Comandos para salir:

```text
salir
exit
quit
```

## Prompts MCP

Listar prompts disponibles:

```text
/prompts
```

Usar el prompt de planificación:

```text
/plan-project fastapi | Crear un sistema de reservas médicas
```

## Ejemplo de solicitud

```text
Crea un proyecto FastAPI mínimo para gestionar tareas, con endpoint de creación, listado y pruebas pytest.
```

El modelo debería analizar el requerimiento, crear tareas, consultar el workspace, pedir aprobación antes de escribir archivos, detectar pytest, ejecutar pruebas y corregir archivos si fallan.

Para proyectos FastAPI minimos, el Host aplica una politica central e inmutable
despues de la generacion y antes de validar o pedir aprobacion:

```text
fastapi[standard]==0.139.0
pytest>=8,<9
```

El Developer no decide las versiones controladas. El nodo
`normalize_dependencies` reemplaza variantes de FastAPI y pytest, agrega lineas
obligatorias, elimina duplicados y rechaza paquetes no permitidos. `httpx` y
`uvicorn` son opcionales: solo se conservan cuando aparecen en la propuesta y se
canonicalizan respectivamente a `httpx>=0.23,<1` y `uvicorn>=0.17,<1`. La
aprobacion muestra las dependencias normalizadas que recibira la tool de
creacion.

El archivo `requirements.txt` no instala dependencias automaticamente hasta que se ejecuta `testing__prepare_test_environment`.

Cada proyecto generado debe usar su propio entorno virtual en `.venv/`. El Host conserva su propio entorno y los proyectos generados no deben reutilizar dependencias globales del Host. La tool `testing__prepare_test_environment` valida `requirements.txt`, crea o reutiliza `.venv`, actualiza `pip`, instala dependencias, ejecuta `pip check` e inspecciona versiones; requiere aprobacion humana porque puede descargar paquetes de internet y puede tardar.

Firma publica de la tool:

```python
prepare_test_environment(project_name: str, dependency_file: str = "requirements.txt")
```

Los timeouts son politica interna del Testing MCP Server y no se exponen al modelo.

Timeouts predeterminados de preparacion:

```text
validate_dependencies: inmediato
create_venv: 60 segundos
upgrade_pip: 120 segundos
install_dependencies: 300 segundos
pip_check: 60 segundos
inspect_versions: 30 segundos
```

Los tests se ejecutan con el Python del proyecto, por ejemplo:

```text
workspace/medical-booking/.venv/Scripts/python.exe -m pytest tests -q --disable-warnings
```

Los proyectos generados deben incluir `.venv/` en su `.gitignore`.

Para que pytest sea determinista en esta demo, el Testing MCP Server ejecuta:

```text
.\.venv\Scripts\python.exe -m pytest tests -q --disable-warnings
```

El proceso hijo recibe `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` para evitar que plugins globales instalados en la maquina alteren o bloqueen la ejecucion de proyectos generados. Tambien fija `PYTHONPATH` a la raiz del proyecto.

Validacion manual equivalente en PowerShell:

```powershell
cd .\workspace\medical-booking
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\python.exe -m pytest tests -q --disable-warnings
```

`python -m pytest` usa el interprete activo o global y no demuestra que el entorno aislado del proyecto funcione.

## Flujo completo validado

Secuencia real estabilizada antes de migrar la orquestacion:

```text
software_factory__analyze_requirement
software_factory__create_tasks
filesystem__list_files
filesystem__create_project_structure
testing__detect_test_framework
testing__prepare_test_environment
testing__run_tests
finalize
```

Resultado validado para el ejemplo `medical-booking`:

```text
Proyecto FastAPI generado
Entorno virtual creado
Dependencias instaladas
1 prueba aprobada
```

El Host y los proyectos generados usan entornos distintos. El Host ejecuta la orquestacion y los MCP clients desde su propio Python, mientras cada proyecto ejecuta sus tests con su propio Python dentro de `.venv/`, por ejemplo:

```text
workspace/medical-booking/.venv/Scripts/python.exe -m pytest tests -q --disable-warnings
```

Las operaciones sensibles, como crear archivos, preparar entornos e iniciar pruebas, requieren aprobacion humana. La orquestacion manual y deterministica se conserva como runtime predeterminado; LangGraph puede activarse mediante configuración sin cambiar los contratos MCP.

Para revisar un proyecto existente, el Host detecta la intencion de revision, inspecciona el workspace, registra el proyecto si aparece en `list_files`, prepara el entorno, ejecuta pruebas y, si pytest falla de forma funcional, entra en una reparacion acotada: lee el test fallido, lee la fuente relacionada, aplica una actualizacion puntual y vuelve a ejecutar pruebas. El requerimiento original tiene prioridad sobre un test roto.

`filesystem__list_files` excluye por defecto carpetas generadas o ruidosas:

```text
.venv
__pycache__
.pytest_cache
.git
node_modules
bin
obj
```

Solo se incluyen si la llamada usa `include_ignored=true`.

## Migración a LangGraph — Fase 1

LangGraph modela ahora el estado, los nodos y las transiciones del workflow. El flujo manual sigue disponible como fallback y se usa por defecto. Para activar el nuevo runtime:

```env
USE_LANGGRAPH=true
```

`SoftwareFactoryState` es un `TypedDict` serializable. Contiene la intención, el proyecto, el entorno, los resultados de pytest y la evidencia de reparación; no contiene clientes OpenAI/MCP ni otros objetos de runtime. `GraphDependencies` entrega esas dependencias a los nodos sin variables globales mutables.

```text
START
  |
detect_intent
  |-- create --> analyze_requirement --> create_tasks --|
  |-- review ------------------------------------------>|
                                                        v
                                              inspect_workspace
                                                |             |
                                      absent/create        existing
                                                |             |
                                         create_project       |
                                                |-------------|
                                                        v
                                          detect_test_framework
                                                        |
                                             prepare_environment
                                                |             |
                                             failure        success
                                                |             |
                                                |         run_tests
                                                |       /    |      \
                                                |   passed repair  infra/limit
                                                |           |
                                                |   read_failing_test
                                                |           |
                                                |   read_related_source
                                                |           |
                                                |       apply_fix
                                                |           |
                                                |-----------| (run_tests)
                                                v
                                             finalize --> END
```

Los edges condicionales reemplazan gradualmente las decisiones de `determine_next_action`. Los MCP Servers, sus contratos, el Tool Registry, la Responses API, las políticas de seguridad y las aprobaciones `[s/n]` permanecen iguales. El adaptador `HostToolExecutor` hace que los nodos reutilicen validación JSON Schema, aprobación, normalización MCP, logging y clasificación de fallos sin acceder a `ClientSession`.

La Fase 1 también puede compilarse sin checkpointer en pruebas unitarias. La configuración durable y sus limitaciones se describen a continuación.

## LangGraph Fase 2 — Persistence and Checkpoints

Cuando `USE_LANGGRAPH=true`, el Host abre un único `AsyncSqliteSaver` para toda la sesión de consola y compila el grafo con ese checkpointer. Cada ejecución recibe un `thread_id` UUID independiente; el estado se guarda entre nodos y puede consultarse después de cerrar y volver a abrir el Host.

La base predeterminada es:

```text
data/langgraph-checkpoints.sqlite
```

Puede sobrescribirse con una ruta absoluta o relativa al repositorio:

```env
LANGGRAPH_CHECKPOINT_DB=
```

Conceptos:

- Un checkpoint es una versión durable del state entre pasos del grafo.
- Un thread agrupa todos los checkpoints de una ejecución mediante su UUID.
- El checkpointer persiste y recupera esos checkpoints desde SQLite local.
- Reanudar usa el mismo `thread_id` y `ainvoke(None, config=...)`; no reconstruye el state inicial.

```text
Host session
  |
  +-- open AsyncSqliteSaver ---------------------------+
  |                                                    |
  +-- new workflow --> UUID thread                     |
  |       |                                            |
  |       +--> node --> checkpoint --> node --> END    |
  |                             |                      |
  |                             +-- failure            |
  |                                                    |
  +-- /workflow UUID ---------> latest snapshot        |
  +-- /workflow-history UUID -> recent checkpoints     |
  +-- /workflow-resume UUID --> pending node --> END   |
  |                                                    |
  +-- close saver -------------------------------------+
```

Comandos:

```text
/workflow <thread_id>
/workflow-history <thread_id>
/workflow-resume <thread_id>
```

El historial muestra hasta 20 checkpoints recientes y resume writes sin incluir `stdout` extenso. Un workflow ya terminado se devuelve sin ejecutar nuevamente el grafo.

`/workflow <thread_id>` inspecciona el Ãºltimo snapshot sin ejecutar nodos ni
crear checkpoints nuevos. La salida muestra el estado general, el resultado de
Planning, el resultado de Implementation, el resultado de Testing/Repair y la
operaciÃ³n pendiente cuando existe. Para compatibilidad, si un checkpoint antiguo
no tiene resultados agrupados, la presentaciÃ³n usa los campos planos legacy del
mismo dominio.

Para pruebas de recuperación en desarrollo puede inyectarse un fallo después de un nodo permitido:

```env
LANGGRAPH_DEVELOPMENT=true
LANGGRAPH_FAIL_AFTER_NODE=prepare_environment
```

`LANGGRAPH_FAIL_AFTER_NODE` está deshabilitado por defecto y rechaza nombres arbitrarios. Desactívalo antes de ejecutar `/workflow-resume <thread_id>`.

## LangGraph Fase 3 - Durable Human-in-the-loop

En el runtime LangGraph, las operaciones sensibles ya no bloquean el Host con un
`input("¿Aprobar? [s/n]")`. El nodo compartido `approval` llama a `interrupt()` y
el checkpointer SQLite conserva la solicitud, sus argumentos y su vista previa.
El Host vuelve al prompt y puede cerrarse mientras la decisión está pendiente.

La aprobación y el rechazo reanudan el mismo thread con
`Command(resume={"approved": ..., "reason": ...})`. LangGraph vuelve a ejecutar
el nodo desde el principio al reanudarlo, por lo que no se realiza ningún efecto
antes de `interrupt()`: crear archivos, instalar dependencias, ejecutar pruebas y
aplicar reparaciones ocurre exclusivamente en nodos `execute_*` posteriores.

```text
prepare_* -> approval (interrupt) -> execute_*
                       | reject
                       +-----------> finalize -> END
```

Comandos durables:

```text
/workflow <thread_id>
/workflow-pending <thread_id>
/workflow-approve <thread_id> [motivo opcional]
/workflow-reject <thread_id> [motivo opcional]
```

Un mismo thread puede interrumpirse varias veces, por ejemplo antes de crear el
proyecto, preparar el entorno y ejecutar las pruebas. Cada aprobación consume
solo el interrupt pendiente y el siguiente reemplaza al anterior. Un rechazo no
ejecuta la tool, finaliza con `terminal_status=user_cancelled` y conserva el
motivo.

Con `USE_LANGGRAPH=false`, el runtime manual mantiene la aprobación bloqueante
tradicional `[s/n]`. Con `USE_LANGGRAPH=true`, la autorización durable omite solo
ese segundo prompt; se mantienen la validación de schema, guards de estado,
validación de rutas, políticas de seguridad y normalización MCP.

## LangGraph Fase 4 - Time Travel

Cada checkpoint SQLite tiene un `checkpoint_id` que permite inspeccionar el
estado de una frontera concreta del grafo. Replay reanuda desde esa configuración
histórica y vuelve a ejecutar únicamente los nodos posteriores; los nodos previos
no se repiten. Un checkpoint final no tiene sucesores y su replay es un no-op.

La política de replay es deliberadamente conservadora. Se permiten fronteras de
pruebas, diagnóstico, preparación de reparación y finalización. Se bloquean
creación de proyecto, preparación de entorno y ejecución de una corrección ya
aprobada. Un checkpoint situado después de una aprobación y antes de una tool
sensible también se bloquea para impedir que time travel reutilice decisiones
humanas antiguas. Al volver a alcanzar `approval`, `interrupt()` se dispara otra
vez y requiere un nuevo `Command(resume=...)`.

Un fork usa `aupdate_state` sobre un checkpoint histórico y crea un checkpoint
nuevo; no modifica ni elimina el original. Las ramas nuevas se presentan en el
historial con `source=fork`, `lineage=fork` y el checkpoint de origen. El Host
determina internamente `as_node` a partir de la frontera y la evidencia del
checkpoint. No se acepta `as_node` desde la consola. Si el productor es ambiguo,
el fork se rechaza en vez de inventar una ruta.

La política usa allowlists por dominio. Planning e Implementation no admiten
updates. TestingRepair permite:

```text
tests_executed
tests_passed
repair_decision
repair_before
repair_after
repair_phase
repair_attempts
failure_type
failure_stage
failure_message
failing_test_files
```

Los tipos, el tamaño JSON y las rutas de tests se validan. No pueden falsificarse
campos agrupados de Planning o Implementation, `terminal_status`, datos de
aprobación o estado del entorno. Cada rama registra
`fork_origin_checkpoint_id`, `fork_reason` y `fork_updated_fields`. El historial
también reconoce ramas antiguas `fork/update`.

Comandos:

```text
/workflow-checkpoint <thread_id> <checkpoint_id>
/workflow-replay-plan <thread_id> <checkpoint_id>
/workflow-replay <thread_id> <checkpoint_id>
/workflow-fork-plan <thread_id> <checkpoint_id> <updates-json|json-file>
/workflow-fork-preview <thread_id> <checkpoint_id> <json-file>
/workflow-fork <thread_id> <checkpoint_id> <updates-json|json-file>
```

Los updates pueden ser un objeto JSON inline o un archivo JSON dentro del
repositorio. El plan muestra valores actuales y nuevos, campos rechazados, el
`as_node` resuelto y los nodos esperados sin crear checkpoints ni continuar la
ejecución. Un plan inválido informa `allowed=false`,
`reason=fork_field_not_allowed` y `disallowed_fields`.

## LangGraph Fase 5.1 - TestingRepairSubgraph

Un subgrafo es un grafo compilado que se registra como un único nodo dentro de
otro grafo. `TestingRepairSubgraph` encapsula el ciclo reutilizable de ejecución
de pruebas, diagnóstico, reparación y reejecución, mientras el padre conserva la
creación del proyecto, preparación del entorno y respuesta final.

El padre y el subgrafo comparten `SoftwareFactoryState`; no existen copias de los
campos ni transformaciones de entrada/salida. El subgrafo se compila con
`builder.compile()` y hereda el checkpointer SQLite del padre por invocación. Así,
los interrupts internos siguen siendo durables y se reanudan con el mismo
`thread_id`.

```text
Parent graph
  detect_intent
       |
  inspect/create project
       |
  detect framework
       |
  prepare environment
       |
  testing_repair ------------------------------------------+
       |                                                   |
       |  TestingRepairSubgraph                            |
       +-> prepare_test_request -> approval -> execute_tests
                                      |              |
                                      |              +-> END (passed/failure)
                                      |
                    read test <- test failure
                         |
                    read source
                         |
                    prepare_fix -> approval -> execute_fix
                                             |             |
                                             +-------------+
                                                   rerun
       |
  finalize
```

Precondiciones de entrada:

```text
project_name
environment_prepared=true
detected_test_framework
expected_test_command
```

Una precondición ausente produce un fallo controlado y lleva al padre a
`finalize`. El subgrafo nunca construye la respuesta final. Los rechazos internos
terminan el subgrafo y el padre finaliza con el mismo estado de cancelación.

Replay desde la frontera `testing_repair` vuelve a solicitar aprobación de tests.
Los checkpoints internos conservan su namespace; un fork previo a `prepare_fix`
mantiene la auditoría del checkpoint hijo y crea una rama del padre que reentra en
la fase de reparación sin repetir creación ni preparación del entorno.

## LangGraph Fase 5.2 - PlanningSubgraph

`PlanningSubgraph` convierte una solicitud de creación en un contrato estructurado
antes de inspeccionar o modificar el workspace. Produce un análisis del
requerimiento, criterios de aceptación verificables y tareas ordenadas que el nodo
Developer consume al preparar `filesystem__create_project_structure`.

Los modelos Pydantic rechazan campos adicionales, strings vacíos, listas sin
contenido obligatorio y órdenes inválidos. El estado guarda exclusivamente
diccionarios y listas obtenidos con `model_dump()`, por lo que los checkpoints
siguen siendo serializables.

```text
Parent graph
  detect_intent
       |
       +-- review --> inspect_workspace
       |
       +-- create --> planning -------------------------------+
                          |                                   |
                          |  PlanningSubgraph                 |
                          +-> analyze_requirement             |
                                  |                           |
                              create_tasks                    |
                                  |                           |
                              validate_plan                   |
                               /     |     \                  |
                           valid   refine   failed             |
                             |       |        |                |
                             |   refine_plan |                |
                             |       +--------> validate_plan  |
                             +-------------------------------> END
                          |
                    inspect_workspace
                          |
                    Developer / approval
```

La validación determinista comprueba nombre y tipo de proyecto, requisitos,
criterios con IDs únicos, tareas consecutivas, dependencias existentes y
acíclicas, tareas de implementación y pruebas, y conservación de endpoints y JSON
literales. También rechaza autenticación, base de datos o Docker cuando no fueron
solicitados.

Un plan inválido puede refinarse como máximo dos veces. El refinamiento usa
Structured Outputs de Responses API y valida nuevamente con `PlanningOutput`;
refusals, timeouts, respuestas incompletas o schemas inválidos se convierten en
fallos controlados. Al agotar el límite, el workflow termina con
`terminal_status=planning_failed` sin intentar crear archivos.

Tools permitidas para Planner Agent:

```text
software_factory__analyze_requirement
software_factory__create_tasks
```

Tools prohibidas para Planner Agent:

```text
filesystem__*
testing__*
```

El refinamiento forzado de desarrollo requiere ambas variables; fuera de ese
modo la opción no tiene efecto:

```env
LANGGRAPH_DEVELOPMENT=true
PLANNING_FORCE_INVALID_FIRST_ATTEMPT=true
```

## LangGraph Fase 5.3 - ImplementationSubgraph

`ImplementationSubgraph` consume el contrato validado de `PlanningSubgraph` y
lo convierte en la estructura minima de archivos ejecutables. El Developer
Agent razona y devuelve `ProjectImplementationPlan` mediante Structured Outputs;
no ejecuta tools ni escribe en el workspace. La escritura, deteccion del
framework y preparacion del entorno permanecen en nodos deterministas separados.

```text
Parent graph
  detect_intent -> planning (create) -> inspect_workspace
                                      |
                               implementation --------------------+
                                      |                            |
                                      | ImplementationSubgraph     |
  create:  generate -> normalize deps -> validate                 |
                              -> approval -> create files          |
                                      |                            |
  existing: --------------------------+-> detect framework         |
                                           -> approval             |
                                           -> prepare environment  |
                                      +----------------------------+
                                      |
                               testing_repair -> finalize
```

La propuesta se guarda como datos serializables y valida nombre de proyecto,
framework y package, rutas relativas unicas, archivo principal, tests,
dependencias, imports, endpoints y literales JSON. Tambien limita cantidad y
tamanio total de archivos, y rechaza auth, base de datos, Docker o CI/CD no
solicitados. La solicitud original tiene prioridad sobre criterios, analisis y
tareas cuando existe una contradiccion.

Antes de solicitar approval, FastAPI pasa un preflight local puro basado en
AST: sintaxis, imports, package, endpoint, status HTTP, JSON esperado y patron
de cliente. La politica central vive en `policies/testing_patterns.py`. Los
tests sincronos usan `fastapi.testclient.TestClient(app)`; `httpx.Client(app=)`
y `AsyncClient(app=)` se rechazan. Un test async solo se admite cuando el plan
lo requiere y usa `ASGITransport(app=app)`, `transport=`, `await` y marcador
async. La plantilla `build_fastapi_health_test` produce el caso minimo conocido
sin depender del modelo.

El mismo preflight comprueba las reglas de coleccion de pytest: el archivo debe
comenzar con `test_` o terminar en `_test.py`, y debe contener una funcion
`test_*` de modulo o un metodo `test_*` dentro de una clase `Test*`. El preview
de creacion muestra `collectable_test_count` y los nombres descubiertos. Para el
endpoint minimo `/health`, `ImplementationService` reemplaza el test propuesto
por la plantilla determinista `tests/test_health.py` con `def test_health()`.
Si pytest devuelve exit code 5, el adaptador del Host lo clasifica como
`no_tests_collected` y el subgrafo no intenta leer un test fallido inexistente.

Un proyecto nuevo requiere approval durable antes de
`filesystem__create_project_structure`; un proyecto existente omite por completo
la generacion y la creacion. Ambos caminos detectan el framework y requieren un
segundo approval antes de `testing__prepare_test_environment`. El subgrafo se
compila sin checkpointer propio, por lo que hereda SQLite y conserva sus
interrupts al reiniciar el Host.

Una propuesta invalida se refina como maximo dos veces y conserva
`resolved_validation_errors` y `remaining_validation_errors`. Si reaparece el
mismo conjunto de errores, termina inmediatamente con
`implementation_refinement_made_no_progress`; al agotar el limite termina con
`terminal_status=implementation_failed`. Ninguno de estos casos solicita
approval. Para
forzar un primer error controlado solo en desarrollo:

```env
LANGGRAPH_DEVELOPMENT=true
IMPLEMENTATION_FORCE_INVALID_FIRST_ATTEMPT=true
```

## LangGraph Fase 5.4 - estados privados

`PlanningSubgraph`, `ImplementationSubgraph` y `TestingRepairSubgraph` compilan
ahora con `PlanningState`, `ImplementationState` y `TestingRepairState`
independientes. El padre no entrega su state completo: funciones puras en
`graph/subgraph_adapters.py` seleccionan las claves de entrada permitidas,
filtran las salidas desconocidas y producen resultados agrupados:

```text
planning_result       analysis, acceptance_criteria, tasks, valid, attempts
implementation_result package_name, generated_files, project_created,
                      environment_prepared, framework, valid, attempts
testing_result        tests_passed, summary, repair_phase, repair_attempts
```

Los campos planos equivalentes siguen presentes para compatibilidad y se
declaran en `DEPRECATED_FLAT_RESULT_FIELDS`; las integraciones nuevas deben usar
los resultados agrupados. Planning no recibe testing o repair, Implementation
no recibe logs de tests ni estado de repair, y TestingRepair no recibe tareas
del Planner ni archivos generados. Con `MCP_FACTORY_DEBUG=true`, el Host registra
solo las listas de claves que cruzan cada frontera, nunca contenidos de archivos
o logs completos.

Los wrappers propagan el `RunnableConfig` del padre, por lo que SQLite,
interrupts y thread ID siguen siendo los mismos. Replay conserva los resultados
agrupados y fork mantiene allowlists separadas por subgrafo; actualmente solo
los campos controlados de `TestingRepairState` son editables.

## Aprobación humana

Estas tools requieren aprobación:

```text
filesystem__create_directory
filesystem__write_file
filesystem__create_project_structure
filesystem__update_project_files
testing__prepare_test_environment
testing__run_tests
```

Antes de ejecutarlas, el Host muestra servidor, tool y argumentos. Si hay contenido de archivos, solo muestra una vista previa truncada.

Respuestas aceptadas para aprobar:

```text
s
si
sí
y
yes
```

Respuestas para rechazar:

```text
n
no
```

## Aprobacion humana vs aclaracion del modelo

Una aclaracion es una pregunta funcional del modelo cuando falta informacion realmente imprescindible para avanzar y no puede inferirse de forma razonable.

Una aprobacion es una autorizacion para ejecutar una tool con efectos, como crear archivos o correr pruebas. Las aprobaciones las muestra y gestiona el Host, no el modelo.

Escribir `s` solo tiene efecto mientras el Host muestra el prompt `[s/n]`. Fuera de ese momento, `s` es un mensaje normal de usuario.

El Host procesa las function calls de cada respuesta en orden. Puede ejecutar varias tools de solo lectura en la misma ronda, pero ejecuta como maximo una tool sensible y vuelve a consultar al modelo antes de cualquier otra operacion con efectos. Las llamadas sensibles restantes de esa respuesta se posponen sin fabricar resultados para sus `call_id`.

Ejemplo correcto:

```text
Usuario:
Crea un proyecto FastAPI llamado medical-booking con GET /health,
pruebas y validacion.

Host:
ejecuta analisis y planificacion.

Host:
solicita aprobacion justo antes de crear archivos.
```

## Seguridad del workspace

Todas las operaciones de filesystem y testing se restringen a `workspace/`, calculado desde la ubicación real de cada server.

Las rutas se validan con `Path.resolve()` y `relative_to()`. Se rechazan rutas absolutas, rutas vacías y traversal fuera del workspace.

Límites:

```text
Máximo por archivo: 1 MB
Máximo por proyecto: 5 MB
Máximo de archivos por operación: 50
Máximo de ejecuciones de testing__run_tests: 3 por proyecto y solicitud
Timeout máximo de pruebas: 120 segundos
```

No existe una tool para ejecutar comandos arbitrarios.

Para una validacion manual limpia del ejemplo `medical-booking`, comprueba primero si existe `workspace/medical-booking`. Si fue creado solo para la prueba, puedes eliminarlo manualmente desde PowerShell:

```powershell
Remove-Item -Recurse -Force .\workspace\medical-booking
```

No existe una tool MCP de borrado.

## LangGraph Fase 5.5 — Supervisor Agent y Handoffs

El grafo padre usa un `Supervisor Agent` entre Planning, inspección del
workspace, Implementation, TestingRepair y finalización. Cada especialista
vuelve al Supervisor y este publica un handoff explícito hacia uno de los
targets permitidos.

La coordinación es híbrida:

- guards deterministas calculan `allowed_handoffs`;
- decisiones obligatorias, estados terminales y caminos con un único
  especialista compatible no llaman OpenAI;
- una decisión estructurada del modelo solo puede elegir un target permitido;
- timeout, refusal o salida inválida usan un fallback determinista;
- tres handoffs repetidos sin progreso activan
  `terminal_status=supervisor_loop_detected`.

`SupervisorState` recibe únicamente resúmenes de Planning, Implementation y
Testing. No recibe contenidos de archivos, stdout/stderr completos ni
argumentos pendientes de tools. Su salida tampoco puede modificar resultados
de otros subgrafos.

Los interrupts siguen dentro de Implementation y TestingRepair. Mientras una
aprobación está pendiente el subgrafo no devuelve control al Supervisor, por lo
que una reanudación continúa en el mismo especialista. Replay conserva el
historial previo; Fork puede recalcular decisiones futuras, pero sus allowlists
no permiten editar `supervisor_decision` ni `handoff_history`.

Los hooks se cargan una sola vez al construir `SupervisorService` y solo se
activan cuando `LANGGRAPH_DEVELOPMENT=true`:

```env
LANGGRAPH_DEVELOPMENT=true
SUPERVISOR_FORCE_MODEL_DECISION=true
SUPERVISOR_FORCE_TIMEOUT=false
SUPERVISOR_FORCE_INVALID_TARGET=false
SUPERVISOR_FORCE_TARGET=
SUPERVISOR_FORCE_STAGNANT_LOOP=false
```

`SUPERVISOR_FORCE_TIMEOUT=true` valida el fallback por timeout.
`SUPERVISOR_FORCE_INVALID_TARGET=true` inyecta `deployment` para comprobar que
el validator lo rechace. `SUPERVISOR_FORCE_TARGET=planning` registra intentos
repetidos sin permitir routing inseguro y activa el detector de loops al tercer
intento sin progreso. Reinicia el Host después de cambiar estas variables.

Cada handoff distingue `attempted_to`, `selected_to` y `executed_to`; el campo
legacy `to` conserva el destino efectivo. Un fingerprint serializable registra
stage, intentos y resultados relevantes. El detector de loops compara tres
destinos efectivos iguales con fingerprints iguales, no intentos inválidos
repetidos. Los fallbacks que avanzan de Planning a inspección e Implementation
no son loops. `supervisor_invalid_decision_count` y
`consecutive_invalid_decisions` contabilizan decisiones inválidas por separado.
Fork usa un `branch_id` propio para no mezclar repeticiones de la rama original.

`SUPERVISOR_FORCE_STAGNANT_LOOP=true` activa un probe interno y seguro:

```text
supervisor -> supervisor_loop_probe -> supervisor
```

El probe no forma parte de los targets disponibles para el modelo y no ejecuta
subgrafos ni tools. Registra tres destinos efectivos y fingerprints idénticos;
el tercer registro activa el detector normal de loops. Los hooks timeout,
invalid target, forced target y stagnant loop son mutuamente excluyentes. Una
configuración conflictiva falla con `supervisor_development_config_conflict`
antes de iniciar el workflow.

Con `USE_LANGGRAPH=false`, el runtime manual no importa ni utiliza
`SupervisorState`.

## Pruebas del repositorio

```bash
uv run pytest
```

Las pruebas no requieren API key. Usan workspaces temporales y mocks para evitar ejecutar npm o dotnet reales.

## API HTTP y SSE

La API reutiliza el mismo grafo, checkpointer SQLite, emitter y protecciones de
aprobacion durable del Host:

```powershell
.\.venv\Scripts\uvicorn.exe api.app:app --host 127.0.0.1 --port 8000
```

Endpoints:

```text
POST /api/workflows
GET  /api/workflows/{thread_id}
GET  /api/workflows/{thread_id}/events
POST /api/workflows/{thread_id}/approve
POST /api/workflows/{thread_id}/reject
```

El stream SSE acepta `Last-Event-ID` o `after_sequence` y entrega solamente
eventos con una secuencia posterior. Los pings de transporte se envian cada 15
segundos y no crean eventos ni consumen secuencia. `API_CORS_ORIGINS` acepta una
lista de origenes separada por comas y permite por defecto
`http://localhost:5173` y `http://127.0.0.1:5173`.

El historial estructurado se persiste en una base SQLite independiente:

```text
WORKFLOW_EVENT_STORE_PATH=data/workflow-events.sqlite
WORKFLOW_EVENT_RETENTION_DAYS=30
WORKFLOW_EVENT_MAX_PER_THREAD=10000
```

Puede consultarse sin abrir un stream:

```text
GET /api/workflows/{thread_id}/events/history
```

El endpoint acepta `branch_id`, `after_sequence`, `limit` y `event_type`. Cada
rama conserva su propia secuencia. La retencion se implementa mediante
`SQLiteWorkflowEventStore.prune()`, pero no se ejecuta automaticamente.

## UI web en tiempo real

La interfaz React consume el snapshot, el historial durable y el stream SSE de
la API. Tambien permite crear workflows y resolver aprobaciones pendientes.

```powershell
cd frontend
Copy-Item .env.example .env
npm install
npm run dev
```

La UI queda disponible en `http://localhost:5173`. Por defecto se conecta a
`http://127.0.0.1:8000`; puede cambiarse con `VITE_API_BASE_URL`.

Validacion del frontend:

```powershell
npm test
npm run lint
npm run build
```

## Estructura

```text
mcp-software-factory/
├── clients/
│   ├── __init__.py
│   ├── mcp_client.py
│   ├── mcp_manager.py
│   └── tool_registry.py
├── servers/
│   ├── __init__.py
│   ├── software_factory_server.py
│   ├── filesystem_server.py
│   ├── testing_server.py
│   └── knowledge_server.py
├── tests/
│   ├── test_filesystem_security.py
│   ├── test_tool_registry.py
│   └── test_testing_server.py
├── workspace/
│   └── .gitkeep
├── host.py
├── pyproject.toml
├── .env.example
├── .gitignore
└── README.md
```

## Limitaciones actuales

- La limpieza del historial durable de eventos debe invocarse explicitamente;
  todavia no existe un scheduler de retencion.
- No hay LangChain.
- LangGraph está disponible detrás de `USE_LANGGRAPH`; el runtime manual continúa siendo el predeterminado.
- La calidad del proyecto generado depende del modelo configurado.
- El Host es una consola interactiva y no está pensado aún como servicio multiusuario.
## Workflow evaluation versions

Workflow evaluations are materialized by `(thread_id, branch_id, scoring_version)`.
Scoring `1.2` is calculated for current requests and does not overwrite historical
`1.0` or `1.1` rows. A previous materialization therefore remains reproducible
while a new-version evaluation can coexist for the same workflow and branch.

## External notifications

Phase 6.11 adds durable notification channels, routing policies, quiet hours,
escalation policies, delivery attempts, retries, dead letters, and an internal
inbox. Configuration and delivery state share the workflow event SQLite file.
Secrets are referenced only through `env:VARIABLE_NAME`; API responses and
delivery errors never expose resolved values.

Supported channels are `internal`, `log`, `webhook`, `slack_webhook`, and
`teams_webhook`. The `email` type is reserved and remains disabled until a real
transport is configured. External HTTP adapters reject redirects and unsafe
targets, validate DNS results, enforce TLS by default, set an idempotency key,
and classify `408`, `425`, `429`, and `5xx` responses as retryable.

```text
GET    /api/notifications/channels
POST   /api/notifications/channels
POST   /api/notifications/channels/{channel_id}/test
GET    /api/notifications/policies
GET    /api/notifications/quiet-hours
GET    /api/notifications/escalation-policies
GET    /api/notifications/deliveries
POST   /api/notifications/deliveries/{delivery_id}/retry
POST   /api/notifications/deliveries/{delivery_id}/cancel
POST   /api/notifications/deliveries/{delivery_id}/redeliver
GET    /api/notifications/summary
GET    /api/notifications/inbox
```

Worker configuration:

```text
NOTIFICATION_WORKER_ENABLED=true
NOTIFICATION_WORKER_INTERVAL_SECONDS=5
NOTIFICATION_WORKER_BATCH_SIZE=50
NOTIFICATION_PROCESSING_LEASE_SECONDS=60
NOTIFICATION_ALLOW_PRIVATE_TARGETS=false
APPLICATION_URL=http://127.0.0.1:5173
```

Private webhook targets are blocked by default. For local development only,
set `APP_ENV=development` and `NOTIFICATION_ALLOW_PRIVATE_TARGETS=true`, then
run the receiver with `python -m scripts.dev_webhook_receiver`. Never enable
that override in production.

The historical backfill is a dry run unless `--execute` is supplied. External
channels additionally require both `--include-external` and `--allow-external`:

```powershell
.\.venv\Scripts\python.exe -m scripts.backfill_notification_deliveries
```

The notification UI is available at `/notifications/channels`,
`/notifications/policies`, and `/notifications/deliveries`. Alert details show
related deliveries, while the global navigation and dashboard expose unread,
failure, and dead-letter counts.

## Alertas operativas

La API mantiene reglas, alertas, ocurrencias y acciones humanas en el mismo
SQLite durable de eventos. Las reglas se evalúan después de eventos relevantes,
al reconciliar una transición y cada 60 segundos mediante un evaluador con lock.

```text
GET   /api/alerts
GET   /api/alerts/summary
GET   /api/alerts/rules
PATCH /api/alerts/rules/{rule_id}
POST  /api/alerts/{alert_id}/acknowledge
POST  /api/alerts/{alert_id}/resolve
POST  /api/alerts/{alert_id}/reopen
POST  /api/alerts/{alert_id}/mute
POST  /api/alerts/evaluate
```

La UI está disponible en `/alerts`. Su badge cuenta alertas `open` o
`acknowledged` con severidad `critical` o `error`. `WORKFLOW_FAILED` no se
auto-resuelve por un reinicio; las condiciones transitorias, como approvals,
tests y métricas stale, sí se resuelven cuando dejan de cumplirse.

Configuración:

```text
ALERT_EVALUATION_ENABLED=true
ALERT_EVALUATION_INTERVAL_SECONDS=60
ALERT_EVALUATION_BATCH_SIZE=100
ALERT_SKIP_STARTUP_BACKFILL=false
```

Backfill idempotente:

```powershell
.\.venv\Scripts\python.exe -m scripts.backfill_alerts
```

Validación segura de un thread inexistente desde PowerShell:

```powershell
$missingBody = @{
    thread_id = "00000000-0000-0000-0000-000000000000"
    branch_id = "original"
} | ConvertTo-Json

try {
    Invoke-RestMethod `
      -Method Post `
      -Uri "http://127.0.0.1:8000/api/alerts/evaluate" `
      -ContentType "application/json" `
      -Body $missingBody
}
catch {
    [PSCustomObject]@{
        StatusCode = [int]$_.Exception.Response.StatusCode
        Body       = $_.ErrorDetails.Message
    }
}
```

La respuesta esperada es `404` con `Workflow not found`. Un cuerpo que no sea
JSON válido continúa devolviendo `422`.

## Observability

The factory persists end-to-end traces in the workflow SQLite database without
changing MCP server contracts. W3C-compatible trace and span identifiers link
HTTP requests, workflows, LangGraph stages, agents, OpenAI calls, approvals,
MCP tools, tests, repairs, logs, metrics, and artifact metadata. Prompts, HTTP
bodies, credentials, cookies, authorization headers, and file contents are
redacted before persistence.

```text
GET  /api/observability/summary
GET  /api/observability/traces
GET  /api/observability/traces/{trace_id}
GET  /api/observability/workflows/{thread_id}
GET  /api/observability/spans
GET  /api/observability/spans/{span_id}
GET  /api/observability/errors
GET  /api/observability/agents
GET  /api/observability/tools
GET  /api/observability/llm
GET  /api/observability/slow-spans
GET  /api/observability/logs
GET  /api/observability/artifacts
POST /api/observability/retention/run
```

The UI is available at `/observability`. Retention defaults to `dry_run=true`
and never removes active or retained traces. Historical events can be imported
idempotently:

```powershell
.\.venv\Scripts\python.exe scripts\backfill_observability.py --dry-run
.\.venv\Scripts\python.exe scripts\backfill_observability.py
.\.venv\Scripts\python.exe scripts\cleanup_observability_noise.py
.\.venv\Scripts\python.exe scripts\cleanup_observability_noise.py --execute
.\.venv\Scripts\python.exe scripts\reconcile_observability_spans.py
.\.venv\Scripts\python.exe scripts\reconcile_observability_spans.py --execute
.\.venv\Scripts\python.exe scripts\reconcile_observability_spans.py --trace-id <trace_id> --terminal-status completed
.\.venv\Scripts\python.exe scripts\reconcile_observability_spans.py --trace-id <trace_id> --terminal-status completed --execute
.\scripts\validate_observability.ps1 -SkipApi
```

Configuration:

```text
OBSERVABILITY_ENABLED=true
OBSERVABILITY_JSON_LOGS=false
OBSERVABILITY_SLOW_SPAN_MS=2000
OBSERVABILITY_RECONCILE_ON_STARTUP=true
OBSERVABILITY_IGNORED_ROUTES=
OBSERVABILITY_IGNORED_PREFIXES=
OBSERVABILITY_POLLING_SAMPLE_RATE=0
OBSERVABILITY_CAPTURE_IGNORED_ERRORS=true
OBSERVABILITY_CLEANUP_PRESERVE_ERRORS=true
OTEL_EXPORTER_OTLP_ENDPOINT=
OTEL_EXPORTER_OTLP_HEADERS=
OTEL_SERVICE_NAME=mcp-software-factory
```

The API middleware excludes observability, documentation, health, and favicon
requests before creating local traces. Read-only polling for alert,
notification, and dashboard summaries is sampled at the configured rate; the
default `0` stores none of it. Exact routes and prefixes in the environment
extend the built-in policy. Query strings are ignored and FastAPI route
templates are used for parameterized endpoints. A 5xx from an ignored route is
still retained as an error-only API trace when
`OBSERVABILITY_CAPTURE_IGNORED_ERRORS=true`. Write operations remain observed
even when their path is under a read-only ignored prefix.

The cleanup command is a dry-run unless `--execute` is supplied. It only
targets root API traces without a workflow id and preserves failed traces by
default. Run it again after deletion to verify an idempotent zero-candidate
result.

Trace details include `hierarchy_validation` with root, orphan, cycle, depth,
relationship, `active_span_count`, and terminal-active-span diagnostics. The
activity counters are calculated from span rows even while the trace itself is
still marked running. Runtime spans follow the
`workflow -> subgraph -> node -> agent -> LLM/MCP` hierarchy. Terminal events
and the API workflow runner both invoke the same transactional lifecycle close.
A terminal trace therefore has a terminal root, `ended_at`, `duration_ms`, and
no running, waiting, or interrupted spans. Repeated close calls are idempotent
and preserve the first terminal status and timestamps. New executions create
only the root span named `workflow`; the historical `workflow.workflow` child
was redundant and is no longer materialized from live workflow events.

Historical reconciliation is dry-run by default. It closes active spans in
already-terminal traces and also recognizes active traces whose durable event
stream contains a terminal workflow event. It preserves existing ids and does
not alter workflows without terminal durable evidence. Backfilled hierarchy
remains explicitly `precision=partial` and `hierarchy_degraded=true` when
durable evidence is incomplete.

SQLite remains the durable source of truth. Install the optional
`observability` dependency group before configuring an OTLP collector; an
unavailable collector must not block workflow execution.

```text
User request
-> FastAPI span
-> Workflow trace
-> LangGraph spans
-> Agent spans
-> LLM spans
-> Tool/MCP spans
-> Testing spans
-> SQLite telemetry store
-> Observability API
-> React Trace Explorer
-> optional OTLP exporter
```

Canonical categories are `workflow`, `subgraph`, `node`, `agent`, `llm`,
`tool`, `mcp`, `approval`, `test`, `repair`, `retry`, `persistence`,
`notification`, `alert`, `api`, `worker`, `artifact`, and `validation`. Span
statuses are `running`, `completed`, `failed`, `cancelled`, `waiting`,
`interrupted`, `timeout`, and `skipped`.

Troubleshooting: use `/api/observability/summary` to verify local collection,
run the backfill with `--dry-run` before materializing historical events, and
inspect `otlp_configured`/`otlp_available` when an external collector is not
receiving spans. A collector outage does not disable local SQLite telemetry.
## LLM Costs And Budgets

Phase 6.16 reuses `observability_llm_calls` and adds durable, versioned pricing,
cost snapshots, budget reservations, and audit events in the workflow SQLite
database. Missing provider usage remains `NULL`; missing pricing produces an
explicit unavailable calculation. Monetary calculations use Python `Decimal`
and historical snapshots are never changed implicitly.

No real model prices are bundled. Load a verified catalog with:

```powershell
.\.venv\Scripts\python.exe scripts\import_llm_pricing.py `
  --file .\your-verified-pricing.json `
  --dry-run
```

Remove `--dry-run` only after checking source, currency, effective dates, and
overlaps. `config/llm-pricing.example.json` is an intentionally disabled test
fixture, not a source of real pricing.

Run historical processing and reconciliation with:

```powershell
.\.venv\Scripts\python.exe scripts\backfill_llm_costs.py --dry-run
.\.venv\Scripts\python.exe scripts\backfill_llm_costs.py
.\.venv\Scripts\python.exe scripts\reconcile_llm_costs.py
.\.venv\Scripts\python.exe scripts\reconcile_llm_usage.py
# Apply only repairs backed by a correlated completed OpenAI Responses span:
.\.venv\Scripts\python.exe scripts\reconcile_llm_usage.py --execute
.\scripts\validate_llm_costs.ps1
```

Budget enforcement defaults to fail-open for internal cost-service failures.
`hard_limit` blocks before provider invocation only when a reliable maximum
cost can be estimated and reserved. Enforcement is aggregated with the fixed
precedence `hard_limit > soft_limit > warn > observe_only`, and decisions use
`block > warn > allow`; a permissive budget cannot downgrade a block. A
`soft_limit` warns for the current call when that call crosses the limit, then
blocks later calls once prior consumption or reservations have exhausted the
budget. `warn` and `observe_only` never block provider invocation. Token
estimation is deliberately separate from provider-reported usage. Currency
conversion and authoritative provider price discovery are outside this phase
and require explicit inputs.
