# Identity and IAM

Who may call what, and why each permission exists.

## The one-line version

There are **no long-lived credentials anywhere in this project**. Every
component authenticates with something short-lived or role-based:

| Hop | Mechanism | Credential at rest |
|---|---|---|
| Caller → Runtime | Cognito JWT (ID token, 60 min) | none — minted per run |
| Runtime → Bedrock | execution role | none |
| Runtime → Gateway | SigV4 with the execution role | none |
| Runtime → Memory | execution role | none |
| Gateway → Lambda | gateway execution role | none |
| Lambda → DynamoDB | per-Lambda execution role | none |
| Test harness → Cognito | username + password from Secrets Manager | one secret, generated, never in the repo |

The only stored secret is the test user's password in Secrets Manager
(`csagent/dev/test-user`), created with a random value by
`scripts/create_cognito.py` and read back by `scripts/get_token.py`. It never
appears in a file, an argument, or shell history.

Role names are CloudFormation-generated. Find the current ones with:

```bash
aws cloudformation describe-stack-resources --stack-name AgentCore-csagent-default \
  --query "StackResources[?ResourceType=='AWS::IAM::Role'].[LogicalResourceId,PhysicalResourceId]" --output table
```

## Inbound authentication

The Runtime's `authorizerType` is `CUSTOM_JWT`, pointed at the Cognito pool's
discovery URL with `allowedAudience` set to the app client id. AgentCore
validates the token's signature, issuer, audience and expiry **before the agent
process is invoked**. A request with no token, a forged token or an expired one
returns 401 and never reaches our code.

### Where the customer identity comes from

`custom:customer_id` — a Cognito user attribute carried in the ID token. The
agent reads it in `agent/identity.py` and uses it as the memory actor. This is
the requirement that the identity come from verified token claims
rather than from user text.

Three properties follow, and each is tested:

1. **A forged token cannot assert it.** Self-signing a token with
   `"custom:customer_id": "CUST-001"` gets a 401 from the service.
2. **A request header cannot override it.** `requestHeaderAllowlist` contains
   only `Authorization`, so the `...Custom-Actor-Id` header used before this
   step is no longer forwarded, and `identity.py` does not read it.
3. **The prompt cannot change it.** The identity is resolved before the model
   runs and is never passed through the tool schema.

`agent/identity.py` does not re-verify the signature — the authorizer has
already done so, and re-doing it would mean a JWKS fetch per cold start for no
extra guarantee. It *does* re-check `iss` and `aud` against `COGNITO_ISSUER` and
`COGNITO_CLIENT_ID`, which catches the case where `agentcore.json` points at a
different pool than the one issuing tokens. A token that arrives but carries no
usable identity raises rather than falling back to anonymous: serving it would
mean guessing whose data to open.

## Runtime execution role

`AgentCore-csagent-default-ApplicationAgentCsagentDe-*`, trusted by
`bedrock-agentcore.amazonaws.com`.

| Permission | Why it is needed |
|---|---|
| `bedrock:InvokeModel`, `InvokeModelWithResponseStream`, `CountTokens` | The agent calls Claude each turn; the streaming variant because the entrypoint streams. **Narrowed to one model** — see below. |
| `bedrock-agentcore:InvokeGateway` on the `csagent-dev-tools` gateway ARN | The only way the agent reaches its tools. |
| `bedrock-agentcore:RetrieveMemoryRecords`, `ListMemoryRecords`, conditioned on the namespace matching `/preferences/*/` or `/facts/*/` | Long-term recall, limited to the two namespaces this project defines. |
| `bedrock-agentcore:CreateEvent`, `GetEvent`, `ListEvents`, `ListSessions`, `ListActors`, `GetMemory`, `GetMemoryRecord`, `DeleteEvent` | Short-term memory. `DeleteEvent` is used by the Strands session manager when it rolls back a partially written turn. |
| Logs on `log-group:/aws/bedrock-agentcore/runtimes/*` | Agent logs, and `agentcore logs` reading them back. |
| `logs:DescribeLogGroups`, `xray:PutTraceSegments`, `xray:PutTelemetryRecords` on `*` | OTEL span export. These three actions do not support resource-level permissions, so `*` is unavoidable. |

The runtime has **no** DynamoDB, Lambda, Cognito or IAM permissions. It cannot
read an order except by asking the Gateway.

### The deny overlay

The CDK generates a Bedrock grant covering `inference-profile/*` and
`foundation-model/*` — every model in the account, not the one we chose. The
execution role cannot simply be replaced: supplying `executionRoleArn` makes the
CDK import the role immutably, and it then silently skips the memory and gateway
grants it would otherwise attach.

