#!/usr/bin/env python3
"""Card accounts registry + provider adapters (multi-account card sync). Byte-identical copy in
command-center/command_center/card_accounts.py (a test enforces it).

Registry (root-owned, NOT secret): /etc/card-accounts.json  (CARD_ACCOUNTS)
  {"version": 1, "max_runs_total": 0,
   "accounts": [{"id": "cal-main", "provider": "cal", "label": "Cal main", "enabled": true,
                 "max_runs_per_day": 2}, ...]}
  id: ^[a-z0-9][a-z0-9-]{1,23}$ (stable; names the credentials, the archive dir and the state dir).
  max_runs_per_day: per-account daily login cap (1..10); null/absent = CAL_MAX_RUNS_PER_DAY (default 2).
  max_runs_total: optional global cap over all accounts per Israel day (0 = off).
A missing registry means exactly one account, cal-main (the pre-multi behaviour). The root runner trusts ONLY
this file; it publishes a sanitized mirror to the spool (accounts.json) for the Command Center to read.

Per account:
  credentials  /etc/credstore.encrypted/card-<id>.<field>   (field = the provider's login_fields)
               cal-main also reads the legacy cal.username / cal.password (sealed by the old cal-setup).
  archive      <archive root>/cards/<id>/<provider>-card-<last4>-YYYY-MM.csv (+ _coverage.json)
  state        <state root>/accounts/<id>/stop-YYYY-MM-DD, runs-YYYY-MM-DD   (root only)
  spool        <spool root>/accounts/<id>/last-status.json, runs-today.json  (Command Center reads)

Providers: an adapter = login fields + OTP site + egress hosts + archive prefix + the container entry. Only
"cal" is implemented; any other provider is rejected with NOT_SUPPORTED ("not supported yet").
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ACCOUNT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,23}$")
DEFAULT_ACCOUNT = "cal-main"
REGISTRY_VERSION = 1
FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


class Provider:
    def __init__(self, name, *, label_he, login_fields, otp_site, egress_hosts, archive_prefix, entry):
        self.name = name
        self.label_he = label_he
        self.login_fields = tuple(login_fields)
        self.otp_site = otp_site
        self.egress_hosts = tuple(egress_hosts)
        self.archive_prefix = archive_prefix
        self.entry = entry  # container command (the image is pinned by setup-cal-host.sh)

    def to_public(self):
        return {"provider": self.name, "label_he": self.label_he, "login_fields": list(self.login_fields),
                "otp_site": self.otp_site, "egress_hosts": list(self.egress_hosts)}


PROVIDERS = {
    "cal": Provider("cal", label_he="כאל", login_fields=("username", "password"), otp_site="cal",
                    # Enforced inside the container (lib/cal/egress.mjs): browser *.cal-online.co.il, Node fetch
                    # api.cal-online.co.il only. Listed here for docs/status only.
                    egress_hosts=("*.cal-online.co.il", "api.cal-online.co.il"), archive_prefix="cal",
                    entry=("node", "/app/cal-auto.mjs")),
}
# Legacy credential names (old cal-setup), read only for cal-main when card-cal-main.* is absent.
LEGACY_CREDS = {DEFAULT_ACCOUNT: {"username": "cal.username", "password": "cal.password"}}


class RegistryError(ValueError):
    def __init__(self, code, text):
        super().__init__(text)
        self.code = code
        self.text = text


def check_id(account):
    account = str(account or "").strip()
    if not ACCOUNT_RE.fullmatch(account):
        raise RegistryError("BAD_ACCOUNT", "account must match ^[a-z0-9][a-z0-9-]{1,23}$ (e.g. cal-main, cal-2)")
    return account


def provider(name):
    name = str(name or "").strip().lower()
    if name in PROVIDERS:
        return PROVIDERS[name]
    raise RegistryError("NOT_SUPPORTED", f"provider {name or '?'!r} is not supported yet (supported: "
                        + ", ".join(sorted(PROVIDERS)) + ")")


def _int_or_none(v, lo, hi):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise RegistryError("BAD_REGISTRY", f"value {v!r} out of range {lo}..{hi}")
    return v


def default_registry():
    return {"version": REGISTRY_VERSION, "max_runs_total": 0,
            "accounts": [{"id": DEFAULT_ACCOUNT, "provider": "cal", "label": "Cal", "enabled": True,
                          "max_runs_per_day": None}]}


def validate(data):
    """-> normalized registry or raises RegistryError. Unknown providers are kept (so status can say
    "not supported yet") but can never run."""
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        raise RegistryError("BAD_REGISTRY", "registry must be an object with an accounts list")
    out = {"version": REGISTRY_VERSION, "max_runs_total": _int_or_none(data.get("max_runs_total") or 0, 0, 100) or 0,
           "accounts": []}
    seen = set()
    for a in data["accounts"]:
        if not isinstance(a, dict):
            raise RegistryError("BAD_REGISTRY", "account entries must be objects")
        ident = check_id(a.get("id"))
        if ident in seen:
            raise RegistryError("BAD_REGISTRY", f"duplicate account {ident}")
        seen.add(ident)
        prov = str(a.get("provider") or "").strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9]{1,15}", prov):
            raise RegistryError("BAD_REGISTRY", f"{ident}: bad provider")
        label = re.sub(r"[\x00-\x1f]", "", str(a.get("label") or ident))[:40]
        out["accounts"].append({"id": ident, "provider": prov, "label": label,
                                "enabled": a.get("enabled", True) is True,
                                "max_runs_per_day": _int_or_none(a.get("max_runs_per_day"), 1, 10)})
    if not out["accounts"]:
        raise RegistryError("BAD_REGISTRY", "no accounts")
    return out


def load_registry(path):
    """Missing file -> default_registry() (cal-main only). Corrupt -> RegistryError (fail closed)."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_registry()
    except OSError as exc:
        raise RegistryError("BAD_REGISTRY", f"cannot read registry: {exc.__class__.__name__}")
    try:
        return validate(json.loads(text))
    except ValueError as exc:
        if isinstance(exc, RegistryError):
            raise
        raise RegistryError("BAD_REGISTRY", "registry is not valid JSON")


