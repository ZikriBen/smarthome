# host/ — NUC-side pieces (installed by Ben, as root, only after he approves the deploy)

| file | what |
|---|---|
| `setup-host.sh` | one-time: users/groups, dirs + modes, egress network + proxy, image build pinned by id, systemd units, runner copied root-owned to `/usr/local/lib/hapoalim/` |
| `setup-secrets.sh` | Ben types the bank user code + password (hidden); they go straight into `systemd-creds encrypt` (stdin). Also generates the age key for the browser profile. No plaintext file, no argv, no env, no shell history |
| `run-once.sh` | the runner (root, one run per approved request) |
| `hapoalim-run.path` / `.service` | trigger + one-shot service (`LoadCredentialEncrypted=`) |
| `egress/` | allowlisting tinyproxy (bank domains only) |
| `reset-lockout.sh` | the ONLY way to clear a lockout after an auth failure (manual, by Ben) |

See ../DESIGN.md.
