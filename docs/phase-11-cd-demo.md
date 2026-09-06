# Fase 11.2 — MiniStack CD Demo Local

Implementa [la especificación 11.1](../specs/phase-11-cd-demo.md) con
`.github/workflows/software-factory-deploy.yml` y dos scripts Python estándar
en `scripts/deployment/`. No cambia la aplicación ni el Compose existente.
La ejecución real requiere que el operador registre su runner Windows.

## Preparación local

1. Mantener el repositorio privado durante la validación. En GitHub, Settings
   / Actions / Runners / New self-hosted runner, seleccionar Windows y seguir
   los comandos de registro que GitHub muestra. Usar una carpeta fuera del
   repositorio. No guardar el token de registro en archivos versionados.
2. Ejecutar el runner interactivamente con `run.cmd`, sin instalar un servicio.
   La misma cuenta debe disponer de Python 3.12+, Git, Docker Desktop con
   contenedores Linux y Compose que soporte `config --no-env-resolution`.
3. Antes de iniciar el runner, definir `SF_DEPLOY_HOME` como ruta absoluta
   persistente fuera del checkout, por ejemplo
   `C:\Users\user\AppData\Local\MCPSoftwareFactory\data\deployment`.
   Restringir su acceso a la cuenta local operadora. No ubicarla bajo `_work`.
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

## Escenarios preparados

| Spec | Validación real pendiente del runner |
| --- | --- |
| A | Publicar y validar SHA A, registrar constancia, deploy; healthy y current=A. |
| B | Nuevo dispatch de A; mismas imágenes y previous sin cambios. |
| C | Publicar/validar B, deploy; current=B, previous=A. |
| E | Ventana para A, rollback con input A; rolled_back, current=A, previous=B. |
| F | Restart de los contenedores del stack; health y datos conservados. |
| G | Stop del stack; puertos dejan de responder y ambos volúmenes permanecen. |

Tests unitarios locales cubren estado, fallos, health, procedencia y secuencia
con Docker simulado. No sustituyen estos escenarios reales. No forzar fallos
destructivos en datos existentes. No publicar el repositorio ni ejecutar CD
automáticamente para completar esta validación.

Validación local reproducible (sin suite completa de runtime):

```powershell
python -m pytest tests/test_demo_deployment.py -q
python -m pytest tests/test_demo_deployment.py -q -m integration
docker compose -f docker-compose.yml config --no-env-resolution --quiet
git diff --check
```

La prueba marcada integration solo usa el parser real de Compose sobre archivos
temporales sin secretos; no construye imágenes ni inicia contenedores. A/B/C/E
están cubiertos por simulación de la secuencia y persistencia real de JSON;
F/G requieren el stack real. Las pruebas de Git y timeout usan procesos reales
aislados sobre fixtures temporales.

## Volver a público

1. Finalizar los jobs, detener el runner y desregistrarlo en GitHub siguiendo
   las instrucciones de retirada. Quitar el registro, no solo detener run.cmd.
2. Deshabilitar Actions/CD o mover el workflow a
   `docs/examples/software-factory-deploy.yml` antes de publicar; conservarlo
   allí como ejemplo inerte. No modificar el ejemplo read-only de Fase 10.
3. Revisar código, historial y assets por secrets; no versionar SF_DEPLOY_HOME,
   `.env`, workspace, data, SQLite ni logs. Confirmar las reglas de ignore.
4. Cambiar visibilidad únicamente por decisión manual posterior. La creación
   del YAML no registra runners, no habilita permisos adicionales ni cambia
   por sí misma la visibilidad del repositorio.

Limitaciones: un solo environment local, ventana manual de mantenimiento,
posible downtime y dependencias de build sin reproducibilidad bit a bit. Las
imágenes saludables retenidas dan identidad exacta al redeploy/rollback; no
hay registry, cloud, backups automáticos ni migraciones nuevas.
