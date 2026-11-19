---
name: dashboard-review-handoff
description: Hand off project_agentic dashboard changes for the user's review before optional testing or further UI iteration. Also records the workspace preference to avoid creating test scripts unless requested.
---

# Dashboard Review Handoff

For changes that affect the dashboard's appearance, controls, or interactive
behavior:

1. Implement the requested code changes, then let the user run and review the
   dashboard. Do not make automated tests a prerequisite for this handoff.
2. Do not start, restart, or open the dashboard for the user unless they
   explicitly request that action in the current turn.
3. Do not claim that the visual result was reviewed when only automated checks
   were performed.
4. After the code changes are complete, stop further UI iteration and end
   with this handoff:

   `Changes made. Please run the dashboard app and review the changes. Let me know what you would like adjusted.`

Testing is an optional follow-up after the user's review. Ask whether they want
tests only when useful for the change, and run them if requested. Do not create
new Python test scripts for dashboard or backend changes unless the user asks.
Existing tests may remain in the workspace. Brief syntax checks are acceptable
when needed; report exactly what was checked and never claim unperformed tests.

If a known error leaves the change incomplete, report it rather than implying
success. Resume dashboard work after the user returns with feedback or
explicitly asks to continue.
