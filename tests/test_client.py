import crossroads
from crossroads.client import Client


def test_init_engine_returns_client():
    client = crossroads.init_engine()
    assert isinstance(client, Client)
    assert client.database_path == ":memory:"
    assert client.con is None  # not opened until build()


def test_empty_build_is_noop_in_memory():
    client = crossroads.init_engine()
    client.registry._transformers = []   # no sources: a genuine no-op build (offline, deterministic)
    result = client.build(years=[2023], include_weather=True, spatial_grain="local_authority")
    # build returns self and leaves an open, queryable connection
    assert result is client
    assert client.con is not None
    assert client.con.execute("SELECT 42").fetchone()[0] == 42
    client.close()
    assert client.con is None


def test_build_against_on_disk_database(tmp_path):
    db_path = tmp_path / "local_analytics.db"
    client = crossroads.init_engine(database_path=str(db_path))
    client.build()
    assert client.con.execute("SELECT 1").fetchone()[0] == 1
    client.close()
    # the database file was created on disk
    assert db_path.exists()


def test_shared_con_fixture_works(con):
    assert con.execute("SELECT 7 * 6").fetchone()[0] == 42
