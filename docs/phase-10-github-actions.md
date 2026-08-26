# Fase 10.1 — Runner local de GitHub Actions

## Arquitectura

GitHub Actions envía un job a un self-hosted runner Windows en la máquina local.
El runner hace checkout del repositorio y accede al Backend existente mediante
`http://127.0.0.1:8000`; el Backend permanece limitado a loopback.

## Prerrequisitos

- Repositorio GitHub que contenga este workflow.
- Docker Compose o el runtime local existente del Backend.
- Cuenta Windows autorizada para ejecutar el runner y acceder a loopback.
- Backend saludable antes de ejecutar el workflow.

## Configuración del runner

En GitHub, abre Repository -> Settings -> Actions -> Runners -> New self-hosted
runner, selecciona Windows y sigue los comandos mostrados. Instálalo fuera del
repositorio, por ejemplo en `C:\actions-runner\`. No guardes la URL ni el token
de registro en source control.

Para esta demo, inicia el runner interactivamente desde su directorio:

```powershell
.\run.cmd
```

No lo instales como Windows Service para esta fase.

## Inicio del Backend

Inicia el Backend existente antes del job. Para el despliegue con contenedores:

```powershell
docker compose --env-file .env.production up -d
Invoke-RestMethod http://127.0.0.1:8000/health
```

El payload esperado es `{"status":"ok"}`. GitHub Actions no inicia ni detiene
el stack.

## Ejecución del workflow

Abre GitHub -> Actions -> Software Factory Local -> Run workflow. El job usa los
labels predeterminados del self-hosted runner y serializa ejecuciones con el
grupo de concurrencia `software-factory-local`.

## Resultado esperado

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

| Escenario | Run | Job | Resultado | Evidencia |
| --- | --- | --- | --- | --- |
| Workflow existente | `32783191193` | `97609496923` | Éxito (`17s`) | Devolvió solo los seis campos seguros para `abb17f25-467e-42a2-a903-ec09f8217feb`. |
| Solo salud | `32993853162` | `98257885090` | Éxito (`23s`) | La salud pasó y la inspección se omitió porque `thread_id` estaba vacío. |
| Workflow inexistente | `32994042081` | `98258527129` | Fallo esperado (`47s`) | La salud pasó y la consulta falló con `Software Factory workflow not found.` |

La consulta exitosa informó `project_name=phase-6-21-e2e-final`,
`intent=create_project`, `status=completed`, `interrupted=false` y
`pending_operation` vacío. El caso inexistente usó
`00000000-0000-0000-0000-000000000000` y terminó con código 1 por diseño.

### Ciclo de vida del runner

El runner registrado se encuentra en `C:\actions-runner` y se ejecuta
interactivamente, no como Windows Service:

```powershell
cd C:\actions-runner
.\run.cmd
```

Espera `Listening for Jobs`, ejecuta los dispatches en serie y detén el runner
con Ctrl+C al terminar. El runner E2E y el Backend temporal se detuvieron tras
la validación; no quedaron procesos de prueba en background.

### Validación de seguridad

El workflow solo se activa manualmente con `workflow_dispatch`, usa
`permissions: contents: read` y apunta a `self-hosted`. Llama exclusivamente
por `GET` a los endpoints loopback de salud y lectura de workflow. No contiene
secretos, credenciales, endpoints de escritura, POST, approvals, mutaciones del
repositorio, túneles ni listeners públicos. La salida se limita a los seis
campos seguros documentados en la Fase 10.2.

### Advertencia conocida

GitHub emitió un warning no bloqueante porque `actions/checkout@v4` apunta a
Node.js 20 y el runner actual lo fuerza a Node.js 24. Checkout y todos los
escenarios E2E funcionaron como se esperaba; no se cambió el runtime para
silenciarlo.

### Resultado del cierre

**PASS — La Fase 10 está cerrada.** La integración local con GitHub Actions fue
validada E2E para un workflow existente, ejecución de solo salud y fallo
controlado de workflow inexistente. Este cierre solo actualiza documentación y
no incorpora capacidades runtime nuevas.
