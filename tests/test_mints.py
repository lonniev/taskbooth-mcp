"""Facts about a mint, from the mint, cashumints.space and the Round Robin Auditor, each with its source."""

import httpx
import pytest
from tollbooth import cashu

from taskbooth_mcp import mints

NOW = 1_800_000_000
DAY = 86_400
URL = "https://mint.example/Bitcoin"
ESCROW = {n: {"supported": True} for n in (*cashu.ESCROW_NUTS, "4", "5")}


def iso(t):
    from datetime import UTC, datetime
    return datetime.fromtimestamp(t, UTC).replace(tzinfo=None).isoformat()


@pytest.fixture(autouse=True)
def forget(monkeypatch):
    mints._kept.clear()

    async def no_address(host):
        return ""

    monkeypatch.setattr(mints, "_address_of", no_address)


def transport(*, auditor=None, cashumints_up=True, info=None, listed=True):
    swaps = auditor if auditor is not None else []

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://mint.example/Bitcoin/v1/info"):
            return httpx.Response(200, json=info or {"name": "Example", "version": "cdk/0.18", "nuts": ESCROW,
                                                      "icon_url": "javascript:alert(1)", "description": "A mint",
                                                      "pubkey": "02" + "ab" * 32, "tos_url": "http://plain.example",
                                                      "contact": [{"method": "email", "info": "a@b.example"},
                                                                  {"method": "", "info": "x"}, "junk"]})
        if url.startswith(mints.CASHUMINTS_API):
            if not cashumints_up:
                return httpx.Response(502)
            if url.endswith("/mints"):
                rows = [{"url": URL, "host": "mint.example-bitcoin", "type": "cashu", "status": "online",
                         "nuts": list(ESCROW), "icon": "/icons/x.png"},
                        {"url": "https://weak.example", "host": "weak", "type": "cashu", "status": "online",
                         "nuts": ["4", "5", "7"]},
                        {"url": "fedimint:abc", "host": "fed", "type": "fedimint", "status": "online", "nuts": []}]
                return httpx.Response(200, json=rows if listed else rows[1:])
            return httpx.Response(200, json={"status": "online", "uptime_30d": 0.995, "first_seen": NOW - 200 * DAY,
                                             "review_count": 12, "last_review_at": NOW - 3 * DAY})
        if url.startswith(mints.AUDITOR_API):
            if auditor is None:
                return httpx.Response(502)
            if "/mints/url" in url:
                return httpx.Response(200, json={"id": 7, "url": URL})
            skip = int(request.url.params["skip"])
            return httpx.Response(200, json=swaps[skip:skip + mints.AUDIT_PAGE])
        return httpx.Response(404)

    return httpx.MockTransport(handle)


async def facts(**kw):
    async with httpx.AsyncClient(transport=transport(**kw)) as http:
        return (await mints.facts([URL], at=NOW, http=http))[0]


async def test_each_source_says_its_piece_with_when_it_was_seen():
    swaps = [{"created_at": iso(NOW - d * DAY), "state": "OK"} for d in range(1, 100)]
    got = await facts(auditor=swaps)
    assert got["mint"]["source"] == "mint" and got["mint"]["missing_escrow_nuts"] == []
    assert got["mint"]["icon_url"] == ""  # only an https icon is passed on
    assert got["mint"]["description"] == "A mint" and got["mint"]["pubkey"] == "02" + "ab" * 32
    assert got["mint"]["tos_url"] == ""  # only https
    assert got["mint"]["contact"] == [{"method": "email", "info": "a@b.example"}]
    assert got["cashumints"] == {"source": "cashumints.space", "observed_at": NOW, "listed": True, "status": "online",
                                 "page_url": "https://cashumints.space/mint/mint.example-bitcoin",
                                 "uptime_30d": 0.995, "first_seen": NOW - 200 * DAY, "reviews": 12,
                                 "latest_review_at": NOW - 3 * DAY, "icon_url": "https://cashumints.space/icons/x.png"}
    assert got["auditor"]["tracked"] and got["auditor"]["failed"] == 0
    assert got["auditor"]["clean_since"] == got["auditor"]["tracked_since"] == NOW - 99 * DAY


async def test_an_unreachable_source_is_said_plainly_and_judges_nothing():
    got = await facts(auditor=None, cashumints_up=False)
    assert got["auditor"]["unreachable"] and got["cashumints"]["unreachable"]
    assert got["mint"]["nuts"]


async def test_a_mint_cashumints_does_not_list():
    got = await facts(listed=False)
    assert got["cashumints"]["listed"] is False


def test_the_clean_run_begins_after_the_latest_failure():
    swaps = [{"created_at": iso(NOW - d * DAY), "state": "OK"} for d in (1, 2, 30, 50, 80)]
    swaps += [{"created_at": iso(NOW - 40 * DAY), "state": "ERROR", "error": "timeout"},
              {"created_at": iso(NOW - 100 * DAY), "state": "ERROR"}]
    record = mints.audit_record(swaps, at=NOW)
    assert record["failed"] == 2 and record["failed_90d"] == 1
    assert record["last_failure_at"] == NOW - 40 * DAY and record["last_error"] == "timeout"
    assert record["clean_since"] == NOW - 30 * DAY
    assert record["tracked_since"] == NOW - 100 * DAY


def test_a_mint_failing_now_has_no_clean_run():
    record = mints.audit_record([{"created_at": iso(NOW - DAY), "state": "OK"},
                                 {"created_at": iso(NOW), "state": "ERROR"}], at=NOW)
    assert record["clean_since"] is None


