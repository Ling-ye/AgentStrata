# AgentStrata QQ message flow v1

This manual Suite verifies the AgentStrata-owned path after a hypothetical QQ
message exists. It uses synthetic identities, loopback WebSockets, temporary
protected state, and a deterministic Agent sentinel. It never connects or
writes to real QQ and is not evidence for real NapCat or cc-connect runtime
behavior.

`quick` covers the positive owned-chain path, attestation failure closing, and
persona persistence into the next PromptPlan. `full` runs all seven Cases;
`security` selects the four ingress and authorization negative Cases.

The persona Case starts from a sender envelope plus one-shot transport
attestation, resolves the synthetic sender as a configured Owner, and runs the
production host persona entry with a deterministic PersonaDraftAgent substitute.
It requires a successful protected-state mutation receipt and then proves that a
fresh ACP host loads the new group persona into the next PromptPlan.
