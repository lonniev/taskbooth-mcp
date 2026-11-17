"""Organizations and Projects: who sees and does what, where a task may be filed, and what a Project adds up to."""

import pytest
from pynostr.key import PrivateKey
from tollbooth.vault_encryption import VaultCipher

from taskbooth_mcp import orgs, payout
from taskbooth_mcp.lifecycle import Action
from taskbooth_mcp.store import NotYours, TaskStore
from tests.invoices import make_invoice
from tests.sqlite_neon import SqliteNeon

NOW = 1_800_000_000
DAY = 86_400
RUBRIC = {"grades": [{"name": "Accepted", "sats": 100_000}, {"name": "Accepted with changes", "sats": 80_000},
                     {"name": "Not accepted", "sats": 0}]}


def _npub() -> str:
    return PrivateKey().public_key.bech32()


@pytest.fixture
async def db():
    store = TaskStore(SqliteNeon(cipher=VaultCipher(nsec_hex=PrivateKey().hex())))
    await store.ensure_schema()
    return store


@pytest.fixture
def people():
    return {"owner": _npub(), "colleague": _npub(), "painter": _npub(), "stranger": _npub()}


@pytest.fixture
async def library(db, people):
    org = await db.save_org(people["owner"], "The New Library", "A reading room for the town", at=NOW)
    await db.save_member(people["owner"], org, people["colleague"], "Head librarian")
    project = await db.save_project(people["owner"], org, "Portraits for the reading room",
                                    brief="Founders and economists", budget_sats=500_000,
                                    circle=[people["painter"]], defaults={"ip_option": "assign", "venue": "Texas"},
                                    at=NOW)
    return org, project


async def _pay_ahead(db, people, task, which, at):
    """The client asks for the deposit or a milestone's invoice, pays it, and the painter says it arrived."""
    async def resolve(address, sats):
        return make_invoice(sats, created_at=at)
    claimed = await db.advance_task(people["owner"], task, which, at=at)
    await payout.advance_invoice(db, claimed, which, address="painter@wallet.example",
                                 requested_by=people["owner"], at=at, resolve=resolve)
    await db.confirm_advance(people["painter"], task, which, at=at + 60)


async def _portrait(db, poster, painter, org, project, **over):
    args = {"title": "Paint a portrait of Murray Rothbard for the new Library", "rubric": RUBRIC,
            "audience": [painter], "needed_by": NOW + 90 * DAY, "deposit_pct": 10,
            "milestones": [{"name": "Sketch approved", "due_at": NOW + 30 * DAY, "pct": 20},
                           {"name": "Underpainting", "due_at": NOW + 60 * DAY, "pct": 30}],
            "org_id": org, "project_id": project, "at": NOW} | over
    return await db.post(poster, **args)


# ---------------------------------------------------------------------------
# The rules, as pure functions
# ---------------------------------------------------------------------------


def test_the_owner_manages_members_see_and_post_and_strangers_have_no_place():
    place = lambda npub: orgs.standing_in("npub1owner", {"npub1member"}, npub)
    assert [place(n) for n in ("npub1owner", "npub1member", "npub1stranger")] == ["owner", "member", ""]
    assert [orgs.may_see(p) for p in ("owner", "member", "")] == [True, True, False]
    assert [orgs.may_post(p) for p in ("owner", "member", "")] == [True, True, False]
    assert [orgs.may_manage(p) for p in ("owner", "member", "")] == [True, False, False]


# ---------------------------------------------------------------------------
# Organizations, members and Projects
# ---------------------------------------------------------------------------


async def test_the_owner_and_members_see_the_organization_and_strangers_do_not(db, people, library):
    org, project = library
    for who, place in (("owner", "owner"), ("colleague", "member")):
        seen = await db.org(people[who], org)
        assert seen["name"] == "The New Library" and seen["your_place"] == place
        assert seen["members"] == [{"npub": people["colleague"], "label": "Head librarian"}]
        assert [p["project_id"] for p in seen["projects"]] == [project]
    with pytest.raises(NotYours):
        await db.org(people["stranger"], org)
    assert [o["your_place"] for o in await db.orgs_of(people["colleague"])] == ["member"]
    assert await db.orgs_of(people["stranger"]) == []


async def test_only_the_owner_changes_the_organization_its_members_or_its_projects(db, people, library):
    org, project = library
    colleague = people["colleague"]
    with pytest.raises(ValueError, match="owner"):
        await db.save_org(colleague, "Renamed", org_id=org)
    with pytest.raises(ValueError, match="owner"):
        await db.save_member(colleague, org, _npub())
    with pytest.raises(ValueError, match="owner"):
        await db.save_project(colleague, org, "Another")
    with pytest.raises(ValueError, match="owner"):
        await db.archive_project(colleague, org, project)
    with pytest.raises(NotYours):
        await db.save_project(people["stranger"], org, "Another")
    with pytest.raises(ValueError, match="already sees"):
        await db.save_member(people["owner"], org, people["owner"])


async def test_a_projects_defaults_are_checked_as_a_cover_page_checks_them(db, people, library):
    org, _ = library
    with pytest.raises(ValueError, match="no default"):
        await db.save_project(people["owner"], org, "Bad", defaults={"deliverables": ["x"]})
    with pytest.raises(ValueError, match="IP option"):
        await db.save_project(people["owner"], org, "Bad", defaults={"ip_option": "mine"})
    with pytest.raises(ValueError, match="custom IP terms"):
        await db.save_project(people["owner"], org, "Bad", defaults={"ip_option": "custom", "ip_custom_text": "x" * 5000})
    with pytest.raises(ValueError, match="npub"):
        await db.save_project(people["owner"], org, "Bad", circle=["not-an-npub"])


