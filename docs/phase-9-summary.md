# Resumen de la Fase 9

## 9.1 Modelo de despliegue

Se definieron la frontera pública Frontend/Backend, la frontera MCP privada, el
workspace y stores persistentes, el contrato de entorno, las verificaciones de
salud y los invariantes de seguridad sin cambiar comportamiento runtime.

## 9.2 Contenedores

Se agregaron imágenes reproducibles de Backend y Frontend, una red Compose
privada, puertos host limitados a loopback, verificaciones de salud, ejecución
non-root del Backend y volúmenes nombrados para `/app/workspace` y `/app/data`.
Los MCP Servers por `stdio` permanecen como procesos hijo privados del Backend.

## 9.3 Validación

Se validaron build sin caché, inicio saludable, descubrimiento MCP, un workflow
sin coste por API/LangGraph/Filesystem MCP, checkpoints y eventos durables,
persistencia de volúmenes después de restart y down/up, y controles de seguridad
de contenedores e imágenes.

## Arquitectura

```text
Navegador -> Frontend -> Backend API / LangGraph -> MCP Servers privados por stdio
```

Frontend y Backend comparten una red Compose privada; los MCPs no tienen
listener de red ni puertos publicados.

## Datos persistentes

`mcp-software-factory-workspace` almacena proyectos generados y estado Git.
`mcp-software-factory-data` almacena checkpoints LangGraph, eventos, CI,
auditoría Git, gobernanza, evaluación, observabilidad, Knowledge y datos SQLite
de FinOps.

## Invariantes de seguridad

Backend es non-root, los contenedores no son privilegiados, no se montan el
socket Docker ni el filesystem del host, los MCPs permanecen privados y los
secretos se inyectan en runtime en lugar de copiarse a imágenes o al bundle del
Frontend.

## Limitaciones conocidas

El despliegue es únicamente Compose local. No incorpora proveedor cloud,
TLS/dominio, autoscaling, Kubernetes, CD remoto, migración a Postgres, Redis,
secret manager externo ni stack externo de observabilidad.

## Estado final

La Fase 9 está cerrada. Su modelo de despliegue, containerización local,
persistencia, recovery, salud, conectividad MCP privada y hardening mínimo
fueron implementados y validados sin agregar capacidades runtime adicionales.
