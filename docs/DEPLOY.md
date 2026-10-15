# Deploying to the gateways

All gateways run the same code from this repository. Everything that is specific to a gateway (ZLANs, meters,
parameters and their intervals, MQTT settings, login, history, backups) lives in `bridge.db` and `backup/`, which git
never touches. So an update is the same command on every gateway.

## One-time setup per gateway (≈ 5 minutes)

1. **Check git is installed** (PuTTY on the gateway): `git --version`.
   If it is missing, install it (`apt install git`). *The gateway you showed me had a broken apt state
   (`python3-pip : Depends: python3-setuptools but it is not going to be installed`). Do not run
   `apt --fix-broken` on a live gateway; ask first, or copy the files by WinSCP instead.*
2. **Allow the gateway to read the repository.**
   * Public repository: nothing to do.
   * Private repository (recommended): create a read-only *deploy key* on the gateway:
     ```bash
     ssh-keygen -t ed25519 -N "" -f ~/.ssh/idms_deploy -C "gateway-$(hostname)"
     cat ~/.ssh/idms_deploy.pub
     ```
     Add the printed key in GitHub → repository → **Settings → Deploy keys → Add deploy key** (leave *Allow write
     access* **off**; use one key per gateway). Then:
     ```bash
     printf 'Host github.com\n  IdentityFile ~/.ssh/idms_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
     ssh -o StrictHostKeyChecking=accept-new -T git@github.com     # should greet you by repository name
     ```
     and use `REPO_URL=git@github.com:usamasial/epic---annexe.git` in the commands below.
3. **Get the update script** onto the gateway (it lives in the repository, so the first time you fetch just this file):
   ```bash
   cd /opt/modbus_bridge
   curl -fsSLO https://raw.githubusercontent.com/usamasial/epic---annexe/<branch>/deploy/update.sh   # public repo
   # or copy deploy/update.sh with WinSCP
   ```
4. **First update** (turns `/opt/modbus_bridge` into a git checkout; your database is left alone and backed up first):
   ```bash
   cd /opt/modbus_bridge
   REPO_URL=git@github.com:usamasial/epic---annexe.git bash update.sh <branch>
   ```
   `<branch>` is the branch that holds the release (`main` once merged). It is remembered for next time.
   Afterwards you can delete the loose copy (`rm /opt/modbus_bridge/update.sh`); the repository now contains `deploy/update.sh`.

## Every later update (all 8 gateways, same command)
```bash
cd /opt/modbus_bridge && bash deploy/update.sh
```
Output ends with `OK: version 1.x.y`. The version is also shown at the top of the web page.
Roll out to **one gateway first**, check the dashboard, then the other seven.

What the script does: backs up `bridge.db` (SQLite online backup, low priority) and the current code → fetches →
installs requirements only if they changed → syntax check → restarts `modbus-bridge` → waits up to 40 s for the web
app to answer. **If anything fails it restores the previous code and database by itself.**

Manual rollback (code **and** database back to just before the last update; readings stored since then are removed
from `bridge.db` but remain in `backup/*.csv`):
```bash
bash deploy/update.sh --rollback
```

## Releasing a change (on your side)
Merge to the release branch on GitHub, bump `APP_VERSION` in `app.py` and add a line to `CHANGELOG.md`, run
`pytest tests`, then run the update command on a gateway.

## Safety notes
* Updates add database columns/tables automatically and never delete data.
* Old backups are pruned automatically (last 5 kept in `db-backups/` and `code-backups/`).
* The CSV backup (`backup/YYYYMM/…`) is a separate, plain-text copy of everything sent to IDMS. Copy it off the
  gateway occasionally (WinSCP) if you want protection against losing the gateway's disk.
* Never put passwords or keys in the repository. MQTT credentials are entered on the web front-end and stay in
  `bridge.db`.
