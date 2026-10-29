"""Bootstrap local services without sending family data or creating cloud resources."""

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument(
    "--native", action="store_true", help="Use installed PostgreSQL instead of Docker/Supabase"
)
parser.add_argument("--port", type=int, default=55433)
args = parser.parse_args()


def command(parts, **kwargs):
    return subprocess.run(
        parts, cwd=root, check=True, creationflags=0x08000000 if os.name == "nt" else 0, **kwargs
    )


if args.native:
    local = root / ".local"
    local.mkdir(exist_ok=True)
    binpath = Path(shutil.which("pg_ctl") or "").parent
    if not (binpath / "pg_ctl.exe" if os.name == "nt" else binpath / "pg_ctl").exists():
        raise SystemExit("Install PostgreSQL and put its bin directory on PATH, or use Supabase with Docker.")
    suffix = ".exe" if os.name == "nt" else ""
    passwordfile = local / "db-password"
    if not passwordfile.exists():
        passwordfile.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    password = passwordfile.read_text().strip()
    data = local / "postgres"
    if not (data / "PG_VERSION").exists():
        # Windows postgres can inherit pipe handles from pg_ctl. Use a file,
        # rather than capture_output, so a daemon cannot hold communicate open.
        with (local / "bootstrap.log").open("a", encoding="utf-8") as stream:
            command(
                [
                    str(binpath / ("initdb" + suffix)),
                    "-D",
                    str(data),
                    "-U",
                    "habitquest",
                    "--auth=scram-sha-256",
                    "--pwfile=" + str(passwordfile),
                    "--encoding=UTF8",
                    "--no-locale",
                ],
                stdout=stream,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
    status = subprocess.run(
        [str(binpath / ("pg_ctl" + suffix)), "-D", str(data), "status"], capture_output=True, check=False
    )
    if status.returncode:
        with (local / "bootstrap.log").open("a", encoding="utf-8") as stream:
            command(
                [
                    str(binpath / ("pg_ctl" + suffix)),
                    "-D",
                    str(data),
                    "-l",
                    str(local / "postgres.log"),
                    "-o",
                    f"-h 127.0.0.1 -p {args.port}",
                    "-w",
                    "start",
                ],
                stdout=stream,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
    base = f"postgresql://habitquest:{password}@127.0.0.1:{args.port}/"
    with psycopg.connect(base + "postgres", autocommit=True) as conn:
        for name in ["habitquest", "habitquest_test"]:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    envfile = root / ".env"
    if not envfile.exists():
        envfile.write_text(
            f"DATABASE_URL={base}habitquest\nTEST_DATABASE_URL={base}habitquest_test\nMODEL_MODE=fake\nJWT_SECRET={secrets.token_urlsafe(48)}\nJWT_ISSUER=http://127.0.0.1:8000/local-demo\nLOCAL_DEMO_AUTH=true\nLANGSMITH_TRACING=false\nLANGCHAIN_TRACING_V2=false\n",
            encoding="utf-8",
        )
    urls = [base + "habitquest", base + "habitquest_test"]
else:
    npx = "npx.cmd" if os.name == "nt" else "npx"
    command([npx, "supabase", "start"])
    status = command([npx, "supabase", "status", "--output", "json"], capture_output=True, text=True)
    data = json.loads(status.stdout)
    testurl = urlsplit(data["DB_URL"])._replace(path="/habitquest_test").geturl()
    with psycopg.connect(data["DB_URL"], autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname='habitquest_test'").fetchone():
            conn.execute("CREATE DATABASE habitquest_test")
    urls = [data["DB_URL"], testurl]
    if not (root / ".env").exists():
        (root / ".env").write_text(
            f"DATABASE_URL={data['DB_URL']}\nTEST_DATABASE_URL={testurl}\nMODEL_MODE=fake\nJWT_ISSUER={data['API_URL']}/auth/v1\nJWT_JWKS_URL={data['API_URL']}/auth/v1/.well-known/jwks.json\nLOCAL_DEMO_AUTH=false\nLANGSMITH_TRACING=false\nLANGCHAIN_TRACING_V2=false\n",
            encoding="utf-8",
        )
for url in urls:
    command([sys.executable, str(root / "scripts/setup_db.py")], env={**os.environ, "DATABASE_URL": url})
print("Local services and databases ready. Existing .env preserved. See README.md for API, worker and tests.")
