#!/usr/bin/env python3
"""smarthome-secrets: systemd-creds backed secrets for the ~/smarthome compose stacks.

One stdlib-only module, installed once and invoked through four names
(argv[0] dispatch, or the first argument when run as smarthome_secrets.py):

  smarthome-secret set <svc> <NAME>      value from stdin (or a hidden prompt on a TTY)
  smarthome-secret list [<svc>]          names only
  smarthome-secret rm <svc> <NAME>
  smarthome-secrets apply|refresh|status decrypt every credential into /run (tmpfs)
  smarthome-secrets-migrate <svc> [--dry-run] [--force] [--overwrite]
  smarthome-secrets-rollback <svc> [--dry-run] [--purge]

Hard rules (enforced, and covered by tests):
  * a secret VALUE is never accepted on argv, never read from the environment,
    never written to a log/stdout/stderr, never stored unencrypted outside the
    root-only tmpfs runtime dir (and the 0600 .env.pre-secrets* backups);
  * credential names are validated with a strict regex (no path traversal);
  * every error message carries the credential NAME only.
"""
from __future__ import annotations

import errno
import getpass
import os
import re
import shutil
import stat
import subprocess
import sys
import time

# --------------------------------------------------------------------------- config

TEST_MODE = os.environ.get("SMARTHOME_SECRETS_TEST") == "1"


def _cfg(name: str, default: str) -> str:
    """Path/config overrides are honoured in test mode only (sudo resets env anyway)."""
    if TEST_MODE:
        return os.environ.get(name, default)
    return default


ETC_DIR = _cfg("SMARTHOME_SECRETS_ETC", "/etc/smarthome-secrets")
CREDSTORE = _cfg("SMARTHOME_SECRETS_CREDSTORE", "/etc/credstore.encrypted/smarthome")
RUNDIR = _cfg("SMARTHOME_SECRETS_RUNDIR", "/run/smarthome-secrets")
SYSTEMD_CREDS = _cfg("SMARTHOME_SECRETS_SYSTEMD_CREDS", "systemd-creds")
REQUIRE_TMPFS = _cfg("SMARTHOME_SECRETS_REQUIRE_TMPFS", "1") == "1"
UNIT = "smarthome-secrets.service"

SVC_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MAX_VALUE_BYTES = 64 * 1024


class SecretsError(Exception):
    """User-facing error. Messages never contain a secret value."""


def log(msg: str) -> None:
    print(f"smarthome-secrets: {msg}", file=sys.stderr, flush=True)


def read_kv_config() -> dict:
    """/etc/smarthome-secrets/config: KEY=VALUE lines (REPO_ROOT, WITH_KEY, COMPOSE_CMD)."""
    out = {"REPO_ROOT": "/home/ben/smarthome", "WITH_KEY": "host", "COMPOSE_CMD": "docker compose"}
    path = os.path.join(ETC_DIR, "config")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    if out["WITH_KEY"] not in ("host", "auto", "tpm2", "host+tpm2"):
        raise SecretsError(f"{path}: WITH_KEY must be host, auto, tpm2 or host+tpm2")
    return out


def require_root() -> None:
    if os.geteuid() != 0 and not TEST_MODE:
        raise SecretsError("must run as root (use sudo)")


def validate(svc: str, name: str | None = None) -> None:
    if not SVC_RE.fullmatch(svc or ""):
        raise SecretsError("invalid service name (want ^[a-z][a-z0-9-]{0,31}$)")
    if name is not None and not NAME_RE.fullmatch(name or ""):
        raise SecretsError("invalid secret NAME (want ^[A-Z][A-Z0-9_]{0,63}$)")


def cred_path(svc: str, name: str) -> str:
    validate(svc, name)
    return os.path.join(CREDSTORE, f"{svc}.{name}")


def parse_cred_filename(fname: str):
    if "." not in fname:
        return None
    svc, name = fname.split(".", 1)
    if SVC_RE.fullmatch(svc) and NAME_RE.fullmatch(name):
        return svc, name
    return None


