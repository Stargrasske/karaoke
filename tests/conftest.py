import os
import pytest
import psycopg
import psycopg_pool

# Point to the test database
os.environ["KARAOKE_PG_URL"] = "postgresql://karaoke:karaoke@localhost:5432/karaoke_test"

# Make sure we don't accidentally load the main pool with the wrong url
import karaoke.localcache
if karaoke.localcache._POOL is not None:
    karaoke.localcache._POOL.close()

# For tests, we use a much larger pool because tests often don't close connections properly
pool_url = os.environ["KARAOKE_PG_URL"]
karaoke.localcache._POOL = psycopg_pool.ConnectionPool(
    conninfo=pool_url, open=True, min_size=1, max_size=500, kwargs={"autocommit": True, "row_factory": psycopg.rows.dict_row}
)

@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    # Load DDL from migration script
    import karaoke.localcache
    pool = karaoke.localcache.get_pool()
    with pool.connection() as conn:
        with open("scripts/migrate_sqlite_to_postgres.py") as f:
            content = f.read()
            import re
            ddl_match = re.search(r'POSTGRES_DDL = """(.*?)"""', content, re.DOTALL)
            if ddl_match:
                ddl = ddl_match.group(1)
                with conn.cursor() as cur:
                    # Execute all DDL statements
                    cur.execute(ddl)
                conn.commit()
            else:
                raise Exception("Could not find POSTGRES_DDL in migration script")

@pytest.fixture(autouse=True)
def clear_db():
    import karaoke.localcache
    pool = karaoke.localcache.get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # Delete from all non-system tables. We use DELETE instead of TRUNCATE
            # because TRUNCATE requires AccessExclusiveLock which deadlocks with leaked
            # background threads from other tests that are still polling (AccessShareLock).
            # We catch foreign_key_violation and loop until all tables are clear.
            cur.execute("""
                DO $$ DECLARE
                    r RECORD;
                    done BOOLEAN;
                BEGIN
                    LOOP
                        done := TRUE;
                        FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = current_schema()) LOOP
                            BEGIN
                                EXECUTE 'DELETE FROM ' || quote_ident(r.tablename);
                            EXCEPTION WHEN foreign_key_violation THEN
                                done := FALSE;
                            END;
                        END LOOP;
                        EXIT WHEN done;
                    END LOOP;
                    
                    -- Reset all sequences
                    FOR r IN (SELECT relname FROM pg_class WHERE relkind = 'S') LOOP
                        EXECUTE 'ALTER SEQUENCE ' || quote_ident(r.relname) || ' RESTART WITH 1';
                    END LOOP;
                END $$;
            """)
        conn.commit()
