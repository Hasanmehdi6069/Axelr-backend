# Security policy — Sentinel Agent 1.0.1

## What this system defends against

Sentinel is a **guarded execution engine**. It runs arbitrary user scripts in a
sandboxed child process, in a filesystem jail, with bounded resources, and
records every attempt in an append-only audit log. It is designed to defend
against:

1. **Honest mistakes.** A script that tries to `open('/etc/passwd', 'w')`, or
   `subprocess.run(['rm', '-rf', '/'])`, or `os.system('...')` is blocked at
   the AST-shield stage (parsed, not regex'd) *before* any child process starts.

2. **Naive attackers.** A script that imports `eval` from `builtins` via
   `getattr(__builtins__, 'eval')`, or reaches `socket.connect` through
   `ctypes`, is blocked at the child-process monkeypatch stage. These are
   bypassable (see below) but they require deliberate effort.

3. **Resource exhaustion.** Every child runs with `RLIMIT_AS` (256 MB),
   `RLIMIT_CPU` (60 s), `RLIMIT_FSIZE` (64 MB), and a 30 s wall clock. A
   fork bomb, memory leak, or infinite loop is killed, not absorbed.

4. **Privilege escalation via filesystem writes.** The WAL takes a snapshot
   before every write. A failed workflow is rolled back to the pre-state.
   A script cannot permanently alter `/scratch` if the calling workflow
   rolls back.

5. **Lateral movement via network egress** *(in `bwrap` mode only).* Inside
   `bwrap`, the network is unshared. No egress is possible at all, allowlist
   or not. In `none` mode, the allowlist is enforced by a Python monkeypatch
   on `socket.socket.connect`, which is bypassable.

## What this system does **not** defend against

**We are publishing these limitations because hiding them is worse than
having them.**

1. **The Python-level shield is not a security boundary in `none` mode.**
   In `SENTINEL_ISOLATION=none`, an attacker who can write Python can bypass
   the shield via `importlib.reload(builtins)`, `__builtins__['eval']`,
   `vars(sys.modules['builtins'])`, or by loading a C extension. Do not run
   adversarial code without `SENTINEL_ISOLATION=bwrap`.

2. **`bwrap` is namespace isolation, not syscall filtering.** It uses
   unprivileged user namespaces (Linux ≥ 5.10) and pivot_root. It blocks
   filesystem and network access. It does not block: CPU side channels,
   kernel exploits, or `/proc`-based attacks via shared kernel state.

3. **`firejail` (fallback) has a history of CVEs.** Prefer `bwrap`. If only
   `firejail` is available, treat the sandbox as defense-in-depth, not
   defense-in-isolation.

4. **The approval gate currently trusts the approver's process identity.**
   There is no cryptographic binding between "a human clicked approve" and
   "this token was issued". For a production deployment, sign approval
   tokens (see `atlassian._sign`) and verify them in `actions.dispatch`.

5. **Audit logs are local JSONL, not tamper-evident.** An attacker with
   write access to `/data/audit.jsonl` can rewrite history. For a
   production deployment, ship audit entries to an append-only remote
   sink (e.g., S3 Object Lock, or `journalctl --seal`).

6. **The MCP server has no authentication.** `--tcp` mode listens on
   `0.0.0.0:8081` with no auth. Deploy behind a TLS-terminating reverse
   proxy with mTLS, or use the stdio transport (default) which inherits
   the parent process's authentication.

## Recommended deployment posture

```yaml
SENTINEL_ISOLATION=bwrap         # only run adversarial code in this mode
SENTINEL_WAL_BACKEND=sqlite      # memory WAL loses rollback on crash
SENTINEL_APPROVAL_SECRET=<32B>   # signs approval tokens (see atlassian._sign)
SENTINEL_AUDIT=/data/audit.jsonl # mounted on a durable volume