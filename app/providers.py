"""Model provider adapters: the only place that knows Claude Code or Codex CLI syntax.

Company logic asks a provider to `run` a task envelope and gets back a ProviderResult.
Nothing outside this module builds a CLI command line, so a CLI change is a one-file fix
and a new provider is one more class (docs/AUTONOMOUS_AGENT_ARCHITECTURE.md §4).

Zero-cost mode (CLAUDE.md §26A) is enforced here, not trusted: a provider that would bill
a metered API key reports METERED_BLOCKED and never starts. Subscription logins
(`CLAUDE_CODE_OAUTH_TOKEN`, Codex `auth_mode = chatgpt`) are existing access; API keys
are new spend. A quota error is never answered by buying capacity.
"""
import enum
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Any of these in the worker's environment means a run could bill per token.
METERED_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX", "OPENAI_API_KEY", "CODEX_API_KEY",
)
# Only these reach the child process; everything else (including metered keys that
# slipped in) is dropped. Defence in depth on top of the METERED_BLOCKED refusal.
# USER/LOGNAME/TMPDIR: the macOS keychain login is invisible without them.
# HTTPS_PROXY/HTTP_PROXY/NO_PROXY: in company-reasoner the only route out is the proxy.
PASS_ENV = ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "TZ",
            "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
            "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")


