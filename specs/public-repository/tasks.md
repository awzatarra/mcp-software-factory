# Public Repository Tasks

Review date: 2026-09-07. **PUBLIC-57 is DONE / PUBLIC**, authorized by the owner
for recruiter visibility. See the publication result below.
No history rewrite, credential revocation or runner registration was performed.
Earlier Phase 12 documentation changes in the working tree were preserved.

Status meanings: **Done** = implemented/verified or explicitly confirmed by the owner;
**Reviewed** = existing control or explicitly bounded evidence;
**Pending** = unresolved external review, not silently approved.

| ID | Task | Status and evidence |
| --- | --- | --- |
| PUBLIC-01 | Audit tracked files | Done: 430 tracked files inspected by path and redacted pattern audit. |
| PUBLIC-02 | Audit complete Git history | Reviewed: all 18 commits reachable from local refs; 491 blob/commit/tag objects. Remote-only, deleted or unreachable history not certified. |
| PUBLIC-03 | Remove real credentials | Reviewed: no real credentials identified; URL findings are synthetic negative tests and the startup key is explicitly dummy. No real credential removed. |
| PUBLIC-04 | Credential evidence review | REVIEWED / ACCEPTED: no evidence of real or active credentials in current files or reviewed history. Current independent scan is clean; the known historical finding is synthetic. Owner accepts this evidence-based criterion; precautionary OpenAI/GitHub/AWS rotation is not required without evidence of compromise. No provider-side validity certification is claimed. |
| PUBLIC-05 | Environment-based secrets | Done: shared entry-point settings; no secret defaults or repr exposure. |
| PUBLIC-06 | Example environment | Done: WORKSPACE_ROOT/DATA_ROOT and configuration link added; key/model remain blank. |
| PUBLIC-07 | Central configuration | Done for Host/API bootstrap; domain-specific policy validation intentionally retained. |
| PUBLIC-08 | Ignore rules | Done: env variants, IDE/cache/runtime directories, databases/sidecars and coverage added; example env exceptions preserved. |
| PUBLIC-09 | Logs/artifacts | Reviewed: no tracked runtime artifacts; no local data deleted. |
| PUBLIC-10 | SQLite/database files | Reviewed: no tracked database files. |
| PUBLIC-11 | Generated projects | Reviewed: no generated workspace projects tracked. |
| PUBLIC-12 | Personal paths | Reviewed: current findings are synthetic fixtures; two old doc blobs contain a Windows profile path used as an example. Owner chose to preserve history and review the finding. No rewrite performed. |
| PUBLIC-13 | Configurable roots | Done: existing WORKSPACE_ROOT retained; API initializes its workspace after loading configuration. |
| PUBLIC-14 | Filesystem boundary | Reviewed: resolved workspace checks and security tests; not an OS sandbox. |
| PUBLIC-15 | Shell/environment tools | Reviewed: closed commands, shell=False and per-project execution remain unchanged. |
| PUBLIC-16 | Human approval | Reviewed: approval/rejection and durable recovery tests; no approval bypass added. |
| PUBLIC-17 | MCP defaults | Reviewed: private stdio, bounded transport, workspace-scoped tools. |
| PUBLIC-18 | Private URLs | Reviewed: credential URLs found only in synthetic rejection/redaction tests; public/example/loopback endpoints retained. |
| PUBLIC-19 | Demo/sample data | Done by owner visual review: GIF, Loom and release video expose no secrets, sensitive personal information or private infrastructure. |
| PUBLIC-20 | Private identity/company data | Done by owner approval: keep Pool Rivera Molina, GitHub awzatarra/poolrivera label and LinkedIn link. No email or company is added. |
| PUBLIC-21 | Compose configuration | Reviewed: loopback bindings, private MCP, named volumes, external environment file. |
| PUBLIC-22 | Docker secrets | Reviewed: no real secret values committed; blank deployment examples retained. |
| PUBLIC-23 | Actions permissions | Done: both manual deployment workflows reviewed. |
| PUBLIC-24 | Minimum permissions | Reviewed: contents: read already present; no workflow mutation required. |
| PUBLIC-25 | Checkout credentials | Reviewed: persist-credentials: false already present on every checkout. |
| PUBLIC-26 | Pinned Actions | Reviewed: checkout uses immutable 40-character SHA; regression test added. |
| PUBLIC-27 | Separate deployment targets | Reviewed: distinct demo-local and demo-aws-emulated workflows/concurrency. |
| PUBLIC-28 | Deployment environment | REVIEWED / OUT OF SCOPE: Netlify, Railway App, Vercel and the offline Windows runner are not part of the public demo. External configuration is not a code-visibility gate absent exposed secrets/sensitive data. Reviewed repository Secrets/Variables, Webhooks, Deploy keys and Environments are empty; master is unprotected; no rulesets. Integrations were not changed or certified. |
| PUBLIC-29 | Unit tests | Done: full backend suite and frontend tests; see validation record below. |
| PUBLIC-30 | Integration tests | Done: 8 local integration tests passed; Docker ECR mutation test explicitly not run. |
| PUBLIC-31 | MCP tests | Done: filesystem/testing/Git tests included in full suite; startup connects private MCP servers. |
| PUBLIC-32 | LangGraph tests | Done: workflow, persistence, supervisor and time-travel tests. |
| PUBLIC-33 | Planning | Done: existing subgraph/contract tests; no paid evaluation. |
| PUBLIC-34 | Implementation | Done: existing implementation tests, no paid generation. |
| PUBLIC-35 | TestingRepair | Done: existing bounded repair tests. |
| PUBLIC-36 | Approvals | Done: existing durable approval tests including restart integrations. |
| PUBLIC-37 | Clean startup | Done: fresh Python env; local API health/docs smoke with dummy key and disabled provider endpoint. |
| PUBLIC-38 | Fresh clone | Done: independent local clone, no hardlinks; reviewed candidate changes overlaid during pre-commit validation. Not a claim about a published SHA. |
| PUBLIC-39 | Documented install | Done: python -m venv and pip install -e .[dev] from public PyPI; npm ci from lockfile. |
| PUBLIC-40 | Run from clone | Done: real Uvicorn and MCP startup on temporary loopback port; no real .env/data/workspace copied. |
| PUBLIC-41 | Clone tests | Done: 1938 passed, 9 deselected, 1 warning in new env; later audit-test additions validated separately. |
| PUBLIC-42 | README | Done: prerequisites, pip alternative, explicit key/model setup and truthful Actions status. |
| PUBLIC-43 | Architecture | Done: design document and existing README. |
| PUBLIC-44 | MCP roles | Done: Host/Client/stdio server flow and boundaries documented. |
| PUBLIC-45 | Environment variables | Done: required-variable table, optional groups, example inventory and precedence. |
| PUBLIC-46 | Local execution | Done: root cwd, env-file startup and container-specific configuration documented. |
| PUBLIC-47 | Tests documentation | Done: no-private-env unit tests; explicit local integrations documented below. |
| PUBLIC-48 | Security model | Done: credentials, privacy gates, no OS-sandbox claim and self-hosted runner risk documented. |
| PUBLIC-49 | Workspace isolation | Done: existing path guards retained and covered by tests. |
| PUBLIC-50 | Final secret scan | Reviewed: Gitleaks 8.30.1 working-tree/candidate scan clean. History scan reached all 18 commits and found one synthetic old test fixture (`generic-api-key`); redacted, no real credential identified. Keep as reviewed false positive, not silently ignored. |
| PUBLIC-51 | Hygiene audit | Reviewed: no tracked ignored/runtime files; only reviewed preparation files are eligible for staging. |
| PUBLIC-52 | Git status | Reviewed: intended source/docs/test changes only; ignored local data not staged or copied. |
| PUBLIC-53 | Final refs review | Done per owner gate update: remote/branches/tags/status reviewed; history preservation retained. This does not certify inaccessible deleted refs or provider-side credential validity (PUBLIC-04). |
| PUBLIC-54 | Public dependencies | Done: clean Python install from PyPI and frontend lockfile install; no private dependency required for tests/startup. |
| PUBLIC-55 | New-developer README | Reviewed: fixes private env test and previously inaccurate Actions claim; paid workflows require own key. |
| PUBLIC-56 | Acceptance review | DONE under owner-approved recruiter visibility scope: no real credentials detected, current files scanned, history and media reviewed, identity controlled, tests green and clean clone validated. |
| PUBLIC-57 | Make repository public | DONE / PUBLIC: preparation commit a5b7611 pushed to master; visibility changed to Public; README/GIF/architecture reviewed in an unsigned-in browser; documentation, code and release video returned anonymous HTTP 200. |

