# Explicit content order contract

When a user explicitly requires an order, the Agent maps each required outline node to exact current Draft blocks and source refs, checks the declared linear sequence, and reports `PASS` or `FAIL`. Ambiguous mapping or missing nodes must be reported, not guessed. The same Outline, candidate, and blocks must produce the same mapping and readback. A stale Draft or changed candidate requires a new check.

没有用户明确顺序要求时结果为 `NOT_APPLICABLE`，不得从章节顺序或重排许可推断用户约束。

This is a deterministic precedence check. It does not build a generic causal graph, run Core NLP or an LLM judge, add a workflow gate, or automatically reorder content.