class Health(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    CREDENTIALS_CONFIGURED = "CREDENTIALS_CONFIGURED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    BROKEN = "BROKEN"
    DISABLED = "DISABLED"
    METERED_BLOCKED = "METERED_BLOCKED"


# Worth trying the next provider for; anything else is the task's problem, not capacity.
FALLBACK_HEALTH = {Health.AUTH_REQUIRED, Health.RATE_LIMITED, Health.QUOTA_EXHAUSTED,
                   Health.DISABLED, Health.BROKEN}


@dataclass
class ProviderResult:
    provider: str
    health: Health
    output: dict | None = None
    error: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    # What the run would cost at list price. Under a subscription it is not billed;
    # recorded so the founder can see usage, never used to decide spend.
    list_cost_usd: float | None = None
    duration_s: float = 0.0
    exit_code: int | None = None

    @property
    def ok(self) -> bool:
        return self.health == Health.AVAILABLE and self.output is not None


RUNNABLE_HEALTH = {Health.AVAILABLE, Health.CREDENTIALS_CONFIGURED}


# (argv, cwd, timeout_s, env) -> (exit_code or None on timeout, stdout, stderr)
Runner = Callable[..., tuple[int | None, str, str]]   # (argv, cwd, timeout, env, stdin=None)


def subprocess_runner(argv: list[str], cwd: str, timeout: int, env: dict,
                      stdin: str | None = None) -> tuple[int | None, str, str]:
    # stdin closed unless text is given: codex otherwise waits on "Reading additional input from stdin".
    # New session so a timeout kills the whole process group, not just the CLI wrapper.
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        out, err = proc.communicate(input=stdin, timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, 9)
        out, err = proc.communicate()
        return None, out or "", err or ""
    except BaseException:
        # Any other failure (stdin encoding, undecodable output, an interrupt): the process group is killed and
        # reaped before the caller cleans up, so nothing keeps writing into a directory that is being wiped.
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        proc.wait()
        raise


def classify(message: str) -> Health:
    m = (message or "").lower()
    if re.search(r"usage limit|session limit|quota|credit|insufficient|billing|payment", m):
        return Health.QUOTA_EXHAUSTED
    if re.search(r"rate.?limit|\b429\b|overloaded|too many requests", m):
        return Health.RATE_LIMITED
    # "Your organization has disabled Claude subscription access for Claude Code" (2026-10-23): the account cannot
    # run at all, so it is a login problem, never "available but this run failed".
    if re.search(r"log ?in|logged in|unauthori[sz]ed|\b401\b|invalid api key|authenticat|oauth|organization has disabled .*subscription access", m):
        return Health.AUTH_REQUIRED
    return Health.BROKEN


_RESET = re.compile(r"try again (?:at|in) ([^.)]+)", re.I)


def quota_message(raw: str) -> str:
    """Subscription limits mean wait. Provider text that suggests buying usage is not
    stored or shown anywhere (Desk, Telegram, task history)."""
    reset = _RESET.search(raw or "")
    return "subscription usage limit reached; waiting for reset" + (
        f" (provider says: try again {reset.group(0)[10:].strip()})" if reset else "")


def metered_env(environ: dict) -> list[str]:
    return [k for k in METERED_ENV if environ.get(k)]


def child_env(environ: dict) -> dict:
    env = {k: environ[k] for k in PASS_ENV if environ.get(k)}
    # Observed 2026-10-01: Claude Code also calls a telemetry intake. Not needed for the
    # work, so it is switched off: one fewer egress destination to allow (§6).
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


@dataclass
class Provider:
    name: str
    binary: str
    model: str | None = None
    environ: dict = field(default_factory=lambda: dict(os.environ))
    runner: Runner = subprocess_runner

    def health_check(self) -> tuple[Health, str | None]:
        found = metered_env(self.environ)
        if found:
            return Health.METERED_BLOCKED, f"metered credentials present: {', '.join(found)}"
        if not shutil.which(self.binary, path=self.environ.get("PATH")):
            return Health.DISABLED, f"{self.binary} is not installed"
        return self._auth_check()

    def _auth_check(self) -> tuple[Health, str | None]:
        return Health.AVAILABLE, None

    def version(self) -> str | None:
        """CLI version for run provenance. Cached; never fatal."""
        if not hasattr(self, "_version"):
            try:
                code, out, _ = self.runner([self.binary, "--version"], "/", 10, child_env(self.environ))
                self._version = out.strip().splitlines()[0] if code == 0 and out.strip() else None
            except (OSError, IndexError):
                self._version = None
        return self._version

    def run(self, prompt: str, schema: dict, *, cwd: str, timeout_s: int,
            max_turns: int) -> ProviderResult:
        health, why = self.health_check()
        if health not in RUNNABLE_HEALTH:
            return ProviderResult(self.name, health, error=why)
        started = time.monotonic()
        result = self._run(prompt, schema, cwd=cwd, timeout_s=timeout_s, max_turns=max_turns)
        result.duration_s = round(time.monotonic() - started, 2)
        return result

    def _run(self, prompt, schema, *, cwd, timeout_s, max_turns) -> ProviderResult:
        raise NotImplementedError


class ClaudeCodeProvider(Provider):
    """`claude -p` with structured output. No tools: the first slice only reasons over
    the context it is given, so there is nothing to grant (Stage A/B rollout)."""

    def __init__(self, model: str | None = "sonnet", **kw):
        super().__init__(name="claude", binary="claude", model=model, **kw)

    def _auth_check(self):
        # macOS keeps the login in the keychain, which cannot be inspected cheaply; on the
        # server the subscription token or a credentials file must exist.
        home = Path(self.environ.get("CLAUDE_CONFIG_DIR") or Path(self.environ.get("HOME", "~")) / ".claude")
        if (self.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or self.environ.get("CLAUDE_CODE_OAUTH_TOKEN_2")
                or sys.platform == "darwin"
                or (home.expanduser() / ".credentials.json").exists()):
            return Health.CREDENTIALS_CONFIGURED, "Claude credentials found; live model access is unverified"
        return Health.AUTH_REQUIRED, "claude is not logged in (set CLAUDE_CODE_OAUTH_TOKEN)"

    def _run(self, prompt, schema, *, cwd, timeout_s, max_turns):
        """One run, on the first subscription account that can answer. The second token
        (CLAUDE_CODE_OAUTH_TOKEN_2, founder decision 2026-10-23: two Claude accounts) is tried only when the first
        produced no answer because of login, quota or rate limit; an account that answered is never re-asked."""
        tokens = [self.environ[v] for v in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_2") if self.environ.get(v)]
        res = None
        for token in tokens or [None]:
            env = {k: v for k, v in self.environ.items() if k != "CLAUDE_CODE_OAUTH_TOKEN_2"}
            if token:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = token
            res = self._run_once(prompt, schema, cwd=cwd, timeout_s=timeout_s, max_turns=max_turns, environ=env)
            if res.output is not None or res.health not in (Health.AUTH_REQUIRED, Health.QUOTA_EXHAUSTED,
                                                            Health.RATE_LIMITED):
                return res
        return res

    def _run_once(self, prompt, schema, *, cwd, timeout_s, max_turns, environ):
        # The prompt goes on stdin: one argv string is capped at 128 KiB on Linux, and a release review packet
        # is larger ("Argument list too long", gateway REVIEW_UNKNOWN on 7a8a82b, 2026-10-23).
        argv = [self.binary, "-p", "--output-format", "json",
                "--json-schema", json.dumps(schema), "--tools", "",
                "--permission-mode", "dontAsk", "--max-turns", str(max_turns),
                "--no-session-persistence"]
        if self.model:
            argv += ["--model", self.model]
        code, out, err = self.runner(argv, cwd, timeout_s, child_env(environ), prompt)
        if code is None:
            return ProviderResult(self.name, Health.AVAILABLE, error=f"timeout after {timeout_s}s")
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            msg = (err or out or f"exit {code}").strip()[:500]
            health = classify(msg)
            return ProviderResult(self.name, health, exit_code=code,
                                  error=quota_message(msg) if health == Health.QUOTA_EXHAUSTED else msg)
        usage = data.get("usage") or {}
        res = ProviderResult(
            self.name, Health.AVAILABLE, exit_code=code,
            model=next(iter(data.get("modelUsage") or {}), self.model),
            input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
            list_cost_usd=data.get("total_cost_usd"),
        )
        if data.get("is_error") or code != 0:
            res.error = str(data.get("result") or data.get("subtype") or f"exit {code}")[:500]
            res.health = classify(res.error)
            if res.health == Health.QUOTA_EXHAUSTED:
                res.error = quota_message(res.error)
            if res.health == Health.BROKEN:
                res.health = Health.AVAILABLE  # the provider works; this run failed
            return res
        out_obj = data.get("structured_output")
        if not isinstance(out_obj, dict):
            res.error = "no structured_output in result"
        res.output = out_obj if isinstance(out_obj, dict) else None
        return res


# `codex exec` with no way to read files, so its own $CODEX_HOME/auth.json stays out of reach of a prompt-injected
# review: the read-only sandbox still lets the shell read anywhere (probed 2026-11-03, codex-cli 0.166.0: `cat` and a
# config-defined node_repl MCP server both printed a canary file). --ignore-user-config drops config.toml MCP servers,
# plugins and hooks (auth still comes from CODEX_HOME); the --disable names are `codex features list` flags. With
# these, the same probe answered NO_TOOL. The Claude equivalent is `--tools ""`.
NO_TOOLS = ("--ignore-user-config", "--ignore-rules",
            *(a for f in ("shell_tool", "unified_exec", "code_mode_host", "multi_agent", "view_image",
                          "browser_use", "computer_use", "plugins", "apps", "memories") for a in ("--disable", f)))
CREDENTIAL_WINDOW = 24
CODEX_UNGUARDED = "codex run refused: its login could not be snapshotted or its output could not be checked"
CODEX_LEAK = "codex output held its own login credentials; withheld"




def _leak_forms(*texts: str) -> list[str]:
    """Every form a credential slice could reach findings or an error in, for the check in CodexProvider._run:
    - the raw texts, and each JSON document in them (whole, or per JSONL line, and a JSON string inside one, as the
      final answer sits in an event's item.text) decoded, so `\\uXXXX` escapes are undone;
    - every decoded string alone, and the strings joined in document order with "" and "\\n": all values of a
      document, all values across documents, and the values sharing one key path (`findings[]`, `item.text`) within
      a document and across all of them, so a token or JWT signature split into short findings or messages reads
      whole again;
    - each of these with all whitespace removed, so a slice wrapped over lines or spaced out is caught too.
    Founder question 2026-11-03: a JWT signature split across findings, wrapped by newlines or whitespace, or
    JSON-escaped is caught (tests/test_stage_c_hardening.py). Transformations the parent cannot undo (reversed,
    re-encoded, paraphrased) are not: NO_TOOLS, which keeps the token out of the model's reach, is the barrier."""
    forms, every, across, queue = list(texts), [], {}, [(t, 0) for t in texts]
    while queue:
        text, depth = queue.pop(0)
        lines = text.splitlines()
        for chunk in [text, *lines] if len(lines) > 1 else [text]:   # in stream order, repeats kept: joins follow it
            try:
                doc = json.loads(chunk)
            except ValueError:
                continue
            # Three document-order sequences: values only, keys only, and both interleaved; a token split into
            # short values under different keys, or into short property names, reads whole in one of them.
            values, keys, both, groups, stack = [], [], [], {}, [("", doc, False)]
            while stack:
                path, v, is_key = stack.pop()
                if isinstance(v, dict):
                    stack += reversed([e for k, x in v.items()
                                       for e in ((f"{path}{{}}", k, True), (f"{path}.{k}", x, False))])
                elif isinstance(v, list):
                    stack += reversed([(f"{path}[]", x, False) for x in v])
                elif isinstance(v, str):
                    (keys if is_key else values).append(v)
                    both.append(v)
                    groups.setdefault(path, []).append(v)
                    if not is_key and depth < 3:   # JSON inside JSON, of any type: the final answer is a JSON
                        queue.append((v, depth + 1))   # object in item.text; a JSON-encoded string ("\\u00..")
                                                       # decodes again; text that is not JSON is skipped
            every += values
            forms += both
            for path, group in groups.items():
                across.setdefault(path, []).extend(group)
            for group in (values, keys, both, *groups.values()):
                forms += ["".join(group), "\n".join(group)]
    for group in (every, *across.values()):   # also across events: one key path, every JSONL line
        forms += ["".join(group), "\n".join(group)]
    return forms + [re.sub(r"\s+", "", f) for f in forms]


def _holds_slice(forms: list[str], windows: set[str]) -> bool:
    # Linear in the text: each CREDENTIAL_WINDOW-long run of a form is looked up in the slice set.
    return any(f[i:i + CREDENTIAL_WINDOW] in windows for f in forms for i in range(len(f) - CREDENTIAL_WINDOW + 1))


CODEX_NO_TMPFS = "codex run refused: no tmpfs for its run files (set CODEX_VOLATILE_DIR to a tmpfs directory)"
CODEX_CLEANUP_FAILED = "codex cleanup failed; output withheld"
CODEX_SWAPPABLE = "codex run refused: its memory could be swapped to disk (needs no swap or cgroup memory.swap.max=0)"
VOLATILE_FS = {"tmpfs", "ramfs"}


def _fs_type(path: str) -> str | None:
    """The filesystem type of the mount holding `path` (Linux /proc/self/mountinfo); None elsewhere."""
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return None
    path, best, fstype = os.path.realpath(path), "", None
    for line in lines:
        fields, _, rest = line.partition(" - ")
        mount = fields.split()[4].encode().decode("unicode_escape")   # octal escapes such as \040 for a space
        if (path == mount or path.startswith(mount.rstrip("/") + "/")) and len(mount) >= len(best):
            best, fstype = mount, rest.split()[0]
    return fstype


def volatile_root(environ: dict) -> str | None:
    """A directory on non-persistent storage for a Codex run's files, or None (the run is refused). Linux: the
    mount must be tmpfs/ramfs (company-reasoner: /tmp is a tmpfs, docker-compose.yml). An explicit path never
    overrides verification; unsupported platforms, including macOS, refuse the run."""
    explicit = environ.get("CODEX_VOLATILE_DIR")
    for root in ([explicit] if explicit else [environ.get("TMPDIR") or "/tmp", "/dev/shm"]):
        if not os.path.isdir(root):
            continue
        fstype = _fs_type(root)
        if fstype in VOLATILE_FS:
            return root
    return None


def _swap_off_impl(proc: str = "/proc", cgroup_root: str = "/sys/fs/cgroup") -> bool:
    """Pages of this process cannot reach a swap device: none is active (/proc/swaps), or this process's cgroup v2
    has memory.swap.max = 0. Kernel docs (admin-guide/cgroup-v2, memory.swap.max): "Swap usage hard limit"; shmem,
    which tmpfs pages are, is swap-backed and charged to the cgroup like anonymous memory, so with 0 it is never
    swapped. company-reasoner gets this from memswap_limit == mem_limit (docker-compose.yml). A tmpfs alone is not
    enough: its pages swap like any anonymous memory (the CI host has an unencrypted /swap.img)."""
    try:
        if len(Path(proc, "swaps").read_text().strip().splitlines()) <= 1:
            return True
        for line in Path(proc, "self/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                group = line[3:].strip().lstrip("/")
                return Path(cgroup_root, group, "memory.swap.max").read_text().strip() == "0"
    except OSError:
        return False
    return False


swap_off = _swap_off_impl   # the seam tests replace


def _cleanup_codex_run(run_dir: Path) -> str | None:
    for attempt in range(2):
        try:
            if attempt:
                for parent, dirs, _ in os.walk(run_dir, followlinks=False):   # permission repair, then retry
                    os.chmod(parent, 0o700)
                    for name in dirs:
                        if not os.path.islink(os.path.join(parent, name)):
                            os.chmod(os.path.join(parent, name), 0o700)
            shutil.rmtree(run_dir)
        except OSError:
            pass
        if not os.path.lexists(run_dir):
            return None
    # Filenames can themselves hold credentials. Report bounded path hashes, never names, contents or exceptions.
    path_ids, errors, incomplete = [], [], False
    try:
        for parent, dirs, files in os.walk(run_dir, onerror=errors.append, followlinks=False):
            for name in dirs + files:
                if len(path_ids) == 8:
                    incomplete = True
                    break
                relative = str((Path(parent) / name).relative_to(run_dir))
                path_ids.append(hashlib.sha256(os.fsencode(relative)).hexdigest()[:16])
            if incomplete:
                break
    except OSError:
        incomplete = True
    incomplete = incomplete or bool(errors)
    error = (f"{CODEX_CLEANUP_FAILED}; run_id={run_dir.name}; remaining_path_ids={','.join(path_ids)}; "
             f"inventory_incomplete={str(incomplete).lower()}")
    logging.getLogger(__name__).error("%s", error)
    return error


class _Swappable(Exception):
    """Swap became possible during a run: no further login read; the run is refused (after cleanup)."""


def _keep_refreshed_login(used: Path, original: bytes, real: Path) -> None:
    """Codex rotates its refresh token when it refreshes; the rotated login lives in the run's copy, which is about
    to be wiped. Written back (atomic, 0600) only if the persistent auth.json still holds exactly the bytes this run
    copied, checked under an flock next to it: another run's newer refresh is never overwritten. Only auth.json
    ever leaves the run directory."""
    import fcntl
    if not swap_off():
        raise _Swappable
    current = used.read_bytes()
    if current == original or not isinstance(json.loads(current), dict):
        return
    lock = os.open(real.parent / ".auth.json.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not swap_off():
            raise _Swappable
        if real.read_bytes() != original:
            return   # changed since the copy: keep the newer login
        fd, part = tempfile.mkstemp(prefix=".auth.json.", dir=real.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(current)
            os.chmod(part, 0o600)
            os.replace(part, real)
        except BaseException:
            Path(part).unlink(missing_ok=True)
            raise
    finally:
        os.close(lock)   # releases the flock


CODEX_METERED = "codex login is not a ChatGPT subscription (API key or other auth_mode); it bills per token"


def _subscription_login(auth) -> bool:
    """A ChatGPT subscription login with no API key: the only Codex login a run may use (zero-cost mode)."""
    return isinstance(auth, dict) and not auth.get("OPENAI_API_KEY") and auth.get("auth_mode") in ("chatgpt", None)


class CodexProvider(Provider):
    """`codex exec` in the read-only sandbox, ephemeral, with an output schema."""

    def __init__(self, model: str | None = None, **kw):
        super().__init__(name="codex", binary="codex", model=model, **kw)

    def run(self, prompt, schema, *, cwd, timeout_s, max_turns) -> ProviderResult:
        # The refusal boundary covers the whole call (health check included): any failure leaves as the fixed
        # string, never as an exception whose text could carry login or run content.
        try:
            return super().run(prompt, schema, cwd=cwd, timeout_s=timeout_s, max_turns=max_turns)
        except Exception:  # noqa: BLE001
            return ProviderResult(self.name, Health.BROKEN, error=CODEX_UNGUARDED)

    def _auth_check(self):
        # Swap first: the login is never read into a process whose memory could be swapped to disk.
        if not swap_off():
            return Health.BROKEN, CODEX_SWAPPABLE
        try:
            auth = json.loads((self._home() / "auth.json").read_text())
        except (OSError, ValueError):   # ValueError: bad JSON or undecodable bytes
            return Health.AUTH_REQUIRED, "codex is not logged in (no auth.json)"
        if not isinstance(auth, dict):
            return Health.AUTH_REQUIRED, "codex auth.json is not a login object"
        # Fixed strings only: no value from auth.json (a malformed auth_mode can hold a token) reaches a message.
        # Only the mode and key presence are read; token values never leave the file.
        if not _subscription_login(auth):
            return Health.METERED_BLOCKED, CODEX_METERED
        # auth.json proves only that subscription credentials are configured. Expiry,
        # entitlement and model reachability require an explicit provider run.
        return Health.CREDENTIALS_CONFIGURED, "ChatGPT credentials found; live model access is unverified"

    def _home(self) -> Path:
        return Path(self.environ.get("CODEX_HOME") or Path(self.environ.get("HOME", "~")) / ".codex").expanduser()

    @staticmethod
    def _credential_windows_at(home: Path) -> set[str] | None:
        try:
            return CodexProvider._credential_windows_of((home / "auth.json").read_bytes())
        except OSError:
            return None

    @staticmethod
    def _credential_windows_of(login: bytes) -> set[str] | None:
        """Every CREDENTIAL_WINDOW-long slice of the secret part of each token/key value in auth.json, computed here
        in the parent and never logged; None when the file cannot be read or holds no such value (the caller fails
        closed). A JWT contributes its signature only: its header and claims are base64 of JSON shared by every
        OpenAI token (`{"alg":"RS256"...`, aud, client_id), so slicing them refuses reviews that quote a public
        token; the signature is what makes it usable. Opaque values contribute whole. Timestamps and ids are skipped."""
        try:
            auth = json.loads(login)
        except ValueError:
            return None
        values, stack = [], [("", auth)]
        while stack:
            key, v = stack.pop()
            if isinstance(v, dict):
                stack += v.items()
            elif isinstance(v, str) and len(v) >= CREDENTIAL_WINDOW and re.search(r"(?i)token|key|secret", key):
                parts = v.split(".")
                values.append(parts[2] if len(parts) == 3 and parts[0].startswith("eyJ") else v)
        windows = {v[i:i + CREDENTIAL_WINDOW] for v in values for i in range(len(v) - CREDENTIAL_WINDOW + 1)}
        return windows or None

    def _run(self, prompt, schema, *, cwd, timeout_s, max_turns):
        # max_turns: `codex exec` (0.166.0) has no turn-limit flag or config key; one exec is one user turn and the
        # runner's timeout_s kill is the bound. With NO_TOOLS there are no tool round-trips to bound.
        # Snapshot auth.json before the run (a token rotated mid-run is still caught) and after it, also when the
        # runner raised; either snapshot unreadable means no check, so the run is refused rather than trusted.
        # Nothing from the run (final message, events, stderr, exit code, exception text) is returned before the
        # check; a refusal is a fixed string. No `-o`: the final message is taken from the event stream in memory.
        #
        # CODEX_HOME, HOME and TMPDIR point into a fresh directory on verified non-persistent storage. Cleanup is
        # attempted on every exit path; a failed removal withholds the result and reports only safe path IDs.
        # Even with --ephemeral (no sessions/ rollout), codex 0.166.0 writes logs_*.sqlite, models_cache.json
        # and skills/ under CODEX_HOME (observed 2026-11-03), so the real CODEX_HOME is never given to the child: it
        # gets a copy of auth.json, and only a login Codex refreshed during the run is written back.
        if not swap_off():   # before any read of the login
            return ProviderResult(self.name, Health.BROKEN, error=CODEX_SWAPPABLE)
        real = self._home()
        try:
            # One read: the slices are taken from exactly the bytes the run gets, so a rotation between a snapshot
            # and the copy cannot leave the copied login unguarded.
            login = (real / "auth.json").read_bytes()
        except OSError:
            login = b""
        before = self._credential_windows_of(login)
        if before is None:
            return ProviderResult(self.name, Health.BROKEN, error=CODEX_UNGUARDED)
        # The subscription gate again, on exactly these bytes: a switch to an API key after the health check must
        # not reach the run.
        if not _subscription_login(json.loads(login)):
            return ProviderResult(self.name, Health.METERED_BLOCKED, error=CODEX_METERED)
        root = volatile_root(self.environ)
        if root is None:
            return ProviderResult(self.name, Health.BROKEN, error=CODEX_NO_TMPFS)
        ran = after = None
        swapped = False
        try:
            run_dir = Path(tempfile.mkdtemp(prefix="codex-run-", dir=root))
        except OSError:
            return ProviderResult(self.name, Health.BROKEN, error=CODEX_UNGUARDED)
        try:
            home, tmp = run_dir / "codex-home", run_dir / "tmp"
            home.mkdir(mode=0o700)
            tmp.mkdir(mode=0o700)
            fd = os.open(home / "auth.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(login)
            schema_path = run_dir / "schema.json"
            schema_path.write_text(json.dumps(schema))
            argv = [self.binary, "exec", "--sandbox", "read-only", "--ephemeral",
                    "--skip-git-repo-check", "--json", *NO_TOOLS, "-C", cwd, "--output-schema", str(schema_path)]
            if self.model:
                argv += ["-m", self.model]
            # "-": the prompt is read from stdin. One argv string is capped at 128 KiB on Linux and a release
            # review packet is larger (B-14, 2026-10-23); see ClaudeCodeProvider.
            argv.append("-")
            env = child_env(self.environ) | {"CODEX_HOME": str(home), "HOME": str(run_dir), "TMPDIR": str(tmp)}
            try:
                ran = self.runner(argv, cwd, timeout_s, env, prompt)
            except Exception:  # noqa: BLE001 — its text may be run output; only the fixed refusal leaves
                ran = None
            if not swap_off():   # rechecked before every later read of a login
                raise _Swappable
            after = self._credential_windows_at(home)
            if after is not None:
                _keep_refreshed_login(home / "auth.json", login, real / "auth.json")
        except _Swappable:
            swapped = True
        except Exception:  # noqa: BLE001 — setup or login write-back failed: refused, never echoed
            ran = None
        finally:
            cleanup_error = _cleanup_codex_run(run_dir)
        if cleanup_error:
            return ProviderResult(self.name, Health.BROKEN, error=cleanup_error)
        if swapped:
            return ProviderResult(self.name, Health.BROKEN, error=CODEX_SWAPPABLE)
        if ran is None or after is None:
            return ProviderResult(self.name, Health.BROKEN, error=CODEX_UNGUARDED)
        try:
            code, out, err = ran
            if _holds_slice(_leak_forms(out or "", err or ""), before | after):
                return ProviderResult(self.name, Health.AVAILABLE, error=CODEX_LEAK)
            return self._result(code, out or "", err or "", timeout_s)
        except Exception:  # noqa: BLE001 — malformed output: refused without echoing it
            return ProviderResult(self.name, Health.BROKEN, error=CODEX_UNGUARDED)

    def _result(self, code, out, err, timeout_s) -> ProviderResult:
        """Only after the leak check: events and the final message (the last agent_message) to a ProviderResult."""
        if code is None:
            return ProviderResult(self.name, Health.AVAILABLE, error=f"timeout after {timeout_s}s")
        res, final = ProviderResult(self.name, Health.AVAILABLE, exit_code=code, model=self.model), None
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
            if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
                final = item.get("text")
            if ev.get("type") in ("error", "turn.failed"):
                res.error = str(ev.get("message") or (ev.get("error") or {}).get("message"))[:500]
            if ev.get("type") == "turn.completed":
                usage = ev.get("usage") or {}
                res.input_tokens = usage.get("input_tokens")
                res.output_tokens = usage.get("output_tokens")
        if res.error or code != 0:
            res.error = res.error or (err.strip()[-500:] or f"exit {code}")
            health = classify(res.error)
            if health == Health.QUOTA_EXHAUSTED:
                res.error = quota_message(res.error)
            res.health = Health.AVAILABLE if health == Health.BROKEN else health
            return res
        try:
            output = json.loads(final) if isinstance(final, str) else None
        except json.JSONDecodeError:
            output = None
        if isinstance(output, dict):
            res.output = output
        else:
            res.error = "codex returned no parseable final message"
        return res


class RemoteProvider(Provider):
    """A provider whose CLI runs in company-reasoner (app/reasoner.py), not in this process.
    Same contract as a local one; an unreachable reasoner is BROKEN, so the worker falls
    back or waits instead of failing the task."""

    def __init__(self, name: str, url: str, client=None, **kw):
        super().__init__(name=name, binary=name, **kw)
        self.url = url.rstrip("/")
        self.client = client

    def _http(self):
        import httpx  # the worker image has it; tests pass a client
        return self.client or httpx.Client()

    def _remote_health(self) -> dict:
        try:
            r = self._http().get(f"{self.url}/health", timeout=15)
            r.raise_for_status()
            return r.json().get(self.name) or {"health": "DISABLED", "why": "reasoner has no such provider"}
        except Exception as e:  # noqa: BLE001 — any transport failure means "not reachable"
            return {"health": "BROKEN", "why": f"reasoner unreachable: {type(e).__name__}"}

    def health_check(self) -> tuple[Health, str | None]:
        found = metered_env(self.environ)  # still refused here, before any request leaves
        if found:
            return Health.METERED_BLOCKED, f"metered credentials present: {', '.join(found)}"
        h = self._remote_health()
        return Health(h["health"]), h.get("why")

    def version(self) -> str | None:
        return self._remote_health().get("version")

    def _run(self, prompt, schema, *, cwd, timeout_s, max_turns) -> ProviderResult:
        body = {"provider": self.name, "prompt": prompt, "schema": schema,
                "timeout_s": timeout_s, "max_turns": max_turns}
        try:
            r = self._http().post(f"{self.url}/run", json=body, timeout=timeout_s + 60)
            r.raise_for_status()
            data = r.json()
            return ProviderResult(**{**data, "health": Health(data["health"])})
        except Exception as e:  # noqa: BLE001
            return ProviderResult(self.name, Health.BROKEN, error=f"reasoner: {type(e).__name__}: {str(e)[:300]}")


def default_providers(environ: dict | None = None) -> dict[str, Provider]:
    env = dict(os.environ if environ is None else environ)
    if env.get("REASONER_URL"):  # production: the CLIs live in company-reasoner
        return {n: RemoteProvider(n, env["REASONER_URL"], environ=env) for n in ("claude", "codex")}
    return {
        "claude": ClaudeCodeProvider(model=env.get("WORKER_CLAUDE_MODEL", "sonnet"), environ=env),
        "codex": CodexProvider(model=env.get("WORKER_CODEX_MODEL") or None, environ=env),
    }
