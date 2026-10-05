"""Supervisor agent: routes requests to flight, hotel and weather specialists (agents-as-tools).

Local run:  python supervisor.py "Plan 4 days in Chicago from DFW, Nov 12-16, 1 adult, under $250/night"
"""
import os
import sys
from contextlib import ExitStack

from dotenv import load_dotenv
from strands import Agent, tool
from strands.models import BedrockModel

load_dotenv()

from agents.dates import TodayInSystemPrompt, with_today  # noqa: E402
from agents.flight_agent import build_flight_agent, duffel_client  # noqa: E402
from agents.hotel_graph import search_hotels  # noqa: E402
from agents.weather_agent import accuweather_client, build_weather_agent  # noqa: E402

SUPERVISOR_PROMPT = """You are a travel planning supervisor. Break the user's request into parts
and delegate: flights -> flight_specialist, lodging -> hotel_specialist, weather -> weather_specialist.
Call only the specialists the request needs. Ask a clarifying question if dates, origin or
destination are missing. A trip with a stay is round trip unless the user says one-way:
ask flight_specialist for both the outbound and return flights, with the return on the
check-out date. Combine their answers into one clear trip plan."""


def build_supervisor(messages: list | None = None) -> Agent:
    """messages: earlier turns as Bedrock Converse messages, oldest first."""
    model = BedrockModel(
        model_id=os.environ["BEDROCK_MODEL_ID"],
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )
    flight_agent = build_flight_agent(model)
    weather_agent = build_weather_agent(model)

    @tool
    def flight_specialist(request: str) -> str:
        """Find flight options. Include origin, destination, departure date, return date
        (for round trips, in YYYY-MM-DD) and passenger count."""
        return str(flight_agent(request))

    @tool
    def hotel_specialist(request: str) -> str:
        """Find hotels. Include destination, check-in/check-out dates, guests and budget."""
        return search_hotels(request)

    @tool
    def weather_specialist(request: str) -> str:
        """Get current weather and forecast for a destination and travel dates."""
        return str(weather_agent(request))

    return Agent(
        model=model,
        system_prompt=with_today(SUPERVISOR_PROMPT),
        hooks=[TodayInSystemPrompt(SUPERVISOR_PROMPT)],
        tools=[flight_specialist, hotel_specialist, weather_specialist],
        messages=messages,
    )


def open_mcp_sessions() -> ExitStack:
    stack = ExitStack()
    stack.enter_context(duffel_client)
    stack.enter_context(accuweather_client)
    return stack


if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]) or "What's the weather in Chicago this week?"
    with open_mcp_sessions():
        build_supervisor()(prompt)  # the default callback handler streams the answer
        print()
