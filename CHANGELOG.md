# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Changes not yet released live in `changelog.d/`, one file per change — see the README there for why, and `scripts/changelog.py` for what folds them in at release time.

## [0.1.0] — 2026-11-17

### Added

- `taskbooth_btc_price(fiat, years)`: what one bitcoin costs today and on this day in earlier years, read from mempool.space and passed on with the time each price was observed. Free, and takes no npub.

- Calendar feeds: `taskbooth_calendar_publish` mints a feed of your tasks reached by an unguessable token, `taskbooth_calendar_fetch` returns it as iCalendar for the site that serves it, and `taskbooth_calendar_list` and `taskbooth_calendar_revoke` manage it. A feed names nobody and carries no npub. It is built on each read, with each task on its due day in the time zone given.

- A task may ask for a deposit: `deposit_pct` (0–90) of its top grade, set on `post_task` or `edit_task` while open. On claim the deposit is recorded; `taskbooth_request_deposit` returns a Lightning invoice for exactly that amount from the performer's wallet (its payment hash written on the task first), and the performer confirms it with `taskbooth_confirm_deposit`. A confirmed deposit is netted from the award: the assessment keeps `earned_sats` and `deposit_sats`, and the payout invoice asks only for the rest. Tasks with no deposit behave as before.

- A task carries a late rule: pay in full, take a percentage off the grade for each day late (never below zero), or cancel it if missed. Graded late work under a discount says what it would have earned and why it earns less.
- A cancel-if-missed task closes itself as **missed** on the first read after its deadline; a repeating one then comes back once. Every task says how many days late its work is or was.
- `edit_task` changes a posted task's terms while nobody has claimed it; the purchaser's old signature is cleared.
- `nudge_task` lets the performer of an owed task remind the purchaser to pay, and the purchaser remind the performer a deadline is near (two days) or past; one of each per task every 12 hours.

- `assets/og-image.png`: a share image for listings.

- The task record and its lifecycle. `taskbooth_mcp.lifecycle` says which moves are legal
  (Posted → Claimed → Done → Assessed → Owed → Paid, or Rejected, with Withdrawn, Appealed
  and a read-time Expired) and who may make each one. `taskbooth_mcp.store` keeps tasks,
  their append-only history, payouts and rubric templates on the operator's Neon schema,
  with every title, rubric, amount and reason encrypted. No tool uses them yet.
- A recurring task's next occurrence is posted by the call that marks the current one
  done, or by the first read that finds it expired. Nothing runs in the background.

- Eighteen tools that take a task from posted to paid: `post_task`, `list_tasks`, `get_task`,
  `task_history`, `claim_task`, `release_claim`, `withdraw_task`, `mark_done`, `assess_task`,
  `reject_task`, `accept_assessment`, `appeal_assessment`, `decide_appeal`,
  `delegate_assessor`, `request_payout`, `confirm_paid`, `save_rubric_template` and
  `list_rubric_templates`. Each is unpriced until the Operator prices it in Pricing Studio.
- Pay by invoice. `request_payout` fetches an invoice from the performer's Lightning address
  for exactly the sats the grade earns, records its payment hash, and returns it with a plain
  `lightning:` link. The purchaser pays from any wallet and the performer confirms receipt.
- A `payout_address` patron property: the Lightning address a performer is paid at. A task
  that can earn sats cannot be claimed without one.

- Task Booth, a Tollbooth DPYC Operator where work is posted with a signed rubric and paid in sats after it is graded. This first version serves the standard DPYC tools only, under the `taskbooth_` prefix, so the service can be stood up on Horizon and adopted before its own tools arrive.

### Changed

- A performer no longer needs a `payout_address` to claim a task.
- `taskbooth_request_payout` takes the performer's own invoice: a performer with no Lightning address makes one for exactly the sats owed in any wallet and passes it as `invoice`. It is checked for amount and expiry, and its payment hash recorded, like a fetched one.

- Requires tollbooth-dpyc 0.121.0, for its BOLT11 invoice reader.

- tollbooth-dpyc 0.122.0, for `tollbooth.ical`.
