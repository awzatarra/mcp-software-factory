# 🤖 MCP Software Factory

A governed Software Factory that transforms a **software requirement into
validated code ready for Promotion**.

It plans, implements, tests, and repairs projects using **LangGraph + MCP +
OpenAI**, while adding durable controls for approvals, Git, CI, observability,
FinOps, knowledge, and agent evaluation.

In short: this project explores how to build an agentic software factory that is
**useful, auditable, and governable**, not merely capable of generating code.

[Read the complete project evolution](docs/project-evolution.md).

---

## 🚀 What it does

The main flow can already:

- receive a software requirement from the CLI, API, or UI
- analyze it and produce a structured plan
- validate the plan contract, executability, dependencies, risk, and quality
- generate an implementation with governed paths and dependencies
- create an isolated environment for each project
- run tests and repair functional failures within bounded limits
- retrieve durable memory through Knowledge MCP and RAG
- request human approval before sensitive side effects
- create Git branches and commits with provenance
- execute a CI pipeline bound to the exact commit SHA
- evaluate environment, build, test, lint, and package gates
- enable manual Promotion when CI evidence is valid
- persist checkpoints, events, and interrupts for recovery
- stream real-time events through SSE
- observe traces, spans, metrics, errors, and agent activity
- record LLM usage, costs, pricing, reservations, and budgets
- evaluate workflows and agents with hybrid metrics and LLM-as-a-Judge
- run locally with Docker Compose
- demonstrate a previously validated, read-only GitHub Actions integration

---

## ✨ Technical differentiators

- combines **LLMs + deterministic rules** without granting unrestricted
  authority to the model
- uses a **Parent Graph + subgraphs with private state** for Planning,
  Implementation, and TestingRepair
- coordinates specialists through a **Supervisor with explicit handoffs**
- implements **durable human-in-the-loop** with checkpoints and interrupts
- supports **Replay and Fork** without changing the original historical branch
- separates proposal, approval, and execution for every side effect
- keeps MCP Servers private and accessible only through the Backend
- integrates **Knowledge MCP + RAG + workflow learning** with provenance
- treats observability and FinOps as first-class capabilities
- binds Git, CI, and Promotion to the **exact SHA**
- keeps recommendations, experiments, and LLM-as-a-Judge as advisory evidence
- performs neither automatic policy application nor automatic code Promotion

This is not simply an LLM-powered project generator. It is a foundation for
exploring **multi-agent orchestration, governance, traceability, and software
delivery** from end to end.

---

## 🧭 Architecture

```text
Requirement
     |
     v
FastAPI / Host / UI
     |
     v
LangGraph / SoftwareFactoryGraph
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
     +--> Software Factory MCP
     +--> Filesystem MCP
     +--> Testing MCP
     +--> Knowledge MCP
     `--> Git MCP
     |
     v
Git --> CI --> Promotion --> Finalize
```

SQLite and specialized stores persist workflows, checkpoints, events, Git, CI,
observability, FinOps, Knowledge, evaluations, policies, and recommendations.

---

## 🎥 Recorrido en video

Recorrido completo de la interfaz de **MCP Software Factory**, mostrando el flujo
desde workflows y ejecución hasta Git, CI, evaluaciones, observabilidad,
conocimiento y FinOps.

<div align="center">
  <a href="https://www.loom.com/share/51f644eabd494a8082b55ccf0be93399">
    <img
      src="https://cdn.loom.com/sessions/thumbnails/51f644eabd494a8082b55ccf0be93399-da36b846ae24318d-full-play.gif"
      alt="MCP Software Factory"
      width="800"
    >
  </a>
  <p>
    <strong>MCP Software Factory</strong><br>
    <a href="https://www.loom.com/share/51f644eabd494a8082b55ccf0be93399">
      ▶️ Ver recorrido completo de MCP Software Factory
    </a>
  </p>
</div>

⬇️ [Descargar video completo (.webm)](https://github.com/awzatarra/mcp-software-factory/releases/latest/download/mcp-software-factory-demo.webm)

**Incluye:** Workflows · Ejecución · Git · CI · Timeline · Evaluaciones · Knowledge · Observabilidad · FinOps

---

## 🛠️ Technologies

- **Python 3.12+**
- **FastAPI**
- **LangGraph**
- **Model Context Protocol (MCP)**
- **OpenAI Responses API**
- **Pydantic**
- **SQLite**
- **Server-Sent Events (SSE)**
- **React 19 + TypeScript + Vite**
- **Zustand**
- **Git**
- **pytest**
- **Docker + Docker Compose**
- **GitHub Actions** (historical, inactive example)

---

## 📦 How to install it

### 1. Clone the repository

```bash
git clone https://github.com/awzatarra/mcp-software-factory.git
cd mcp-software-factory
```

### 2. Install the Backend

```bash
uv sync --dev
```

You can also use an equivalent Python 3.12+ environment and install the
dependencies declared in `pyproject.toml`.

### 3. Configure the environment

```powershell
Copy-Item .env.example .env
```

Minimum configuration:

```env
OPENAI_API_KEY=
OPENAI_MODEL=
USE_LANGGRAPH=false
MCP_FACTORY_DEBUG=false
LANGGRAPH_CHECKPOINT_DB=
WORKFLOW_EVENT_STORE_PATH=data/workflow-events.sqlite
API_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
```

`.env.example` contains the complete contract for MCP timeouts, Knowledge,
observability, FinOps, evaluation, alerts, notifications, and CI. Never commit
API keys, tokens, credentials, `.env`, or `.env.production`.

### 4. Install the Frontend

```bash
cd frontend
npm install
cd ..
```

---

## ▶️ How to run it

### Interactive Host

```bash
uv run python host.py
```

Enable the durable runtime with:

```env
USE_LANGGRAPH=true
```

### FastAPI API

```powershell
.\.venv\Scripts\uvicorn.exe api.app:app --host 127.0.0.1 --port 8000
```

The API is available at:

```text
http://127.0.0.1:8000
```

### React UI

```bash
cd frontend
npm run dev
```

The UI is available at:

```text
http://127.0.0.1:5173
```

## ✅ How to test it quickly

### 1. Check Backend health

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok"}
```

