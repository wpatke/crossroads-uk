import crossroads


def test_build_loads_spatial_extension():
    # After an (empty) build, spatial functions must be available on the connection.
    client = crossroads.init_engine()  # in-memory
    client.build()
    # ST_Point + ST_AsText prove the extension loaded.
    assert client.con.execute(
        "SELECT ST_AsText(ST_Point(1, 2))"
    ).fetchone()[0] == "POINT (1 2)"
    # Reprojection EPSG:27700 -> EPSG:4326 must work (used by later steps).
    lon_lat = client.con.execute(
        "SELECT ST_X(g), ST_Y(g) FROM ("
        "  SELECT ST_Transform(ST_Point(530000, 180000), 'EPSG:27700', 'EPSG:4326') AS g"
        ")"
    ).fetchone()
    # Central London-ish: latitude ~51.5, longitude ~ -0.13.
    # DuckDB returns (latitude, longitude) as (X, Y) under EPSG:4326 axis order.
    assert 50.0 < lon_lat[0] < 53.0
    client.close()


def test_existing_empty_build_still_succeeds():
    # Loading spatial must not break the zero-transformer no-op build.
    client = crossroads.init_engine()
    client.build()
    assert client.con.execute(
        "SELECT count(*) FROM data_quality_log"
    ).fetchone()[0] == 0
    client.close()
