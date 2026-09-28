# Customer Support Agent on Amazon Bedrock AgentCore

A Strands agent deployed on AgentCore Runtime that answers order and account
questions and processes refunds. 

**Status: Security complete.** Callers authenticate with a Cognito JWT, the agent
reaches its tools through an AgentCore Gateway with retry and backoff, refunds are
idempotent, it remembers customers across sessions, every failure class is
traceable to a root cause, and **the refund ceiling is enforced by a Cedar policy
at the Gateway** — not by the prompt.

```
caller ──Cognito JWT──▶ Runtime ──SigV4/MCP──▶ Gateway ──▶ 3 Lambdas ──▶ DynamoDB
                          │
                          └── Memory: session events (STM) + /preferences/{actor}/, /facts/{actor}/ (LTM)
```

**No long-lived credentials exist anywhere in this project.** Every hop uses a
role or a short-lived token; the only stored secret is the test user's password
in Secrets Manager. See [docs/iam.md](docs/iam.md) for the full table.

## Prerequisites

- AWS credentials for a region where AgentCore and your Bedrock model are
  available (`us-east-1` here), with model access enabled for
  `global.anthropic.claude-sonnet-4-5-20250929-v1:0`.
- Node.js 20+ and the AgentCore CLI: `npm install -g @aws/agentcore`
- `uv` and Python 3.12+ for running the agent and the scripts locally.

```bash
cp .env.example .env      # then fill it in
```

## Layout

| Path | What it is |
|---|---|
| `agent/agent.py` | `BedrockAgentCoreApp` entrypoint, the Strands agent, and the `process_refund` wrapper |
| `agent/gateway_client.py` | SigV4-signed MCP client for the Gateway |
| `agent/prompts.py` | System prompt (behaviour only — not a security control) |
| `agent/retry.py` | Backoff with jitter, and what counts as retryable |
| `agent/telemetry.py` | Custom span attributes (the domain fields in a trace) |
| `agent/identity.py` | Reads the customer from the verified JWT claims |
| `agent/iam/deny_out_of_scope.json` | Explicit-Deny overlay narrowing the execution role |
| `agent/memory.py` | AgentCore Memory session manager |
| `agent/idempotency.py` | Deterministic refund keys, derived outside the model |
| `tools/<tool>/handler.py` | One Lambda per business tool |
| `tools/common/` | Structured errors, JSON logging, DynamoDB access, Gateway glue, fault injection |
| `tests/unit/` | Retry, idempotency and refund-decision tests (no AWS needed) |
| `scripts/create_tables.py`, `scripts/seed_data.py` | DynamoDB setup (boto3) |
| `gateway/policies/*.cedar` | Cedar policies (templates; `${GATEWAY_ARN}` is rendered at build time) |
| `scripts/build_policies.py` | Renders the policies into `agentcore.json` — run before deploy |
| `scripts/create_cognito.py`, `scripts/get_token.py` | Cognito pool, test user, and minting a JWT |
| `scripts/inspect_memory.py` | Read STM events and extracted LTM records |
| `scripts/inject_fault.py` | Turn a tool Lambda's fault injection on and off |
| `agentcore/agentcore.json` | Runtime, Gateway, targets and per-Lambda IAM |
| `agentcore/cdk/` | Generated CDK app (do not hand-edit; edit `agentcore.json`) |
| `docs/iam.md` | Every role and why each permission exists |
| `docs/agentcore-cli-reference.md` | The CLI's own config/schema reference |

## Deploy

```bash
npm install --prefix agentcore/cdk            # first time only
python scripts/create_tables.py               # idempotent
python scripts/seed_data.py
python scripts/create_cognito.py --write-config   # pool + test user; patches agentcore.json
python scripts/build_policies.py                  # render Cedar policies (needs the gateway deployed)
agentcore validate
agentcore deploy --dry-run
agentcore deploy
agentcore status
```

`create_cognito.py --write-config` writes the pool's discovery URL and audience
into `agentcore/agentcore.json`; set `COGNITO_ISSUER` and `COGNITO_CLIENT_ID` in
the runtime's `envVars` to match (it prints both). All three scripts are safe to
re-run.