def find(registry, account):
    account = check_id(account)
    for a in registry["accounts"]:
        if a["id"] == account:
            return a
    raise RegistryError("UNKNOWN_ACCOUNT", f"unknown account {account!r}; known: "
                        + ", ".join(a["id"] for a in registry["accounts"]))


def runnable(registry, account):
    """-> (entry, Provider) for an account that may log in, else RegistryError."""
    entry = find(registry, account)
    prov = provider(entry["provider"])
    if not entry["enabled"]:
        raise RegistryError("ACCOUNT_DISABLED", f"account {entry['id']} is disabled in the registry")
    return entry, prov


def max_runs(entry, default):
    return entry.get("max_runs_per_day") or default


def mirror(registry, default_max_runs=2):
    """Sanitized copy for the Command Center (no credentials exist in the registry anyway)."""
    accts = []
    for a in registry["accounts"]:
        prov = PROVIDERS.get(a["provider"])
        accts.append({"id": a["id"], "provider": a["provider"], "label": a["label"], "enabled": a["enabled"],
                      "supported": prov is not None, "max_runs_per_day": max_runs(a, default_max_runs)})
    return {"version": REGISTRY_VERSION, "default": DEFAULT_ACCOUNT, "max_runs_total": registry["max_runs_total"],
            "accounts": accts}


def cred_name(account, field):
    if not FIELD_RE.fullmatch(field):
        raise RegistryError("BAD_FIELD", "bad credential field")
    return f"card-{check_id(account)}.{field}"


def read_credentials(creds_dir, account, prov):
    """-> {field: value} from $CREDENTIALS_DIRECTORY, or None if any field is missing/empty. New names win;
    cal-main falls back to the legacy cal.* names field by field only when NONE of the new ones exist."""
    if not creds_dir:
        return None
    base = Path(creds_dir)
    names = {f: cred_name(account, f) for f in prov.login_fields}
    if not any((base / n).exists() for n in names.values()) and account in LEGACY_CREDS:
        names = {f: LEGACY_CREDS[account].get(f, "") for f in prov.login_fields}
    out = {}
    for f, n in names.items():
        if not n:
            return None
        try:
            v = (base / n).read_text(encoding="utf-8")
        except OSError:
            return None
        v = v.rstrip("\r\n") if f == "password" else v.strip()
        if not v:
            return None
        out[f] = v
    return out


