"""Migration regression against an isolated Postgres URL supplied by the caller."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
DB_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DB_DIR))
spec = importlib.util.spec_from_file_location("db_setup", DB_DIR / "setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def test_search_function_guc_registered_in_fresh_connection():
    url = os.environ.get("MIGRATION_TEST_URL")
    if not url:
        pytest.skip("Set MIGRATION_TEST_URL to an isolated database with pgvector >= 0.8")
    with psycopg.connect(url, prepare_threshold=None) as conn:
        version = setup._pgvector_version(conn)
        if not version or version < (0, 8):
            pytest.skip("pgvector >= 0.8 required")
        sql = setup._render(
            "CREATE FUNCTION pg_temp.migration_guc_probe() RETURNS integer "
            "LANGUAGE sql {{HNSW_SET}} AS 'SELECT 1'", conn)
        conn.execute(sql)
        assert conn.execute("SELECT pg_temp.migration_guc_probe()").fetchone() == (1,)
        conn.rollback()
