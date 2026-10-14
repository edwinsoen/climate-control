"""Agent Runtime entry point for the rule planner, staged by scripts/deploy_agent.py.

Tools come from the app's MCP endpoint with a read-only access token; the planner never
holds planning leases, so the app's scheduled job submits validated proposals.
"""

from climate_control.agent.remote import planner_from_environment

root_agent = planner_from_environment()
