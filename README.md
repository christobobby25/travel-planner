# Agentic AI Travel Planner

A production-oriented **multi-agent AI travel planner** built on **Amazon Bedrock AgentCore**. A supervisor agent interprets a trip request and delegates work to specialized flight, hotel, and weather agents. Each specialist uses real travel services through **Model Context Protocol (MCP)** and REST APIs before the supervisor combines the results into a single travel plan.

> **Example:**  
> "Weekend in Austin from DFW, leaving this Friday, back Sunday, hotel under $250/night"
>
> → Returns nonstop flight options with prices, hotels with nightly rates, and a day-by-day weather forecast.

[Example Conversations](docs/examples.md) · [Lessons Learned](docs/lessons-learned.md) · [Demo Script](docs/demo-script.md)

---

## What I Built

- **Multi-agent orchestration** with a supervisor that routes tasks to flight, hotel, and weather specialists.
- **Custom FastMCP server** over the Duffel API with responses trimmed specifically for LLM consumption.
- **LangGraph ReAct workflow** combining LiteAPI MCP search with a custom LangChain hotel-rates tool.
- **Hosted MCP integration** with AccuWeather using a restricted read-only tool allowlist.
- **Amazon Bedrock / Claude Haiku 4.5** for reasoning, tool selection, and response generation.
- **Bedrock AgentCore Runtime** for deploying and running the agent system on AWS.
- **Asynchronous API architecture** using API Gateway, Lambda, and DynamoDB to support agent runs that can take 25–40 seconds.
- **Session isolation and conversation memory** backed by DynamoDB instead of keeping user state inside long-running agents.
- **AWS Secrets Manager** for external API credentials with least-privilege IAM access.
- **CloudWatch observability** for agent latency, specialist calls, failures, token usage, and end-to-end execution time.
- **AWS CDK infrastructure** for repeatable deployment of the serverless backend.

---

## Architecture

```text
                         ┌──────────────────┐
                         │  Streamlit / API │
                         └────────┬─────────┘
                                  │
                                  ▼
                         ┌──────────────────┐
                         │   API Gateway    │
                         │ API Key + Limits │
                         └────────┬─────────┘
                                  │
                                  ▼
                         ┌──────────────────┐
                         │    API Lambda    │
                         │ Create async job │
                         └────────┬─────────┘
                                  │
                   ┌──────────────┴──────────────┐
                   ▼                             ▼
            ┌──────────────┐              ┌──────────────┐
            │   DynamoDB   │              │Worker Lambda │
            │Jobs + History│◄─────────────│              │
            └──────────────┘              └──────┬───────┘
                                                 │
                                                 ▼
                                      ┌─────────────────────┐
                                      │ Bedrock AgentCore   │
                                      │      Runtime        │
                                      └──────────┬──────────┘
                                                 │
                                                 ▼
                                      ┌─────────────────────┐
                                      │ Supervisor Agent    │
                                      │   Strands Agents    │
                                      └──────────┬──────────┘
                                                 │
                          ┌──────────────────────┼──────────────────────┐
                          ▼                      ▼                      ▼
                 ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
                 │  Flight Agent   │   │   Hotel Agent   │   │  Weather Agent  │
                 │    Strands      │   │    LangGraph    │   │     Strands     │
                 └────────┬────────┘   └────────┬────────┘   └────────┬────────┘
                          │                     │                     │
                          ▼                     ▼                     ▼
                 ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
                 │ Custom FastMCP  │   │ LiteAPI MCP +   │   │ AccuWeather MCP │
                 │  Duffel Server  │   │ LangChain Tool  │   │                 │
                 └─────────────────┘   └─────────────────┘   └─────────────────┘
```

### Request Flow

A full trip plan can take **25–40 seconds**, which can exceed API Gateway's default synchronous integration timeout.

To avoid that limitation, the backend uses an asynchronous job pattern:

1. `POST /chat` validates the API key and creates a job.
2. The API Lambda asynchronously invokes the worker and immediately returns `202` with a `job_id`.
3. The worker loads the session's last 20 conversation turns from DynamoDB.
4. The request and conversation history are sent to the AgentCore runtime.
5. AgentCore creates a fresh supervisor for the request.
6. The supervisor delegates work to the appropriate specialist agents.
7. The worker stores the response and marks the job complete.
8. The client polls `GET /chat/{job_id}` for the result.

The AgentCore runtime itself is **stateless between requests**, preventing conversation history from leaking between users.

---

## Multi-Agent Design

### Supervisor

The supervisor is built with **Strands Agents** and treats each specialist as a tool.