## Audit record

```powershell
.\.venv\Scripts\python.exe scripts/audit_public_repository.py --history --include-untracked
git diff --check
git ls-files --cached --ignored --exclude-standard
```

Audit exit 1 is expected while findings require review; it is not a clean bill
of health. No secret values are emitted. Eight current findings are synthetic
fixtures across six test files (Windows redaction paths and credential URL
rejection tests). Eight corresponding historical findings are the same fixtures.
The new startup harness adds one reviewed secret-assignment finding: its
explicit dummy key, with a disabled loopback provider endpoint. The candidate
scan covers 440 files and reports no real credential value; this is not proof
of provider-side validity or a replacement for independent scanning.
Two additional history findings reference `docs/phase-11-cd-demo.md`, line 19,
objects `b331910eea2250ff8c718dd6243a8b5f83563ff0` and
`5d595fb784ba71101273d9af952369210084528a`. Their personal path is not reproduced
here. No current author-specific path was detected in text.

Owner decision: **preserve history and review the finding**. Inspection confirms
the historical line was an example for SF_DEPLOY_HOME, under a Windows user
profile, not a token, password, customer record or credential-bearing URL.
The current documentation uses an external generic deployment home. The old
example remains discoverable by design; no rewrite or force-push is warranted
by the selected disposition alone.