def list_creds(svc: str | None = None):
    if svc is not None:
        validate(svc)
    try:
        entries = sorted(os.listdir(CREDSTORE))
    except FileNotFoundError:
        return []
    out = []
    for f in entries:
        parsed = parse_cred_filename(f)
        if not parsed or not os.path.isfile(os.path.join(CREDSTORE, f)):
            continue
        if svc is None or parsed[0] == svc:
            out.append(parsed)
    return out


# --------------------------------------------------------------------------- systemd-creds


def _ensure_credstore() -> None:
    parent = os.path.dirname(CREDSTORE)
    for d in (parent, CREDSTORE):
        if not os.path.isdir(d):
            os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)


def encrypt_value(svc: str, name: str, value: bytes) -> None:
    """Encrypt ``value`` (passed via the systemd-creds stdin pipe) to the credstore."""
    check_value(name, value)
    _ensure_credstore()
    final = cred_path(svc, name)
    tmp = os.path.join(CREDSTORE, f".tmp-{svc}.{name}.{os.getpid()}")
    with_key = read_kv_config()["WITH_KEY"]
    cmd = [SYSTEMD_CREDS, "encrypt", f"--with-key={with_key}", f"--name={svc}.{name}", "-", tmp]
    try:
        proc = subprocess.run(cmd, input=value, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if proc.returncode != 0 or not os.path.isfile(tmp):
            raise SecretsError(f"systemd-creds encrypt failed for {svc}.{name} (rc={proc.returncode})")
        os.chmod(tmp, 0o600)
        os.replace(tmp, final)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def decrypt_value(svc: str, name: str) -> bytes:
    path = cred_path(svc, name)
    cmd = [SYSTEMD_CREDS, "decrypt", f"--name={svc}.{name}", path, "-"]
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        # stderr of systemd-creds names the file/reason, never the plaintext.
        raise SecretsError(f"decrypt failed for {svc}.{name} (rc={proc.returncode})")
    return proc.stdout


def check_value(name: str, value: bytes) -> None:
    if not value:
        raise SecretsError(f"empty value for {name}; refusing")
    if len(value) > MAX_VALUE_BYTES:
        raise SecretsError(f"value for {name} is larger than {MAX_VALUE_BYTES} bytes; refusing")
    if b"\0" in value:
        raise SecretsError(f"value for {name} contains a NUL byte (cannot be an env var); refusing")


# --------------------------------------------------------------------------- smarthome-secret


def read_value_from_stdin(name: str) -> bytes:
    if sys.stdin.isatty():
        first = getpass.getpass(f"value for {name} (hidden): ")
        second = getpass.getpass(f"repeat {name} (hidden): ")
        if first != second:
            raise SecretsError("the two entries differ; nothing stored")
        return first.encode("utf-8")
    data = sys.stdin.buffer.read(MAX_VALUE_BYTES + 2)
    # Strip exactly one trailing newline (what `printf '%s\n'` / `echo` / a file adds).
    if data.endswith(b"\r\n"):
        data = data[:-2]
    elif data.endswith(b"\n"):
        data = data[:-1]
    return data


USAGE_SECRET = """usage:
  smarthome-secret set <svc> <NAME>   (value on stdin, or a hidden prompt on a TTY)
  smarthome-secret list [<svc>]       (names only)
  smarthome-secret rm <svc> <NAME>
Values are NEVER accepted on the command line."""


def cmd_secret(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE_SECRET)
        return 0 if argv else 2
    sub, args = argv[0], argv[1:]
    if sub == "set":
        if len(args) != 2:
            # Do not echo args back: an extra arg may be a value pasted by mistake.
            raise SecretsError(
                "set takes exactly <svc> <NAME>; values are never accepted on the command line "
                "(pipe it on stdin or answer the hidden prompt). Nothing stored."
            )
        svc, name = args
        validate(svc, name)
        require_root()
        value = read_value_from_stdin(name)
        encrypt_value(svc, name, value)
        print(f"stored {svc}.{name}  (run: sudo smarthome-secrets refresh, then restart the container)")
        return 0
    if sub == "list":
        if len(args) > 1:
            raise SecretsError("list takes at most one <svc>")
        require_root()
        for svc, name in list_creds(args[0] if args else None):
            print(f"{svc} {name}")
        return 0
    if sub == "rm":
        if len(args) != 2:
            raise SecretsError("rm takes exactly <svc> <NAME>")
        svc, name = args
        validate(svc, name)
        require_root()
        path = cred_path(svc, name)
        if not os.path.exists(path):
            raise SecretsError(f"no such credential {svc}.{name}")
        os.unlink(path)
        rt_dir = os.path.join(RUNDIR, svc)
        rt = os.path.join(rt_dir, name)
        if os.path.lexists(rt):  # drop the plaintext copy now, not at the next refresh
            os.chmod(rt_dir, 0o700)
            os.unlink(rt)
            os.chmod(rt_dir, 0o500)
        print(f"removed {svc}.{name}")
        return 0
    raise SecretsError(f"unknown subcommand (see --help)")


# --------------------------------------------------------------------------- apply / refresh


def load_owners() -> dict:
    """owners.conf: '<svc|*> <uid>:<gid>' per line. Default '*' = 0:0 (root)."""
    owners = {"*": (0, 0)}
    path = os.path.join(ETC_DIR, "owners.conf")
    try:
        with open(path, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                parts = line.split()
                m = re.fullmatch(r"(\d{1,10}):(\d{1,10})", parts[1]) if len(parts) == 2 else None
                if not m or not (parts[0] == "*" or SVC_RE.fullmatch(parts[0])):
                    raise SecretsError(f"{path}:{n}: want '<svc|*> <uid>:<gid>'")
                owners[parts[0]] = (int(m.group(1)), int(m.group(2)))
    except FileNotFoundError:
        pass
    return owners


def fs_type(path: str) -> str:
    """Filesystem type of the mount containing ``path`` (from /proc/self/mounts)."""
    path = os.path.realpath(path)
    best, best_type = "", ""
    try:
        with open("/proc/self/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt = parts[1].replace("\\040", " ")
                if (path == mnt or path.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
                    best, best_type = mnt, parts[2]
    except FileNotFoundError:
        return ""
    return best_type


def _write_runtime_file(svc_dir: str, name: str, value: bytes, uid: int, gid: int) -> None:
    final = os.path.join(svc_dir, name)
    tmp = os.path.join(svc_dir, f".tmp-{name}.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    try:
        os.fchmod(fd, 0o400)
        if os.geteuid() == 0:
            os.fchown(fd, uid, gid)
        view = memoryview(value)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(tmp)
        raise
    os.close(fd)
    os.replace(tmp, final)


def apply_all() -> int:
    """Decrypt every credential into RUNDIR/<svc>/<NAME> (0400, owner from owners.conf).

    Idempotent; removes orphans; on a decrypt failure logs the NAME, keeps the
    previous runtime file (if any) and continues; returns 1 if anything failed.
    """
    require_root()
    os.makedirs(RUNDIR, mode=0o700, exist_ok=True)
    os.chmod(RUNDIR, 0o700)
    if REQUIRE_TMPFS and fs_type(RUNDIR) not in ("tmpfs", "ramfs"):
        raise SecretsError(f"{RUNDIR} is not on tmpfs; refusing to write plaintext there")
    owners = load_owners()
    creds = list_creds()
    expected: dict[str, set] = {}
    failures = 0
    written = 0
    for svc, name in creds:
        expected.setdefault(svc, set()).add(name)
        uid, gid = owners.get(svc, owners["*"])
        svc_dir = os.path.join(RUNDIR, svc)
        if os.path.islink(svc_dir) or (os.path.exists(svc_dir) and not os.path.isdir(svc_dir)):
            os.unlink(svc_dir)
        os.makedirs(svc_dir, mode=0o700, exist_ok=True)
        os.chmod(svc_dir, 0o700)  # writable while we work; tightened to 0500 below
        try:
            value = decrypt_value(svc, name)
            check_value(name, value)
        except SecretsError as exc:
            failures += 1
            log(f"FAILED {svc}.{name}: {exc}")
            continue
        _write_runtime_file(svc_dir, name, value, uid, gid)
        del value
        written += 1
    # Orphans: anything in RUNDIR that no longer has a credential.
    removed = 0
    for entry in os.listdir(RUNDIR):
        p = os.path.join(RUNDIR, entry)
        if entry not in expected or not os.path.isdir(p) or os.path.islink(p):
            if os.path.isdir(p) and not os.path.islink(p):
                os.chmod(p, 0o700)
                shutil.rmtree(p)
            else:
                os.unlink(p)
            removed += 1
            continue
        for f in os.listdir(p):
            if f not in expected[entry]:
                os.unlink(os.path.join(p, f))
                removed += 1
        os.chmod(p, 0o500)
    log(f"applied {written} secret(s), {failures} failure(s), {removed} orphan(s) removed")
    return 1 if failures else 0


def systemd_running() -> bool:
    return os.path.isdir("/run/systemd/system") and not TEST_MODE


def cmd_secrets(argv: list[str]) -> int:
    sub = argv[0] if argv else "--help"
    if sub == "apply":
        return apply_all()
    if sub == "refresh":
        require_root()
        if systemd_running():
            rc = subprocess.run(["systemctl", "restart", UNIT]).returncode
            if rc != 0:
                log(f"{UNIT} failed; see: journalctl -u {UNIT} -n 50")
            return rc
        return apply_all()
    if sub == "status":
        require_root()
        for svc, name in list_creds():
            p = os.path.join(RUNDIR, svc, name)
            try:
                st = os.stat(p)
                print(f"{svc} {name} runtime={oct(st.st_mode & 0o777)} owner={st.st_uid}:{st.st_gid}")
            except FileNotFoundError:
                print(f"{svc} {name} runtime=MISSING (run: sudo smarthome-secrets refresh)")
        return 0
    print("usage: smarthome-secrets apply|refresh|status")
    return 0 if sub in ("-h", "--help", "help") else 2


# --------------------------------------------------------------------------- manifest


def parse_manifest_text(text: str) -> dict:
    """Parse the small YAML subset used by secrets/manifest.yaml.

    Supported: nested mappings by indentation, '- item' lists, inline [a, b]
    lists, plain or quoted scalars, '#' comments. Anything else is an error.
    """
    lines = []
    for raw in text.splitlines():
        stripped = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if stripped.strip():
            indent = len(stripped) - len(stripped.lstrip(" "))
            lines.append((indent, stripped.strip()))

    def scalar(v: str):
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            return v[1:-1]
        if v.startswith("[") and v.endswith("]"):
            return [scalar(x) for x in v[1:-1].split(",") if x.strip()]
        return v

    def block(i: int, indent: int):
        if i < len(lines) and lines[i][1].startswith("- "):
            out = []
            while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
                out.append(scalar(lines[i][1][2:]))
                i += 1
            return out, i
        out = {}
        while i < len(lines) and lines[i][0] == indent:
            content = lines[i][1]
            if ":" not in content:
                raise SecretsError(f"manifest: cannot parse line {content!r}")
            key, rest = content.split(":", 1)
            key = key.strip()
            i += 1
            if rest.strip():
                out[key] = scalar(rest)
            elif i < len(lines) and lines[i][0] > indent:
                out[key], i = block(i, lines[i][0])
            elif i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
                out[key], i = block(i, indent)
            else:
                out[key] = None
        return out, i

    data, i = block(0, 0)
    if i != len(lines):
        raise SecretsError("manifest: unexpected indentation")
    return data


def load_manifest(path: str | None = None) -> dict:
    path = path or os.path.join(ETC_DIR, "manifest.yaml")
    with open(path, encoding="utf-8") as fh:
        data = parse_manifest_text(fh.read())
    services = data.get("services") or {}
    for svc, entry in services.items():
        validate(svc)
        for n in entry.get("names") or []:
            validate(svc, n)
        if entry.get("status") not in ("ready", "needs-review", "plan-only"):
            raise SecretsError(f"manifest: {svc}: bad status")
    return services


# --------------------------------------------------------------------------- .env handling

ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def parse_env_value(name: str, raw: str) -> str:
    """Return the value docker compose would read for this .env line.

    Refuses (NAME only in the message) anything whose meaning is ambiguous:
    '$' interpolation in unquoted/double-quoted values, unknown escapes,
    multi-line or unterminated quotes.
    """
    v = raw.strip()
    if v.startswith("'"):
        end = v.find("'", 1)
        if end == -1 or v[end + 1:].strip() not in ("",) and not v[end + 1:].strip().startswith("#"):
            raise SecretsError(f"{name}: unsupported single-quoted value in .env")
        return v[1:end]
    if v.startswith('"'):
        out, i = [], 1
        while i < len(v):
            c = v[i]
            if c == "\\" and i + 1 < len(v):
                nxt = v[i + 1]
                mapping = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"'}
                if nxt not in mapping:
                    raise SecretsError(f"{name}: unsupported escape in double-quoted .env value")
                out.append(mapping[nxt])
                i += 2
                continue
            if c == '"':
                rest = v[i + 1:].strip()
                if rest and not rest.startswith("#"):
                    raise SecretsError(f"{name}: trailing data after quoted .env value")
                val = "".join(out)
                if "$" in val:
                    raise SecretsError(f"{name}: contains '$' (compose would interpolate); migrate by hand")
                return val
            out.append(c)
            i += 1
        raise SecretsError(f"{name}: unterminated double-quoted .env value")
    # unquoted: compose strips an inline ' #comment'
    m = re.search(r"\s#", v)
    if m:
        v = v[: m.start()]
    v = v.strip()
    if "$" in v:
        raise SecretsError(f"{name}: contains '$' (compose would interpolate); migrate by hand")
    return v


def read_env_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8") as fh:
        return fh.read().splitlines(keepends=True)


def env_lookup(lines: list[str]) -> dict:
    """name -> (index, raw value). Last definition wins (compose semantics)."""
    found = {}
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        m = ENV_LINE_RE.match(line.rstrip("\r\n"))
        if m:
            found[m.group(1)] = (i, m.group(2))
    return found


def write_atomic_like(path: str, lines: list[str], like: str | None = None, mode: int | None = None) -> None:
    ref = os.stat(like or path)
    tmp = f"{path}.smarthome-secrets.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("".join(lines))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode if mode is not None else stat.S_IMODE(ref.st_mode))
        if os.geteuid() == 0:
            os.chown(tmp, ref.st_uid, ref.st_gid)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


COMPOSE_MARKER = "# smarthome-secrets: COMPOSE_FILE added by smarthome-secrets-migrate (rollback removes it)\n"
BASE_COMPOSE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")


def base_compose_file(compose_dir: str) -> str:
    for n in BASE_COMPOSE_NAMES:
        if os.path.isfile(os.path.join(compose_dir, n)):
            return n
    raise SecretsError(f"no compose file in {compose_dir}")


def compose_files_from_env(lines: list[str], compose_dir: str) -> list[str]:
    found = env_lookup(lines)
    if "COMPOSE_FILE" in found:
        return [x for x in parse_env_value("COMPOSE_FILE", found["COMPOSE_FILE"][1]).split(":") if x]
    return [base_compose_file(compose_dir)]


def set_compose_files(lines: list[str], files: list[str], compose_dir: str) -> list[str]:
    lines = list(lines)
    found = env_lookup(lines)
    base = base_compose_file(compose_dir)
    if "COMPOSE_FILE" in found:
        idx = found["COMPOSE_FILE"][0]
        if files == [base] and idx > 0 and lines[idx - 1] == COMPOSE_MARKER:
            del lines[idx - 1: idx + 1]
        else:
            lines[idx] = "COMPOSE_FILE=" + ":".join(files) + "\n"
        return lines
    if files == [base]:
        return lines
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return lines + [COMPOSE_MARKER, "COMPOSE_FILE=" + ":".join(files) + "\n"]


def interpolation_refs(compose_dir: str, files: list[str], name: str):
    """(required_refs, other_refs) of ${NAME...} in the given compose files."""
    req, other = [], []
    pat_req = re.compile(r"\$\{" + re.escape(name) + r":?\?")
    pat_any = re.compile(r"\$(\{" + re.escape(name) + r"[}:\-?+]|" + re.escape(name) + r"\b)")
    for f in files:
        p = os.path.join(compose_dir, f)
        try:
            with open(p, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    if line.lstrip().startswith("#"):
                        continue
                    if pat_req.search(line):
                        req.append(f"{f}:{n}")
                    elif pat_any.search(line):
                        other.append(f"{f}:{n}")
        except FileNotFoundError:
            continue
    return req, other


def _chown_like(path: str, ref: str) -> None:
    if os.geteuid() == 0:
        st = os.stat(ref)
        os.chown(path, st.st_uid, st.st_gid)


def backup_env(env_path: str, suffix: str) -> str:
    dst = f"{env_path}.pre-secrets{suffix}"
    if not os.path.exists(dst):
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with open(env_path, "rb") as src, os.fdopen(fd, "wb") as out:
            out.write(src.read())
        os.chmod(dst, 0o600)
        _chown_like(dst, env_path)
    return dst


def compose_config_check(compose_dir: str) -> tuple[bool, str]:
    """`docker compose config --quiet` in compose_dir; returns (ok, sanitized_stderr)."""
    cmd = read_kv_config()["COMPOSE_CMD"].split() + ["config", "--quiet"]
    try:
        proc = subprocess.run(cmd, cwd=compose_dir, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        return False, "docker compose not found"
    # `config --quiet` never prints values; keep only the first lines of errors.
    return proc.returncode == 0, "\n".join(proc.stderr.splitlines()[:5])


def verify_rendered(u) -> tuple[bool, str]:
    """Render the project (in memory only) and check every migrated env var is gone
    from the services' environment and a /run/secrets mount is declared instead.
    Catches an old docker compose without `!reset` support. Prints names only."""
    cmd = read_kv_config()["COMPOSE_CMD"].split() + ["config", "--format", "json"]
    try:
        proc = subprocess.run(cmd, cwd=u.compose_dir, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        return False, "docker compose not found"
    if proc.returncode != 0:
        return False, "\n".join(proc.stderr.splitlines()[:5])
    import json
    try:
        rendered = json.loads(proc.stdout)
    finally:
        proc = None  # drop the rendered text (it may hold other, unmigrated values)
    problems = []
    for svc_name in u.services:
        svc = (rendered.get("services") or {}).get(svc_name)
        if svc is None:
            problems.append(f"service {svc_name} not rendered")
            continue
        env = svc.get("environment") or {}
        for var in u.env_vars:
            if env.get(var) not in (None, ""):
                problems.append(f"{svc_name}: {var} still rendered with a value")
        if not svc.get("secrets"):
            problems.append(f"{svc_name}: no secrets mounted")
    rendered = None
    return (not problems), "; ".join(problems)


# --------------------------------------------------------------------------- migrate / rollback


class Unit:
    def __init__(self, svc: str, entry: dict, repo_root: str):
        self.svc = svc
        self.entry = entry
        self.status = entry.get("status")
        self.names = list(entry.get("names") or [])
        self.compose_dir = os.path.join(repo_root, entry.get("compose_dir") or "")
        self.env_path = os.path.join(self.compose_dir, entry.get("env_file") or ".env")
        self.override = entry.get("override") or ""
        self.services = list(entry.get("compose_services") or [])
        self.requires = list(entry.get("requires") or [])
        # env vars that must NOT be rendered with a value once migrated (default: names)
        self.env_vars = list(entry.get("env_vars") or self.names)


def _parse_flags(argv: list[str], allowed: set) -> tuple[list[str], set]:
    pos, flags = [], set()
    for a in argv:
        if a in ("-h", "--help"):
            return [], {"--help"}
        if a.startswith("--"):
            if a not in allowed:
                raise SecretsError(f"unknown option {a}")
            flags.add(a)
        else:
            pos.append(a)
    return pos, flags


def _load_unit(svc: str) -> Unit:
    validate(svc)
    cfg = read_kv_config()
    services = load_manifest()
    if svc not in services:
        raise SecretsError(f"{svc} is not in the manifest")
    return Unit(svc, services[svc], cfg["REPO_ROOT"])


def cmd_migrate(argv: list[str]) -> int:
    pos, flags = _parse_flags(argv, {"--dry-run", "--force", "--overwrite", "--skip-compose-check"})
    if len(pos) != 1 or "--help" in flags:
        print("usage: smarthome-secrets-migrate <svc> [--dry-run] [--force] [--overwrite]")
        return 2
    require_root()
    u = _load_unit(pos[0])
    dry = "--dry-run" in flags
    if u.status == "plan-only":
        raise SecretsError(f"{u.svc} is plan-only in the manifest; not migratable by this tool yet")
    if u.status != "ready" and "--force" not in flags:
        raise SecretsError(f"{u.svc} is '{u.status}' in the manifest; review it, then use --force")
    if not os.path.isfile(u.env_path):
        raise SecretsError(f"missing env file {u.env_path}")
    if not os.path.isfile(os.path.join(u.compose_dir, u.override)):
        raise SecretsError(f"missing override {u.override} in {u.compose_dir}")
    for req in u.requires:
        rsvc, _, rname = req.partition("/")
        if not os.path.exists(cred_path(rsvc, rname)):
            raise SecretsError(f"{u.svc} needs {rsvc}.{rname}; migrate {rsvc} first")

    lines = read_env_lines(u.env_path)
    found = env_lookup(lines)
    files = compose_files_from_env(lines, u.compose_dir)
    check_files = [f for f in files if f != u.override]

    plan_store, plan_already = [], []
    for name in u.names:
        exists = os.path.exists(cred_path(u.svc, name))
        if name in found:
            plan_store.append(name)
        elif exists:
            plan_already.append(name)
        else:
            raise SecretsError(f"{name} is neither in {u.env_path} nor stored as {u.svc}.{name}")
        req, other = interpolation_refs(u.compose_dir, check_files, name)
        if req and name in found:
            raise SecretsError(
                f"{name} is still a REQUIRED interpolation (${{{name}:?...}}) at {', '.join(req)}; "
                "removing it from .env would break `docker compose` for the whole project. "
                "Convert that line first (see secrets/README.md)."
            )
        if other:
            log(f"note: {name} is interpolated at {', '.join(other)}; the override resets it (!reset)")

    print(f"[migrate {u.svc}] env={u.env_path}")
    print(f"  store from .env : {' '.join(plan_store) or '-'}")
    print(f"  already stored  : {' '.join(plan_already) or '-'}")
    new_files = files if u.override in files else files + [u.override]
    print(f"  COMPOSE_FILE    : {':'.join(new_files)}")
    if dry:
        print("  DRY RUN: nothing changed")
        return 0

    # 1) encrypt (value goes only into the systemd-creds stdin pipe)
    for name in plan_store:
        value = parse_env_value(name, found[name][1]).encode("utf-8")
        path = cred_path(u.svc, name)
        if os.path.exists(path):
            try:
                same = decrypt_value(u.svc, name) == value
            except SecretsError:
                same = False
            if not same and "--overwrite" not in flags:
                raise SecretsError(f"{u.svc}.{name} already stored with a DIFFERENT value; use --overwrite")
            if same:
                continue
        encrypt_value(u.svc, name, value)
        del value
        print(f"  stored {u.svc}.{name}")

    # 2) runtime files must exist before the override is activated
    if apply_all() != 0:
        raise SecretsError("smarthome-secrets apply reported failures; .env NOT changed")
    for name in u.names:
        if not os.path.isfile(os.path.join(RUNDIR, u.svc, name)):
            raise SecretsError(f"runtime file for {u.svc}.{name} missing after apply; .env NOT changed")

    # 3) backups (0600, never overwritten), then rewrite .env
    b1 = backup_env(u.env_path, "")
    b2 = backup_env(u.env_path, f".{u.svc}")
    drop = {found[n][0] for n in plan_store}
    # also drop earlier duplicate definitions of the same names
    for i, line in enumerate(lines):
        m = ENV_LINE_RE.match(line.rstrip("\r\n"))
        if m and m.group(1) in u.names and not line.lstrip().startswith("#"):
            drop.add(i)
    new_lines = [l for i, l in enumerate(lines) if i not in drop]
    new_lines = set_compose_files(new_lines, new_files, u.compose_dir)
    if new_lines != lines:
        write_atomic_like(u.env_path, new_lines)
    print(f"  backups: {os.path.basename(b1)} {os.path.basename(b2)} (0600)")

    # 4) validate the rendered project; restore .env on failure
    if "--skip-compose-check" not in flags:
        ok, err = compose_config_check(u.compose_dir)
        if ok:
            ok, err = verify_rendered(u)
        if not ok:
            write_atomic_like(u.env_path, lines)
            raise SecretsError(f"compose check failed, .env restored: {err}")
        print("  compose config OK; migrated vars are no longer rendered as env values")
    svcs = " ".join(u.services) or u.svc
    print(f"OK. Not restarted. Next:\n  cd {u.compose_dir} && docker compose up -d {svcs}")
    return 0


def _quote_env_value(name: str, value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:+=@,%^~-]*", value):
        return value
    if "'" not in value and "\n" not in value:
        return f"'{value}'"
    raise SecretsError(f"{name}: value cannot be written back safely; restore it from the .env.pre-secrets backup")


def cmd_rollback(argv: list[str]) -> int:
    pos, flags = _parse_flags(argv, {"--dry-run", "--purge", "--skip-compose-check"})
    if len(pos) != 1 or "--help" in flags:
        print("usage: smarthome-secrets-rollback <svc> [--dry-run] [--purge]")
        return 2
    require_root()
    u = _load_unit(pos[0])
    lines = read_env_lines(u.env_path)
    found = env_lookup(lines)
    backup = f"{u.env_path}.pre-secrets.{u.svc}"
    backup_found = env_lookup(read_env_lines(backup)) if os.path.isfile(backup) else {}
    backup_lines = read_env_lines(backup) if os.path.isfile(backup) else []

    restore = [n for n in u.names if n not in found]
    files = compose_files_from_env(lines, u.compose_dir)
    new_files = [f for f in files if f != u.override]
    print(f"[rollback {u.svc}] env={u.env_path}")
    print(f"  restore to .env : {' '.join(restore) or '-'}")
    print(f"  COMPOSE_FILE    : {':'.join(new_files)}")
    if "--dry-run" in flags:
        print("  DRY RUN: nothing changed")
        return 0

    new_lines = list(lines)
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"
    for name in restore:
        if name in backup_found:
            line = backup_lines[backup_found[name][0]]  # original line, verbatim
            new_lines.append(line if line.endswith("\n") else line + "\n")
        else:
            value = decrypt_value(u.svc, name).decode("utf-8")
            new_lines.append(f"{name}={_quote_env_value(name, value)}\n")
            del value
    new_lines = set_compose_files(new_lines, new_files, u.compose_dir)
    if new_lines != lines:
        write_atomic_like(u.env_path, new_lines)
    if "--skip-compose-check" not in flags:
        ok, err = compose_config_check(u.compose_dir)
        if not ok:
            write_atomic_like(u.env_path, lines)
            raise SecretsError(f"`docker compose config` failed, .env restored:\n{err}")
    if "--purge" in flags:
        for name in u.names:
            p = cred_path(u.svc, name)
            if os.path.exists(p):
                os.unlink(p)
        apply_all()
        print("  credentials purged")
    svcs = " ".join(u.services) or u.svc
    print(f"OK. Not restarted. Next:\n  cd {u.compose_dir} && docker compose up -d {svcs}")
    return 0


# --------------------------------------------------------------------------- main

DISPATCH = {
    "smarthome-secret": cmd_secret,
    "smarthome-secrets": cmd_secrets,
    "smarthome-secrets-migrate": cmd_migrate,
    "smarthome-secrets-rollback": cmd_rollback,
}


def main(argv: list[str]) -> int:
    prog = os.path.basename(argv[0])
    args = argv[1:]
    if prog not in DISPATCH:
        if args and args[0] in DISPATCH:
            prog, args = args[0], args[1:]
        else:
            print("usage: smarthome_secrets.py {" + ",".join(DISPATCH) + "} ...")
            return 2
    os.umask(0o077)
    try:
        return DISPATCH[prog](args)
    except SecretsError as exc:
        log(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        log("interrupted; nothing further done")
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv))
