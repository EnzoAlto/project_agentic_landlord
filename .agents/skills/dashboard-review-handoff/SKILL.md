---
name: dashboard-review-handoff
description: Pause after implementing and locally validating changes that affect the project_agentic Streamlit dashboard so the user can launch the app and visually review them before further UI iteration. Do not use for backend-only changes with no dashboard impact.
---

# Dashboard Review Handoff

For changes that affect the dashboard's appearance, controls, or interactive
behavior:

1. Implement the requested change and run appropriate nonvisual checks, such as
   compilation, focused backend tests, and Streamlit AppTest when useful.
2. Do not start, restart, or open the dashboard for the user unless they
   explicitly request that action in the current turn.
3. Do not claim that the visual result was reviewed when only automated checks
   were performed.
4. After the code and checks are complete, stop further UI iteration and end
   with this handoff:

   `Changes made. Please run the dashboard app and review the changes. Let me know what you would like adjusted.`

If checks fail or the change is incomplete, report the concrete problem instead
of giving the successful-review handoff. Resume dashboard work after the user
returns with feedback or explicitly asks to continue.
