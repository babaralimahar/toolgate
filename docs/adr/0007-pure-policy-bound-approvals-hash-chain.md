# ADR 0007: decide() is pure, approvals bind to the call, audit is a hash chain

**Status:** accepted (P5 ToolGate)

## Context

Several teams want to connect AI clients to internal systems. Security needs one place that
answers "who may call what, with which arguments, and what did they do", with answers that are
explainable and testable, and that keep working when an agent misbehaves.

## Decisions

1. **Policy as a pure function.** `decide(policy, client, tool, args, ctx) -> Decision` does no
   I/O; kill-switch state is passed in. Every decision is a table-test row, and every audit row
   carries the reason code. The YAML schema is strict and has no way to say "allow by default".
   Alternatives considered: OPA/Rego and Cedar (better at large scale and shared across
   services); a 60-line evaluator is enough for 4 tools and easier to review here.
2. **Approvals bind to the call, not the tool.** The approval token is valid once, for the
   client, tool and SHA-256 of the canonical arguments it was granted for, before expiry,
   enforced by one atomic UPDATE. An approval for invoice 42 can't be used for invoice 43, by
   another agent, or twice.
3. **Hash-chained, append-only audit in Postgres.** Gapless sequence assigned under an advisory
   lock; each row hashes the previous hash plus its canonical content; a trigger refuses edits.
   No new infrastructure, and verification is a single pass.
4. **No unaudited actions.** If the audit write fails, the call fails.
5. **Static tool registry.** Tools are code, reviewed like code; there is no dynamic
   registration, so no tool shadowing.

## Consequences

- Audited throughput is bounded by the audit lock (one chain head). Measured overhead is
  ~33 ms p50 on a Windows/Docker dev box, dominated by database round trips; sharding the chain
  per tool or batching are the next steps if needed.
- A superuser can rewrite the chain consistently: anchor the head hash externally for that.
- Caches (kill switch, identity) trade up to 1 s of staleness for one fewer database round trip
  per call; the admin kill-switch endpoint invalidates the local cache immediately.