### 2. Create a workflow

```powershell
$body = @{
    request = 'Create a FastAPI project with GET /health and pytest tests.'
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri 'http://127.0.0.1:8000/api/workflows' `
  -ContentType 'application/json' `
  -Body $body
```

### 3. Open the UI

```text
http://127.0.0.1:5173
```

From the UI, you can follow events, resolve approvals, explore the project,
inspect Git and CI, and review observability, FinOps, and evaluations.

---

## 🐳 Docker Compose

The local deployment separates Frontend and Backend, keeps MCPs private, and
uses named volumes for `workspace/` and `data/`. The Backend runs as a non-root
user.

```powershell
Copy-Item .env.production.example .env.production
docker compose --env-file .env.production config
docker compose --env-file .env.production build
docker compose --env-file .env.production up -d
```

Validation:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-WebRequest http://127.0.0.1:5173/health -UseBasicParsing
```

`docker compose down` preserves the volumes. `docker compose down -v` deletes
them intentionally.

---

## 🔐 Governance and security

- human approvals for writes, environment preparation, tests, and Git
- allowlists for commands, tools, dependencies, and editable fields
- rejection of absolute paths and traversal outside the workspace
- private MCP Servers over `stdio`
- Testing and Git subprocesses isolated from MCP transport
- secrets and sensitive content excluded or redacted from observability
- `hard_limit` budgets enforced before calling the provider
- Git and CI evidence bound to the current commit
- Promotion separated from CI and never automatically approved
- recommendations and experiment results never applied automatically

For public portfolio safety, this repository contains no active GitHub Actions
workflow or registered self-hosted runner. The read-only workflow validated in
Phase 10 is preserved as an inert example in
[`docs/examples/software-factory-local.yml`](docs/examples/software-factory-local.yml).
See the [security policy](SECURITY.md) for responsible disclosure and secret
handling guidance.

---

## 🧪 Tests

Backend:

```bash
uv run pytest
```

Frontend:

```bash
cd frontend
npm test
npm run typecheck
npm run lint
npm run build
```

Tests use mocks and temporary workspaces. They do not require an API key and
must not perform paid calls.

---

## 📌 Current status

Implemented and validated in the project:

- durable SoftwareFactoryGraph with a Parent Graph and Supervisor
- Planning, Implementation, and TestingRepair as private subgraphs
- approvals, checkpoints, Replay, Fork, and recovery
- API, SSE, UI, and secure project explorer
- observability, metrics, alerts, and notifications
- LLM usage and costs, pricing, reservations, and budgets
- Knowledge MCP, RAG, and cross-workflow learning
- Planner calibration, policy governance, rollout, and experiments
- LLM-as-a-Judge, hybrid evaluation, agent performance, and RCA
- governed Git, approved commits, and Promotion
- CI pipeline, gates, SHA binding, CI Repair, and audit
- Docker Compose with persistence and a non-root Backend
- historically validated read-only GitHub Actions integration (inactive)
  
---

## 💡 Project value

MCP Software Factory demonstrates practical experience in:

- multi-agent architecture with LangGraph
- MCP Server design and consumption
- human-in-the-loop systems and durable recovery
- governed software generation, testing, and repair
- RAG and cross-workflow memory
- observability and agent evaluation
- LLMOps and FinOps with budgets
- Git, CI, and Promotion integration
- APIs and real-time streaming
- an operational React frontend
- local containerization and persistence
- secure design and E2E evidence for a read-only GitHub Actions integration

It is not just “AI generating code.” It is a platform designed to turn software
automation into an **observable, measurable, and controlled** process.

---

## 👨‍💻 Author

**Pool Rivera Molina**

- GitHub: [poolrivera](https://github.com/AwZatarra)
- LinkedIn: [Pool Rivera](https://www.linkedin.com/in/pool-rivera-molina/)

## 📄 License

Released under the [MIT License](LICENSE).

---

## ⚡ Quickstart

```powershell
# 1. Install Backend dependencies
uv sync --dev

# 2. Create local configuration
Copy-Item .env.example .env

# 3. Start the API
.\.venv\Scripts\uvicorn.exe api.app:app --host 127.0.0.1 --port 8000

# 4. In another terminal, start the UI
cd frontend
npm install
npm run dev

# 5. Check Backend health
Invoke-RestMethod http://127.0.0.1:8000/health
```
