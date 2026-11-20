"""A live test of a mint's locks, run against a mint in memory that keeps NUT-11's rules, and one that does not."""

import pytest
from pynostr.key import PrivateKey
from tollbooth import cashu

from taskbooth_mcp import minttest
from tests.fake_mint import MINT_URL, FakeMint, sign_digest
from tests.invoices import make_invoice

NOW = 1_800_000_000
ADDRESS = "operator@wallet.example"


class CarelessMint(FakeMint):
    """Claims NUT-11 but checks no lock: any proof it signed spends."""

    def _spend(self, inputs, message):
        for p in inputs:
            if p["secret"] in self.spent:
                raise cashu.CashuError("Token already spent", code=11001)


@pytest.fixture
def keys():
    return [PrivateKey() for _ in range(3)]


def npubs(keys):
    return [k.public_key.bech32() for k in keys]


async def invoice_for(address, sats):
    assert address == ADDRESS
    return make_invoice(sats, created_at=NOW)


def signed(keys, items):
    by = {k.public_key.bech32(): k for k in keys}
    return {item["npub"]: sign_digest(by[item["npub"]], item["sign_digest"]) for item in items}


async def run(mint, keys, sats=100):
    """Every step a page takes, start to finish; the record after each."""
    record = await minttest.start(lambda url: mint, MINT_URL, npubs(keys), sats, at=NOW, test_id="t1")
    steps = [record]
    if record["state"] == minttest.STOPPED:
        return steps
    mint.pay(record["funding"]["quote"])
    while minttest.to_sign(record) or record["state"] == minttest.SPENT and not record.get("melt"):
        items = minttest.to_sign(record)
        record = await minttest.advance(lambda url: mint, invoice_for, ADDRESS, record, signed(keys, items), at=NOW)
        steps.append(record)
    return steps


async def test_a_sound_mint_passes_every_check_and_returns_the_sats(keys):
    mint = FakeMint(now=NOW, fee_ppk=0)
    steps = await run(mint, keys)
    done = steps[-1]
    assert done["state"] == minttest.DONE
    assert done["checks"] == {"nuts": "ok", "fund": "ok", "one_signature": "refused, as it must be",
                              "two_signatures": "ok", "refund": "ok", "melt": "ok"}
    assert minttest.verdict(done) == "passed"
    assert mint.paid_invoices and mint.paid_invoices[0][1] == done["returned_sats"] == 100


async def test_fees_come_out_of_the_sats(keys):
    mint = FakeMint(now=NOW, fee_ppk=100)
    done = (await run(mint, keys, sats=200))[-1]
    assert minttest.verdict(done) == "passed"
    assert done["returned_sats"] + done["change_sats"] < 200


async def test_a_mint_that_takes_one_signature_fails(keys):
    mint = CarelessMint(now=NOW, fee_ppk=0)
    done = (await run(mint, keys))[-1]
    assert done["checks"]["one_signature"].startswith("TAKEN")
    assert minttest.verdict(done) == "failed"
    assert done["state"] == minttest.DONE and done["returned_sats"] > 0


async def test_a_mint_without_the_escrow_nuts_stops_before_any_sats(keys):
    mint = FakeMint(now=NOW)
    mint.info_nuts.pop("11")
    record = await minttest.start(lambda url: mint, MINT_URL, npubs(keys), 100, at=NOW, test_id="t1")
    assert record["state"] == minttest.STOPPED and "11" in record["checks"]["nuts"]
    assert not mint.quotes
    assert minttest.verdict(record) == "failed"


async def test_funding_waits_for_the_invoice(keys):
    mint = FakeMint(now=NOW)
    record = await minttest.start(lambda url: mint, MINT_URL, npubs(keys), 100, at=NOW, test_id="t1")
    with pytest.raises(ValueError, match="not paid yet"):
        await minttest.advance(lambda url: mint, invoice_for, ADDRESS, record,
                               signed(keys, minttest.to_sign(record)), at=NOW)


async def test_a_wrong_key_signature_is_refused(keys):
    mint = FakeMint(now=NOW)
    record = await minttest.start(lambda url: mint, MINT_URL, npubs(keys), 100, at=NOW, test_id="t1")
    mint.pay(record["funding"]["quote"])
    item = minttest.to_sign(record)[0]
    forged = {item["npub"]: sign_digest(keys[1], item["sign_digest"])}
    with pytest.raises(ValueError, match="not over its sign_digest"):
        await minttest.advance(lambda url: mint, invoice_for, ADDRESS, record, forged, at=NOW)


async def test_the_return_needs_a_payout_address(keys):
    mint = FakeMint(now=NOW, fee_ppk=0)
    record = await minttest.start(lambda url: mint, MINT_URL, npubs(keys), 100, at=NOW, test_id="t1")
    mint.pay(record["funding"]["quote"])
    record = await minttest.advance(lambda url: mint, invoice_for, ADDRESS, record,
                                    signed(keys, minttest.to_sign(record)), at=NOW)
    record = await minttest.advance(lambda url: mint, invoice_for, ADDRESS, record,
                                    signed(keys, minttest.to_sign(record)), at=NOW)
    assert record["state"] == minttest.SPENT and not record.get("melt")
    with pytest.raises(ValueError, match="payout_address"):
        await minttest.advance(lambda url: mint, invoice_for, "", record, {}, at=NOW)


def test_the_page_sees_no_proofs_or_blinding_factors(keys):
    record = {"test_id": "t", "mint": MINT_URL, "sats": 100, "state": "funded", "started_at": NOW, "checks": {},
              "keys": npubs(keys), "proofs": {"multisig": [{"secret": "s"}]}, "spend": {"x": {"pending": {"rs": ["r"]}}}}
    view = minttest.summary(record)
    assert "proofs" not in view and "spend" not in view and "keys" not in view


@pytest.mark.parametrize("bad", [[], ["npub1x"] * 3, "npub", None])
def test_three_distinct_npubs_are_required(bad):
    with pytest.raises(ValueError):
        minttest.checked_keys(bad)


@pytest.mark.parametrize("bad", [0, 19, 1001, True, "100"])
def test_the_sats_are_bounded(bad):
    with pytest.raises(ValueError):
        minttest.checked_sats(bad)


async def test_the_store_keeps_each_mints_latest_finished_test(keys):
    from tollbooth.vault_encryption import VaultCipher

    from taskbooth_mcp.store import TaskStore
    from tests.sqlite_neon import SqliteNeon

    db = TaskStore(SqliteNeon(cipher=VaultCipher(nsec_hex=PrivateKey().hex())))
    await db.ensure_schema()
    base = {"mint": MINT_URL, "sats": 100, "keys": npubs(keys), "checks": {}}
    await db.save_mint_test({**base, "test_id": "old", "state": "done", "started_at": NOW, "finished_at": NOW + 1})
    await db.save_mint_test({**base, "test_id": "new", "state": "done", "started_at": NOW + 5, "finished_at": NOW + 9})
    await db.save_mint_test({**base, "test_id": "open", "state": "invoiced", "started_at": NOW + 20})
    assert (await db.finished_mint_tests([MINT_URL]))[MINT_URL]["test_id"] == "new"
    assert [t["test_id"] for t in await db.mint_tests()] == ["open", "new", "old"]
    assert (await db.mint_test("open"))["state"] == "invoiced"
    assert "invoiced" not in str(db._neon.rows("taskbooth_mint_tests"))  # sealed, not in the clear
