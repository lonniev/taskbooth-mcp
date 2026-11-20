# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Changes not yet released live in `changelog.d/`, one file per change — see the README there for why, and `scripts/changelog.py` for what folds them in at release time.

## [0.9.1] — 2026-11-19

### Changed

- The MCP Registry listing carries a title (Task Booth) and a link to https://cafe.tollbooth-dpyc.com/products/taskbooth-mcp.

## [0.9.0] — 2026-11-19

### Changed

- Standard Terms v1, still a draft and edited in place, after a peer review:
  - §4.8: with no Arbiter, the Worker may still appeal, and the Client (or the Client's tie-breaker) decides.
  - §4.10: every late rule is covered: full, a percentage per day, late steps, or ending as missed.
  - New §5.11: a payment that has reached a party is final, and only escrow can go back to the Client.
  - §6.7 states the refund-date floor the code already enforces.
  - New §9.8: a moral-rights waiver on assignment.
  - §14.3: a Worker who gives the job back keeps the deposit. §14.9 points to §5.11.
- The signed offer writes out every term. Every late rule reads as the code applies it; before, late steps read "paid in full". The offer also lists the spec link, each deliverable, the deemed grade, the failure point, IP, confidentiality, third-party material, governing law and venue. Free text is flattened to one line, so no field can add a line of its own.

### Removed

- The cover page and Project defaults no longer ask whether the client is a consumer: every party to a commission is there by choice. A `client_is_consumer` field is refused like any unknown field.

## [0.8.0] — 2026-11-19

### Added

- `file_task(task_id, org_id, project_id)`: the purchaser moves a task into a Project, or from one Project to another, in any state. Its offer and signatures stand, and its terms may differ from the Project's defaults, which only start new tasks.

## [0.7.0] — 2026-11-19

### Changed

- A cover page sets its own **appeal window**, **arbiter's ruling time** and **payment window** (`appeal_hours`, `ruling_hours`, `pay_hours`, 1 hour to 90 days, 7 days when left out) in place of the fixed 7 days. The appeal window also bounds a claim of cause, the escrow refund floor follows all three, and the offer states them. Standard Terms v1 4.6, 4.7, 7.2 and 14.8 say so. A cover signed before reads as 7 days.
- A commission (a task with a cover page) must be filed in a Project of an Organization you are in. Chores and existing commissions are unaffected.
- The reputation ranks' thresholds are six operator properties (`rank_min_finished`, `rank_proven_pct`, `rank_trusted_pct`, `rank_trusted_finished`, `rank_exemplary_pct`, `rank_exemplary_finished`); `reputation` returns them as `ranks`.

## [0.6.0] — 2026-11-19

### Added

- An **inspector** a cover page may name (`inspector_npub`): they sign the offer like the arbiter, and once the work is delivered give one signed, advisory opinion of it — verified, concerns or not verified — with `inspect_task`, or by DM with `request_inspection_signature` and `receive_inspection_signature`. It grades nothing and holds no escrow key. Standard Terms v1 gains 1.7 and 8.6–8.10.
- `reputation(of_npub)`: what any npub did on commissions as client, artist, arbiter and inspector, as counts only. Chores without a cover page do not count.

## [0.5.0] — 2026-11-18

### Added

- Late steps: `late={"kind": "steps", "steps": [{"after_hours": 0, "pct_off": 10}, {"after_hours": 72, "pct_off": 25}]}` takes off the percentage of the last step the work is more than that many hours late.
- A cover page's failure point, `fail_after_hours`: a claimed job still undelivered that long past needed_by ends as missed, for failure to deliver (terms 14.7). Escrow needs one, and the refund floor counts from it in place of the fixed 7 days' grace.
- `taskbooth_claim_cause` (the performer, once, within 7 days) and `taskbooth_decide_cause` (the arbiter: upheld or rejected). A task's `failure` says when cause may be claimed until and whether the failure is without cause; a failure without cause on an escrowed job reads `escrow_due_to: "client"` (terms 14.10). Both tools start unpriced.
- Standard Terms v1, still a draft, edited in place: 4.10 late steps; 14.3 drops the fixed 7 days; 14.7–14.12 failure to deliver, cause, no clawback of paid amounts, the whole escrow to the client, and handed-over materials become the client's.

## [0.4.4] — 2026-11-18

### Added

- Every escrow item to sign now carries `parts`: the inputs, outputs and quote its message is built from, so a signer's client rebuilds and checks the message itself instead of signing a digest it is handed.

## [0.4.3] — 2026-11-18

### Changed

- The Escrow mints setting suggests `https://testnut.cashu.space`, shown as an example in the empty field, and says it is a test mint whose sats are not real.

## [0.4.2] — 2026-11-18

### Added

- On a job under the Standard Terms, the performer now has 7 days from a grade to appeal it (terms 4.6). After that the grade is accepted as it stands, and a late appeal is refused. Tasks without a cover page keep no appeal clock.

## [0.4.1] — 2026-11-18

### Changed

- Escrow now needs a deemed grade, and its refund period must be at least 21 days plus the deemed-acceptance period (7 days' grace for a late delivery, 7 to appeal, 7 for the arbiter to decide). A shorter one let the purchaser take the escrow back alone while an appeal was still open.

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
