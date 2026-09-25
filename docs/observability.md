# Observability: finding the root cause of a failure

How to investigate the five failure classes this agent can hit, using the traces
and logs it already emits. Every example below is a real trace from a reproduced
failure; the trace IDs are genuine and the queries are the ones that found them.

---

## 1. Where things are

Four places, in the order you normally need them.

| What | Where | Use it for |
|---|---|---|
| **Trace + spans** | X-Ray, or the GenAI Observability console | The shape of a turn: how many tool calls, which failed, how long |
| **Agent log** | `/aws/bedrock-agentcore/runtimes/csagent_csagent_dev_support-zDg6vM8kZf-DEFAULT` | Retry attempts, identity, budget, per-tool decisions |
| **Tool log** | `/aws/lambda/csagent-orders`, `-customers`, `-refunds` | The actual root cause: timeouts, stack traces, DynamoDB errors |
| **Metrics** | CloudWatch namespace `csagent` | Rates and alarms, not individual incidents |

The console entry point:

```bash
agentcore traces list --since 1h          # trace IDs + session IDs
agentcore logs --since 30m --query "<text>"
```

`agentcore traces list` prints a GenAI Observability deep link. That console view
is the fastest way to *see* a turn; the CLI and X-Ray are better for querying.

### Getting a trace's spans

`agentcore traces get <id>` downloads the **log events** for a trace, not its
spans. For spans, convert the trace ID to X-Ray form — insert dashes after 1 and
after the first 8 hex characters — and ask X-Ray:

```bash
# trace 6ab6353f58fed9a7140c9b22357ac181  ->  1-6ab6353f-58fed9a7140c9b22357ac181
aws xray batch-get-traces --trace-ids "1-6ab6353f-58fed9a7140c9b22357ac181" \
  --query "Traces[0].Segments[].Document" --output text
```

### Two traps that will mislead you

**`gen_ai.tool.status` is not the tool's outcome.** Strands sets it to `success`
whenever the tool function *returned* instead of raising — and our tools return
structured errors rather than raising. Every failing span below reads
`gen_ai.tool.status: success`. Use **`tool.outcome`** and **`error.code`**.

**X-Ray's `error` / `fault` flags are not set on tool spans.** The agent sets the
OTEL span status to ERROR on a failed tool call, but that does not surface as
`error: true` on the X-Ray subsegment. Filtering a trace list by "has errors"
will not find these. Filter on the metadata attributes instead.

### Span layout of one turn

```
POST /invocations                       (root; actor_id, session.id, operation_id)
└── invoke_agent Strands Agents
    ├── execute_event_loop_cycle        (one per model turn)
    │   ├── chat  <model>               (Bedrock call)
    │   └── execute_tool get_order      (one per tool call — the useful span)
    └── execute_event_loop_cycle
```

Our domain attributes live in the **`execute_tool`** span's X-Ray *metadata*
(they are OTEL span attributes; X-Ray files non-indexed attributes there):

| Attribute | Meaning |
|---|---|
| `tool.name` | which tool |
| `tool.outcome` | `success` or `error` — the real outcome |
| `error.code` | why it failed; the single most useful field |
| `error.retryable` | whether retry was even attempted |
| `tool.attempts` | how many attempts were made (1 = not retried) |
| `loop.tool_calls` | this call's position in the turn — the loop signal |
| `loop.budget_exceeded` | the turn's tool-call budget ran out |
| `order_id`, `customer_id` | what it was operating on |
| `refund.amount`, `refund.idempotency_key`, `refund.id`, `tool.duplicate` | refund specifics |
| `session.id`, `actor_id`, `operation_id` | correlation |

---

## 2. Tool timeout

**Reproduce:** `python scripts/inject_fault.py --function csagent-orders --mode timeout`

**Real trace:** `6ab63207155b540a1949e1293f3b946a`, session `obs-timeout-01-…`

### What the trace shows

Two `execute_tool get_order` spans, both for `ORD-1001`:

```
tool.name=get_order  order_id=ORD-1001  tool.outcome=error
error.code=TOOL_TRANSPORT_ERROR  error.retryable=True  tool.attempts=3
```

Read it as: one tool call, retried 3 times, all failed — and then the *model*
asked again, producing the second span. Nine seconds of Lambda time per span.

### Root cause — in the Lambda log

`TOOL_TRANSPORT_ERROR` only says "the tool did not answer". The cause is in the
tool's own log group, in the `REPORT` line:

```
REPORT RequestId: 03d81525-…  Duration: 10000.00 ms  Billed Duration: 10938 ms
       Memory Size: 256 MB  Max Memory Used: 90 MB  Status: timeout
XRAY TraceId: 1-6ab63207-155b540a1949e1293f3b946a  Sampled: true
```