def test_no_swaps_is_no_record():
    assert mints.audit_record([], at=NOW)["swaps"] == 0


async def test_candidates_claim_every_escrow_nut():
    async with httpx.AsyncClient(transport=transport()) as http:
        assert await mints.candidates(at=NOW, http=http) == [URL]


async def test_answers_are_kept_ten_minutes():
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"nuts": ESCROW})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        await mints.own_word(URL, at=NOW, http=http)
        await mints.own_word(URL, at=NOW + 599, http=http)
        assert len(calls) == 1
        await mints.own_word(URL, at=NOW + 600, http=http)
        assert len(calls) == 2


@pytest.mark.parametrize("bad", ["http://mint.example", "https://localhost", "https://10.0.0.1", 7])
def test_only_public_https_mints_are_asked_about(bad):
    with pytest.raises(ValueError):
        mints.checked_mints([bad])


def test_at_most_sixty_mints():
    with pytest.raises(ValueError):
        mints.checked_mints([f"https://m{i}.example" for i in range(61)])


# --- where the mint is, and its Lightning node --------------------------------------------

#: BOLT11's own example invoice, signed by node 03e7156ae33b…
SPEC_INVOICE = (
    "lnbc1pvjluezsp5zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zygspp5qqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqf"
    "qqqsyqcyq5rqwzqfqypqdpl2pkx2ctnv5sxxmmwwd5kgetjypeh2ursdae8g6twvus8g6rfwvs8qun0dfjkxaq9qrsgq357wnc5r2u"
    "eh7ck6q93dj32dlqnls087fxdwk8qakdyafkq3yap9us6v52vjjsrvywa6rt52cm9r9zqt8r2t7mlcwspyetp5h2tztugp9lfyql"
)
NODE = "03e7156ae33b0a208d0744199163177e909e80176e55d97a2f221ede0f934dd9ad"


def place_and_node(*, org="AS24940 Hetzner Online GmbH", node_known=True, quote_ok=True, asked=None):
    asked = asked if asked is not None else []

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(mints.IPINFO_API):
            return httpx.Response(200, json={"ip": "195.201.20.134", "city": "Nuremberg", "region": "Bavaria",
                                             "country": "DE", "org": org})
        if url.endswith("/v1/mint/quote/bolt11"):
            asked.append(request.read())
            return (httpx.Response(200, json={"quote": "q", "request": SPEC_INVOICE}) if quote_ok
                    else httpx.Response(400, json={"detail": "amount too small", "code": 11006}))
        if url.startswith(mints.MEMPOOL_API):
            if not node_known:
                return httpx.Response(404)
            return httpx.Response(200, json={"alias": "minibits.cash", "capacity": "28868357", "active_channel_count": 8,
                                             "first_seen": NOW - 1000 * DAY, "updated_at": NOW - 10 * DAY,
                                             "iso_code": "DE", "country": {"en": "Germany"}, "city": {"en": "Gunzenhausen"},
                                             "as_organization": "Hetzner Online GmbH", "sockets": "195.201.20.134:9735"})
        return httpx.Response(404)

    return httpx.MockTransport(handle)


@pytest.fixture
def addressed(monkeypatch):
    async def address(host):
        return "195.201.20.134"

    monkeypatch.setattr(mints, "_address_of", address)


async def test_the_web_server_is_placed_by_its_address(addressed):
    async with httpx.AsyncClient(transport=place_and_node()) as http:
        got = await mints.located(URL, at=NOW, http=http)
    assert got == {"source": "ipinfo.io", "observed_at": NOW, "ip": "195.201.20.134", "city": "Nuremberg",
                   "region": "Bavaria", "country": "DE", "org": "AS24940 Hetzner Online GmbH", "front": False}


async def test_a_shared_front_is_named_as_one(addressed):
    async with httpx.AsyncClient(transport=place_and_node(org="AS13335 Cloudflare, Inc.")) as http:
        assert (await mints.located(URL, at=NOW, http=http))["front"] is True


async def test_no_address_is_said_plainly():
    async with httpx.AsyncClient(transport=place_and_node()) as http:
        assert (await mints.located(URL, at=NOW, http=http))["unreachable"]


async def test_the_node_is_the_one_its_invoice_names():
    asked = []
    async with httpx.AsyncClient(transport=place_and_node(asked=asked)) as http:
        got = await mints.lightning_node(URL, at=NOW, http=http)
    assert got["pubkey"] == NODE and got["announced"] is True
    assert got["alias"] == "minibits.cash" and got["capacity_sats"] == 28868357 and got["channels"] == 8
    assert got["country"] == "DE" and got["country_name"] == "Germany" and got["tor_only"] is False
    assert got["updated_at"] == NOW - 10 * DAY
    assert len(asked) == 1  # one unpaid quote
    async with httpx.AsyncClient(transport=place_and_node(asked=asked)) as http:
        await mints.lightning_node(URL, at=NOW + 3600, http=http)
    assert len(asked) == 1  # kept for a day


async def test_a_node_mempool_does_not_know_is_unannounced():
    async with httpx.AsyncClient(transport=place_and_node(node_known=False)) as http:
        got = await mints.lightning_node(URL, at=NOW, http=http)
    assert got == {"source": "mempool.space", "observed_at": NOW, "pubkey": NODE, "announced": False}


async def test_a_mint_that_gives_no_quote_names_no_node():
    asked = []
    async with httpx.AsyncClient(transport=place_and_node(quote_ok=False, asked=asked)) as http:
        got = await mints.lightning_node(URL, at=NOW, http=http)
    assert got["unreachable"] and len(asked) == len(mints.QUOTE_SATS)