Instead, `agent/iam/deny_out_of_scope.json` is attached via `additionalPolicies`
as an inline policy of explicit **Deny** statements, which override any Allow:

| Sid | Effect |
|---|---|
| `DenyAnyModelButTheConfiguredOne` | Denies `bedrock:InvokeModel*` on everything except the configured inference profile and its two foundation-model ARNs |
| `DenyMemoryRecordDeletion` | Denies `DeleteMemoryRecord`; the agent never deletes extracted records |
| `DenyConfigurationBundleMutation` | Denies creating, updating or deleting configuration bundles, which this project does not use |

Verified with the policy simulator:

```
global.anthropic.claude-sonnet-4-5-20250929-v1:0  -> allowed
us.anthropic.claude-opus-4-5-20251101-v1:0        -> explicitDeny
anthropic.claude-3-haiku-20240307-v1:0            -> explicitDeny
```

`DeleteEvent` is deliberately **not** denied: the Strands session manager calls
it, and denying it breaks short-term memory.

## Gateway execution role

`AgentCore-csagent-default-McpGatewayCsagentDevTools-kWojXa3Gp5Fd` (the one
whose inline policy ends in `RoleDefaultPolicy`; the three similarly-prefixed
roles are the Lambdas'). Trusted by the Gateway service.

| Permission | Why |
|---|---|
| `lambda:InvokeFunction` on `csagent-orders`, `csagent-customers`, `csagent-refunds` and their `:*` aliases | Invoking the three tool Lambdas is the Gateway's entire job. |

The Gateway's own inbound auth is `AWS_IAM`: the agent SigV4-signs each MCP
request with its execution role. This was chosen over a JWT/OAuth hop because it
stores no credential at all — there is no client secret to rotate and no token
endpoint to fail. The user-facing front door is still JWT.

## Lambda execution roles

One role per Lambda, so a flaw in one tool cannot reach another's data. Each has
`AWSLambdaBasicExecutionRole` plus one inline policy declared in
`agentcore/agentcore.json` under the target's `compute.iamPolicy`:

| Lambda | Inline policy | Explicitly not allowed |
|---|---|---|
| `csagent-orders` | `dynamodb:GetItem` on `csagent-dev-orders` | any write; Customers and Refunds |
| `csagent-customers` | `dynamodb:GetItem` on `csagent-dev-customers` | any write; Orders and Refunds |
| `csagent-refunds` | `GetItem` on `csagent-dev-orders`; `GetItem` + `PutItem` on `csagent-dev-refunds` | `DeleteItem`, `UpdateItem`, `Scan`, `Query`; writing to Orders |

`csagent-refunds` needs Orders because it refuses to refund more than the order
total, and `GetItem` on Refunds to return the original refund when a conditional
write loses to a duplicate key.

## Wildcards used, and why

| Where | Pattern | Justification |
|---|---|---|
| Lambda `iamPolicy` resources | `arn:aws:dynamodb:*:*:table/<name>` | `compute.iamPolicy` is raw JSON handed to CloudFormation, so it cannot carry CDK tokens. DynamoDB is not reachable cross-account without a resource policy, so this does not widen real access. |
| Deny overlay `NotResource` | `arn:aws:bedrock:*:*:inference-profile/...` | Same reason. Widening the *exception* to a deny is the conservative direction, and a cross-account inference profile is not reachable anyway. |
| Runtime X-Ray / `logs:DescribeLogGroups` | `*` | These actions do not support resource-level permissions. |

## Remaining gaps

1. **One shared test identity.** The pool has a single user mapped to
   `CUST-001`. Demonstrating that customer A cannot read customer B's *orders*
   (as opposed to their memory, which is already isolated) needs a second user,
   and needs the tools to filter on the caller's `customer_id` — the Lambdas
   currently return any order by id. That belongs with the Cedar policy step.
2. **No refresh-token handling** in `scripts/get_token.py`; it signs in afresh
   each time. Fine for tests, not a pattern for a real client.
3. **Cross-customer reads are not authorized.** The Cedar policies scope which
   *actions* are allowed, not which rows. A valid `CUST-001` token can still ask
   for `ORD-123`, which belongs to `CUST-002`. See
   [docs/security.md](security.md) §7 — this is now the most serious open item.

The Cedar policy engine (`csagent_dev_policy`, ENFORCE) is deployed and holds the
refund ceiling at the Gateway. See [docs/security.md](security.md).
