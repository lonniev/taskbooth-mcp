# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Changes not yet released live in `changelog.d/`, one file per change — see the README there for why, and `scripts/changelog.py` for what folds them in at release time.

## [0.4.0] — 2026-11-18

### Added

- Escrow at a Cashu mint, with no one needing a Cashu wallet. A cover page may name an arbiter (`arbiter_npub`) and ask for escrow (`escrow`, `escrow_mint`, `refund_days`). The purchaser pays the Balance by a Lightning invoice into a token locked to the purchaser's, performer's and arbiter's Nostr keys: any two may split it, and after the refund date the purchaser alone may take it back. Each share is paid to its owner's payout address. New tools: `escrow_invoice`, `escrow_status`, `escrow_propose`, `escrow_collect`, `escrow_sign`. The Operator builds and submits every step and holds no key that can move the sats.
- The owner lists the mints offered for escrow in the operator property `escrow_mints`; none are offered until it is set.
- A named arbiter signs the offer, decides appeals in the tie-breaker's place, and cannot claim the job. A Project may name a default arbiter.
- Standard Terms v1 (current): section 6 describes Cashu escrow, section 8 the arbiter, and the name MakerMarket is gone. Jobs signed under v0 keep v0.

## [0.3.0] — 2026-11-17

### Added

- Organizations and Projects: `save_organization`, `list_organizations`, `get_organization`, `save_member`, `remove_member`, `save_project`, `archive_project` and `get_project`. An Organization's owner adds members by npub; members see its Projects and every task filed in them (a new read-only `member` role) and may post tasks into them; only the owner changes the Organization, its members or its Projects. A Project carries a brief, a budget, a default circle and default cover terms, and `get_project` adds up what its tasks committed and paid.
- `post_task`'s `org_id` and `project_id` are now checked: both or neither, a Project of that Organization, not archived, and the poster must own or belong to the Organization.
- Sites sharing this Operator: `post_task`, `list_tasks` and `calendar_publish` take an optional `site` (e.g. "chipin"). A task is stamped with the site that posted it; a site lists its own tasks and every task posted with no site; a feed keeps to its site. Omitting `site` lists every task, as before.

## [0.2.1] — 2026-11-17

### Fixed

- `service_status` reports the released version again; it had stayed at 0.1.0 after the 0.2.0 release.

## [0.2.0] — 2026-11-17

### Added

- The MakerMarket Standard Terms, version v0, are published in the Operator, and each version is fixed by the sha256 of its text. `taskbooth_standard_terms` returns the text and hash, and is free. Escrow is not offered under v0, so every job is paid over Lightning.
- A task may carry a cover page (`cover` on `post_task` and `edit_task`) with a spec link and hash, deliverables, a deemed grade and its period, an IP option, and confidentiality. The Operator stamps the terms version and hash on it, and refuses escrow fields for now.
- Finished work left ungraded past the cover page's deemed-acceptance period is graded at the deemed grade on the next read, marked `deemed`. The performer may still appeal it.
- Milestones: dated part-payments of the top grade (`milestones` on `post_task` and `edit_task`). `taskbooth_request_milestone` and `taskbooth_confirm_milestone` work like the deposit's tools, from each milestone's date. With the deposit they ask for at most 90%.
- An assessment now reports `paid_ahead_sats` (the deposit plus confirmed milestones) in place of `deposit_sats`, and the award pays only what the grade earns beyond it.
- A performer who gives a job back records `deposit_owed_back`; one released by the purchaser keeps the deposit, and either way keeps paid milestones.

- A task with a cover page is a contract once both parties sign one offer (terms 2.3). `taskbooth_offer_terms` shows the offer as one statement, with its sha256, that names every term and nothing about who claims it.
- The purchaser signs before anyone may claim, and the performer signs before marking the work done. An edit clears the purchaser's signature, and a released performer's signature goes with them.
- Two ways to sign, with no new Nostr event kind:
  - `taskbooth_sign_offer`, called as a NIP-98 signed request, keeps that request as the signature. This is for agents with their own key.
  - `taskbooth_request_offer_signature` and `taskbooth_receive_offer_signature` sign by replying to a DM, kept with this Operator's witness.
  - Every signature is checked before it is kept.

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
