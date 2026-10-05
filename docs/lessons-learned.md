# Lessons learned

Problems hit while building the travel planner, what caused them, and how they were fixed.
Numbers are from real runs against Claude Haiku 4.5 on Bedrock.

## 1. The model didn't know today's date

**Symptom.** "Hotels in Chicago Nov 12-16" searched **2024-11-12**, a date in the past. LiteAPI
timed out on it and the hotel agent fell back to listing hotels without prices. "Weather next
week" got a clarifying question because the agent couldn't tell which week.

**Fix.** Every agent's system prompt gets today's date. In AgentCore the agents live in a
long-running container, so a date baked in at startup would go stale; a Strands
`BeforeInvocationEvent` hook refreshes it on every call ([agents/dates.py](../agents/dates.py)).

## 2. …and then it miscounted weekdays

**Symptom.** With today's date in the prompt, "leaving next Thursday" (today: Monday Oct 5)
still came out as Friday Oct 9, and "4 nights" as 3. The supervisor wrote dates as prose
("Thursday October 9") when delegating, and computed them instead of looking them up.

**Fix.** Give a lookup table instead of a fact to reason from: `next Thursday = 2026-10-08`,
a two-week calendar, and a rule to pass every date to other agents as `YYYY-MM-DD`.

**Measured.** Same request, supervisor with stubbed specialists, 5 runs each:
plain calendar → wrong dates in **3/5**; lookup table + ISO rule → **0/5**.

## 3. A hosted MCP server's tools hung

**Symptom.** LiteAPI's MCP rate tools (`post_hotels_rates`, `post_hotels_min_rates`) never
answered, even for 10 hotel IDs, and the resulting `httpx.ReadTimeout` crashed the LangGraph run.

**Diagnosis.** The same request to LiteAPI's REST API returned in **~3 s**, so the API was fine and
the MCP wrapper was the problem. Hotel search and details through MCP worked normally.

**Fix.** A small LangChain tool (`get_hotel_rates`) calls the REST endpoint and returns a compact
summary; MCP is still used for search. The MCP connection got a 60 s read timeout, and tool
errors are returned to the model (`handle_tool_error`) instead of crashing the graph.

## 4. Too many tools, and tool results that were far too large

**Symptom.** LiteAPI's MCP server exposes **91 tools**, including bookings, payments, vouchers
and cancellations, all offered to the hotel agent. Separately, `get_data_hotel` returned
**60K-250K characters per hotel** (every photo and room), and the agent called it three times
in a row: well over 100K tokens added to a single answer.

**Fix.** Read-only allowlists: the hotel agent gets 4 MCP tools plus the rates tool, the weather
agent 5 of AccuWeather's 26. `get_data_hotel` is excluded; search results already include
name, address and rating. A smoke test ([tests/smoke.py](../tests/smoke.py)) fails if a server renames an
allowlisted tool or an agent receives a tool outside its allowlist.

## 5. API Gateway's 29-second limit vs. a 25-40 second agent

**Symptom.** A full trip plan takes 25-40 s (40 s measured on AgentCore). API Gateway REST
integrations time out at ~29 s by default, so a synchronous API would fail on most trips.

**Fix.** Asynchronous jobs: `POST /chat` stores a job and invokes a worker Lambda with
`InvocationType=Event`, returning `202 {job_id}` immediately; clients poll `GET /chat/{job_id}`.
The worker has async retries disabled so a failure can't save a turn twice. Alternatives
considered: raising the timeout via a Service Quotas request (depends on approval, can lower
throttle limits) or a Lambda Function URL (drops API keys and usage plans).

## 6. Conversation state was shared between users

**Symptom.** The first AgentCore entrypoint built one supervisor at startup and reused it for every
request, so its message history belonged to whoever called it: two users would see each other's
trips in context.

**Fix.** DynamoDB is the single source of conversation history. The worker sends the last 20
turns with each message, and the runtime builds a fresh supervisor from them per request.
**Verified:** "And on Sunday? Same city." answers for Denver when the history is sent, and asks
"which city?" when it isn't.

## 7. Smaller things

- **Duplicate output.** Sub-agents used Strands' default callback handler, so each answer was
  streamed by the sub-agent, by the supervisor, and printed again. Sub-agents now run with
  `callback_handler=None`.
- **Deploy tooling.** The Python AgentCore starter toolkit is deprecated in favor of the npm
  `@aws/agentcore` CLI (CDK-based). The old toolkit's `agentcore` command inside `.venv`
  shadows the new one, so deploy from a shell without the venv.
- **Packaging.** The CLI zips the code directory and always skips `.env*` and `.venv`, but not other
  folders: a stray `.venv-1` would have added 333 MB, and CDK build output in `infra/` ~90 MB.
- **Root vs. IAM user.** CDK's deploy roles can't be assumed by the root user; it falls back to root
  credentials with warnings. Deploying as an IAM user works the way CDK expects.
- **Account-wide side effects.** CDK's `RestApi` creates an account-level API Gateway CloudWatch
  role by default; it's disabled here (`cloud_watch_role=False`) since this API doesn't need it.
