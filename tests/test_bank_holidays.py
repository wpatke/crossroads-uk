"""Offline tests for the GOV.UK bank-holidays source (Stage 01)."""

import os
import shutil

import crossroads
from crossroads.transformers.bank_holidays import BankHolidaysTransformer

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "bank_holidays",
                       "bank-holidays-sample.json")


def _bh_client(tmp_path):
    cache = str(tmp_path / "cache")
    os.makedirs(cache, exist_ok=True)
    shutil.copy(FIXTURE, os.path.join(cache, "bank-holidays.json"))   # pre-seed: no download
    client = crossroads.init_engine(cache_dir=cache)
    client.registry._transformers = [BankHolidaysTransformer()]        # this source only
    return client


def test_bank_holidays_table_built_and_typed(tmp_path):
    client = _bh_client(tmp_path)
    client.build(datasets=["bank_holidays"])          # runs §9 invariants (exemption recorded)
    try:
        con = client.con
        # Table exists and has rows.
        n = con.execute("SELECT count(*) FROM bank_holidays").fetchone()[0]
        assert n > 0
        # All three divisions present.
        divs = {r[0] for r in con.execute(
            "SELECT DISTINCT division FROM bank_holidays").fetchall()}
        assert divs == {"england-and-wales", "scotland", "northern-ireland"}
        # `date` is a real DATE (typed silver), not text.
        dtype = con.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='bank_holidays' AND column_name='date'").fetchone()[0]
        assert dtype.upper() == "DATE"
        # A known england-and-wales holiday is present and correctly divisioned.
        hit = con.execute(
            "SELECT count(*) FROM bank_holidays "
            "WHERE division='england-and-wales' AND date=DATE '2023-04-10'").fetchone()[0]
        assert hit == 1
        # The exemption was recorded (source is deliberately not audited).
        ex = con.execute(
            "SELECT count(*) FROM quality_exemptions WHERE source_id='bank_holidays'").fetchone()[0]
        assert ex >= 1
    finally:
        client.close()


def test_bank_holidays_build_is_idempotent(tmp_path):
    """A second build over the same seeded cache reproduces the same row count (CREATE OR
    REPLACE, not a doubling INSERT)."""
    client = _bh_client(tmp_path)
    client.build(datasets=["bank_holidays"])
    n1 = client.con.execute("SELECT count(*) FROM bank_holidays").fetchone()[0]
    client.build(datasets=["bank_holidays"])
    n2 = client.con.execute("SELECT count(*) FROM bank_holidays").fetchone()[0]
    assert n1 == n2 and n1 > 0
    client.close()
