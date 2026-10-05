"""Flight agent (Strands) that uses the custom Duffel MCP server over stdio."""
import os
import sys
from pathlib import Path

from mcp import StdioServerParameters, stdio_client
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

from agents.dates import TodayInSystemPrompt, with_today

SERVER = Path(__file__).resolve().parent.parent / "mcp_servers" / "duffel_server.py"

FLIGHT_PROMPT = """You are a flight specialist. Use the flight tools to find options.
Always ask for (or infer) origin, destination and dates as IATA codes and YYYY-MM-DD.
For round trips, pass return_date so both legs are priced together.
Summarize the 3 best options: airline, total price, stops, and departure/arrival
times for every leg.
Never invent flights or prices; only report what the tools return."""

duffel_client = MCPClient(
    lambda: stdio_client(
        StdioServerParameters(command=sys.executable, args=[str(SERVER)], env=dict(os.environ))
    )
)


def build_flight_agent(model: BedrockModel) -> Agent:
    # Caller must be inside `with duffel_client:` so the MCP session is open.
    return Agent(
        model=model,
        system_prompt=with_today(FLIGHT_PROMPT),
        hooks=[TodayInSystemPrompt(FLIGHT_PROMPT)],
        tools=duffel_client.list_tools_sync(),
        callback_handler=None,  # answer goes back to the supervisor; don't also stream it
    )
