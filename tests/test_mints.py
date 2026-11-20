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
def forget():
    mints._kept.clear()


def transport(*, auditor=None, cashumints_up=True, info=None, listed=True):
    swaps = auditor if auditor is not None else []

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://mint.example/Bitcoin/v1/info"):
            return httpx.Response(200, json=info or {"name": "Example", "version": "cdk/0.18", "nuts": ESCROW,
                                                      "icon_url": "javascript:alert(1)"})
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
    assert got["cashumints"] == {"source": "cashumints.space", "observed_at": NOW, "listed": True, "status": "online",
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
