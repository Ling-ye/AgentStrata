---
name: harness-code-health
description: Use after a Code Health finding selects AgentStrata Harness code for a behavior-preserving improvement.
---

# Harness Code Health

Use this procedure only for the selected Harness finding in the current Code Health task. The frozen task goal, golden principles, role assignment, permissions, and host verification remain authoritative.

1. Read the selected finding, its exact baseline excerpts, and the relevant current Harness contract. Follow the actual caller to the narrowest place that owns the behavior.
2. Explain the concrete failure path or duplicated responsibility before changing code. Keep the candidate within the selected finding and its affected paths.
3. Check the changed behavior at its real boundary. Report which checks ran and distinguish a local check from the host's frozen verification and external delivery.
4. If the evidence changes the root cause or requires a protected contract change, return that finding to the host instead of broadening the edit.

Read [references/evidence.md](references/evidence.md) only when deciding whether a previous repair lesson applies to this finding.
