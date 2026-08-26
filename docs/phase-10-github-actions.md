# Fase 10 — Evidencia histórica de GitHub Actions

> Estado público: integración inactiva. El runner fue desregistrado y el
> workflow se conserva únicamente como ejemplo en
> [`docs/examples/software-factory-local.yml`](examples/software-factory-local.yml).
> Publicar el repositorio no registra ningún workflow de GitHub Actions.

## Arquitectura

GitHub Actions envía un job a un self-hosted runner Windows en la máquina local.
El runner hace checkout del repositorio y accede al Backend existente mediante
`http://127.0.0.1:8000`; el Backend permanece limitado a loopback.

## Prerrequisitos usados durante la validación

- Repositorio GitHub privado que contenía el workflow.
- Docker Compose o el runtime local existente del Backend.
- Runner Windows autorizado para acceder a loopback.
- Backend saludable antes de ejecutar el workflow.

## Configuración del runner

Durante la validación privada se registró manualmente un runner Windows fuera
del repositorio. Su URL y token de registro nunca se guardaron en source
control. El runner ya no forma parte de la configuración pública del proyecto.

## Inicio del Backend durante la validación

Inicia el Backend existente antes del job. Para el despliegue con contenedores:

```powershell
docker compose --env-file .env.production up -d
Invoke-RestMethod http://127.0.0.1:8000/health
```

El payload esperado es `{"status":"ok"}`. GitHub Actions no inicia ni detiene
el stack.

## Ejecución histórica del workflow

La validación original se ejecutó mediante `workflow_dispatch`. El archivo
actual es documentación inerte y no puede ejecutarse desde GitHub Actions sin
que un operador lo instale explícitamente como workflow y registre su propio
runner.

## Resultado validado

`Checkout`, `Runner context` y `Software Factory health` terminan correctamente.
El último paso imprime `Software Factory backend reachable.`

Como comprobación negativa opcional, detén el Backend y ejecuta manualmente. El
paso de salud debe fallar claramente. Reinicia el Backend después; esta prueba
destructiva no se automatiza.

## Seguridad

El workflow usa únicamente `contents: read`, no imprime credenciales, no define
secretos, no realiza writes en GitHub, no abre puertos y no crea túneles. El
registro del runner es manual y su token permanece fuera del repositorio.

## Limitaciones conocidas

El runner es local, interactivo y exclusivo de Windows para esta demo; presupone
que el Backend ya está activo. No hay CI remoto, instalación como servicio,
ciclo automático del stack, webhook público, despliegue cloud ni alta
disponibilidad.

## Siguiente fase 10.2

La Fase 10.2 amplía el runner validado con una consulta read-only al Backend,
preservando approvals, Git, CI, Promotion, MCP y el alcance loopback-only.

## Fase 10.2 — Integración de lectura con el Backend

El dispatch manual acepta un input string opcional `thread_id`. Si está vacío,
el job realiza solo la verificación obligatoria de salud. Si está presente, el
runner ejecuta un `GET` read-only a
`http://127.0.0.1:8000/api/workflows/{thread_id}`.

Una consulta exitosa registra únicamente `thread_id`, `project_name`,
`workflow_intent`, `terminal_status`, `interrupted` y `pending_operation`. Un
workflow inexistente falla con `Software Factory workflow not found.` No se
registran request bodies, argumentos de tools, archivos, prompts, responses,
tokens, approvals, environment dump ni credenciales. La integración no ejecuta
POST, writes en GitHub, Git, CI, Promotion, Repair ni operaciones LLM.

## Fase 10.3 — Validación E2E y cierre

La Fase 10 se cerró con ejecuciones reales de `software-factory-local.yml` en el
self-hosted runner Windows registrado. El Backend permaneció en
`127.0.0.1:8000` y cada ejecución usó el commit
`5cf027f525343d2fed1e14896b96b807511b885e`.

### Resultados E2E

| Escenario | Resultado | Evidencia |
| --- | --- | --- |
| Workflow existente | Éxito (`17s`) | Devolvió únicamente los seis campos seguros documentados. |
| Solo salud | Éxito (`23s`) | La salud pasó y la inspección se omitió porque `thread_id` estaba vacío. |
| Workflow inexistente | Fallo esperado (`47s`) | La salud pasó y la consulta falló con `Software Factory workflow not found.` |

La consulta exitosa informó únicamente los campos seguros documentados. El
caso inexistente usó un identificador sintético y terminó con código 1 por
diseño.

### Ciclo de vida del runner

El runner E2E y el Backend temporal se detuvieron tras la validación. Para
preparar el repositorio público, el runner se desregistró y el workflow activo
se retiró de `.github/workflows`.

### Validación de seguridad

El workflow solo se activa manualmente con `workflow_dispatch`, usa
`permissions: contents: read` y apunta a `self-hosted`. Llama exclusivamente
por `GET` a los endpoints loopback de salud y lectura de workflow. No contiene
secretos, credenciales, endpoints de escritura, POST, approvals, mutaciones del
repositorio, túneles ni listeners públicos. La salida se limita a los seis
campos seguros documentados en la Fase 10.2.

### Resultado del cierre

**PASS — La Fase 10 está cerrada.** La integración local con GitHub Actions fue
validada E2E para un workflow existente, ejecución de solo salud y fallo
controlado de workflow inexistente. Este cierre solo actualiza documentación y
no incorpora capacidades runtime nuevas.
