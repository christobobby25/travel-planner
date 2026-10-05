# 2-minute demo script

**Setup before recording:** `.venv/bin/streamlit run ui/app.py` with `TRAVEL_API_URL` /
`TRAVEL_API_KEY` set (sidebar shows "Mode: public API"). Open the CloudWatch dashboard
**TravelPlanner** in a second tab. Send one warm-up message so the runtime isn't cold.
Answers take 15-40 s, so trim the waits in editing.

| Time | Show | Say |
|---|---|---|
| 0:00-0:15 | README architecture diagram | "A multi-agent travel planner. A supervisor agent on Amazon Bedrock AgentCore delegates to flight, hotel and weather specialists, each using real APIs through MCP." |
| 0:15-0:45 | UI: click the Austin weekend example | "One request: flights, hotel and weather. The supervisor resolves 'this Friday' to a date, calls the three specialists, and combines their answers. Prices come from Duffel and LiteAPI test environments; the forecast is live AccuWeather." Point at the "Specialists used" line. |
| 0:45-1:05 | Type: "Make the hotel cheaper, under $200" | "Follow-ups work because conversation history is stored in DynamoDB and sent with each message. The runtime itself is stateless, so sessions never share context." |
| 1:05-1:30 | Code: `agents/` folder, then `infra/app.py` | "Two frameworks: Strands for the supervisor, flight and weather agents, LangGraph for the hotel workflow. Flights use a custom MCP server I wrote over Duffel's API; weather and hotels use hosted MCP servers, limited to read-only allowlists." |
| 1:30-1:50 | CloudWatch dashboard | "API Gateway, Lambda, AgentCore and Bedrock metrics on one page, plus a custom metric for which specialists run. A request takes about 30 seconds, longer than API Gateway's 29-second limit, so the API is asynchronous: POST returns a job id, the client polls." |
| 1:50-2:00 | `docs/lessons-learned.md` | "The write-up covers the problems I hit: the model miscounting weekdays (wrong dates in 3 of 5 runs, fixed to 0 of 5), a hosted MCP server whose tools hung, and tool results over 100K tokens." |
