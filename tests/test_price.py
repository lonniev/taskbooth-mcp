"""Bitcoin's price, as read from mempool.space and handed on."""

import httpx
import pytest

from taskqueue_mcp import price

NOW = 1_800_000_000
YEAR = 365 * 86_400


def mempool(*, now=None, old=None, fail=None, seen=None):
    def answer(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        if fail:
            return httpx.Response(fail)
        if request.url.path.endswith("/prices"):
            return httpx.Response(200, json=now if now is not None else {"time": NOW - 30, "USD": 64000, "EUR": 58000})
        when = int(request.url.params["timestamp"])
        fiat = request.url.params["currency"]
        rows = old(when, fiat) if old else [{"time": when - 60, fiat: 1000 + (NOW - when) // YEAR}]
        return httpx.Response(200, json={"prices": rows})
    return httpx.AsyncClient(transport=httpx.MockTransport(answer))


@pytest.fixture(autouse=True)
def forget():
    price._kept.clear()


async def test_a_reading_is_todays_price_and_one_for_each_earlier_year_oldest_first():
    got = await price.reading("eur", 3, at=NOW, client=mempool())
    assert got["fiat"] == "EUR" and got["price"] == 58000.0 and got["observed_at"] == NOW - 30
    assert [h["observed_at"] for h in got["history"]] == [NOW - 3 * YEAR - 60, NOW - 2 * YEAR - 60, NOW - YEAR - 60]
    assert [h["price"] for h in got["history"]] == [1003.0, 1002.0, 1001.0]
    assert got["source"] == "mempool.space"


async def test_a_reading_is_reused_for_an_hour_then_read_again():
    seen: list[str] = []
    await price.reading("USD", 1, at=NOW, client=mempool(seen=seen))
    await price.reading("USD", 1, at=NOW + 3599, client=mempool(seen=seen))
    assert len(seen) == 2
    await price.reading("USD", 1, at=NOW + 3600, client=mempool(seen=seen))
    assert len(seen) == 4


async def test_no_years_asks_only_for_today():
    seen: list[str] = []
    got = await price.reading("USD", 0, at=NOW, client=mempool(seen=seen))
    assert got["history"] == [] and len(seen) == 1


async def test_a_year_mempool_has_no_price_for_is_left_out():
    got = await price.reading("USD", 2, at=NOW, client=mempool(old=lambda when, fiat: [] if when < NOW - YEAR - 1 else [{"time": when, fiat: 5}]))
    assert [h["price"] for h in got["history"]] == [5.0]


@pytest.mark.parametrize("fiat", ["XAU", "", "usd ", None, 7])
async def test_only_the_quoted_currencies_are_offered(fiat):
    if fiat == "usd ":
        assert (await price.reading(fiat, 0, at=NOW, client=mempool()))["fiat"] == "USD"
        return
    with pytest.raises(ValueError, match="quoted in"):
        await price.reading(fiat, 0, at=NOW, client=mempool())


@pytest.mark.parametrize("years", [-1, 11, 2.5, "4", True])
async def test_the_span_is_a_whole_number_of_years_up_to_ten(years):
    with pytest.raises(ValueError, match="years"):
        await price.reading("USD", years, at=NOW, client=mempool())


@pytest.mark.parametrize("now", [{"time": NOW}, {"USD": 0}, {"USD": -5}, {"USD": "lots"}, {"USD": True}, []])
async def test_an_answer_with_no_usable_price_is_refused_and_not_kept(now):
    with pytest.raises(ValueError, match="mempool.space"):
        await price.reading("USD", 0, at=NOW, client=mempool(now=now))
    assert price._kept == {}


async def test_a_service_that_is_down_is_a_refusal():
    with pytest.raises(ValueError, match="did not answer"):
        await price.reading("USD", 1, at=NOW, client=mempool(fail=503))
