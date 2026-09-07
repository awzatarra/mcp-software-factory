# Public Repository Design

## Configuration

```text
Repository .env + process environment (higher precedence)
                         |
                         v
           runtime_config.load_settings
                         |
                         v
            Host / API -> domain settings
```

Only the repository's own `.env` is loaded. Secret values are excluded from
settings representation. Runtime settings contain key, model, workspace and
data roots. Existing domain policy modules remain authoritative; no blanket
settings rewrite changes FinOps, Git, deployment or scoring semantics.

API workspace initialization happens after configuration, avoiding the old
import-time default when WORKSPACE_ROOT is supplied only in `.env`.

## Architecture and approvals

```text
User -> FastAPI / UI -> SoftwareFactoryGraph -> Supervisor
                                              |
                +-----------------------------+-------------------+
                |                             |                   |
                v                             v                   v
         PlanningSubgraph          ImplementationSubgraph  TestingRepairSubgraph
         validate / refine         files / dependencies    tests / repair
                |                             |                   |
                +-----------------------------+-------------------+
                                              |
                                              v
                              Durable approval -> MCP Client
                                              |
                   +--------------------------+------------------+
                   |             |            |          |       |
                   v             v            v          v       v
              Filesystem      Testing     Knowledge     Git   Factory
                   |
                   v
              WORKSPACE_ROOT

Delivery: Git -> CI (exact SHA) -> governed Promotion -> Finalize
```

Planning proposes; side effects require existing human approval and bounded
tool contracts. Rejections cannot be bypassed by publication tooling.
Absolute configuration roots are operator-controlled; user-supplied file
paths are checked against the configured root, including resolved traversal.
Running project code still requires a trusted execution environment.

## Public and private artifacts

```text
Tracked: source / tests / docs / specs / dummy examples
Ignored: .env / data / workspace / snapshots / logs / artifacts / databases
External: deployment homes / runner registration / provider credentials
```

Do not delete ignored local data or copy it into validation clones. A clone
candidate may overlay reviewed uncommitted source changes explicitly; report
that distinction rather than claiming validation of a published commit.

## Audit mechanism

`scripts/audit_public_repository.py` reads tracked files and optionally all
objects reachable from local refs, including commit messages. It reports
rule/path/line/object only, never matched secret values, stdout or environment.
It flags real env/artifact paths and credential/personal-path patterns.
Binary assets are inventoried for manual review. Exit 1 means findings need
review, including synthetic fixtures; exit 2 means incomplete audit. No broad
test-directory allowlist hides potential future credentials.

The scanner does not certify provider revocation, scan remote-only refs,
inspect binary pixels or rewrite history. Owner review and an independent
secret scan remain release gates. Findings are not automatically suppressed
merely because tests use dummy values.

## GitHub Actions boundary

Both deployment workflows already specify `contents: read`, manual dispatch,
SHA-pinned checkout, non-persistent credentials and distinct target groups.
Their functional content is preserved. Public repository access does not
justify exposing a self-hosted runner, credentials or the local API.
PUBLIC-57 is Ready under the owner's recruiter-visibility scope. External
Apps and the offline runner are out of scope; no deployment is activated.
No evidence of real credentials was identified in the reviewed repository.
