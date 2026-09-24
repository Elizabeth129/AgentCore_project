# Customer Support Agent on Amazon Bedrock AgentCore

A Strands agent deployed on AgentCore Runtime that answers order and account
questions and processes refunds. See `CLAUDE.md` for the full architecture and
the acceptance criteria this repo is built against.

**Status: Identity / IAM complete.** Callers authenticate with a Cognito JWT,
the agent reaches its tools through an AgentCore Gateway, and it remembers
customers across sessions. The Cedar policy is still ahead.

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
| `agent/identity.py` | Reads the customer from the verified JWT claims |
| `agent/iam/deny_out_of_scope.json` | Explicit-Deny overlay narrowing the execution role |
| `agent/memory.py` | AgentCore Memory session manager |
| `agent/idempotency.py` | Deterministic refund keys, derived outside the model |
| `tools/<tool>/handler.py` | One Lambda per business tool |
| `tools/common/` | Structured errors, JSON logging, DynamoDB access, Gateway glue |
| `scripts/create_tables.py`, `scripts/seed_data.py` | DynamoDB setup (boto3) |
| `scripts/create_cognito.py`, `scripts/get_token.py` | Cognito pool, test user, and minting a JWT |
| `scripts/inspect_memory.py` | Read STM events and extracted LTM records |
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

1. The model picks a tool. Two of them, `orders___get_order` and
   `customers___get_customer`, come straight from the Gateway's tool list.
2. `process_refund` is different: the Gateway tool `refunds___process_refund` is
   **hidden from the model** and wrapped by a local tool in `agent/agent.py`.
   The wrapper derives `idempotency_key` from the order, the amount and the
   session, so a re-plan or a retry cannot produce a second refund.
3. The Runtime SigV4-signs the MCP request with its execution role, which holds
   `bedrock-agentcore:InvokeGateway` on this gateway and nothing else.
4. The Gateway invokes the tool's Lambda; each Lambda has its own role with
   access to only the tables it needs (`docs/iam.md`).
5. Tools return `{"status": "success", ...}` or
   `{"status": "error", "code", "message", "retryable"}` — never prose.

The $1,000 refund ceiling is enforced in the `process_refund` Lambda, against
both the requested amount and the order total in DynamoDB. The system prompt
mentions it only so the agent can explain itself; it is not the control.

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

## Logs and traces

```bash
agentcore logs
agentcore traces
aws logs tail /aws/lambda/csagent-refunds --follow
```

Lambda logs are one JSON object per line, with events such as
`refund_processed`, `refund_denied` and `duplicate_refund_prevented`.

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