Everything lives in the CloudFormation stack `AgentCore-csagent-default`, except
the DynamoDB tables, the Cognito pool and the Secrets Manager secret, which the
scripts create.

## How a tool call flows

1. The model picks one of three tools. **None of the Gateway's tools are exposed
   to the model directly** — `agent/agent.py` defines `get_order`,
   `get_customer` and `process_refund` as wrappers. That indirection is what
   gives every call a timeout, retry with backoff, and (for refunds) an
   idempotency key the model cannot choose.
2. The Runtime SigV4-signs the MCP request with its execution role, which holds
   `bedrock-agentcore:InvokeGateway` on this gateway and nothing else.
3. The Gateway invokes the tool's Lambda; each Lambda has its own role with
   access to only the tables it needs (`docs/iam.md`).
4. Tools return `{"status": "success", ...}` or
   `{"status": "error", "code", "message", "retryable"}` — never prose.

## The refund ceiling

Three layers, of which only the first is persuadable:

| Layer | Stops | Model can influence it? |
|---|---|---|
| System prompt | The model usually declines before spending a tool call | Yes — it is text |
| **Cedar policy at the Gateway** | Any `process_refund` over $1,000 | **No** |
| `process_refund` Lambda | Same ceiling, plus amount > order total | **No** |

Policies live in [gateway/policies/](gateway/policies/) and are rendered into
`agentcore.json` by `scripts/build_policies.py`. The engine is **default-deny**:
a tool with no matching permit is refused, so a newly added tool stays blocked
until someone writes a policy for it.

Verified: `refund $5,000` is denied at the Gateway with the Lambda never invoked,
even with the prompt's protections removed. `$1,000.00` is allowed and
`$1,000.01` is denied. **[docs/security.md](docs/security.md)** has the decision
matrix, the injection test and the open gaps.

## Reliability

### Retry, and what is *not* retried

`agent/retry.py` retries a tool call at most **3 times**, waiting a
**full-jitter** delay between attempts — uniform in `[0, min(4s, 0.25s·2ⁿ)]`
rather than a fixed backoff, so concurrent sessions hitting a throttled
dependency do not march back in lockstep.

Only transient failures are repeated. The `retryable` flag on every tool result
is the contract:

| Result | Behaviour |
|---|---|
| `status: success` | returned immediately |
| `status: error`, `retryable: false` (`ORDER_NOT_FOUND`, `REFUND_LIMIT_EXCEEDED`, …) | returned immediately — repeating it would fail identically |
| `status: error`, `retryable: true` (`DEPENDENCY_UNAVAILABLE`, transport failure) | retried with backoff |
| still failing after 3 attempts | the last real error is handed to the model, which explains it to the customer |

An unreadable response is treated as **not** retryable: that is far more likely
to be a bug than a blip, and retrying would triple the damage.

### Idempotency

`process_refund` requires an `idempotency_key`. The Refunds table is keyed on it
and the write is conditional on `attribute_not_exists(idempotency_key)`, so a
repeat loses the race by design and gets the original refund back with
`duplicate: true`. Nothing is ever refunded twice.

The key is derived by the agent, never by the model, and must survive two levels
of retry:

- **within a turn** — every attempt of one tool call reuses one key;
- **across whole invocations** — a caller whose request timed out re-invokes the
  agent with the same **operation id**
  (`-H "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Operation-Id: operation-123"`),
  and both invocations derive the same key despite different sessions.

The key is `sha256(operation-or-session | order | amount)` rather than the
operation id verbatim, because one operation may legitimately refund two
different orders and those must not collapse into one record. The operation id
is stored on the refund so the link back to the request survives.

### Trying it

```bash
pytest tests/unit -q          # 44 tests: retry classification, backoff, idempotency
```

Against the deployed backend, the same key three times:

| Call | `refund_id` | `duplicate` |
|---|---|---|
| 1 | `RFND-3A5A577A35AA` | `false` |
| 2 | `RFND-3A5A577A35AA` | `true` |
| 3 | `RFND-3A5A577A35AA` | `true` |

