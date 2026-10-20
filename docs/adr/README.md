# Architecture Decision Records

Each ADR records one significant decision: its context, the decision, and its
consequences. ADRs are immutable once accepted; to change a decision, add a new
ADR that supersedes the old one.

| ADR | Title | Status |
| - | - | - |
| [0001](0001-python-and-pinned-sdk-behind-adapter.md) | Python, with the official SDK pinned behind our own adapter | Accepted |
| [0002](0002-llm-proposes-deterministic-risk-disposes.md) | The LLM proposes, deterministic code disposes | Accepted |
| [0003](0003-autonomy-envelope.md) | Agent autonomy as a config-defined envelope (playbooks + bounded discretion) | Accepted |
| [0004](0004-runtime-cloud-paper-vps-live.md) | Paper trading in Claude Code cloud sessions; live trading on a self-hosted VPS | Accepted |
| [0005](0005-paper-fill-simulator.md) | Conservative in-house fill simulator for paper trading | Accepted |
| [0006](0006-results-on-git-branch.md) | Paper results persisted to a `paper-results` git branch | Accepted |
| [0007](0007-engineering-workflow.md) | Engineering workflow: protected main, PRs, CI, phase tags | Accepted |
| [0008](0008-short-window-regime-aware-evaluation.md) | Short-window, regime-aware strategy evaluation with frequent iteration | Accepted |
| [0009](0009-position-management-and-exit-plans.md) | Agent-managed positions: adding, exit plans, trailing stops and runners | Accepted |
| [0010](0010-decision-process-and-learning-loop.md) | Scenario-based decisions and an agent-owned learning loop | Accepted |

Template: copy an existing ADR and keep the headings *Context*, *Decision*,
*Consequences*.
