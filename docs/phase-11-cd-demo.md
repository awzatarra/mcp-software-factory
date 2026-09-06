# Fase 11 — MiniStack CD Demo Local: cierre E2E

Implementa [la especificación 11.1](../specs/phase-11-cd-demo.md) con
`.github/workflows/software-factory-deploy.yml` y dos scripts Python estándar
en `scripts/deployment/`. No cambia la aplicación ni el Compose existente.
La Fase 11.3 documenta el cierre de la demo con los resultados reales aportados
por el operador. Para repetirla se requiere un runner Windows registrado
manualmente. El alcance de cada criterio se detalla en el
[resultado de implementación](../specs/phase-11-cd-demo.md#resultado-de-implementación).

## Preparación local

1. Mantener el repositorio privado durante la validación. En GitHub, Settings
   / Actions / Runners / New self-hosted runner, seleccionar Windows y seguir
   los comandos de registro que GitHub muestra. Usar una carpeta fuera del
   repositorio. No guardar el token de registro en archivos versionados.
2. Ejecutar el runner interactivamente con `run.cmd`, sin instalar un servicio.
   La misma cuenta debe disponer de Python 3.12+, Git, Docker Desktop con
   contenedores Linux y Compose que soporte `config --no-env-resolution`.
3. Preparar `SF_DEPLOY_HOME`, que el workflow actual establece en
   `C:\software-factory-deploy`, fuera del checkout. Restringir su acceso a la
   cuenta local operadora. No ubicarla bajo `_work`.
4. Guardar la configuración local existente en
   `SF_DEPLOY_HOME/config/.env.production`. No se copia al checkout, imágenes,
   logs ni artefactos. No requiere nuevos secrets de GitHub ni PAT.
5. Verificar que los puertos loopback 8000/5173 estén libres o pertenezcan al
   stack Compose existente. El controlador rechaza listeners ajenos.

El archivo de configuración externo se usa para interpolación de Compose y
como env_file del Backend. Los puertos siguen siendo `127.0.0.1:8000` y
`127.0.0.1:5173`. Los MCP permanecen internos. No cambiar manualmente variables
Docker para apuntar a un daemon remoto.

## Evidencia de validación de la plataforma

No existe actualmente un store apropiado de CI de la plataforma; los stores CI
de proyectos generados no son evidencia del checkout del Software Factory.
Para esta demo se exige una constancia explícita del operador por SHA, tras
ejecutar las validaciones existentes pertinentes fuera de CD. No es una firma
criptográfica ni una verificación remota automática de CI.

Guardar `SF_DEPLOY_HOME/validated/<SHA_COMPLETO>.json` con esta forma (sustituir
los marcadores; el ejemplo no constituye evidencia válida):

```json
{
  "kind": "operator_platform_validation",
  "repository": "awzatarra/mcp-software-factory",
  "commit_sha": "<SHA_COMPLETO_VALIDADO>",
  "status": "passed",
  "validated_at": "<FECHA_UTC_ISO_8601>",
  "validated_by": "<OPERADOR>",
  "evidence_reference": "<REFERENCIA_LOCAL_A_RESULTADOS_REALES>",
  "stores_compatible": true,
  "checks": [{"name": "<COMANDO_O_CHECK_REAL>", "result": "passed"}]
}
```

Los checks describen lo realmente ejecutado, con resultados conservados
localmente. `stores_compatible=true` confirma compatibilidad con los stores
actuales, también para rollback; no autoriza nuevas migraciones. CD valida
estructura, procedencia, fecha y SHA de la constancia, sin ejecutar pytest
completo ni inventar gates de CI. Mantener estos archivos fuera de GitHub.

Antes de cada operación, detener la actividad mutante del Software Factory y
guardar `SF_DEPLOY_HOME/maintenance.json`:

```json
{
  "commit_sha": "<SHA_OBJETIVO>",
  "quiescent": true,
  "expires_at": "<FECHA_UTC_FUTURA_ISO_8601>"
}
```

La ventana debe cubrir el build y el deploy. Se comprueba antes de construir
y justo antes de reemplazar contenedores; la confirmación de ausencia de jobs
activos es responsabilidad del operador, no se agrega un endpoint de runtime.

## Ejecutar y revertir

Desde Actions / Software Factory Deploy / Run workflow, seleccionar `master`:

| Input | Valor |
| --- | --- |
| `environment` | `demo-local` |
| `operation` | `deploy` o `rollback` |
| `commit_sha` | SHA completo de 40 caracteres; para rollback debe coincidir con previous_sha. |

El controlador se obtiene del SHA del dispatch en master y el código a desplegar
se obtiene en otro checkout. Esto permite volver a una versión que todavía no
contuviera los scripts CD. Ambos checkouts deshabilitan persistencia de
credenciales. La fuente debe coincidir con el input, estar limpia (incluidos
archivos ignorados) y ser ancestro de `origin/master` obtenido por checkout.

El controlador genera fuera del checkout una representación temporal del
Compose existente con imágenes etiquetadas por SHA, labels de revisión y la
ruta del env_file externo. La elimina al terminar; no es una segunda arquitectura
Docker. Rechaza un candidato cuyo Compose difiera del revisado por el controlador.

Se construyen imágenes para un SHA nuevo; los SHA previamente saludables
reutilizan sus imágenes locales exactas. Las imágenes quedan retenidas para
current/previous y no se limpian automáticamente. Si faltan o cambiaron las
imágenes de un SHA previamente desplegado, la operación falla explícitamente:
no reconstruye silenciosamente una versión histórica con dependencias distintas.

Rollback usa el mismo pipeline, exige constancia y ventana para previous_sha,
verifica sus imágenes y health y solo entonces intercambia current/previous.
Un rollback sin previous_sha o con input stale se rechaza. No hay auto-rollback.

## Metadata, salud y recuperación

`SF_DEPLOY_HOME/deployment.json` contiene environment, status, current_sha,
previous_sha y el historial de attempts. Cada intento registra environment,
deployed_sha, version, deployed_at, status, current_sha, previous_sha, attempt_id,
fechas e IDs de imágenes. `attempt_id` usa run_id y run_attempt de Actions.

Se escribe con archivo temporal, flush/fsync y reemplazo atómico. Un lock del
sistema operativo y la concurrency de Actions serializan deploy/rollback.
No se cancela una ejecución en curso. Repetir un attempt exitoso no ejecuta
efectos ni duplica historial; un attempt fallido requiere nuevo ID. Tras una
interrupción, el siguiente intento cierra el registro incompleto como failed.

Un éxito inicial A deja current=A/previous=null. Desplegar B deja B/A.
Redesplegar B conserva B/A. Rollback a A deja A/B y registra `rolled_back`;
el estado del environment queda healthy. Los punteros no avanzan en fallos.
Representan la última versión confirmada, no garantizan que un deploy fallido
no haya reemplazado parcialmente contenedores.

Health exige Backend HTTP 200 con `status=ok` y Frontend HTTP 200. Ventana
máxima de 120 s, peticiones de hasta 5 s y pausas de hasta 5 s. Se verifica la
identidad real de las imágenes de ambos contenedores antes y después del health.
Un fallo de build, deploy, health o metadata falla el job; solo se imprimen
etapas, códigos de error y metadata segura. Diagnósticos crudos de subprocess
se suprimen para no filtrar configuración. Investigar fallos localmente.

## Restart y stop

Los volúmenes siguen siendo `mcp-software-factory-workspace` y
`mcp-software-factory-data`. No se montan sockets Docker en contenedores.
Para reiniciar o detener sin necesitar el checkout efímero del runner, obtener
los contenedores por el label `com.docker.compose.project=mcp-software-factory`
y ejecutar `docker restart` o `docker stop` únicamente sobre esos IDs.
Después de restart, comprobar los dos endpoints y datos existentes. Después de
stop, comprobar servicios detenidos y volúmenes presentes. Ninguna operación
rutinaria debe eliminar volúmenes ni ejecutar comandos prune.

Metadata y configuración permanecen en SF_DEPLOY_HOME aunque los contenedores
se detengan. El controlador no elimina imágenes anteriores ni datos.

Ejemplo PowerShell para stop manual del stack:

```powershell
$demoContainerIds = @(docker ps -q --filter 'label=com.docker.compose.project=mcp-software-factory')
if ($demoContainerIds.Count -gt 0) { docker stop @demoContainerIds }
```

Para restart, obtener los IDs con `docker ps -aq` y el mismo filtro de proyecto,
y ejecutar `docker restart @demoContainerIds` si hay IDs. Verificar después
`http://127.0.0.1:8000/health` y `http://127.0.0.1:5173/`. No usar un filtro vacío
ni operar sobre todos los contenedores de la máquina.

## Validación E2E real

Resultados reales ya validados y proporcionados por el operador para el cierre
11.3. No se repitieron deployments ni operaciones Docker durante esta tarea.
No se adjuntan logs completos ni se atribuyen IDs de ejecución no aportados.

- SHA A: `ac05dcf9967e9136732fa4938630de7b843f53fd`.
- SHA B: `151999aa1a22321922bd6bdd80db2ec87a4d8e36`.

| Escenario | Resultado real |
| --- | --- |
| A — First Deploy (primer despliegue) | `current_sha=A`, `previous_sha=null`, `status=healthy`. |
| B — Redeploy Same SHA (mismo SHA) | `current_sha=A`, `previous_sha=null`, `status=healthy`; previous no se convierte en current. |
| C — New Version (nueva versión) | `current_sha=B`, `previous_sha=A`, `status=healthy`. |
| E — Rollback manual | `current_sha=A`, `previous_sha=B`; último intento con `operation=rollback` y `status=rolled_back`. |
| F — Restart (reinicio) | Backend `/health`: `ok`; Frontend: HTTP 200; metadata preservada. |
| G — Stop (detención) | `docker compose down` sin `-v`; volúmenes `mcp-software-factory-data` y `mcp-software-factory-workspace` preservados, al igual que la metadata externa. |

Los resultados acreditan la persistencia de metadata y conservación de volúmenes.
No incluyen una comparación individual del contenido de todos los stores.
Tampoco afirman que el servicio siga encendido después de la prueba de stop.

La Fase 11 queda cerrada para la demostración local de publicación por SHA,
rollback, reinicio y apagado. La matriz de AC distingue evidencia E2E de pruebas
automatizadas previas y deja explícitas las verificaciones pendientes.

Validación local reproducible (sin suite completa de runtime):

```powershell
python -m pytest tests/test_demo_deployment.py -q
python -m pytest tests/test_demo_deployment.py -q -m integration
docker compose -f docker-compose.yml config --no-env-resolution --quiet
git diff --check
```

La prueba marcada integration solo usa el parser real de Compose sobre archivos
temporales sin secretos; no construye imágenes ni inicia contenedores. Las
pruebas automatizadas complementan los resultados E2E anteriores. El último
resultado registrado antes de 11.3 fue `52 passed, 1 deselected`; la prueba
de parsing de Compose real se había validado por separado en 11.2. No se ejecutó
pytest para este cierre documental.

## Limitaciones conocidas

- Target actual: `demo-local`, self-hosted runner Windows y Docker Desktop iniciado.
- Evidencia de plataforma local y manual por SHA; ventana de mantenimiento local y manual.
- Rollback dependiente de imágenes retenidas; no existe registry remoto.
- Sin AWS, auto-deploy ni rollback automático.
- Posible indisponibilidad durante cambios; sin backups automáticos ni migraciones nuevas.

## Checklist para publicación segura del repositorio

Checklist pendiente de ejecución por el operador antes de volver a público:

1. Finalizar los jobs y detener el self-hosted runner.
2. Desregistrar el runner en GitHub; detener `run.cmd` no elimina el registro.
3. Retirar `software-factory-deploy.yml` de `.github/workflows` o moverlo fuera.
4. Opcionalmente conservarlo en `docs/examples/software-factory-deploy.yml`
   como ejemplo inerte, sin modificar el ejemplo read-only de Fase 10.
5. Verificar que el contenido a publicar no incluya `.env`, `.env.production`,
   `workspace/`, datos runtime, SQLite, logs, tokens ni credenciales.
6. Confirmar que `C:\software-factory-deploy` está fuera del repositorio y del checkout.
7. Ejecutar revisión de secretos sobre contenido, historial y assets.
8. Comprobar `git status` limpio después de preparar los cambios de publicación.
9. Revisar `.gitignore`: archivos locales sensibles y datos deben permanecer excluidos.
10. Solo después, cambiar manualmente la visibilidad del repositorio.

Este cierre no ejecuta el checklist, no retira el workflow ni cambia la
visibilidad del repositorio. La revisión documental no sustituye la auditoría
completa previa a publicación.