The independent Gitleaks run used version 8.30.1 downloaded from its official
release and a verified archive SHA256. Directory mode over tracked files plus
non-ignored additions returned `no leaks found`. Git mode with process-local
`safe.directory=*` configuration scanned 18 commits and reported one historical
`generic-api-key` at `tests/test_ci_pipeline.py:410` in commit
`474d04f068a61cdb463ca5cffdf80689b355fccd`. It is the old synthetic test
fixture replaced in the current tree; its value is not reproduced. This is a
reviewed false positive, not a clean history certification.

The owner confirmed full visual review of the tracked GIF
`docs/images/mcp-software-factory-walkthrough.gif`, Loom and release video:
no secrets, sensitive personal information or private infrastructure are visible.
PUBLIC-19 is closed on that explicit owner evidence, not an automated binary
scan. Public author attribution remains approved under PUBLIC-20. Synthetic
test values are reviewed fixtures, not a blanket exemption from future scans.

## Final manual gates update

Read-only GitHub review on 2026-09-07 found:

- Repository remains private; Actions is enabled. No settings were changed.
- Repository Actions Secrets and Variables: zero entries from the API.
- Webhooks, Deploy keys and Environments: empty API results.
- Branch API: `master` has `protected=false`.
- Rulesets API returned a private-plan restriction, not an empty-list result.
  The authenticated repository Settings page separately confirmed:
  "You haven't created any rulesets". No visibility change was used to bypass
  that API restriction.
- Repository GitHub Apps Settings lists Netlify, Railway App and Vercel.
  Opening Configure requires GitHub reauthentication. Permissions, repository
  access and deployment behavior have not yet been verified; no installation
  was changed. An installed App is not by itself evidence of a leak, but its
  presence is not evidence that PUBLIC-28 is complete either.
- Existing offline Windows runner is outside the public demo scope.
  It is not required to run or publish the portfolio demo.

The owner subsequently narrowed publication to recruiter access to code,
architecture and documentation, not an active hosted service or deployment.
The observations above are retained as bounded evidence, not pending gates.

PUBLIC-04 now means no evidence of real or active credentials in the repository
or reviewed history. It does not require proving that every historical value
was never active. The synthetic finding is accepted; no precautionary provider
rotation is required absent evidence of compromise.

Netlify, Railway App, Vercel and the offline Windows runner are explicitly out
of scope. Their external configuration was not certified and is not changed.
Existing offline Windows runner is outside the public demo scope.

PUBLIC-04 is Reviewed/Accepted; PUBLIC-28 is Reviewed/Out of Scope.
PUBLIC-19, PUBLIC-20, PUBLIC-53 and PUBLIC-56 are Done; PUBLIC-50 is
Reviewed/Clean with the historical synthetic finding disposition above.
PUBLIC-57 is Ready and the owner authorized commit, push and public visibility.
Ready itself is not a claim that publication has already occurred.

