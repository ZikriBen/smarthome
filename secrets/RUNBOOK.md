# smarthome-secrets runbook (NUC)

Run every command on the NUC as `ben`. `sudo` is written out where it is needed.
**Never paste a secret into chat or into a command line.** Values go in only at the hidden prompt or on stdin.

## A. Install (once)

1. Check the prerequisites:
   ```
   systemctl --version | head -1        # systemd >= 250
   docker compose version --short       # >= 2.24.4
   stat -f -c %T /run                   # tmpfs
   ```
2. Install the files without enabling anything yet:
   ```
   cd ~/smarthome && sudo secrets/host/install-host.sh
   ```
3. Enable the boot-time oneshot (ordered before docker.service) and run it once:
   ```
   sudo secrets/host/install-host.sh --enable --start
   systemctl status smarthome-secrets.service --no-pager      # active (exited)
   ```

## B. Set / change / remove a secret

4. Set a value at the hidden prompt (asked twice):
   ```
   sudo smarthome-secret set <svc> <NAME>
   ```
   From a file instead (no echo, no argv): `sudo smarthome-secret set <svc> <NAME> < file`.
5. Push it to `/run`, then restart the container. Running containers keep the old copy.
   ```
   sudo smarthome-secrets refresh
   cd ~/smarthome/<stack> && docker compose up -d --force-recreate <service>
   ```
6. List names (never values) and check the runtime copies:
   ```
   sudo smarthome-secret list
   sudo smarthome-secrets status
   ```
7. Remove a secret: `sudo smarthome-secret rm <svc> <NAME>`.

## C. Pilot: searxng

8. Dry run. It shows names only and changes nothing:
   ```
   sudo smarthome-secrets-migrate searxng --dry-run
   ```
9. Migrate. This encrypts SEARXNG_SECRET, writes `/run/smarthome-secrets/searxng/SEARXNG_SECRET`,
   backs up `home-agent/.env` to `.env.pre-secrets` and `.env.pre-secrets.searxng` (0600),
   removes the key, sets `COMPOSE_FILE=compose.yaml:compose.secrets.searxng.yaml` and
   validates the rendered config. It does not restart anything.
   ```
   sudo smarthome-secrets-migrate searxng
   ```
10. Recreate searxng, then restart hermes. Hermes's network guard pins searxng's IP at start, and
    a recreated searxng may get a new IP.
    ```
    cd ~/smarthome/home-agent
    docker compose up -d searxng
    docker compose restart hermes
    ```
11. Verify. None of these print the value:
    ```
    docker inspect searxng --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -c '^SEARXNG_SECRET='   # 0
    docker inspect searxng --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^SEARXNG_SECRET_FILE=' # the path
    docker compose config | grep -c 'SEARXNG_SECRET:'                                                      # 0
    grep -c '^SEARXNG_SECRET=' .env                                                                       # 0
    docker logs searxng --since 5m 2>&1 | grep -E 'secrets-env-shim|ultrasecretkey'   # "loaded SEARXNG_SECRET" only
    docker exec searxng wget -qO /dev/null -S http://127.0.0.1:8080/healthz 2>&1 | head -1                # HTTP/1.1 200
    ```
    Then ask Hermes for a web search in Telegram. It should answer with results.
12. Optional reboot test: `sudo reboot`. Afterwards `systemctl status smarthome-secrets` should be
    active (exited) and `docker ps` should show searxng up.

## D. Roll back

13. Roll back the pilot service. This puts the original `.env` line back verbatim and removes the
    override from `COMPOSE_FILE`:
    ```
    sudo smarthome-secrets-rollback searxng          # add --purge to also delete the credential
    cd ~/smarthome/home-agent && docker compose up -d searxng && docker compose restart hermes
    ```
14. Remove the mechanism entirely, only after every service is rolled back:
    ```
    sudo ~/smarthome/secrets/host/install-host.sh --uninstall
    ```
    Then roll back the code with the package's `99-rollback-secrets.sh --yes`.
15. Once the pilot has been fine for a few days, delete the plaintext backups:
    `shred -u ~/smarthome/home-agent/.env.pre-secrets*`. Do this only when **no** home-agent unit
    still needs a rollback, because rollback falls back to the credential.

## E. Next services (each one after Ben says go)

Order: beszel-agent → homarr → mail-rss-service → telegram-downloader / telegram-browser →
command-center → hermes. For each: run `migrate <svc> --dry-run`, then `migrate <svc>`, then the
printed `docker compose up -d …`, then the step 11 checks. Units marked `needs-review` need
`--force`, and the command-center and hermes units also need the base edits in README.md first.
