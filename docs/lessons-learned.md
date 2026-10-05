# Lessons Learned

These are some of the main problems I ran into while building the travel planner, what was causing them, and how I fixed them. The performance numbers below are from actual runs using Claude Haiku 4.5 through Amazon Bedrock.

## 1. The agents didn't know the current date

One of the first issues I noticed was that the agents had no reliable sense of today's date. For example, asking for `"Hotels in Chicago Nov 12-16"` resulted in a search for **2024-11-12**, which was already in the past. LiteAPI eventually timed out, and the hotel agent fell back to returning hotels without prices. Requests like `"weather next week"` had a similar problem because the agent couldn't reliably determine which week I meant.

I initially added the current date to each agent's system prompt. Since AgentCore can keep agents running inside a long-lived container, though, a date added only at startup would eventually become outdated.

To handle that, I added a Strands `BeforeInvocationEvent` hook that refreshes the date before every request ([agents/dates.py](https://github.com/christobobby25/travel-planner/blob/main/agents/dates.py)).

## 2. Giving the model the date still wasn't enough

Even after the agents knew today's date, I found that the model could still make simple calendar mistakes.

For example, with Monday, October 5 as the current date, `"leaving next Thursday"` was interpreted as Friday, October 9, and a four-night trip sometimes became three nights. The supervisor was also passing dates between agents as natural language, such as `"Thursday October 9"`, which gave the model another opportunity to misinterpret them.

Instead of asking the model to calculate dates itself, I started giving it a small lookup table with values like `next Thursday = 2026-10-08`, along with a two-week calendar. I also required dates passed between agents to use `YYYY-MM-DD`.

I tested both approaches five times using the same request and stubbed specialist agents. With only the calendar context, the supervisor produced incorrect dates in **3/5 runs**. With the lookup table and ISO date rule, it produced the correct dates in **5/5 runs**.

## 3. LiteAPI's MCP rate tools kept hanging

The LiteAPI MCP tools for hotel rates (`post_hotels_rates` and `post_hotels_min_rates`) repeatedly hung, even when I only sent around 10 hotel IDs. Eventually the request would hit an `httpx.ReadTimeout`, which also caused the LangGraph run to fail.

To figure out where the problem was, I sent the same request directly to LiteAPI's REST API. It returned in about **3 seconds**, while other LiteAPI MCP tools like hotel search and hotel details also worked normally. That narrowed the issue down to the MCP rate tools rather than LiteAPI itself.

I worked around it by creating a small LangChain tool called `get_hotel_rates` that calls the REST endpoint directly and returns a compact result to the model. I kept MCP for hotel search, increased the MCP read timeout to 60 seconds, and added `handle_tool_error` so a failed tool call is returned to the model instead of crashing the entire graph.

## 4. The agents had access to way too many tools

LiteAPI's MCP server exposes **91 tools**, including tools for bookings, payments, cancellations, and vouchers. Most of those had nothing to do with what my hotel agent needed to accomplish.

I also found that some tool responses were much larger than expected. `get_data_hotel`, for example, could return **60K-250K characters for a single hotel** because the response included every photo and room. When the agent called it three times in one request, it could add well over 100K tokens of unnecessary context.

I switched to strict read-only tool allowlists. The hotel agent now gets four MCP tools plus my hotel-rates tool, while the weather agent gets five of AccuWeather's 26 available tools. I removed `get_data_hotel` entirely because the normal search results already provide the hotel name, address, and rating.

I also added a smoke test ([tests/smoke.py](https://github.com/christobobby25/travel-planner/blob/main/tests/smoke.py)) that catches cases where an MCP server renames one of the tools I depend on or an agent accidentally receives a tool outside its allowlist.

## 5. The agent was slower than API Gateway's default timeout

A complete trip plan usually takes around **25-40 seconds**, with one AgentCore test taking about 40 seconds. That's a problem because API Gateway REST integrations have a default timeout of roughly 29 seconds.

Instead of keeping the request synchronous, I changed the API to use an asynchronous job pattern.

`POST /chat` creates a job, stores it, and asynchronously invokes a worker Lambda using `InvocationType=Event`. The API can then immediately return `202` with a `job_id`. The client checks `GET /chat/{job_id}` until the result is ready.

I also disabled asynchronous retries on the worker so a failed invocation can't accidentally save the same conversation turn twice.

I considered increasing the API Gateway timeout through Service Quotas, but that depends on approval and can affect throttling limits. I also considered Lambda Function URLs, but I wanted to keep API Gateway features such as API keys and usage plans.

## 6. Conversation history was accidentally shared between users

My first AgentCore entrypoint created one supervisor when the container started and reused it for every request. That meant the supervisor's message history wasn't isolated by user. In a multi-user application, one person's trip could potentially end up in another person's context.

I changed the design so DynamoDB is the source of truth for conversation history. The worker loads the most recent 20 turns and sends them with each request, and the runtime creates a fresh supervisor using that history.

I tested this with the follow-up question, `"And on Sunday? Same city."` When the previous conversation was included, the agent correctly understood that the city was Denver. Without the history, it asked which city I meant.

## 7. A few smaller problems I ran into

- **Duplicate output:** Strands' default callback handler caused responses to be printed by the sub-agent, then again by the supervisor, and then again by my application. Setting `callback_handler=None` on the sub-agents fixed it.
- **AgentCore deployment:** The Python AgentCore starter toolkit has been replaced by the npm `@aws/agentcore` CLI. I also found that the old `agentcore` executable inside my `.venv` could shadow the newer CLI, so I deploy outside the virtual environment.
- **Deployment package size:** The CLI automatically ignores `.env*` and `.venv`, but not every environment or build directory. A leftover `.venv-1` would have added about **333 MB**, while CDK output under `infra/` added roughly another **90 MB**.
- **Root vs. IAM:** CDK deployment roles couldn't be assumed correctly when deploying as the AWS root user, which caused CDK to fall back to root credentials and generate warnings. Deploying through an IAM user worked as expected.
- **CDK side effects:** CDK's `RestApi` construct creates an account-level API Gateway CloudWatch role by default. Since this API doesn't need it, I disabled it with `cloud_watch_role=False`.