Its job is to:

- understand the user's overall trip request,
- determine which specialists are needed,
- pass structured context to each agent,
- coordinate multiple tool calls,
- and combine the results into one response.

Dates passed between agents use ISO `YYYY-MM-DD` format to reduce ambiguity.

### Flight Agent

The flight specialist uses a **custom FastMCP server** built on top of Duffel's flight API.

Instead of exposing large raw API responses directly to the LLM, the MCP server extracts the fields the agent actually needs, including:

- airline,
- route,
- departure and arrival times,
- number of stops,
- duration,
- and price.

This keeps tool responses smaller and reduces unnecessary context usage.

### Hotel Agent

The hotel workflow uses **LangGraph and LangChain**.

LiteAPI's hosted MCP server handles hotel search, while a custom LangChain `get_hotel_rates` tool calls LiteAPI's REST API for pricing.

This hybrid approach was added after testing showed that LiteAPI's MCP rate tools could hang while the equivalent REST request returned in roughly **3 seconds**.

The hotel agent only receives the tools required for search and pricing rather than all **91 tools** exposed by the LiteAPI MCP server.

### Weather Agent

The weather specialist uses **Strands Agents** with AccuWeather's hosted MCP server.

AccuWeather exposes 26 MCP tools, but the agent receives only **5 read-only tools** required for location lookup and forecasting.

---

## Reliability and Guardrails

### Tool Allowlists

External MCP servers can expose far more functionality than an agent actually needs.

LiteAPI exposes **91 tools**, including booking, payment, cancellation, and voucher operations. The hotel agent receives only four MCP search tools plus the custom rates tool.

This reduces unnecessary tool selection and prevents the agent from performing booking-related actions.

### Date Handling

LLMs were not reliable enough at calculating relative dates such as `"next Thursday"` from the current date.

Each request now provides:

- today's date,
- a two-week calendar,
- explicit weekday lookups,
- and an ISO date requirement for agent-to-agent communication.

In a small repeatability test using the same request five times:

| Approach | Incorrect Dates |
|---|---:|
| Basic calendar context | **3/5** |
| Lookup table + ISO dates | **0/5** |

### Context Management

One LiteAPI hotel-details tool returned **60K–250K characters per hotel**, including large collections of photos and room information.

The tool was removed from the agent's allowlist because hotel search already returned the information needed for planning.

Keeping tool responses compact reduces token usage and prevents irrelevant API data from filling the model's context window.

### Session Isolation

An early implementation reused one supervisor instance across requests, which meant conversation state could potentially be shared between users.

Conversation history now lives in **DynamoDB**. Each request creates a fresh supervisor using only that session's recent history.

This was tested with:

> "And on Sunday? Same city."

With the previous conversation included, the agent correctly continued the Denver trip. Without the history, it asked the user which city they meant.

---

## Observability

The infrastructure creates a **CloudWatch dashboard** for monitoring the system in production.

Metrics include:

- API requests and 4xx/5xx errors
- completed and failed jobs
- calls per specialist agent
- AgentCore p50/p90/max latency
- end-to-end worker duration
- errors by component
- Bedrock token usage
- model latency

AgentCore runtime logs and traces are also available through CloudWatch.

---

## Tech Stack

**AI / Agentic Systems**

`Amazon Bedrock` · `Claude Haiku 4.5` · `Bedrock AgentCore` · `Strands Agents` · `LangGraph` · `LangChain` · `MCP` · `FastMCP`

**Backend**

`Python 3.13` · `REST APIs` · `API Gateway` · `AWS Lambda` · `DynamoDB`

**Cloud / DevOps**

`AWS CDK` · `IAM` · `Secrets Manager` · `CloudWatch`

**External APIs**

`Duffel` · `LiteAPI` · `AccuWeather`

**UI**

`Streamlit`

---

## Project Structure

| Component | Purpose |
|---|---|
| [`supervisor.py`](supervisor.py) | Strands supervisor and agents-as-tools orchestration |
| [`agents/flight_agent.py`](agents/flight_agent.py) | Flight specialist using the custom Duffel MCP server |
| [`mcp_servers/duffel_server.py`](mcp_servers/duffel_server.py) | Custom FastMCP server over Duffel's flight API |
| [`agents/hotel_graph.py`](agents/hotel_graph.py) | LangGraph hotel workflow using MCP + REST |
| [`agents/weather_agent.py`](agents/weather_agent.py) | Weather specialist using AccuWeather MCP |
| [`agents/dates.py`](agents/dates.py) | Dynamic date context and weekday lookup |
| [`agentcore_app.py`](agentcore_app.py) | Bedrock AgentCore Runtime entrypoint |
| [`infra/`](infra/) | AWS CDK infrastructure |
| [`ui/app.py`](ui/app.py) | Streamlit chat interface |
| [`tests/smoke.py`](tests/smoke.py) | MCP, API credential, and tool allowlist smoke tests |

