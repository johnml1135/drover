# Windows workers and gates in Microsoft Execution Containers

Date: 2026-10-19 · Task: T-cd · Status: design for implementation

## Spike report first: what is established

The owner selected Microsoft Execution Containers (MXC) as the Windows containment standard, following [Microsoft's general-availability announcement](https://blogs.windows.com/windowsdeveloper/2026/10/07/microsoft-execution-containers-policy-driven-containment-for-ai-agents/). Use the released executor and its policy, with Drover supplying paths, environment and network profiles. Do not replace it with a homemade restricted token, command allowlist or dedicated-user sandbox. This document does not enable Windows workers.

Evidence is the lead's `mxc-spike.md` (2026-10-19, 11:25), following `mxc-research.md`, in `/tmp/claude-1000/-mnt-c-Users-johnm/7ef20f6f-0f6e-48de-8bb6-fe1800f36fd4/scratchpad/build/`. Exact inputs and outputs are in `C:\Users\johnm\.drover\tools\mxc\spike\cfg` and `out`; `drover-worker.json` there is a probe fixture, not a production worker policy. These are reported lead results, not tests performed by this documentation task. Preserve their configs and logs with the implementation evidence.

| Question | Observed result | Consequence |
| --- | --- | --- |
| Install and privilege | SDK v1.0.1, released 2026-10-17; Microsoft-valid signatures on `wxc-exec.exe` and `wxc-host-prep.exe`. Windows 11 Pro 26H2, build 26300.9711. Probe: `base-container`, `needsDaclAugmentation:false`, deny paths supported, no warnings. | Installation is complete by owner M1. Normal execution needs neither elevation nor host prep on this machine. |
| Native tools | pwsh and git fail initialization with the default `ui.disable:true`; both start with `false`. | Permit Win32k initialization while retaining the other UI restrictions. |
| Filesystem | Work and scratch writes succeed; toolchain/cache/`.git` writes fail. Owner profile, sibling repos and explicit denied paths are inaccessible. | Policy can express the required filesystem grants. Test linked-worktree metadata separately. |
| Children | Nested pwsh/cmd, `Start-Process` and git retain denied filesystem access. | Run the whole worker tree inside MXC, including build and test children. |
| Git | Native git works with a scratch global config; read-only `.git` allows history and refuses commit. | All worker metadata is read-only; Drover commits. Disable hooks inside. |
| .NET | Build and run succeed with `--no-restore` after a restore outside the spike container. Named mutexes, shared memory and pipes work. | This proves tool compatibility, not permission to restore untrusted projects on the host. |
| Test runners | VSTest times out with full deny; succeeds with broad host loopback, which also exposes host ports 135/445. Self-hosted xUnit v3/MTP-style tests pass under full deny. | Broad host loopback is unacceptable. VSTest needs the further test below. |
| Network and credentials | Default deny blocks NuGet; one numeric IP/port exception works. `cmdkey` lists no owner credentials; gh and owner `.claude`, `.codex`, `.ssh` are inaccessible. | Use proxy destination filtering for changing API/feed addresses; never inherit owner credentials. |
| Environment | Default environment exposes host secrets. Explicit `env` and `inheritDefaultEnv:false` prevent the observed leak. `SYSTEMROOT`, `LOCALAPPDATA`, `PATHEXT` are necessary. | Environment construction is mandatory, including for gates. |
| Lifetime and cost | Timeout kills the tree; children stop when the root exits. Start overhead about 140 ms. Build cost is comparable when both sides disable shared compilation. | Keep MXC destruction and existing runner supervision; do not use host compiler servers. |
| Agent CLIs | `claude --version` and `codex --version` start; scratch config reports no login. Git Bash/MSYS fails creating `\BaseNamedObjects` objects. | Version output does not prove authenticated agent/tool use. Claude PowerShell and model proxy remain rollout blockers. |

The earlier research interpreted the elevation warning as applying to the executor. The spike and [Microsoft's host-prep guide](https://github.com/microsoft/mxc/blob/main/docs/backends/process-container/host-prep.md) resolve this: the elevated program is **wxc-host-prep**, not the normal executor. Its system-drive preparation adds persistent, non-inheriting root ACEs; null-device preparation can be needed after reboot on downlevel tiers. Neither is needed on the observed base-container machine. The earlier suggestions to prefer a custom sandbox or run VSTest outside MXC are superseded by the owner decisions below.

### Remaining spike, before enabling workers

Use small trusted fixtures, a clean scratch home and the installed v1.0.1. Owner-authorized proxy/listener setup is a prerequisite; this docs task starts no service. Save complete request JSON, probe JSON, exit status, bounded logs and host-side canary checks for each row. Test denial with reachable control listeners so a timeout cannot be mistaken for enforcement.

1. **Container-local loopback:** under the deny profile, test runner/child TCP over both `127.0.0.1` and `::1`, then a representative VSTest project with a short timeout. Concurrently prove that a sibling container and reachable host listeners remain inaccessible. There is no invented `containerLoopback` policy key. If v1.0.1 still blocks this, VSTest remains unsupported; retain contained self-hosted gates or block that repo's Windows gate.
2. **Selected host ports:** test an explicitly approved HTTP listener through an exact proxy route, with adjacent ports, other host services and direct sockets denied. Also test any requested raw TCP case. MXC has no general per-port ingress selector; failure must become an unsupported-policy result, never `hostLoopback:allow` without endpoint enforcement.
3. **Identityless proxy:** the machine reports this capability, but that is only discovery. Test Model 2 with `runtimeConfig.networkProxy`, no `allowedProxyPeer`, and its required loopback setting. Prove that only the exact proxy endpoint is reachable, including direct IPv4/IPv6 bypass attempts, host 135/445, Drover's listeners, LAN and sibling-container addresses. Attempt host-to-container connections too; record the actual ingress behavior. Test proxy exit and restart/port reuse without widening access.
4. **Model API session:** through the proxy, allow only the configured model API authorities on HTTPS 443. Test an authenticated model response, streaming, cancellation, denied authorities and loss of proxy. Prove owner keys/tokens are unavailable to the workload. A CONNECT tunnel alone does not solve authentication.
5. **Claude PowerShell:** from a real contained Claude session, select its PowerShell tool, create/edit a fixture, run pwsh 7, git and a .NET child, and verify denied access. Use one pwsh 7 script set on Windows and Linux. No Bash/MSYS fallback. If the installed Claude version cannot offer or reliably use PowerShell, Windows Claude workers remain refused.

The spike verdict is **native contained gates are feasible now; VSTest and authenticated workers are conditional**. No proxy, real Claude PowerShell session, concurrent isolation or Codex login-artifact regression pass is claimed.

### Owner-machine setup

The installed binaries live at `C:\Users\johnm\.drover\tools\mxc\v1.0.1\bin\x64\`. The recorded release artifact SHA-256 is `0e832239454ba7fcd338ff56218a287eef4c0a01c1484084b869f55feff8abe9`; this is not a claimed hash of either extracted executable. Enrollment records their actual identities and signatures separately.

For another host, the owner installs the Microsoft release, verifies provenance/signatures and pins its absolute executor path; Drover never downloads or installs it. The owner supplies supported Windows servicing updates, pwsh 7, toolchains and an approved readable NuGet cache. [Microsoft's OS table](https://github.com/microsoft/mxc/blob/main/docs/backends/process-container/os-version-support.md) lists process isolation from 24H2 26100.9278 / 25H2 26200.9278 and 26H2 26300.9550; runtime policy probing remains authoritative. Do not enable Hyper-V, Windows Sandbox or another optional feature speculatively: this design uses the process backend.

Run the pinned executor's `--probe` as the ordinary runner account. If it reports host prep, a disabled feature, an unsupported policy or elevation requirements, stop admission and show the exact diagnosis to the owner. Only the owner may enable a required feature, run elevated `wxc-host-prep`, change ACLs/firewall policy or install/configure a proxy. Explain persistent root ACEs and any per-boot null-device change before that setup. Reprobe and repeat containment tests afterward; setup alone does not authorize a weaker tier. On this machine no host-prep action is prescribed.

## Decision and boundaries

Owner decisions X1–X4 are requirements: all Windows gates execute inside MXC; in-container git uses `core.hooksPath=NUL` and Drover handles hooks when committing; Claude uses PowerShell; model-only proxy egress must pass before Windows workers go live. Research may have broad public internet through the proxy, with no owner credentials or host services.

Use `wxc-exec.exe` with stable request version `1.0.0`, explicitly selecting `processcontainer`. This is Microsoft's Windows process backend; the observed tier is BaseContainer, not a claim that every Windows host uses legacy AppContainer. Drover is Python, so the published executor interface avoids embedding another language SDK. This is an intended [Microsoft consumption path](https://github.com/microsoft/mxc#how-do-i-use-mxc), not a fork. Microsoft owns containment, child inheritance and teardown; Drover owns admission and resource policy. No new Windows security mechanism, service or dependency is introduced by this document.

The host hub, runner, policy compiler and proxy configuration are trusted. Workload commands, repo code, tests, project files and agent output are untrusted. Job Objects still provide lifetime supervision; they are not the filesystem boundary. MXC policy stays outside the workload's write grants. Agent permission prompts, pinned instructions and a CLI's internal sandbox cannot widen it.

Containment restricts authority to granted resources; it does not validate edits within a writable worktree. Existing task scope checks, review, commit checks, exact-SHA gates and the landing queue remain necessary. Platform requirements in `[run].platforms` and Windows-only build requirements still apply.

## Policy compiler and filesystem

Add one host-side MXC adapter, shared by workers and gates. It accepts a resolved run description and returns an immutable launch request plus a policy digest, or a typed refusal. It does not accept arbitrary MXC JSON from tasks. Owner-managed repo config selects a profile, approved cache and toolchain roots; task worktree files cannot override security settings.

| Resource | Grant |
| --- | --- |
| Assigned task worktree, or isolated exact-SHA gate checkout | Read-write, except protected metadata and secrets. A review/read task receives read-only source. |
| Fresh run scratch subtree | Read-write; separate from run control files and every other run. |
| Git metadata | Read-only: worktree `.git` file/directory, resolved worktree gitdir, common gitdir and required object stores. No write exception for index/locks/refs. |
| Toolchain | Read-only approved install roots: required Program Files directories, dotnet, MSBuild/Visual Studio Build Tools, native Git and pwsh 7; exact agent/Python runtime directories where needed. |
| NuGet cache | Read-only canonical approved path. This host uses `H:\caches\nuget\packages`. |
| Owner credentials, state and other repositories | Denied: owner homes/configs, credential files, hub/ledger/run control directories, main checkout and sibling worktrees; deny secret subpaths within any otherwise granted root. |
| Everything else | No grant. Retain MXC's required OS/runtime baseline; do not add owner-profile, drive-root or broad repository grants to fix tool discovery. |

Resolve Windows paths and linked-worktree `gitdir`/`commondir` on the host; require absolute canonical local paths. Protect metadata even when it sits below the RW worktree. The `.git` pointer file itself must not be replaceable. Confirm actual precedence and delete/rename protection with access tests; a policy that permits replacing protected metadata is refused. If the installed tier cannot preserve this overlap, use an isolated checkout with protected metadata whose layout passes those tests, or refuse. Workers use history/diff/status with optional Git locks disabled; staging and commit happen in Drover.

Do not grant the entire main checkout to make linked Git work. Reject grants that encompass owner state or another run. Resolve/reject junctions, symlinks, alternate path spellings, UNC/device paths and overlapping protected roots before launch; test reparse-point changes inside the container rather than trusting string prefixes as the boundary. Validate all runtime accesses through MXC. A layout that cannot meet the policy is unsupported.

RO does not mean safe to disclose: repo Git config/remote URLs, cache configuration and tool-install subpaths must not expose credentials. Deny secret-bearing files within grants. If required Git metadata mixes secrets with required readable data, prepare a credential-free isolated checkout/config on the trusted side or refuse; never expose it just to satisfy Git discovery. Approved task read snapshots, when present, are separate RO inputs whose metadata is also protected; they never expand the writable task root or grant the owner checkout.

Program Files means approved tool install subtrees, not a blanket read of every installed application's data. The strong argument for narrower grants is that unrelated applications can store account configuration there; a build does not require them. Required Windows system files/devices remain supplied by Microsoft's backend. The claimed deny boundary applies to workload data outside explicit grants and that necessary OS baseline, not to an assertion that a process can run without OS resources.

### Example deny-network gate request

This is illustrative policy for a prepared fixture, not a drop-in path list. The compiler substitutes enrolled tool paths and resolved Git metadata; it must prove overlapping read-only metadata is protected. Read-only Python/agent runtime grants are added only for commands that need them. The fields use the [published 1.0.0 contract](https://github.com/microsoft/mxc/blob/v1.0.1/schemas/stable/mxc-config.schema.1.0.0.json).

```json
{
  "version": "1.0.0",
  "containerId": "drover-gate-example-unique",
  "containment": "processcontainer",
  "lifecycle": { "destroyOnExit": true, "preservePolicy": false },
  "fallback": { "allowDaclMutation": false },
  "process": {
    "commandLine": "\"C:\\Program Files\\PowerShell\\7\\pwsh.exe\" -NoLogo -NoProfile -NonInteractive -File H:\\drover-scratch\\example\\gate.ps1",
    "cwd": "H:\\worktrees\\example",
    "timeout": 120000,
    "inheritDefaultEnv": false,
    "env": [
      "SYSTEMROOT=C:\\Windows",
      "SystemDrive=C:",
      "COMSPEC=C:\\Windows\\System32\\cmd.exe",
      "PATH=C:\\Program Files\\PowerShell\\7;C:\\Program Files\\dotnet;C:\\Program Files\\Git\\cmd;C:\\Windows\\System32",
      "PATHEXT=.COM;.EXE;.BAT;.CMD",
      "LOCALAPPDATA=H:\\drover-scratch\\example\\localappdata",
      "APPDATA=H:\\drover-scratch\\example\\appdata",
      "USERPROFILE=H:\\drover-scratch\\example\\home",
      "HOME=H:\\drover-scratch\\example\\home",
      "TEMP=H:\\drover-scratch\\example\\tmp",
      "TMP=H:\\drover-scratch\\example\\tmp",
      "DOTNET_CLI_HOME=H:\\drover-scratch\\example\\dotnet",
      "NUGET_PACKAGES=H:\\caches\\nuget\\packages",
      "DOTNET_CLI_TELEMETRY_OPTOUT=1",
      "DOTNET_NOLOGO=1",
      "DOTNET_ADD_GLOBAL_TOOLS_TO_PATH=0",
      "MSBUILDDISABLENODEREUSE=1",
      "GIT_CONFIG_GLOBAL=H:\\drover-scratch\\example\\gitconfig",
      "GIT_CONFIG_NOSYSTEM=1",
      "GIT_CONFIG_COUNT=2",
      "GIT_CONFIG_KEY_0=core.hooksPath",
      "GIT_CONFIG_VALUE_0=NUL",
      "GIT_CONFIG_KEY_1=credential.helper",
      "GIT_CONFIG_VALUE_1=",
      "GIT_OPTIONAL_LOCKS=0",
      "GIT_TERMINAL_PROMPT=0"
    ]
  },
  "filesystem": {
    "readwritePaths": ["H:\\worktrees\\example", "H:\\drover-scratch\\example"],
    "readonlyPaths": [
      "H:\\worktrees\\example\\.git",
      "H:\\repos\\example\\.git",
      "C:\\Program Files\\PowerShell\\7",
      "C:\\Program Files\\dotnet",
      "C:\\Program Files\\Git",
      "H:\\caches\\nuget\\packages"
    ],
    "deniedPaths": ["H:\\worktrees\\example\\.env"]
  },
  "network": {
    "egress": { "default": "deny" },
    "ingress": { "default": "deny", "hostLoopback": "deny" }
  },
  "ui": { "disable": false, "clipboard": "none", "injection": false }
}
```

Generate the complete environment from an allowlist; never merge `os.environ`, `seat_env` or a shell profile into the workload block. The [schema guide](https://github.com/microsoft/mxc/blob/main/docs/schema.md#environment) distinguishes an omitted environment from an explicit one. Build a minimal PATH from enrolled RO installs and set home, temp, app-data and CLI config locations to scratch. Keep `CLAUDE_CONFIG_DIR` or `CODEX_HOME` in scratch when applicable. Never copy the owner's login/config files there. Provide locale/certificate data only when explicitly needed and non-secret. Do not pass hub tokens, SSH agents, askpass helpers, cloud credentials, credential-manager connections or owner proxy settings. Use `-NoProfile` for pwsh and disable interactive credential prompts.

The default Win32k lockdown changes to `ui.disable:false` because the spike demonstrates that required native pwsh/git cannot initialize otherwise. This is a strong compatibility reason for one documented switch, not permission for desktop automation. Retain clipboard/input denial and backend default handle/atom isolation, desktop/system-settings restrictions and IME denial. Use the stable schema's field placement rather than copying the older draft UI example. [Microsoft's UI guide](https://github.com/microsoft/mxc/blob/v1.0.1/docs/backends/process-container/UIPolicy_Schema.md) explains the mitigation distinction.

Git's scratch global config is host-generated and contains no credentials. Command-line `-c core.hooksPath=NUL` is also applied to Drover-issued in-container Git commands. An untrusted process can change its environment or command line; these Git settings ensure predictable tool behavior, while MXC remains the authority boundary. Disable external credential helpers and unneeded external diff/filter execution in trusted host-side Git operations. Do not enable MSYS support by relaxing named-object isolation.

### Packages and caches

Leave `NUGET_PACKAGES` pointing at the enrolled RO cache, never at a writable replacement. Preserve the conventional `~/.nuget/packages` path on machines that actually use it; this machine's approved location is on H:. Prepared assets can build/test with `--no-restore`. If assets must be regenerated, an offline restore runs **inside MXC**, using only the approved readable cache and owner-provided offline configuration. NuGet config must exclude owner feed credentials and prevent automatic network/source fallback; the worker cannot invent an alternate package source.

A missing package/version or denied cache read is a blocker: report the exact package or access error and ask the owner to arrange an approved readable local cache. Do not install packages, change sources, point at another cache, use a proxy or restore an agent-modified project on the host as a workaround. The outside restore in the spike is historical evidence only. This requirement takes precedence over the preliminary lead recommendation.

## Network profiles and the v1.0.1 gaps

Separate the model transport from repo tool networking. Owner-managed repo config declares tool network `none` (default) or `package-feeds` with exact approved authorities; it cannot enable arbitrary worker egress. The .NET cache-only rule above remains stricter even when a repo has a feed profile for other tooling. Gate and worker defaults are:

| Kind | Required effective access | Admission condition |
| --- | --- | --- |
| Gate | External egress deny; same-container runner/testhost loopback only. No host services, proxy or credentials. | Deny profile passes; VSTest additionally needs the container-local loopback test. |
| Worker | Model APIs only through Model 2. Repo tools get no internet by default; any approved feed use shares the constrained proxy transport. | Proxy confinement, authentication isolation and real agent PowerShell tests pass. |
| Research | Broad public HTTP/S internet through Model 2; no private, link-local, host or credential access. | Same proxy confinement tests; separate owner-selected profile, never inferred from a failed build. |
| Explicit host-service need | Only named, owner-approved localhost service/port through a constrained HTTP route; absent from the three profiles above. | Exact route and bypass tests pass. Raw TCP needs an enforceable supported control or is refused. |

Microsoft recommends Model 2. Use `egress.default:deny`, `ingress.default:allow` and `runtimeConfig.networkProxy` with no direct egress rules. The proxy enforces destination restrictions; it is caller-managed. Prefer the documented identity-scoped deployment with `processContainer.network.allowedProxyPeer` and `hostLoopback:deny` for production. Identityless mode requires `hostLoopback:allow`, lacks peer identity binding and is described as a weaker development/testing option. Direct numeric egress rules cannot stand in for durable DNS-name filtering. These are the constraints of [Microsoft's networking guide](https://github.com/microsoft/mxc/blob/v1.0.1/docs/backends/process-container/networking.md).

Test the identityless mode requested in X1, but do not equate its probe capability with endpoint enforcement or production readiness. Its endpoint-only WFP behavior must be demonstrated on this build. Even if bypass tests pass, port ownership/rebinding lacks identity assurance: use it for the authorized spike, then enroll an identity-scoped proxy for production. If that deployment is unavailable, workers stay blocked. Do not silently accept global host-loopback access. This follows Microsoft's preferred production deployment; it does not add a new sandbox.

The proxy starts before a client and remains until it exits. Bind only the configured loopback address/port. Pin its identity, authority list and profile revision in the run policy. The worker profile admits exact configured model authorities on 443, no suffix wildcards, arbitrary CONNECT targets or alternate ports. The feed profile adds exact approved public feed/CDN authorities, never all internet. Research permits public destinations but rejects loopback, RFC1918/private IPv6, link-local/metadata addresses and other non-public destinations after DNS resolution and at connection time. Reject redirects or CONNECT authorities that escape the profile, DNS rebinding, ambiguous host syntax and direct-IP bypasses. Do not log credentials or model payloads.

MXC provides transport confinement, not model authentication. Owner API keys must remain outside the client container. The remaining spike must prove a CLI-supported base-URL/gateway arrangement in which a trusted model gateway supplies upstream authentication only for approved API routes, with no token-export endpoint or owner credential pass-through. A generic opaque CONNECT proxy cannot inject an Authorization header. If the installed agent cannot use such an arrangement, workers remain blocked; copying a subscription login or API key into scratch is not an alternative. Do not add ad hoc TLS interception or a new CA as an implicit setup step. Proxy/gateway implementation and deployment need their own review and owner setup. This is a proposed authentication integration, not an MXC feature or a demonstrated Claude capability.

Two gaps require explicit handling. First, no shared policy knob separately enables intra-container loopback. The local VSTest failure therefore remains a compatibility issue despite the intended separation from host-loopback policy; use a proven self-hosted contained test runner or wait for a supported MXC fix. Second, ingress has no per-source/per-port selectors. A constrained HTTP proxy route can mediate an approved HTTP service; it cannot transparently relay VSTest TCP or promise arbitrary localhost port filtering. Unsupported raw TCP stays blocked. Do not replace either gap with host-wide firewall/ACL changes, an experimental backend or execution outside MXC.

`ingress.default:allow` is a required Model 2 trade-off because Windows grants bidirectional private-network capability. Probe and test actual inbound reachability; do not advertise total inbound isolation solely from the policy name. Workloads must not expose listeners or credentials; an incompatible required ingress guarantee blocks the profile. Gate Model 3 does not incur this proxy trade-off.

## Support detection and fail-closed launch

Enrollment pins the ordinary Windows account, executor/version/signature, OS build, tool paths, cache, proxy identity and accepted compatibility results. Use a bounded read-only `wxc-exec.exe --probe` for general discovery and the release's request-aware probe for **each complete policy** before launching. Neither a successful OS version check nor a general probe validates proxy connectivity. Parse the pinned release's documented JSON; missing/unknown fields, timeout, malformed output or unsupported requests are refusals, not guessed support.

Initially admit only the tested `base-container` tier with denied-path support, no DACL augmentation and no required host prep. Set `fallback.allowDaclMutation:false`. A lower tier may be Microsoft-supported, but it has not passed Drover's overlapping metadata, no-host-mutation and network tests. The strong reason for refusing it is avoiding host ACL changes and loss of required policy guarantees; do not try another backend automatically. Report the rejected tier/capability and owner action needed. If a future release needs per-run elevation, block ordinary dispatch; that is an acceptable reason to reconsider the integration with the owner, not to elevate the worker.

Invalidate compatibility evidence when executor bytes/version, OS servicing build, account, agent CLI/toolchain, proxy identity/config or policy changes. Reprobe at launch, not just hub startup; keep canary fixtures part of enrollment/upgrade checks. Record redacted probe results and policy digest with every run, including a refusal reason if no command started. No `--audit`, permissive diagnostic execution or automatic policy broadening for untrusted code.

Launch sequence:

1. Admission resolves repo/platform/access and checks MXC compatibility. Freeze tool and network profiles from trusted configuration. A failed capability check leaves Windows work held with an actionable blocker; WSL remains available only when existing routing/platform policy permits it.
2. Prepare worktree and fresh scratch. Keep policy JSON, gate plan, brief, output, status, stop request and receipt control paths in the host-owned run directory, outside RW grants. Pass input by stdin and output by narrowly inherited pipes/handles; never mount the whole run directory. Any required script/input path is separately RO, not a reason to grant hub state.
3. Compile explicit command line, cwd, environment and paths. Serialize Windows argument quoting with a tested argv-to-command-line routine; invoke the executor with an argv array, no `cmd /c`, shell interpolation or `.cmd` agent shim. Store immutable request/digest before probing it.
4. Existing host runner launches `[pinned-wxc-exec, policy-json]` and supervises it in its Windows Job Object. The executor creates the contained root worker; every pwsh, dotnet, MSBuild, testhost and git child stays inside. No host-side tool callback executes agent commands. Keep MXC child teardown and disable build server reuse/shared compilation so no host compiler/MSBuild server handles untrusted requests.
5. Maintain existing wall/idle timeouts, nonce-checked stops, stdout capture and action-record parsing. Add a finite MXC `process.timeout` in milliseconds as a backstop to runner limits. Cancellation terminates this run's executor/tree by verified identity and confirms no children remain; nested-job incompatibility refuses launch rather than dropping either boundary.
6. On restart, adopt the host runner by its existing run ID/nonce and process identity. Retain pinned policy/proxy metadata and never adopt an unknown process as contained. Failure to prove tree termination or proxy ownership is a hold, not a retry with broader access.
7. After confirmed tree exit, close handles, retire per-run proxy authorization and clean only that run's scratch, including `localappdata\Packages\sandbox.*\AC\Temp`. Reject reparse points during cleanup; do not follow them into owner data. Preserve redacted diagnostics/receipts in host-owned storage. Cleanup failure is visible and retried by owned-run cleanup, not shared-profile deletion.

The implementation seams are `RunnerLauncher.command/launch` in `drover/launch.py`, `spawn_worker` and supervision in `drover/runner.py`, plus the Windows command execution path in `drover/gate.py`. The existing launcher constructs host-inherited environments and currently allocates Windows scratch below the run directory; the adapter must separate those trusted control files from the granted scratch subtree. Retain existing action-state/nonce contracts.

## Windows gates, commits and T-bd

Every Windows gate command, retry, scoped/full test and landing-batch gate uses the same MXC adapter. Close bypasses in direct CLI `gate.run_gate` callers as well as dispatcher-launched `gatejob.py`. Never treat `wrap_command` returning an unwrapped argv as permission to run on Windows. Trusted host orchestration may read Git metadata and collect exit information; all repo scripts, builds, test executables and evaluated project files execute inside MXC.

Use an isolated gate checkout of the planned SHA, RW for build outputs with protected RO Git metadata and a separate scratch. No owner checkout or shared bin/obj outputs. Keep plan, expected SHA and final receipt outside the workload grants. The trusted gate coordinator invokes each workload through MXC, then constructs the receipt from observed exit/timeout results and independent Git checks; do not accept a receipt file the test process could rewrite. Record executor identity, policy/probe digest and network profile alongside existing exact-SHA/platform evidence. Containment refusal produces no passing receipt. A task/batch is READY only after all configured platform gates pass for its exact SHA.

X2 disables in-container hooks. Drover's commit path runs hooks outside **that worker container**, but X1 forbids executing agent-written hooks or hook-launched project/test code unsandboxed. Pin hook definitions from owner-approved configuration/base; host commit orchestration never executes a newly modified worktree hook. Hook validation that evaluates repo code runs as a separate MXC gate. A hook requiring MSYS or a host execution path that cannot meet this split blocks the commit until an approved pwsh-compatible contained path exists. The owner decision does not grant arbitrary agent code a host hook escape.

T-bd's refusal is replaced with a conditional admission check, not deleted wholesale: Windows Claude writes are allowed only when the exact machine policy, authenticated model proxy and real PowerShell session have passed, and required repo gates are supported. Until then preserve refusal. Missing support never falls back to Job Object-only execution. Windows gates can roll out independently for compatible repos before workers.

Codex remains WSL-only under ADR 0002 and the pinned worker constraint. Starting its binary and denying owner config does not show that Windows shelling no longer creates login artifacts. A separate owner-authorized Windows investigation must compare host login/registry state before and after representative shell children, normal completion, timeout, cancellation and crash, with no owner credential exposure and writes confined to scratch. Only successful evidence plus an explicit owner change to the existing prohibition can permit Codex Windows placement. This task does not run that experiment or change the prohibition.

## Deviations and rejected shortcuts

| Choice | Strong argument relative to Microsoft defaults/guidance |
| --- | --- |
| `ui.disable:false` | Reproduced pwsh/git initialization failures with Win32k disabled; remaining UI isolation stays restrictive. |
| Explicit non-inherited environment | Default inheritance demonstrably exposes owner secrets. The released schema supports a caller-owned environment. |
| Base-container required; DACL fallback disabled | Required network/path guarantees must be tested; unapproved host ACL mutation is unacceptable. Reconsider only with owner setup and equivalent evidence. |
| Exact Windows process backend | A tested Windows policy must not select an untested VM/session or other platform backend implicitly. It is Microsoft's normal Windows backend. |
| Narrow RO tool subtrees and RO Git metadata | Least access satisfies build/history needs while preventing owner-data reads and shared-repo mutation; Drover owns commit. |
| Proxy rather than feed/model IP snapshots | Microsoft recommends Model 2; rotating CDN addresses make IP snapshots unreliable. This is normal MXC use, not a custom containment layer. |
| Identityless mode limited to spike | Microsoft calls it a weaker compatibility deployment. Production uses identity binding rather than relying on a port remaining owned by the right process. |
| No host restore, unsandboxed gate, MSYS relaxation or owner login injection | Each would bypass an explicit owner boundary. Unsupported tooling blocks instead. No evidence justifies deviating from MXC to implement a parallel sandbox. |

## Verification required for implementation

Unit tests use fake probes/processes and temporary layouts; Windows integration tests use owner-provisioned tools and trusted canary fixtures, serially within admitted capacity. Do not install packages, start services or run Windows Codex as part of an ordinary test suite. Integration fixtures are untrusted execution from the host's perspective and must themselves run in MXC.

| Area | Required assertions |
| --- | --- |
| Compiler and config | Correct native absolute paths/quoting; only worktree/scratch RW; read-only tasks stay RO; `.git` overlaps protected; reparse/UNC/device/parent grant rejection; unknown policy/profile values fail; workload config cannot modify trusted policy. |
| Discovery/refusal | Missing/tampered executor, changed build/account, elevation/prep requirement, downlevel tier, deny capability absent, malformed/timed-out general/request probe and unsupported network policy all refuse with zero workload launches. |
| Filesystem | Child writes succeed only in assigned RW roots. Reads/writes of sibling/main/owner state denied. Tool/cache/metadata writes, delete/rename/replace attempts and junction/hardlink escape attempts denied; other runs' files unchanged. |
| Environment/credentials | A synthetic secret placed in host env never appears in parent/child output. Credential canaries and credential-manager/SSH/gh/CLI owner stores inaccessible; scratch login stays empty; no secret in policy/log/receipt. |
| Tools and lifetime | pwsh 7, native git read operations, dotnet build/run and actual MSBuild/testhost children work; denied access persists through grandchildren and Start-Process. Timeout, main exit, runner death, stop and hub adoption leave no orphan or host-server work. |
| Cache behavior | Present package builds offline; missing version and denied cache read produce actionable blockers. No network restore, cache mutation, alternate source/cache or host restore attempt. |
| Network | Execute the five remaining-spike rows above; IPv4/IPv6, proxy bypass/failure, local services, peer containers, DNS rebinding, redirects and unauthorized authorities are covered. VSTest success counts only with host-loopback canaries still denied. |
| Agent workflow | Real Claude PowerShell editing/tool/model session, cancellation and authentication isolation pass. Bash use does not trigger host fallback. Research profile allows public internet while rejecting host/private services. |
| Gate/receipt | Direct and dispatcher Windows gates, retries, hooks and batch gates all traverse MXC; refusal/timeout cannot forge a pass. Trusted receipt binds planned SHA, policy, executor and platform; dirty/moved SHA fails. Other platforms keep existing behavior. |
| Concurrency/cleanup | Distinct container IDs/scratch/proxy authorization; no cross-run reads or interference; AC Temp cleanup after success/failure/cancel/restart; reparse-safe cleanup and cleanup-failure visibility. |

## Small implementation tasks and handoff

These are follow-up tasks, not changes included in T-cd. Each can land independently with a disabled rollout default.

1. **Finish the Windows capability spike.** Run the five remaining-spike rows using owner-authorized listeners/proxy. Preserve policy/output/canary evidence and report each unsupported control. Done when VSTest, selected-port, identityless-proxy and Claude PowerShell/auth statuses are established independently; a failure is a documented profile blocker, not a sandbox exception.
2. **Add the policy compiler and config validation.** Introduce a narrow host adapter (proposed `drover/mxc.py`) plus owner-managed host/repo profile parsing in `config.py`/`repos.py`. Cover canonical grants, protected metadata, clean env, schema/quoting and no task overrides with unit tests. Do not add a new package dependency implicitly.
3. **Add enrollment and request-aware support checks.** Pin executor/signature/version/build, record compatibility evidence and implement typed refusal reasons. Test missing, changed, malformed and unsupported inputs with no workload launch; publish owner setup diagnostics without performing setup.
4. **Provision the model proxy/gateway.** Review and owner-install an identity-scoped deployment using Microsoft's Model 2. Prove destination enforcement and CLI authentication without exposing owner credentials. Keep workers disabled until both transport and authenticated agent tests pass; no installation/service starts by a worker.
5. **Integrate Windows worker launch and supervision.** Connect `launch.py`/`runner.py`, isolated scratch/control paths, finite MXC timeout, verified tree stop/adoption and safe cleanup. Test nested jobs and process inheritance with native tools. Enable Claude per validated machine/agent profile; preserve Codex's Windows refusal.
6. **Contain every Windows gate and commit validation.** Route direct, task, retry and batch gate commands through the adapter; implement trusted receipts and the approved-hook split. Add offline-cache and real contained .NET tests. Prove no uncontained fallback and exact-SHA receipts before enabling any repo.
7. **Roll out with denial evidence.** Run serial machine acceptance and cross-run isolation/cleanup checks; enroll one compatible Windows gate repo first, then Claude workers after task 4/5 acceptance. Update operator guidance and T-bd admission messaging in that follow-up. Unsupported VSTest/host-service profiles remain held. Codex requires its separate investigation and owner policy change.

T-cd hands off the design and existing spike evidence only. The rollout gate is not satisfied by this document, a generic support probe or a CLI version check.