## Publication result

On 2026-09-07, preparation commit `a5b7611` was pushed to `master`, after
the final candidate scan and staged diff review. The owner-authorized
visibility change succeeded for
[awzatarra/mcp-software-factory](https://github.com/awzatarra/mcp-software-factory).

An isolated browser with no GitHub session displayed **Public** and **Sign in**.
The README and architecture rendered, the walkthrough GIF loaded at 800 pixels
wide, and the project-evolution document rendered its phase headings.
Separate HTTP requests without cookies or authorization returned 200 for
the repository, configuration guide, project evolution, `host.py`, and the
release video download. No paid workflow, deployment or integration was run.
The existing full-test and clean-clone evidence is recorded below; those full
suites were not repeated merely to change repository visibility.

The preparation commit used GitHub's noreply author address, not a personal
email. Private environment files, data, logs and the video were not added to
Git. No runner or App configuration was changed. PUBLIC-57 is now Done/Public.

If a real credential is found later: revoke at the provider, replace locally,
review history cleanup and rescan. Do not paste the value into issues or this
report. Do not rewrite published history, delete refs, or force-push without a
reviewed plan; cached/forked copies may remain even after rewriting.

## Validation record

- Final recruiter-release targeted suite: `58 passed, 1 warning in 13.39s`.
  The CI redaction fixture explicitly checks `[REDACTED]` with dummy values;
  it does not rely on truncation to conceal an unredacted value.
- Final candidate Gitleaks 8.30.1 directory scan: exit 0, `no leaks found`.
  Only tracked files and non-ignored additions were copied into the isolated
  candidate; no private `.env`, database, runtime directory or video was staged.
- Initial suite: 1 failed, 1920 passed, 9 deselected, 1 warning. Failure was a
  test comparing the author's private `.env` to the example; replaced with a
  temporary configuration generated only from `.env.example`.
- Backend after fix: `1938 passed, 9 deselected, 1 warning in 196.94s`.
- Final full backend suite including all new regressions:
  `1941 passed, 9 deselected, 1 warning in 180.12s`.
- Fresh clone/new Python environment: `1938 passed, 9 deselected, 1 warning in 190.87s`.
- Latest targeted audit/settings/closure tests: `26 passed in 4.17s`.
- Latest audit/settings tests, including ignore-rule regression, in the fresh
  clone: `20 passed in 2.25s`.
- Local integration subset: `8 passed, 176 deselected in 6.38s`.
- Frontend: 23 files / 442 tests passed; typecheck and lint passed; build passed
  with the existing warning about a chunk larger than 500 kB.
- Fresh frontend clone: npm ci installed 300 public packages with install
  scripts disabled; the same 23 files / 442 tests passed in 22.44s.
- Python warnings are dependency deprecations (Starlette/httpx and an AnyIO
  alias in the new environment), not failing tests.
- Fresh install resolved current public dependency versions; no private wheel
  cache or shared system-site-packages used. This is not a committed lockfile
  guarantee across future releases.
- Final real-process smoke: health=200, docs=200, provider endpoint disabled,
  cleanup verified with no remaining validation child processes; exit code 0.
- `git diff --check` passed. All 57 task IDs are present; readiness/configuration
  documentation relative links resolve. No tracked ignored files were found.

The integration command excludes the ECR test that creates Docker resources:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m integration tests/test_langgraph_persistence.py tests/test_langgraph_time_travel.py tests/test_supervisor.py tests/test_testing_server.py tests/test_demo_deployment.py
```

The smoke harness uses Start-Process (hidden), redirected local logs, a
maximum 10-second readiness wait, curl, and process-tree cleanup in finally:

```powershell
powershell -NoProfile -File scripts/validate_public_startup.ps1 -CloneRoot <isolated-clone>
```

Run it under an account permitted to inspect and terminate its own children.
The first sandbox attempt passed health/docs but cleanup was denied; the
owned process tree was explicitly stopped with elevated permission. No
unrelated service was stopped. The harness verifies remaining child PIDs and
does not treat an already-exited child as a failed cleanup.

No new paid LLM request, AWS request, deployment, commit, promotion, push or
visibility change was part of these validations.
