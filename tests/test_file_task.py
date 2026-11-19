# ruff: noqa: F811 — the fixtures are imported, then named as test arguments
"""A task is moved into a Project, or between Projects, by its purchaser, whatever its state."""

import pytest

from taskbooth_mcp.lifecycle import Action
from taskbooth_mcp.store import NotYours
from tests.signing import agree
from tests.test_escrow import family, keys  # noqa: F401 — fixtures
from tests.test_late_steps_and_failure import _page
from tests.test_store import NOW, _filed_in, _post, queue  # noqa: F401 — fixtures


async def test_an_unfiled_chore_is_moved_into_a_project(queue, family):
    _, db = queue
    chore = await _post(db, family)
    where = await _filed_in(db, family["parent"])
    moved = await db.file(family["parent"], chore, **where, at=NOW + 5)
    assert (moved["org_id"], moved["project_id"]) == (where["org_id"], where["project_id"])
    events = await db.events(family["parent"], chore)
    assert events[-1]["action"] == "file" and events[-1]["project_id"] == where["project_id"]
    assert events[-1]["from"] == events[-1]["to"] == "posted"


async def test_a_signed_commission_moves_and_its_signatures_stand(queue, family, keys):
    _, db = queue
    task = await _post(db, family, cover=_page(family))
    await agree(db, keys["parent"], task)
    await agree(db, keys["arbiter"], task)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    await agree(db, keys["child"], task)
    other = await _filed_in(db, family["parent"])
    moved = await db.file(family["parent"], task, **other, at=NOW + 20)
    assert moved["project_id"] == other["project_id"] and moved["state"] == "claimed"
    assert moved["agreement"]["purchaser_signed"] and moved["agreement"]["performer_signed"]


async def test_only_the_purchaser_moves_it_and_only_into_a_project_they_may_post_in(queue, family):
    _, db = queue
    task = await _post(db, family)
    mine = await _filed_in(db, family["parent"])
    with pytest.raises(NotYours):
        await db.file(family["child"], task, **mine, at=NOW + 5)
    theirs = await _filed_in(db, family["child"])
    with pytest.raises(ValueError, match="Organization you are in"):
        await db.file(family["parent"], task, **theirs, at=NOW + 5)
    with pytest.raises(ValueError, match="Name the Project"):
        await db.file(family["parent"], task, "", "", at=NOW + 5)
    with pytest.raises(ValueError, match="not in that Organization"):
        await db.file(family["parent"], task, mine["org_id"], theirs["project_id"], at=NOW + 5)