Three things to notice:

1. **`Status: timeout`** with `Duration` exactly equal to the configured timeout.
   (This runtime reports it this way; older ones print `Task timed out after …`.)
2. The `REPORT` line carries the **`XRAY TraceId`**, which is how you get from a
   Lambda log back to the agent turn — and vice versa.
3. There is **no error log from our code**, because the invocation was killed
   mid-execution. Absence of a `tool_error` line alongside a failed call is
   itself the fingerprint of a timeout.

```
fields @timestamp, @message
| filter @message like /Status: timeout/ or @message like /Task timed out/
| sort @timestamp desc
```
Log group: `/aws/lambda/csagent-orders` (and the other two).

### Why it looks the way it does

The agent's per-call timeout is 15s and the Lambda's is 10s — deliberately, so
the Lambda dies first and *reports* the timeout instead of the agent giving up on
a call that is still running. If you ever see the agent time out before the
Lambda, those two numbers have drifted.

---

## 3. Invalid parameters

This splits into two layers, and telling them apart is the whole diagnosis.

### Layer A — rejected by the Gateway, before the Lambda runs

The Gateway validates arguments against the tool schema in `agentcore.json`.
A wrong type or a missing required field never reaches your code:

```
ValidationException - Parameter validation failed: Invalid request parameters:
- Field '/amount_cents' has invalid type: string found, integer expected

ValidationException - Parameter validation failed: Invalid request parameters:
- Missing required field(s): 'idempotency_key'
```

**The fingerprint: a failed tool call with no Lambda log line at all.** If the
agent log shows a `tool_call` and the Lambda log shows nothing for that
timestamp, the Gateway rejected it. Do not go looking for a bug in the handler.

The agent maps these to `error.code=TOOL_INVALID_ARGUMENTS`,
`error.retryable=False` — deliberately not retryable, because identical
arguments will be rejected identically.

### Layer B — rejected by the Lambda, on the values

Well-formed but wrong: unknown ids, non-positive amounts, amounts over the order
total. These produce a normal structured error and a log line.

**Real trace:** `6ab632a215430b744092277d71c0e42d`, session `obs-badparam-01-…`

```
tool.name=get_order  order_id=ORD-99999  tool.outcome=error
error.code=ORDER_NOT_FOUND  error.retryable=False  tool.attempts=1
```

`tool.attempts=1` is the thing to check: a business error must **not** be
retried. If you see `ORDER_NOT_FOUND` with `tool.attempts=3`, the retry
classification has regressed.

```
fields @timestamp, event, tool, code, order_id, customer_id
| filter event = "tool_error"
| stats count() by code, tool
| sort count desc
```
Log group: the three `/aws/lambda/csagent-*` groups.

### Root cause, usually

Not a code bug — the model passed something the customer gave it. Check
`order_id` on the span against what the customer actually said. A systematic
pattern of one error code points at a tool description that needs sharpening,
since the schema description is the only instruction the model has.

---

## 4. Wrong tool selection

Nothing in the platform flags a *correct* call to the *wrong* tool, so this one
is inferred rather than reported.

### The signatures

| Signature | What happened |
|---|---|
| `tool.name=get_order` with an `order_id` shaped like `ORD-CUST-001` | The model passed a customer id to the order tool |
| `tool.name=get_customer` with `customer_id=CUST-MAREK NOWAK` | It passed a name where an id belongs |
| `ORDER_NOT_FOUND` / `CUSTOMER_NOT_FOUND` on an id that does not match `ORD-<digits>` / `CUST-<digits>` | Wrong tool or wrong argument |
| Zero `execute_tool` spans on a turn that clearly needed data | It answered without looking — worse than calling the wrong tool |

Confirmed by direct call: `get_order` with `order_id=CUST-001` returns
`ORDER_NOT_FOUND: No order with id ORD-CUST-001.` The doubled prefix is the
giveaway — the handler normalises a bare value by prepending `ORD-`, so
`ORD-CUST-…` can only come from a customer id arriving at the order tool.

```
fields @timestamp, event, tool, code, order_id, customer_id
| filter event = "tool_error" and (code = "ORDER_NOT_FOUND" or code = "CUSTOMER_NOT_FOUND")
| filter (order_id like /CUST/) or (customer_id like /ORD/)
  or (order_id like /[^0-9-]/ and order_id not like /^ORD-[0-9]+$/)
| sort @timestamp desc
```

For the "answered without calling anything" case, compare turns to tool spans:

```
fields @timestamp, @message
| filter @message like /invoke session_id=/
| parse @message "session_id=* actor_id=*" as session, actor
| stats count() as turns by session
```
…then check each session for `tool_call` lines. A turn that answered a data
question with no `tool_call` is the one to read.

**Real example of the healthy case:** trace
`6ab632b41ff5d5b07987d0be63c03577` (session `obs-wrongtool-01-…`) has **zero**
`execute_tool` spans. Asked to "tell me about order CUST-001", the model
recognised the mismatch and asked for clarification instead of guessing. That is
the right outcome, and it is why this failure class is hard to reproduce
deliberately — the tool descriptions carry the id formats.

### Root cause

Almost always the tool description or the system prompt, not the model. The fix
is in `agentcore.json`'s `inputSchema.description` (which the model reads) and
the routing rules in `agent/prompts.py`.

---

## 5. HTTP 500 / unhandled exception

**Reproduce:** `python scripts/inject_fault.py --function csagent-orders --mode crash`

**Real trace:** `6ab6327c0eaa6bb82ee4c9494bd5178b`, session `obs-crash-01-…`

### What the trace shows

```
tool.name=get_order  order_id=ORD-1002  tool.outcome=error
error.code=TOOL_TRANSPORT_ERROR  error.retryable=True  tool.attempts=3
```

**Identical to the timeout case.** Three tool calls × three attempts = nine
failed Lambda invocations. From the agent's side a hung dependency and a crashed
one look the same, which is exactly why the next step is not optional.

### Root cause — the Lambda log has the stack trace

```
[ERROR] InjectedCrash: Injected unhandled failure in get_order.
Traceback (most recent call last):
  File "/var/task/common/gateway.py", line 48, in lambda_handler
    faults.maybe_crash(label)
  File "/var/task/common/faults.py", line 45, in maybe_crash
    raise InjectedCrash(...)
```

So: **timeout vs crash is decided in the Lambda log, not the trace.**

| Lambda log shows | Diagnosis |
|---|---|
| `Status: timeout`, no traceback | hung dependency or too-low timeout |
| `[ERROR] <Exception>` + traceback | a bug in the tool |
| our `tool_unhandled_exception` JSON line | a bug the handler *caught* — returns `INTERNAL_ERROR`, still retryable |
| nothing at all | rejected by the Gateway (see §3, Layer A) |

```
fields @timestamp, @message
| filter @message like /\[ERROR\]/ or event = "tool_unhandled_exception"
| sort @timestamp desc
| limit 50
```

Note the distinction: a caught exception becomes `INTERNAL_ERROR` from our own
handler; an escaping one kills the invocation and the agent sees
`TOOL_TRANSPORT_ERROR`. Both are retryable, because a crash may be transient —
but nine invocations for one question is expensive, which is what the
`ToolErrors` metric is for.

---

## 6. LLM loop

The most expensive failure, because every individual call is legitimate and
nothing downstream refuses it.

**Reproduce:** arm a retryable fault, which makes the model try again:
`--mode error`, with `TOOL_CALL_BUDGET` lowered.

**Real trace:** `6ab6353f58fed9a7140c9b22357ac181`, session `obs-loop-02-…`

### What the trace shows

`loop.tool_calls` is the counter. It increments across the whole turn:

```
execute_tool get_order  order_id=ORD-1001  loop.tool_calls=1  error.code=DEPENDENCY_UNAVAILABLE  tool.attempts=3
execute_tool get_order  order_id=ORD-1002  loop.tool_calls=2  error.code=DEPENDENCY_UNAVAILABLE  tool.attempts=3
execute_tool get_order  order_id=ORD-123   loop.tool_calls=3  error.code=TOOL_CALL_BUDGET_EXCEEDED  loop.budget_exceeded=True
```

The guard stopped the turn at the budget. `TOOL_CALL_BUDGET_EXCEEDED` is
non-retryable and its message instructs the model to answer with what it has, so
the turn ends cleanly instead of running until the request times out.

```
fields @timestamp, @message
| filter @message like /tool_call_budget_exceeded/
| parse @message "tool=* calls=* budget=*" as tool, calls, budget
| sort @timestamp desc
```

### Spotting a loop that did *not* trip the budget

The budget is 12 — generous, so a shorter loop still wastes time silently. Look
for the same tool with the same arguments repeated inside one trace:

```
fields @timestamp, @message
| filter @message like /tool_call tool=/
| parse @message "tool_call tool=* order_id=*" as tool, order_id
| stats count() as calls by bin(5m), tool, order_id
| filter calls > 3
| sort calls desc
```

Or against metrics: `ToolInvocations` climbing while `RefundsProcessed` and
successful lookups stay flat means effort without progress.

