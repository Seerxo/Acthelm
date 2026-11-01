"""Release review signer (F1): the only holder of the reviewer private key. Runs no model and accepts no verdict.

Why a separate container: in company-reasoner the key would sit beside model CLIs that the worker can drive through
the unauthenticated /run; a read-only sandbox still reads files, so the key could be exfiltrated. Here:

- no model CLI, no npm, no Docker socket, read-only root, all capabilities dropped, its own uid (10002), its own
  internal network shared only with company-reasoner (the worker is not on it), no route to the internet;
- the gateway reaches it through a Unix socket in a host directory no agent container mounts;
- each request must be signed by the gateway key (namespace seerxo-review-request), be fresh, and carry a nonce
  never seen before; only then does the signer build the review itself (fixed RELEASE_REVIEW_PROMPT + the
  packet), ask company-reasoner, validate the structured answer strictly, and sign the attestation.

Nothing a caller sends can become the verdict: the request has no verdict field, and the reasoner's answer is
accepted only if it comes from the reviewer provider, matches the schema and copies this packet's fingerprint.

The reviewer is fixed in this reviewed code, never chosen on the host (a root-chosen reviewer was withdrawn on
2026-10-23): the provider asked is REVIEW_PROVIDER and the identity signed is REVIEWER_ID, which must be enrolled in
the founder-signed policy and differ from every coder identity (release_gateway.verify_review). Each verdict's
findings are appended to FINDINGS_LOG (0600, the signer's own volume), never the prompt, diff or any key;
if that record cannot be written, nothing is signed. Standard library only.

Usage (container): python -m app.review_signer
"""
import json
import os
import socketserver
import stat
import sys
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from app import release_gateway as rg

MAX_BODY = 4_000_000
REVIEW_TIMEOUT_S = 900
REVIEW_PROVIDER = "claude"            # the model CLI company-reasoner runs for the review
REVIEWER_ID = "claude-reviewer"       # the identity signed into the attestation; never a coder identity
MAX_FINDINGS, MAX_FINDING_CHARS = 50, 4000
FINDINGS_LOG = "/var/lib/review-signer/findings.jsonl"   # the signer's own volume (compose); fixed like the reviewer
REQUEST_FIELDS = {"packet", "issued_at", "request_signature"}


