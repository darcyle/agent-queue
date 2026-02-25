# UX Feedback

## Task Query Defaults

- **Default behavior** should show only **active** (IN_PROGRESS) and **pending** (DEFINED/READY) tasks for the current project — not completed tasks.
- Completed tasks clutter the view and should be opt-in.
- Need a way to list **all tasks** (including completed) explicitly.
- Need a way to list **active tasks across all projects** (not just the current one) — but this should not be the default.

## Task Dependencies

- It would be nice to show **dependencies between tasks** in task listings.
- Only if it can be done without being confusing or cluttering the display.
- Could use indentation, arrows, or a separate section — keep it lightweight.

## Multi-device / single Discord app
- Goal: run agent-queue on more than one device, with only one Discord app installed, using a **different channel per device**
- Currently running two separate Discord apps → causes conflicting Discord actions
- Need to research: what happens when two agents are both notified of an action?
- Possible approach: routing logic based on channel → device mapping
- **Research needed before implementing**
