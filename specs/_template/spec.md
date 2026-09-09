---
id: <spec-id>
type: architecture
status: draft
created: YYYY-MM-DD
---

# <Title>

## Summary

Describe the current behavior, the concrete problem, the intended outcome, and explicit non-goals.

## Design

Describe contracts, data flow, security boundaries, rollout, rollback, and material alternatives.
For runtime architecture, cross-layer contracts, runtime deployment or related data migration,
follow and cite the [four-layer runtime baseline](../runtime-four-layer-definition/spec.md).
Explain the affected responsibilities, handoff contracts and dependency directions here.
For assembly, Console or Evaluation changes, describe their runtime boundary when relevant;
these supporting systems are not message layers. No extra section or not-applicable form is required.

## Acceptance

List observable acceptance criteria. Do not encode implementation path allowlists or document inventories.

## Verification

List the checks that establish the acceptance criteria. Recorded results may use any clear format.
