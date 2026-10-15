"""deploy/update.sh against a local 'origin' repository and a fake gateway directory."""
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from conftest import REPO

SCRIPT = REPO / "deploy" / "update.sh"
OLD_APP = 'APP_VERSION = "1.0.0"\nprint("old app")\n'


def run(cmd, cwd=None, env=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True)
    if check and r.returncode:
        raise AssertionError(f"{cmd} failed:\n{r.stdout}\n{r.stderr}")
    return r


class Env:
    def __init__(self, tmp):
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.work = tmp / "work"
        self.gw = tmp / "gateway"
        run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)])
        self.work.mkdir()
        for name in ["app.py", "requirements.txt", "modbus-bridge.service", ".gitignore", "static", "templates", "deploy"]:
            src = REPO / name
            if src.is_dir(): shutil.copytree(src, self.work / name, ignore=shutil.ignore_patterns("__pycache__"))
            else: shutil.copy(src, self.work / name)
        run(["git", "init", "-q", "-b", "main"], cwd=self.work)
        run(["git", "config", "user.email", "t@t"], cwd=self.work); run(["git", "config", "user.name", "t"], cwd=self.work)
        run(["git", "remote", "add", "origin", str(self.origin)], cwd=self.work)
        self.commit("release 1")
        # the gateway as it is today: old code, live database, no git
        self.gw.mkdir()
        (self.gw / "app.py").write_text(OLD_APP)
        (self.gw / "requirements.txt").write_text((REPO / "requirements.txt").read_text())
        con = sqlite3.connect(self.gw / "bridge.db")
        con.execute("CREATE TABLE history(id INTEGER PRIMARY KEY, v TEXT)")
        con.execute("INSERT INTO history(v) VALUES('precious reading')")
        con.commit(); con.close()
        self.healthy(True)

    def commit(self, msg):
        run(["git", "add", "-A"], cwd=self.work)
        run(["git", "commit", "-q", "-m", msg], cwd=self.work)
        run(["git", "push", "-q", "origin", "main"], cwd=self.work)

    def healthy(self, yes):
        f = self.gw / "ok"
        f.write_text("1") if yes else (f.unlink() if f.exists() else None)

    def update(self, *args, check=True, extra=None):
        env = dict(os.environ, APP_DIR=str(self.gw), REPO_URL=str(self.origin),
                   RESTART_CMD=f"touch {self.gw}/restarted", STOP_CMD="true",
                   HEALTH_CMD=f"test -f {self.gw}/ok", HEALTH_TIMEOUT="2")
        env.update(extra or {})
        return run(["bash", str(SCRIPT), *args], cwd=self.gw, env=env, check=check)

    def version(self):
        for line in (self.gw / "app.py").read_text().splitlines():
            if line.startswith("APP_VERSION"): return line.split('"')[1]

    def db_rows(self):
        con = sqlite3.connect(self.gw / "bridge.db")
        rows = [r[0] for r in con.execute("SELECT v FROM history")]
        con.close(); return rows


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_first_run_turns_the_gateway_into_a_git_checkout_and_keeps_data(env):
    r = env.update("main")
    assert "OK: version 1.1.0" in r.stdout, r.stdout
    assert env.version() == "1.1.0"
    assert (env.gw / ".git").exists() and (env.gw / "restarted").exists()
    assert env.db_rows() == ["precious reading"]                       # database untouched
    assert len(list((env.gw / "db-backups").glob("bridge-*.db"))) == 1  # ...and backed up first
    assert len(list((env.gw / "code-backups").glob("code-*.tgz"))) == 1
    assert (env.gw / ".deploy_branch").read_text().strip() == "main"
    status = run(["git", "status", "--porcelain"], cwd=env.gw).stdout
    assert "bridge.db" not in status and "db-backups" not in status     # data files are git-ignored


def test_second_run_without_changes_does_nothing(env):
    env.update("main")
    (env.gw / "restarted").unlink()
    r = env.update()                                                    # branch remembered
    assert "already up to date" in r.stdout
    assert not (env.gw / "restarted").exists()


def test_new_release_is_deployed(env):
    env.update("main")
    (env.work / "app.py").write_text((env.work / "app.py").read_text().replace('APP_VERSION = "1.1.0"', 'APP_VERSION = "1.2.0"'))
    env.commit("release 2")
    r = env.update()
    assert "OK: version 1.2.0" in r.stdout and env.version() == "1.2.0"
    assert env.db_rows() == ["precious reading"]


def test_unhealthy_release_is_rolled_back_with_the_database(env):
    env.update("main")
    # release 2 "migrates" the database and then does not start
    (env.work / "app.py").write_text((env.work / "app.py").read_text().replace('APP_VERSION = "1.1.0"', 'APP_VERSION = "1.2.0"'))
    env.commit("broken release")
    env.healthy(False)
    # the broken release starts, migrates/writes to the database, and then never answers
    migrate = env.tmp / "migrate.py"
    migrate.write_text(f"import sqlite3\n"
                       f"if '1.2.0' in open('{env.gw}/app.py').read():      # only the broken release misbehaves\n"
                       f"    c = sqlite3.connect('{env.gw}/bridge.db')\n"
                       "    c.execute(\"INSERT INTO history(v) VALUES('written by broken release')\")\n    c.commit()\n")
    r = env.update(check=False, extra={"RESTART_CMD": f"python3 {migrate}"})
    assert r.returncode == 1 and "ROLLING BACK" in r.stdout
    assert env.version() == "1.1.0"                                     # previous code is back
    assert env.db_rows() == ["precious reading"]                        # database from before the update


def test_syntax_error_never_reaches_a_restart(env):
    env.update("main")
    (env.gw / "restarted").unlink()
    (env.work / "app.py").write_text("def broken(:\n")
    env.commit("syntax error")
    r = env.update(check=False)
    assert r.returncode == 1 and "does not compile" in r.stdout
    assert env.version() == "1.1.0"
    assert (env.gw / "restarted").exists()           # restarted only by the rollback, to bring the old code back


def test_failed_first_install_restores_the_original_gateway(env):
    env.healthy(False)
    r = env.update("main", check=False)
    assert r.returncode == 1
    assert (env.gw / "app.py").read_text() == OLD_APP
    assert not (env.gw / ".git").exists()                               # next attempt starts clean
    assert env.db_rows() == ["precious reading"]


def test_manual_rollback(env):
    env.update("main")
    (env.work / "app.py").write_text((env.work / "app.py").read_text().replace('APP_VERSION = "1.1.0"', 'APP_VERSION = "1.2.0"'))
    env.commit("release 2")
    env.update()
    r = env.update("--rollback", extra={"YES": "1"})
    assert "rolled back" in r.stdout and env.version() == "1.1.0"


def test_wrong_branch_fails_cleanly_and_changes_nothing(env):
    r = env.update("no-such-branch", check=False, extra={"HEALTH_TIMEOUT": "1"})
    assert r.returncode != 0
    assert (env.gw / "app.py").read_text() == OLD_APP and env.db_rows() == ["precious reading"]