class Signer:
    def __init__(self, key_path: str, requester_key: str | None, ask, clock=None, findings_log: str | None = None,
                 require_findings_log: bool = False):
        self.key_path, self.requester_key, self.ask = Path(key_path), requester_key, ask
        self.require_findings_log = require_findings_log
        self.provider, self.reviewer_id = REVIEW_PROVIDER, REVIEWER_ID
        self.findings_log = Path(findings_log) if findings_log else None
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.nonces: dict[str, float] = {}
        self.lock = threading.Lock()

    def key_ready(self) -> str | None:
        """Why the key must not be used, or None. Only a private regular file owned by this uid counts."""
        try:
            st = os.lstat(self.key_path)
        except OSError:
            return "review signing key is not installed"
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            return "review signing key must be a regular file owned by the signer with mode 0600 or 0400"
        if not self.requester_key:
            return "gateway requester key is not installed"
        if self.require_findings_log:
            if self.findings_log is None:
                return "the review findings log is not configured: verdicts would go unrecorded"
            fd = self._open_log()                    # before the model is asked: a 503 is retried, never final
            if fd is None:
                return "the review findings log cannot be written safely (missing directory, owner, mode or link)"
            os.close(fd)
        return None

    def _open_log(self) -> int | None:
        """The log opened for appending, or None. Never follows a link; only a regular file with one name, owned by
        this uid and private to it, is used. A newly created file's directory entry is made durable."""
        try:
            # O_NONBLOCK: a FIFO planted at the path fails at once (ENXIO) instead of blocking the signer forever.
            fd = os.open(self.findings_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600)
        except OSError:
            return None
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            os.close(fd)
            return None
        if info.st_size == 0:
            try:
                dfd = os.open(self.findings_log.parent, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
        return fd

    def handle(self, body) -> tuple[int, dict]:
        problem = self.key_ready()
        if problem:
            return 503, {"error": problem}
        if not isinstance(body, dict) or set(body) != REQUEST_FIELDS or not rg.valid_packet(body["packet"]):
            return 400, {"error": "a review request is exactly {packet, issued_at, request_signature}, well-formed"}
        packet, asked_at, now = body["packet"], body["issued_at"], self.clock()
        try:
            age = (now - datetime.fromisoformat(asked_at)).total_seconds()
        except (TypeError, ValueError):
            age = None
        if (age is None or not -rg.CLOCK_SKEW_S <= age <= rg.CLOCK_SKEW_S
                or not rg.verify(rg.review_request(packet, asked_at), body["request_signature"], self.requester_key,
                                 rg.NS_REVIEW_REQUEST)):
            return 403, {"error": "review requests must be signed by the gateway and recent"}
        with self.lock:                                    # check-and-set atomically under ThreadingMixIn
            stamp = now.timestamp()
            for old in [n for n, at in self.nonces.items() if stamp - at > 2 * rg.CLOCK_SKEW_S]:
                del self.nonces[old]
            if packet["nonce"] in self.nonces:
                return 409, {"error": "nonce already used"}
            self.nonces[packet["nonce"]] = stamp
        try:
            result = self.ask({"provider": self.provider, "prompt": rg.RELEASE_REVIEW_PROMPT + json.dumps(packet),
                               "schema": rg.RELEASE_REVIEW_SCHEMA, "timeout_s": REVIEW_TIMEOUT_S, "max_turns": 1})
        except Exception as error:   # the reviewer is unreachable: no verdict, nothing signed
            return 502, {"error": f"reviewer unreachable: {type(error).__name__}"}
        # company-reasoner /run returns asdict(ProviderResult) + health, which has no "ok" key (ok is a property);
        # derive it exactly as ProviderResult.ok does (app/providers.py, keep in step): AVAILABLE and output present.
        ok = (isinstance(result, dict) and result.get("health") == "AVAILABLE" and result.get("output") is not None
              and result.get("provider") == self.provider)        # the answer comes from the reviewer's own CLI
        output = result.get("output") if ok else None
        if not rg.valid_review_output(output, packet):
            return 502, {"error": "reviewer gave no valid verdict for this packet; nothing signed"}
        att = rg.attestation(self.reviewer_id, packet, output, self.clock().isoformat())
        if not self.record(packet, att, output, result.get("model")):
            return 502, {"error": "the review record could not be written; nothing signed"}
        return 200, {"attestation": att, "signature": rg.sign(rg.canonical(att), self.key_path, rg.NS_REVIEW)}

    def record(self, packet: dict, att: dict, output: dict, model=None) -> bool:
        """Append why the reviewer decided (verdict, findings size-capped, the model that answered) to the signer's
        own 0600 log, before any signature: it lists verdicts given, not only attestations signed. The diff, the
        prompt and keys are never written. No log configured (tests) records nothing."""
        if self.findings_log is None:
            return True
        findings = [str(f)[:MAX_FINDING_CHARS] for f in (output.get("findings") or [])[:MAX_FINDINGS]]
        entry = {"at": att["issued_at"], "reviewer": att["reviewer"], "verdict": att["verdict"],
                 "base": packet["base"], "sha": packet["sha"], "nonce": packet["nonce"],
                 "fingerprint": packet["fingerprint"], "packet_sha256": att["packet_sha256"], "findings": findings,
                 "model": model if isinstance(model, str) else None}
        fd = self._open_log()
        if fd is None:
            return False
        try:
            try:
                data = (json.dumps(entry, sort_keys=True) + "\n").encode()
                while data:                                   # a short write is retried until all of it is written
                    data = data[os.write(fd, data):]
                os.fsync(fd)                                  # on disk before anything is signed
            finally:
                os.close(fd)
        except OSError:
            return False
        return True


def reasoner_ask(url: str):
    def ask(body: dict) -> dict:
        req = urllib.request.Request(url.rstrip("/") + "/run", data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=REVIEW_TIMEOUT_S + 60) as resp:
            return json.loads(resp.read())
    return ask


def handler(signer: Signer):
    class Handler(BaseHTTPRequestHandler):
        def address_string(self):           # Unix socket peers have no host/port
            return "unix"

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path != "/review":
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("content-length") or 0)
            if not 0 < length <= MAX_BODY:
                return self._send(413, {"error": "body missing or too large"})
            try:
                body = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError):
                return self._send(400, {"error": "body is not JSON"})
            self._send(*signer.handle(body))

        def log_message(self, fmt, *args):   # never the packet, never a key path
            print(f"review-signer {self.command} {self.path} {args[1] if len(args) > 1 else ''}", flush=True)

    return Handler


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(socket_path: str, signer: Signer) -> UnixHTTPServer:
    path = Path(socket_path)
    if path.exists() or path.is_symlink():
        path.unlink()
    server = UnixHTTPServer(str(path), handler(signer))
    os.chmod(path, 0o660)            # the gateway user reaches it through the shared group; others cannot
    return server


def main() -> int:
    key, requester = os.environ.get("REVIEW_SIGNING_KEY", ""), os.environ.get("REVIEW_REQUESTER_KEY", "")
    requester_key = Path(requester).read_text().strip() if requester and Path(requester).is_file() else None
    signer = Signer(key, requester_key, reasoner_ask(os.environ["REASONER_URL"]),
                    findings_log=FINDINGS_LOG, require_findings_log=True)
    if signer.key_ready():
        print(f"review-signer: {signer.key_ready()}; requests will be refused", file=sys.stderr)
    serve(os.environ["REVIEW_SIGNER_SOCKET"], signer).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
