- The task record and its lifecycle. `taskbooth_mcp.lifecycle` says which moves are legal
  (Posted → Claimed → Done → Assessed → Owed → Paid, or Rejected, with Withdrawn, Appealed
  and a read-time Expired) and who may make each one. `taskbooth_mcp.store` keeps tasks,
  their append-only history, payouts and rubric templates on the operator's Neon schema,
  with every title, rubric, amount and reason encrypted. No tool uses them yet.
- A recurring task's next occurrence is posted by the call that marks the current one
  done, or by the first read that finds it expired. Nothing runs in the background.
