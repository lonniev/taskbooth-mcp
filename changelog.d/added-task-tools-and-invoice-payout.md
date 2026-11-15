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