def archive_dir(root, account):
    return Path(root) / "cards" / check_id(account)


def state_dir(root, account):
    return Path(root) / "accounts" / check_id(account)


def spool_dir(root, account):
    return Path(root) / "accounts" / check_id(account)


def credential_dropin(store, registry):
    """systemd drop-in text: one LoadCredentialEncrypted= per EXISTING sealed file of a registry account
    (a missing file in LoadCredentialEncrypted= would make the unit fail to start)."""
    store = Path(store)
    lines = ["# Generated by card-setup / setup-cal-host.sh from /etc/card-accounts.json — do not edit by hand.",
             "[Service]"]
    names = []
    for a in registry["accounts"]:
        prov = PROVIDERS.get(a["provider"])
        if not prov:
            continue
        for f in prov.login_fields:
            names.append(cred_name(a["id"], f))
        names.extend(LEGACY_CREDS.get(a["id"], {}).values())
    for n in sorted(set(names)):
        if (store / n).is_file():
            lines.append(f"LoadCredentialEncrypted={n}:{store / n}")
    return "\n".join(lines) + "\n"


def _write_json(path, data, mode):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as h:
        json.dump(data, h, ensure_ascii=False, indent=1)
        h.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _cli(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="card_accounts.py")
    ap.add_argument("--registry", default=os.environ.get("CARD_ACCOUNTS", "/etc/card-accounts.json"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("fields", help="print the provider's login fields for a new/existing account")
    c.add_argument("account")
    c.add_argument("provider")
    c = sub.add_parser("add", help="add or update an account (idempotent)")
    c.add_argument("account")
    c.add_argument("provider")
    c.add_argument("--label", default="")
    c.add_argument("--max-runs", type=int, default=None)
    c = sub.add_parser("ensure-default", help="create the registry with cal-main if it does not exist")
    c = sub.add_parser("mirror")
    c.add_argument("out")
    c.add_argument("--default-max", type=int, default=2)
    c = sub.add_parser("dropin")
    c.add_argument("store")
    c.add_argument("out")
    sub.add_parser("list")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "fields":
            check_id(args.account)
            print(" ".join(provider(args.provider).login_fields))
            return 0
        if args.cmd == "ensure-default":
            if not Path(args.registry).exists():
                _write_json(args.registry, default_registry(), 0o644)
                print("registry created (cal-main)")
            else:
                load_registry(args.registry)
                print("registry exists, valid")
            return 0
        reg = load_registry(args.registry)
        if args.cmd == "add":
            ident = check_id(args.account)
            prov = provider(args.provider)
            entry = next((a for a in reg["accounts"] if a["id"] == ident), None)
            if entry and entry["provider"] != prov.name:
                raise RegistryError("BAD_REGISTRY", f"{ident} already exists with provider {entry['provider']}")
            if entry is None:
                entry = {"id": ident, "provider": prov.name, "label": ident, "enabled": True, "max_runs_per_day": None}
                reg["accounts"].append(entry)
            if args.label:
                entry["label"] = args.label
            if args.max_runs is not None:
                entry["max_runs_per_day"] = args.max_runs
            entry["enabled"] = True
            reg = validate(reg)
            _write_json(args.registry, reg, 0o644)
            print(f"registry: {ident} ({prov.name}) ok")
        elif args.cmd == "mirror":
            _write_json(args.out, mirror(reg, args.default_max), 0o640)
        elif args.cmd == "dropin":
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(args.out).with_name(f".{Path(args.out).name}.tmp")
            tmp.write_text(credential_dropin(args.store, reg), encoding="utf-8")
            os.chmod(tmp, 0o644)
            os.replace(tmp, args.out)
        elif args.cmd == "list":
            for a in reg["accounts"]:
                print(f"{a['id']}\t{a['provider']}\t{'enabled' if a['enabled'] else 'disabled'}\t{a['label']}")
        return 0
    except RegistryError as exc:
        print(f"{exc.code}: {exc.text}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