# ---------------------------------------------------------------------------
# Filing a task
# ---------------------------------------------------------------------------


async def test_owners_and_members_file_tasks_in_open_projects_and_nobody_else_does(db, people, library):
    org, project = library
    assert await _portrait(db, people["owner"], people["painter"], org, project)
    assert await _portrait(db, people["colleague"], people["painter"], org, project)
    with pytest.raises(ValueError, match="Organization you are in"):
        await _portrait(db, people["stranger"], people["painter"], org, project)
    with pytest.raises(ValueError, match="both"):
        await _portrait(db, people["owner"], people["painter"], org, "")
    other_org = await db.save_org(people["owner"], "The Old Museum", at=NOW)
    other = await db.save_project(people["owner"], other_org, "Busts", at=NOW)
    with pytest.raises(ValueError, match="not in that Organization"):
        await _portrait(db, people["owner"], people["painter"], org, other)
    await db.archive_project(people["owner"], org, project)
    with pytest.raises(ValueError, match="archived"):
        await _portrait(db, people["colleague"], people["painter"], org, project)
    await db.archive_project(people["owner"], org, project, archived=False)
    assert await _portrait(db, people["colleague"], people["painter"], org, project)


async def test_members_see_every_task_in_their_projects_in_every_state_and_move_none(db, people, library):
    org, project = library
    task = await _portrait(db, people["owner"], people["painter"], org, project)
    await db.act(people["painter"], task, Action.CLAIM, at=NOW + 10)

    seen = await db.get(people["colleague"], task, at=NOW + 20)
    assert seen["your_roles"] == ["member"] and seen["standing"] == "claimed"
    assert [t["task_id"] for t in await db.tasks_of(people["colleague"], at=NOW + 20)] == [task]
    assert await db.events(people["colleague"], task)
    with pytest.raises((NotYours, ValueError)):
        await db.act(people["colleague"], task, Action.MARK_DONE, at=NOW + 30)

    with pytest.raises(NotYours):
        await db.get(people["stranger"], task, at=NOW + 20)
    assert await db.tasks_of(people["stranger"], at=NOW + 20) == []


async def test_a_removed_member_stops_seeing_at_once_and_keeps_what_they_posted(db, people, library):
    org, project = library
    theirs = await _portrait(db, people["colleague"], people["painter"], org, project)
    owners = await _portrait(db, people["owner"], people["painter"], org, project, title="A bust of Mises")
    assert await db.remove_member(people["owner"], org, people["colleague"])
    with pytest.raises(NotYours):
        await db.org(people["colleague"], org)
    with pytest.raises(NotYours):
        await db.get(people["colleague"], owners, at=NOW + 1)
    assert [t["task_id"] for t in await db.tasks_of(people["colleague"], at=NOW + 1)] == [theirs]
    assert not await db.remove_member(people["owner"], org, people["colleague"])


async def test_a_professional_outside_the_organization_sees_only_what_their_own_roles_show(db, people, library):
    org, project = library
    task = await _portrait(db, people["owner"], people["painter"], org, project)
    seen = await db.get(people["painter"], task, at=NOW + 1)
    assert seen["your_roles"] == ["audience"]
    with pytest.raises(NotYours):
        await db.org(people["painter"], org)
    with pytest.raises(NotYours):
        await db.project(people["painter"], project)


# ---------------------------------------------------------------------------
# What a Project adds up to
# ---------------------------------------------------------------------------


async def test_a_project_counts_what_its_tasks_committed_and_paid(db, people, library):
    org, project = library
    task = await _portrait(db, people["owner"], people["painter"], org, project)
    withdrawn = await _portrait(db, people["owner"], people["painter"], org, project, title="A bust of Mises")
    await db.act(people["owner"], withdrawn, Action.WITHDRAW, at=NOW + 5)
    await db.act(people["painter"], task, Action.CLAIM, at=NOW + 10)
    await _pay_ahead(db, people, task, None, NOW + 20)
    await _pay_ahead(db, people, task, 0, NOW + 30 * DAY)

    seen = await db.project(people["colleague"], project, at=NOW + 30 * DAY + 1)
    assert seen["name"] == "Portraits for the reading room" and seen["budget_sats"] == 500_000
    assert seen["circle"] == [people["painter"]] and seen["defaults"]["venue"] == "Texas"
    assert seen["totals"] == {"committed_sats": 100_000, "paid_sats": 30_000, "tasks": 2,
                              "by_standing": {"claimed": 1, "withdrawn": 1}}


def test_a_graded_task_commits_what_it_earned_and_counts_the_award_once_paid():
    graded = {"rubric": RUBRIC, "standing": "owed", "deposit": {"sats": 10_000, "paid_at": 1},
              "milestones": [], "assessment": {"sats": 70_000}}
    paid = graded | {"standing": "paid"}
    assert orgs.project_totals([graded]) == {"committed_sats": 80_000, "paid_sats": 10_000, "tasks": 1,
                                             "by_standing": {"owed": 1}}
    assert orgs.project_totals([paid])["paid_sats"] == 80_000