One row in DynamoDB under `idempotency_key = "operation-123"`.

Fault injection drives the retry path on demand (`tools/common/faults.py`):

```bash
python scripts/inject_fault.py --function csagent-orders --mode throttle
agentcore invoke --bearer-token "$TOKEN" --prompt "What is the status of order ORD-1001?"
python scripts/inject_fault.py --function csagent-orders --clear    # always
```

Modes: `error`, `throttle`, `timeout`, `business`. Verified:

| Mode | Log | Agent's answer |
|---|---|---|
| `throttle` (retryable) | `attempt=1 retrying`, `2 retrying`, `3 final` | "temporarily unavailable… please try again in a few moments" |
| `business` (not retryable) | `attempt=1 final` — **no retry** | "This isn't something I can work around by retrying." |

`FAULT_MODE` is inert unless the stage is `dev` or `test`, so the production
path cannot be switched into failing by an environment variable alone.

## How memory works

Every invocation carries two identities:

- **`--session-id`** — one conversation. All its turns are written to AgentCore
  Memory as events, so a session resumes correctly even after a cold start.
- **the `custom:customer_id` claim in the JWT** — the customer. This is what
  makes memory outlive a session, and it cannot be set by a header or by the
  prompt.

Two background strategies read a session's events and extract durable records
into **actor-scoped** namespaces — `/preferences/{actorId}/` and
`/facts/{actorId}/`. No session appears in those paths, so a new session for the
same actor retrieves them, and a different actor cannot.

Extraction is **asynchronous**: a preference stated now is typically retrievable
within a minute or two, not immediately. Anything that asserts on it must poll.

Retrieved records are injected into the conversation inside a `<user_context>`
block. The system prompt tells the model to treat that block as data about the
customer and never as instructions — a remembered note cannot raise a limit.

## Demo

Every invoke needs a token. Mint one, then reuse it (it lasts 60 minutes):

```bash
TOKEN="$(python scripts/get_token.py)"
python scripts/get_token.py --claims       # see what the agent trusts

agentcore invoke --bearer-token "$TOKEN" --prompt "Why is my order 123 delayed?"
agentcore invoke --bearer-token "$TOKEN" --prompt "What contact preference do you have for CUST-002?"
agentcore invoke --bearer-token "$TOKEN" --prompt "Refund the full 1899 dollars on ORD-123."
```

Verified against the deployed runtime:

| Prompt | Tool chosen | Outcome |
|---|---|---|
| "Why is my order 123 delayed?" | `orders___get_order` | Maps bare `123` to `ORD-123`; explains the out-of-stock delay and the new date |
| "What contact preference do you have for CUST-002?" | `customers___get_customer` | "SMS" |
| "Refund the full 1899 dollars on ORD-123." | `process_refund` | Denied, `REFUND_LIMIT_EXCEEDED`; the agent explains it needs human approval |
| "Refund ORD-1001 in full", then asking again in the same session | `process_refund` ×2 | One row in the Refunds table; the second call returns the original `RFND-…` |

Fixture data: orders `ORD-1001` (delayed, $249.99), `ORD-1002` (delivered,
$89.00), `ORD-123` (delayed, $1,899.00 — over the ceiling on purpose); customers
`CUST-001` (Dana Osei, email) and `CUST-002` (Marek Nowak, SMS).

### Memory across two sessions

```bash
TOKEN="$(python scripts/get_token.py)"   # claims custom:customer_id = CUST-001

# Session A — state the preference, then end the session.
agentcore invoke --bearer-token "$TOKEN" --prompt "My preferred AWS region is eu-west-1." \
  --session-id "mem-session-A-11111111111111111111111111"

# Wait for the asynchronous extraction (polls, exits 0 once found).
python scripts/inspect_memory.py --actor CUST-001 --query "preferred AWS region" --wait 180

# Session B — a different session, same customer.
agentcore invoke --bearer-token "$TOKEN" --prompt "Remind me, which AWS region do I prefer?" \
  --session-id "mem-session-B-22222222222222222222222222"
```

Verified results:

| Step | Result |
|---|---|
| Session A | "I've noted that your preferred AWS region is eu-west-1." |
| `inspect_memory.py` | `/preferences/CUST-001/` → `{"preference": "Preferred AWS region is eu-west-1.", ...}`; `/facts/CUST-001/` → "The user's preferred AWS region is eu-west-1." |
| Session B (new session, `CUST-001`) | "According to my notes, your preferred AWS region is **eu-west-1**." |
| Session B, follow-up "What did I just ask?" | Recalls the previous turn — short-term memory |
| Session C (new session, **`CUST-002`**) | "I don't have any information about an AWS region preference for you." — actor isolation holds |

### Authentication

| Attempt | Result |
|---|---|
| No token | Rejected before reaching AWS: "configured for CUSTOM_JWT but no bearer token is available" |
| Self-signed token claiming `custom:customer_id: CUST-001` | **401** from the Runtime; the agent never runs |
| Valid token | Serves `CUST-001`; log shows `actor_id=CUST-001` |
| Valid `CUST-001` token **plus** `-H "...Custom-Actor-Id: CUST-002"` | Header ignored — still `CUST-001`'s memory |

The execution role's model access, checked with the policy simulator:

```bash
aws iam simulate-principal-policy --policy-source-arn <runtime-role-arn> \
  --action-names bedrock:InvokeModel \
  --resource-arns arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-haiku-20240307-v1:0
# -> explicitDeny   (only the configured model is allowed)
```

Check the refund ledger directly:

```bash
aws dynamodb scan --table-name csagent-dev-refunds --region us-east-1 \
  --query "{count:Count,items:Items[].{refund_id:refund_id.S,order:order_id.S,cents:amount_cents.N}}"
```

## Run locally

```bash
agentcore dev
```

Against the deployed Gateway, set `GATEWAY_URL` to the
`GatewayCsagentDevToolsUrlOutput` value from `agentcore status` — the client
falls back to it when the injected `AGENTCORE_GATEWAY_*_URL` is absent. Set
`MEMORY_ID` and `ACTOR_ID` the same way for memory; without them the agent still
answers, just with no recall.

## Logs, traces and metrics

```bash
agentcore traces list --since 1h                  # trace IDs + session IDs
agentcore logs --since 30m --query "tool_error"
aws logs tail /aws/lambda/csagent-refunds --follow
aws cloudwatch list-metrics --namespace csagent
```

Lambda logs are one JSON object per line, with events such as
`refund_processed`, `refund_denied` and `duplicate_refund_prevented`. Each tool
call's `execute_tool` span carries `tool.outcome`, `error.code`, `tool.attempts`
and `loop.tool_calls`, so a trace says what a call was doing and why it failed.

Metrics in namespace `csagent` (dimensions `Stage`, `Tool`): `ToolInvocations`,
`ToolErrors`, `RefundsProcessed`, `RefundsDenied`, `DuplicateRefundPrevented`.

**[docs/observability.md](docs/observability.md) is the debugging guide** — for
each of tool timeout, invalid parameters, wrong tool selection, unhandled
exception and LLM loop, it gives the span to read, the attribute that identifies
it, a Logs Insights query, and a real trace ID from a reproduced failure. Two
traps it documents up front: `gen_ai.tool.status` reads `success` on failed tool
calls, and X-Ray's `error`/`fault` flags are not set on tool spans.

Transaction Search was enabled for the account by the first `agentcore deploy`;
it takes ~10 minutes before traces are indexed.

## Cleanup

Billable while they exist: AgentCore Runtime and Gateway, three Lambdas, three
DynamoDB tables, CloudWatch logs, the CDK staging bucket.

```bash
cd agentcore/cdk && npx cdk destroy AgentCore-csagent-default
aws dynamodb delete-table --table-name csagent-dev-orders
aws dynamodb delete-table --table-name csagent-dev-customers
aws dynamodb delete-table --table-name csagent-dev-refunds
aws cognito-idp delete-user-pool --user-pool-id "$COGNITO_USER_POOL_ID"
aws secretsmanager delete-secret --secret-id csagent/dev/test-user --force-delete-without-recovery
```