---

## Testing

The smoke test validates external integrations without requiring an LLM call:

```bash
python -m tests.smoke
```

It checks:

- Duffel MCP connectivity
- LiteAPI MCP connectivity
- AccuWeather MCP connectivity
- API credentials
- expected MCP tool names
- agent tool allowlists

This also catches breaking changes if a hosted MCP provider renames or removes a tool the application depends on.

---

## Run Locally

### Requirements

- Python 3.13
- AWS credentials with Amazon Bedrock access
- Duffel test token
- AccuWeather API key
- LiteAPI sandbox key

### Setup

```bash
git clone https://github.com/christobobby25/travel-planner.git
cd travel-planner

python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
```

Add your credentials and Bedrock model ID to `.env`.

Run the smoke tests:

```bash
python -m tests.smoke
```

Run a request directly:

```bash
python supervisor.py \
  "Weekend in Austin from DFW, leaving this Friday, back Sunday"
```

Or launch the Streamlit interface:

```bash
streamlit run ui/app.py
```

---

## AWS Deployment

The project can be deployed using **Bedrock AgentCore + AWS CDK**.

### 1. Store API Credentials

External API credentials are stored in AWS Secrets Manager under the `travelagent` secret.

The AgentCore execution role follows least privilege and can read only this secret.

### 2. Deploy AgentCore Runtime

Install the AgentCore CLI:

```bash
npm install -g @aws/agentcore
```

Then deploy:

```bash
agentcore deploy --diff
agentcore deploy
```

### 3. Deploy the Serverless API

```bash
cd infra
npx aws-cdk@2 deploy
```

The CDK stack provisions:

- API Gateway
- API and worker Lambda functions
- DynamoDB job storage
- DynamoDB conversation history
- IAM permissions
- CloudWatch monitoring

---

## API Example

Create a request:

```bash
curl -X POST "$TRAVEL_API_URL/chat" \
  -H "x-api-key: $TRAVEL_API_KEY" \
  -d '{"message":"Weekend in Austin from DFW, leaving Friday, back Sunday"}'
```

Response:

```json
{
  "job_id": "...",
  "session_id": "..."
}
```

Check the result:

```bash
curl "$TRAVEL_API_URL/chat/<job_id>" \
  -H "x-api-key: $TRAVEL_API_KEY"
```

When continuing a conversation, pass the returned `session_id` with the next request.

---

## Design Decisions

**Why MCP?**  
MCP gives the specialist agents a consistent interface for discovering and calling external tools while keeping API-specific implementation details outside the agent.

**Why multiple agents?**  
Flights, hotels, and weather require different APIs, tools, and instructions. Specialist agents keep each context focused while the supervisor handles coordination.

**Why combine MCP and REST?**  
MCP is useful where the hosted tools are reliable, but it doesn't need to be used everywhere. When LiteAPI's MCP pricing tools repeatedly timed out, I used the underlying REST endpoint instead.

**Why an asynchronous API?**  
Complete plans can take 25–40 seconds. Returning a job ID avoids holding an API Gateway request open while multiple agents and external APIs execute.

**Why DynamoDB for conversation history?**  
Keeping state outside AgentCore makes the runtime horizontally scalable and prevents conversation history from being shared across sessions.

---

## Lessons Learned

Building the project exposed several issues that don't show up in a simple agent demo: unreliable date reasoning, oversized tool responses, MCP timeouts, unnecessary tool exposure, API Gateway timeout limits, and conversation-state isolation.

I documented the problems, how I diagnosed them, and the changes I made here:

**[Read the full Lessons Learned write-up →](docs/lessons-learned.md)**

---

## Safety and Limitations

- Duffel runs in **test mode**.
- LiteAPI runs in its **sandbox environment**.
- Agents have **read-only travel tools**.
- The application cannot purchase flights or hotels.
- Prices are intended for demonstration and may not represent currently bookable inventory.

---

## Author

**Christo Bobby**  
Computer Science Graduate — The University of Texas at Dallas

Built as a hands-on project exploring **Generative AI engineering, multi-agent systems, MCP, LLM tool use, AWS serverless architecture, and production AI reliability**.