### Root cause

Usually a tool returning something the model reads as "try again" — a retryable
error, an empty result, or an ambiguous message. Check `error.code` on the
repeated spans:

- `DEPENDENCY_UNAVAILABLE` / `TOOL_TRANSPORT_ERROR` → the dependency is the
  problem; the loop is a symptom, and the retries already multiplied it 3×.
- `ORDER_NOT_FOUND` repeated with the *same* id → the model is not accepting a
  definitive answer. Sharpen the prompt.
- `ORDER_NOT_FOUND` with *different* ids each time → not a loop; it is guessing.

### A bug this found

In the first run the guard never fired: `loop.tool_calls` read `1` on all six
spans of trace `6ab633d2362a0f816b47070d0b919ccc`. The counter was an `int` in a
`ContextVar`, and Strands runs sync tools on worker threads that receive a
*copy* of the context — so each call incremented its own copy. It is now a shared
mutable object, and `tests/unit/test_tool_budget.py` pins the behaviour by
running calls through `contextvars.copy_context()` on worker threads.

If `loop.tool_calls` is ever `1` on every span of a multi-call turn again, that
regression is back.

---

## 7. Metrics

Namespace `csagent`, dimensions `Stage` and `Tool`. Emitted as Embedded Metric
Format from the Lambdas' own logs, so no `cloudwatch:PutMetricData` is needed on
the tool roles (which would have required `"Resource": "*"` on three otherwise
tightly scoped roles).

| Metric | Emitted when | Worth alarming on |
|---|---|---|
| `ToolInvocations` | every tool invocation | sudden climb = loop or retry storm |
| `ToolErrors` | any error result, by `error_code` | rate > a few % |
| `RefundsProcessed` | a refund is written | a spike is worth a look |
| `RefundsDenied` | over the ceiling or over the order total | sustained rate = probing or a broken client |
| `DuplicateRefundPrevented` | a repeated idempotency key | steady non-zero = a caller retrying blindly; **a jump means idempotency is earning its keep** |

Order and refund ids are log *fields*, never dimensions — a dimension per order
id would create a metric per order.

```bash
aws cloudwatch get-metric-statistics --namespace csagent --metric-name ToolErrors \
  --dimensions Name=Stage,Value=dev Name=Tool,Value=get_order \
  --start-time "$(date -u -d '1 hour ago' +%FT%TZ)" --end-time "$(date -u +%FT%TZ)" \
  --period 300 --statistics Sum
```

Errors grouped by code, over the three tool log groups:

```
fields @timestamp, error_code, Tool
| filter event = "metric" and ispresent(ToolErrors)
| stats sum(ToolErrors) as errors by error_code, Tool
| sort errors desc
```

---

## 8. A worked triage path

A customer says the agent could not check their order.

1. `agentcore traces list --since 1h` → find the trace by session ID.
2. Pull its spans from X-Ray. Read `tool.outcome`, `error.code`, `tool.attempts`
   on each `execute_tool` span. Ignore `gen_ai.tool.status`.
3. Branch on `error.code`:
   - `ORDER_NOT_FOUND`, `REFUND_LIMIT_EXCEEDED`, `AMOUNT_EXCEEDS_ORDER_TOTAL`
     → working as designed. Confirm `tool.attempts=1`.
   - `TOOL_INVALID_ARGUMENTS` → the model built a malformed call; the message
     names the field.
   - `TOOL_TRANSPORT_ERROR` / `DEPENDENCY_UNAVAILABLE` → go to step 4.
   - `TOOL_CALL_BUDGET_EXCEEDED` → a loop; find what the earlier spans returned.
4. In the tool's Lambda log group, filter the same minute. `Status: timeout` →
   hung dependency. `[ERROR]` + traceback → a bug. Nothing → the Gateway
   rejected it.
5. Correlate back with the `XRAY TraceId` on the Lambda `REPORT` line.

---

## 9. Setup notes

Transaction Search is enabled account-wide (`CloudWatchLogs` destination, 100%
sampling) — the first `agentcore deploy` did it:

```bash
aws xray get-trace-segment-destination   # -> {"Destination":"CloudWatchLogs","Status":"ACTIVE"}
aws xray get-indexing-rules
```

In this account the `aws/spans` log group Transaction Search creates is present
but has not been populated, so the span queries above go through X-Ray rather
than Logs Insights on `aws/spans`. Once it fills, the same attributes are
queryable there with Logs Insights, which is more convenient than
`batch-get-traces`. Newly emitted spans also take 2–3 minutes to appear, and a
freshly enabled Transaction Search takes about 10 minutes before anything is
indexed — traces from invocations in that window are simply missing.
