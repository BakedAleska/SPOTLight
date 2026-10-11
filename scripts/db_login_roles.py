"""Create the LOGIN users the services connect as, inside the NOLOGIN group roles from
db/migrations/007_roles.sql. Idempotent; run as the database owner AFTER `db/setup.py migrate`.

    SPOT_APP_USER / SPOT_APP_PASSWORD   -> member of spot_app         (backend)
    SPOT_MCP_USER / SPOT_MCP_PASSWORD   -> member of spot_mcp_reader  (mcp server; system1's call logging)

A missing user is created with its password. An existing user keeps its password (only the group membership is
re-granted) unless SPOT_ROTATE_PASSWORDS=1: db-init runs this on every `docker compose up`, and must never reset a
deployed password to whatever the env holds (audit PP-04). The compose dev defaults (spot_app_dev, spot_mcp_dev)
are refused (exit 1) when they would create a user or set its password on a host other than localhost / 127.0.0.1 /
::1 / db (the compose Postgres) / a unix socket, e.g. never on *.neon.tech.

Used by the compose `db-init` job. Works against Neon too (DATABASE_URL = the owner's URL); if the owner may not
CREATE ROLE there, create the users in the Neon console and re-run. Never prints the URL or a password.
"""
import ipaddress
import os
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

PAIRS = [("SPOT_APP_USER", "SPOT_APP_PASSWORD", "spot_app_user", "spot_app"),
         ("SPOT_MCP_USER", "SPOT_MCP_PASSWORD", "spot_mcp_user", "spot_mcp_reader")]
DEV_PASSWORDS = {"spot_app_dev", "spot_mcp_dev"}          # docker-compose.yml / .env.example defaults
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "db"}     # db = the compose Postgres service


def _conninfo(url: str) -> dict:
    try:
        return conninfo_to_dict(url)
    except psycopg.ProgrammingError:
        return {}


def _hosts(url: str) -> list:
    """The host(s) libpq connects to: from the URL or key=value DSN, else PGHOST. '' = the local socket."""
    host = _conninfo(url).get("host") or os.environ.get("PGHOST") or ""
    return [h.strip().lower() for h in host.split(",")]


def _hostaddrs(url: str) -> list:
    """hostaddr (else PGHOSTADDR): when set, libpq connects to these IPs and uses host only for TLS and auth."""
    addr = _conninfo(url).get("hostaddr") or os.environ.get("PGHOSTADDR") or ""
    return [a.strip() for a in addr.split(",") if a.strip()]


def _loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def is_local(url: str) -> bool:
    return (all(h in LOCAL_HOSTS or h == "" or h.startswith("/") for h in _hosts(url))
            and all(_loopback(a) for a in _hostaddrs(url)))


def _connect(url: str) -> psycopg.Connection:
    # db/spotdb.connect adds sslmode=require to Neon URLs and turns off prepared statements (Neon's pooler).
    db_dir = Path(__file__).resolve().parents[1] / "db"
    if (db_dir / "spotdb.py").exists():
        if str(db_dir) not in sys.path:
            sys.path.insert(0, str(db_dir))
        import spotdb
        return spotdb.connect(url, autocommit=True)
    return psycopg.connect(url, autocommit=True, prepare_threshold=None)


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("db_login_roles: DATABASE_URL is not set", file=sys.stderr)
        return 2
    rotate = os.environ.get("SPOT_ROTATE_PASSWORDS") == "1"
    local = is_local(url)
    status = 0
    with _connect(url) as conn:
        for user_var, pw_var, default_user, group in PAIRS:
            user = os.environ.get(user_var) or default_user
            pw = os.environ.get(pw_var)
            if not pw:
                print("db_login_roles: %s unset, skipping %s" % (pw_var, user))
                continue
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (group,)).fetchone():
                print("db_login_roles: group role %s missing (run migrate first)" % group, file=sys.stderr)
                return 1
            if user in {g for _, _, _, g in PAIRS}:
                print("db_login_roles: %s=%s names a NOLOGIN group role; use a separate user" % (user_var, user),
                      file=sys.stderr)
                return 1
            role = conn.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (user,)).fetchone()
            exists = role is not None
            set_password = not exists or rotate
            if set_password and pw in DEV_PASSWORDS and not local:
                print("db_login_roles: refusing to %s %s with the public dev default password on a non-local "
                      "database; set %s to a real secret (openssl rand -hex 24)"
                      % ("change the password of" if exists else "create", user, pw_var), file=sys.stderr)
                status = 1
                continue
            if set_password:
                verb = sql.SQL("ALTER" if exists else "CREATE")
                conn.execute(sql.SQL("{} ROLE {} LOGIN PASSWORD {}").format(
                    verb, sql.Identifier(user), sql.Literal(pw)))
            elif not role[0]:
                # An existing NOLOGIN role: restore LOGIN (as before PP-04) without touching its password.
                conn.execute(sql.SQL("ALTER ROLE {} LOGIN").format(sql.Identifier(user)))
            conn.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(group), sql.Identifier(user)))
            if not exists:
                done = "created"
            elif set_password:
                done = "rotated the password of"
            else:
                done = "kept the existing password of" + ("" if role[0] else " (and restored LOGIN on)")
            print("db_login_roles: %s %s in %s%s" % (done, user, group, "" if set_password else
                                                       " (SPOT_ROTATE_PASSWORDS=1 changes it)"))
    return status


if __name__ == "__main__":
    sys.exit(main())
